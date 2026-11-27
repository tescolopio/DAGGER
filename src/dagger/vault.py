"""Local-only 0o600 credential vault for metered API keys."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

_PROVIDER_KEY_MAP = {
    "groq": "GROQ_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "api": "DAGGER_API_KEY",
    "local": "DAGGER_LOCAL_API_KEY",
}

_PROVIDER_BASE_URL_MAP = {
    "api": "DAGGER_API_BASE_URL",
    "local": "DAGGER_LOCAL_BASE_URL",
}

_IMPORTABLE_KEYS = (
    "GROQ_API_KEY",
    "TAVILY_API_KEY",
    "DAGGER_API_KEY",
    "DAGGER_API_BASE_URL",
    "DAGGER_LOCAL_API_KEY",
    "DAGGER_LOCAL_BASE_URL",
)


def validate_base_url(url: str) -> str:
    """Validate and normalize an HTTP(S) base URL without embedded credentials."""
    if not isinstance(url, str) or "\r" in url or "\n" in url:
        raise ValueError("base_url must be single-line")
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("base_url must not be empty")
    try:
        parsed = urlsplit(cleaned)
        port = parsed.port
    except ValueError:
        raise ValueError("base_url must be a valid HTTP(S) URL without embedded credentials") from None
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not (1 <= port <= 65535))
    ):
        raise ValueError("base_url must be a valid HTTP(S) URL without embedded credentials")
    return cleaned


def get_base_url(provider_id: str) -> str | None:
    """Return configured base URL for a provider from vault or environment."""
    env_key = _PROVIDER_BASE_URL_MAP.get(provider_id.strip().lower())
    if env_key is None:
        return None
    return get_vault_credential(env_key)


def _get_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def get_vault_path() -> Path:
    override = os.environ.get("DAGGER_CREDENTIALS_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return _get_repo_root() / ".dagger" / "credentials.env"


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    creds: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            creds[key] = value
    return creds


def _write_vault_file(path: Path, creds: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)

    lines = [f"{k}={v}" for k, v in sorted(creds.items())]
    content = "\n".join(lines) + ("\n" if lines else "")

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".credentials-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _resolve_dotenv_path(vault_path: Path) -> Path | None:
    dotenv_override = os.environ.get("DAGGER_DOTENV_FILE", "").strip()
    if dotenv_override:
        return Path(dotenv_override).expanduser()
    if not os.environ.get("DAGGER_CREDENTIALS_FILE", "").strip():
        return _get_repo_root() / ".env"
    if vault_path.parent.name == ".dagger":
        return vault_path.parent.parent / ".env"
    return None


def load_vault_credentials() -> dict[str, str]:
    path = get_vault_path()
    creds = _parse_env_file(path)
    dotenv_path = _resolve_dotenv_path(path)
    if dotenv_path is not None and dotenv_path.is_file():
        dotenv_creds = _parse_env_file(dotenv_path)
        imported = False
        for key in _IMPORTABLE_KEYS:
            if not creds.get(key) and dotenv_creds.get(key):
                creds[key] = dotenv_creds[key]
                imported = True
        if imported:
            _write_vault_file(path, creds)
    return creds


def get_vault_credential(key: str) -> str | None:
    creds = load_vault_credentials()
    value = creds.get(key)
    if value:
        return value
    env_val = os.environ.get(key, "").strip()
    return env_val or None


def save_provider_credentials(
    provider: str, api_key: str = "", base_url: str | None = None
) -> Path:
    provider_id = provider.strip().lower()
    env_key = _PROVIDER_KEY_MAP.get(provider_id)
    if env_key is None:
        raise ValueError(f"Unsupported credential provider: {provider}")
    if "\r" in api_key or "\n" in api_key:
        raise ValueError("api_key must be non-empty and single-line")
    cleaned_key = api_key.strip()
    if provider_id != "local" and not cleaned_key:
        raise ValueError("api_key must be non-empty and single-line")
    if provider_id == "local" and base_url is None and not cleaned_key:
        raise ValueError("base_url is required for local provider")

    creds = load_vault_credentials()
    if cleaned_key:
        creds[env_key] = cleaned_key
    elif provider_id == "local":
        creds.pop(env_key, None)

    url_env_key = _PROVIDER_BASE_URL_MAP.get(provider_id)
    if url_env_key is not None and base_url is not None:
        if "\r" in base_url or "\n" in base_url:
            raise ValueError("base_url must be single-line")
        cleaned_url = base_url.strip()
        if cleaned_url:
            creds[url_env_key] = validate_base_url(cleaned_url)
        else:
            creds.pop(url_env_key, None)

    path = get_vault_path()
    _write_vault_file(path, creds)
    return path
