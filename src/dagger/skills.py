"""Skill discovery, validation, matching, and prompt formatting for DAGGER."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from .schema import SkillSummary

SkillSource = Literal["dagger_local", "skillforge", "workspace"]

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MUTATING_TOOLS = frozenset({
    "write", "edit", "patch", "shell", "bash", "sh", "powershell", "pwsh",
    "execute", "run", "delete", "rm", "update_symbol", "rename_symbol",
    "delete_symbol", "simulate_refactor",
})
READ_ONLY_CODESCALPEL_MCP_TOOLS: tuple[str, ...] = (
    "mcp__codescalpel__extract_code",
    "mcp__codescalpel__get_symbol_references",
    "mcp__codescalpel__get_call_graph",
    "mcp__codescalpel__get_cross_file_dependencies",
    "mcp__codescalpel__analyze_code",
    "mcp__codescalpel__security_scan",
    "mcp__codescalpel__scan_dependencies",
)
MUTATING_CODESCALPEL_MCP_TOOLS: tuple[str, ...] = (
    "mcp__codescalpel__rename_symbol",
    "mcp__codescalpel__update_symbol",
    "mcp__codescalpel__delete_symbol",
    "mcp__codescalpel__simulate_refactor",
)
_CONTEXT_ALIASES: dict[str, tuple[str, ...]] = {
    "audit": ("code", "security", "analyze", "read"),
    "auditing": ("audit", "code", "security", "analyze", "read"),
    "review": ("code", "analyze", "security"),
    "py": ("code", "python"),
    "ts": ("code", "typescript"),
    "tsx": ("code", "typescript"),
    "js": ("code", "javascript"),
    "worker": ("swarm", "coordination"),
    "workers": ("swarm", "coordination"),
    "parallel": ("swarm", "coordination"),
    "decompose": ("swarm", "coordination"),
    "decomposing": ("swarm", "coordination"),
}
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "in", "is", "it", "of", "on", "or", "that", "the", "to", "use", "when",
    "with", "this", "will", "can", "all", "any", "into", "only",
})


@dataclass
class LoadedSkill:
    """In-memory representation of a discovered SKILL.md."""

    name: str
    description: str
    source: SkillSource
    path: str
    locations: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    valid: bool = True
    validation_errors: list[str] = field(default_factory=list)
    body: str = ""

    def to_summary(self) -> SkillSummary:
        return SkillSummary(
            name=self.name,
            description=self.description,
            source=self.source,
            path=self.path,
            locations=list(self.locations),
            tags=list(self.tags),
            allowed_tools=list(self.allowed_tools),
            valid=self.valid,
            validation_errors=list(self.validation_errors),
        )


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) > 1 and t not in _STOPWORDS]


def _normalize_allowed_tools(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in re.split(r"[\s,]+", raw) if part.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _extract_tags(name: str, description: str, metadata: object) -> list[str]:
    seen: dict[str, None] = {}
    if isinstance(metadata, dict):
        raw_tags = metadata.get("tags")
        if isinstance(raw_tags, str):
            for part in re.split(r"[\s,]+", raw_tags):
                tok = part.strip().lower()
                if tok:
                    seen.setdefault(tok, None)
        elif isinstance(raw_tags, list):
            for item in raw_tags:
                tok = str(item).strip().lower()
                if tok:
                    seen.setdefault(tok, None)
    for tok in _tokenize(f"{name} {description}"):
        seen.setdefault(tok, None)
        if len(seen) >= 16:
            break
    return list(seen.keys())


def _load_lockfile_names(lock_path: Path) -> set[str]:
    if not lock_path.is_file():
        return set()
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    names: set[str] = set()
    if isinstance(data, dict):
        skills_val = data.get("skills")
        if isinstance(skills_val, dict):
            names.update(str(k) for k in skills_val.keys())
        elif isinstance(skills_val, list):
            for item in skills_val:
                if isinstance(item, dict) and item.get("name"):
                    names.add(str(item["name"]))
                elif isinstance(item, str):
                    names.add(item)
        else:
            for k in data.keys():
                if k not in {"version", "lockfileVersion", "generated_at"}:
                    names.add(str(k))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]))
            elif isinstance(item, str):
                names.add(item)
    return names


def _parse_skill_file(skill_file: Path, root: Path, source: SkillSource) -> LoadedSkill:
    folder_name = skill_file.parent.name
    rel_path = skill_file.relative_to(root).as_posix()
    try:
        content = skill_file.read_text(encoding="utf-8")
    except OSError as exc:
        return LoadedSkill(
            name=folder_name,
            description="",
            source=source,
            path=rel_path,
            locations=[rel_path],
            valid=False,
            validation_errors=[f"unreadable SKILL.md: {exc}"],
        )

    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return LoadedSkill(
            name=folder_name,
            description="",
            source=source,
            path=rel_path,
            locations=[rel_path],
            valid=False,
            validation_errors=["missing YAML frontmatter opening '---'"],
        )

    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return LoadedSkill(
            name=folder_name,
            description="",
            source=source,
            path=rel_path,
            locations=[rel_path],
            valid=False,
            validation_errors=["missing YAML frontmatter closing '---'"],
        )

    frontmatter_raw = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :]).strip()
    try:
        meta = yaml.safe_load(frontmatter_raw)
    except yaml.YAMLError as exc:
        return LoadedSkill(
            name=folder_name,
            description="",
            source=source,
            path=rel_path,
            locations=[rel_path],
            valid=False,
            validation_errors=[f"invalid YAML frontmatter: {exc}"],
            body=body,
        )

    if not isinstance(meta, dict):
        return LoadedSkill(
            name=folder_name,
            description="",
            source=source,
            path=rel_path,
            locations=[rel_path],
            valid=False,
            validation_errors=["YAML frontmatter must be a mapping"],
            body=body,
        )

    errors: list[str] = []
    raw_name = meta.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        errors.append("missing required 'name' field")
        name = folder_name
    else:
        name = raw_name.strip()
        if name != folder_name:
            errors.append(f"name '{name}' does not match folder '{folder_name}'")
        if len(name) > 64:
            errors.append("name exceeds 64 characters")
        if not _NAME_RE.match(name):
            errors.append("name must match ^[a-z0-9]+(?:-[a-z0-9]+)*$")

    raw_desc = meta.get("description")
    if not isinstance(raw_desc, str) or not raw_desc.strip():
        errors.append("missing required non-empty 'description' field")
        description = ""
    else:
        description = raw_desc.strip()
        if len(description) > 1024:
            errors.append("description exceeds 1024 characters")

    allowed_tools = _normalize_allowed_tools(
        meta.get("allowed-tools") if "allowed-tools" in meta else meta.get("allowed_tools")
    )
    tags = _extract_tags(name, description, meta.get("metadata"))

    return LoadedSkill(
        name=name,
        description=description,
        source=source,
        path=rel_path,
        locations=[rel_path],
        tags=tags,
        allowed_tools=allowed_tools,
        valid=len(errors) == 0,
        validation_errors=errors,
        body=body,
    )


def discover_skills(workspace_root: str | Path) -> list[LoadedSkill]:
    """Discover and validate SKILL.md files in priority order, deduplicated by name."""
    root = Path(workspace_root).resolve()
    if not root.is_dir():
        return []

    search_roots: list[tuple[Path, str | None]] = [
        (root / ".agent" / "skills", "dagger_local"),
        (root / ".agents" / "skills", None),
        (root / ".claude" / "skills", None),
    ]

    by_name: dict[str, LoadedSkill] = {}
    for skills_dir, fixed_source in search_roots:
        if not skills_dir.is_dir():
            continue
        lock_names = (
            _load_lockfile_names(skills_dir / ".skillforge-lock.json")
            if fixed_source is None
            else set()
        )
        for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
            folder_name = skill_file.parent.name
            source: SkillSource = (
                fixed_source  # type: ignore[assignment]
                if fixed_source is not None
                else ("skillforge" if folder_name in lock_names else "workspace")
            )
            loaded = _parse_skill_file(skill_file, root, source)
            if fixed_source is None and loaded.name in lock_names:
                loaded.source = "skillforge"

            existing = by_name.get(loaded.name)
            if existing is None:
                by_name[loaded.name] = loaded
            else:
                if loaded.path not in existing.locations:
                    existing.locations.append(loaded.path)

    return list(by_name.values())


class SkillRegistry:
    """Convenience wrapper around workspace skill discovery and matching."""

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    def list_skills(self) -> list[LoadedSkill]:
        return discover_skills(self.workspace_root)

    def match(
        self,
        required_skills: list[str],
        context_text: str = "",
        read_only: bool = False,
        max_skills: int = 3,
    ) -> list[LoadedSkill]:
        return match_skills(
            self.list_skills(),
            required_skills=required_skills,
            context_text=context_text,
            read_only=read_only,
            max_skills=max_skills,
        )

    def resolve_allowed_mcp_tools(
        self,
        matched_skills: list[LoadedSkill],
        read_only: bool = False,
    ) -> list[str]:
        return resolve_allowed_mcp_tools(matched_skills, read_only=read_only)


def _is_mutating_mcp_tool(tool: str) -> bool:
    base = tool.strip().lower().split("(", 1)[0].strip()
    suffix = base.rsplit("__", 1)[-1]
    return base in _MUTATING_TOOLS or suffix in _MUTATING_TOOLS


def resolve_allowed_mcp_tools(
    matched_skills: list[LoadedSkill],
    read_only: bool = False,
) -> list[str]:
    """Compute the allowed MCP tools for a set of matched skills and container mode."""
    if not matched_skills:
        return []

    has_codescalpel = any(
        s.name == "using-code-scalpel"
        or any("codescalpel" in t.lower() for t in s.allowed_tools)
        for s in matched_skills
    )
    has_audit_or_inspection = any(
        s.name == "dagger-read-only-audit"
        or "audit" in s.name.lower()
        for s in matched_skills
    )

    declared_mcp = [
        t.strip()
        for s in matched_skills
        for t in s.allowed_tools
        if t.strip().lower().startswith("mcp__")
    ]

    result: list[str] = []
    seen: set[str] = set()

    def _add(tool_name: str) -> None:
        if tool_name not in seen:
            seen.add(tool_name)
            result.append(tool_name)

    if read_only:
        ro_allowlist = {t.lower(): t for t in READ_ONLY_CODESCALPEL_MCP_TOOLS}
        if has_codescalpel or has_audit_or_inspection:
            for tool_name in READ_ONLY_CODESCALPEL_MCP_TOOLS:
                _add(tool_name)
        for tool_name in declared_mcp:
            canonical = ro_allowlist.get(tool_name.strip().lower())
            if canonical:
                _add(canonical)
        return result

    if has_codescalpel:
        for tool_name in READ_ONLY_CODESCALPEL_MCP_TOOLS:
            _add(tool_name)
        for tool_name in MUTATING_CODESCALPEL_MCP_TOOLS:
            _add(tool_name)
    for tool_name in declared_mcp:
        _add(tool_name)
    return result


def _requires_mutating_tools(skill: LoadedSkill) -> bool:
    for tool in skill.allowed_tools:
        base = tool.strip().lower().split("(", 1)[0].strip()
        suffix = base.rsplit("__", 1)[-1]
        if base in _MUTATING_TOOLS or suffix in _MUTATING_TOOLS:
            return True
    return False


def match_skills(
    skills: list[LoadedSkill],
    required_skills: list[str],
    context_text: str = "",
    read_only: bool = False,
    max_skills: int = 3,
) -> list[LoadedSkill]:
    """Rank and filter valid skills against required_skills and context_text."""
    if max_skills <= 0:
        return []

    req_exact = {r.strip().lower() for r in required_skills if r.strip()}
    req_tokens = set(_tokenize(" ".join(required_skills)))
    ctx_tokens = set(_tokenize(context_text))
    expanded_ctx_tokens = set(ctx_tokens)
    for tok in ctx_tokens:
        expanded_ctx_tokens.update(_CONTEXT_ALIASES.get(tok, ()))

    scored: list[tuple[int, str, LoadedSkill]] = []
    for skill in skills:
        if not skill.valid:
            continue
        if read_only and _requires_mutating_tools(skill):
            continue

        score = 0
        skill_name_lower = skill.name.lower()
        skill_tokens = set(_tokenize(skill.name)) | set(skill.tags)
        desc_tokens = set(_tokenize(skill.description))

        if skill_name_lower in req_exact:
            score += 100
        score += 20 * len(req_tokens & skill_tokens)
        score += 5 * len(expanded_ctx_tokens & skill_tokens)
        score += 2 * len(expanded_ctx_tokens & desc_tokens)

        if score > 0:
            scored.append((-score, skill.name, skill))

    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored[:max_skills]]


def format_skills_for_planner(skills: list[LoadedSkill], max_chars: int = 2400) -> str:
    """Format a compact catalog of valid skills for ProjectManager planning."""
    valid_skills = [s for s in skills if s.valid]
    if not valid_skills or max_chars <= 0:
        return ""

    lines: list[str] = []
    total = 0
    for skill in valid_skills:
        line = f"- {skill.name}: {skill.description}"
        addition = len(line) + (1 if lines else 0)
        if total + addition > max_chars:
            break
        lines.append(line)
        total += addition
    return "\n".join(lines)


def format_matched_skills_for_prompt(
    matched: list[LoadedSkill],
    read_only: bool = False,
    max_chars: int = 3200,
    allowed_mcp_tools: list[str] | None = None,
) -> str:
    """Format matched skill instructions within a strict character budget."""
    if not matched or max_chars <= 0:
        return ""

    sections: list[str] = []
    if read_only:
        ro_allowlist = {t.lower(): t for t in READ_ONLY_CODESCALPEL_MCP_TOOLS}
        raw_mcp_tools = (
            allowed_mcp_tools
            if allowed_mcp_tools is not None
            else resolve_allowed_mcp_tools(matched, read_only=True)
        )
        mcp_tools: list[str] = []
        seen_mcp: set[str] = set()
        for tool_name in raw_mcp_tools:
            canonical = ro_allowlist.get(tool_name.strip().lower())
            if canonical and canonical not in seen_mcp:
                seen_mcp.add(canonical)
                mcp_tools.append(canonical)
        allowed_str = ", ".join(mcp_tools) if mcp_tools else "none"
        blocked_str = ", ".join(
            tool.rsplit("__", 1)[-1] for tool in MUTATING_CODESCALPEL_MCP_TOOLS
        )
        guardrail = (
            "[READ-ONLY GUARDRAIL] You are a least-privilege read-only auditor. "
            "Use read/search tools only and never invoke mutating, write, or shell tools. "
            f"Allowed read-only Code Scalpel MCP tools: {allowed_str}. "
            f"Explicitly blocked mutating MCP tools: {blocked_str}."
        )
        if len(guardrail) + 120 > max_chars:
            short_allowed = ", ".join(t.rsplit("__", 1)[-1] for t in mcp_tools) if mcp_tools else "none"
            guardrail = (
                "[READ-ONLY GUARDRAIL] Read-only auditor. "
                f"Allowed Code Scalpel MCP: {short_allowed}. "
                f"Blocked: {blocked_str}."
            )
        sections.append(guardrail)

    header_len = sum(len(s) for s in sections) + (2 if sections else 0)
    remaining = max_chars - header_len
    if remaining <= 0:
        return "\n\n".join(sections)[:max_chars]

    per_skill_budget = max(200, remaining // len(matched))
    for skill in matched:
        snippet = skill.body.strip() or skill.description
        prefix = f"### Skill: {skill.name}\n"
        avail = min(per_skill_budget, remaining) - len(prefix)
        if avail <= 0:
            break
        if len(snippet) > avail:
            snippet = snippet[: max(0, avail - 3)].rstrip() + "..."
        block = f"{prefix}{snippet}"
        sections.append(block)
        remaining -= len(block) + 2
        if remaining <= 0:
            break

    return "\n\n".join(sections)[:max_chars]
