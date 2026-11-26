"""Planning and evidence-based verification for project work.

Milestone verification commands run as shell checks on the local host. Only
trusted plans should be audited: the worktree is not a sandbox and shell
commands have the permissions of the DAGGER process.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence


Executor = Callable[[str], Awaitable[str]]

SECRETS_ENV_VARS = frozenset({
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "GITHUB_TOKEN", "GH_TOKEN", "TAVILY_API_KEY",
})

_NUMERIC_TEST_OPS: dict[str, str] = {
    "-eq": "==",
    "-ne": "!=",
    "-gt": ">",
    "-ge": ">=",
    "-lt": "<",
    "-le": "<=",
}

_FILE_CONTENT_WITH_SCRIPT = frozenset({"awk", "gawk", "mawk", "nawk", "sed", "jq", "yq"})
_FILE_CONTENT_WITH_PATTERN = frozenset({"grep", "egrep", "fgrep", "rg"})
_FILE_CONTENT_DIRECT = frozenset({
    "cat", "wc", "head", "tail", "cut", "sort", "uniq", "diff", "cmp",
    "nl", "od", "hexdump", "strings", "file",
})
_SCRIPT_INTERPRETERS = frozenset({"python", "python3", "node", "perl", "ruby"})

_OPTS_WITH_ARG: dict[str, frozenset[str]] = {
    "awk": frozenset({"-F", "-v", "-f", "-i", "-l", "-E", "-W"}),
    "gawk": frozenset({"-F", "-v", "-f", "-i", "-l", "-E", "-W"}),
    "mawk": frozenset({"-F", "-v", "-f", "-i", "-l", "-E", "-W"}),
    "nawk": frozenset({"-F", "-v", "-f", "-i", "-l", "-E", "-W"}),
    "sed": frozenset({"-e", "-f"}),
    "jq": frozenset({"-f", "--from-file", "--arg", "--argjson", "--slurpfile", "--rawfile", "--argfile", "-L"}),
    "yq": frozenset({"-f", "--from-file", "--arg", "--argjson", "--slurpfile", "--rawfile", "--argfile", "-L"}),
    "grep": frozenset({
        "-e", "-f", "-m", "-A", "-B", "-C", "-D", "-d",
        "--regexp", "--file", "--max-count", "--after-context", "--before-context",
        "--context", "--label", "--exclude", "--include", "--exclude-dir", "--include-dir",
    }),
    "egrep": frozenset({
        "-e", "-f", "-m", "-A", "-B", "-C", "-D", "-d",
        "--regexp", "--file", "--max-count", "--after-context", "--before-context",
        "--context", "--label", "--exclude", "--include", "--exclude-dir", "--include-dir",
    }),
    "fgrep": frozenset({
        "-e", "-f", "-m", "-A", "-B", "-C", "-D", "-d",
        "--regexp", "--file", "--max-count", "--after-context", "--before-context",
        "--context", "--label", "--exclude", "--include", "--exclude-dir", "--include-dir",
    }),
    "rg": frozenset({
        "-e", "-f", "-m", "-A", "-B", "-C", "-g", "-t", "-T",
        "--regexp", "--file", "--max-count", "--after-context", "--before-context",
        "--context", "--glob", "--type", "--type-not",
    }),
    "head": frozenset({"-n", "-c", "--lines", "--bytes"}),
    "tail": frozenset({"-n", "-c", "--lines", "--bytes"}),
    "wc": frozenset({"--files0-from"}),
    "cut": frozenset({"-b", "-c", "-d", "-f", "--bytes", "--characters", "--delimiter", "--fields", "--output-delimiter"}),
    "sort": frozenset({"-k", "-t", "-o", "-T", "-S", "--key", "--field-separator", "--output", "--temporary-directory", "--buffer-size"}),
    "uniq": frozenset({"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"}),
    "diff": frozenset({"-U", "-C", "-I", "-x", "-X", "-L", "--unified", "--context", "--label"}),
    "cmp": frozenset({"-i", "-n", "--ignore-initial", "--bytes"}),
}

_SCRIPT_PROVIDING_FLAGS: dict[str, frozenset[str]] = {
    "awk": frozenset({"-f", "-E"}),
    "gawk": frozenset({"-f", "-E"}),
    "mawk": frozenset({"-f", "-E"}),
    "nawk": frozenset({"-f", "-E"}),
    "sed": frozenset({"-e", "-f"}),
    "jq": frozenset({"-f", "--from-file"}),
    "yq": frozenset({"-f", "--from-file"}),
    "grep": frozenset({"-e", "-f", "--regexp", "--file"}),
    "egrep": frozenset({"-e", "-f", "--regexp", "--file"}),
    "fgrep": frozenset({"-e", "-f", "--regexp", "--file"}),
    "rg": frozenset({"-e", "-f", "--regexp", "--file"}),
}

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_INT_TOKEN_RE = re.compile(r"""^(?:"(-?\d+)"|'(-?\d+)'|(-?\d+))(?=$|[\s;&|)\]])""")


def _scan_cmd_subst(text: str, start: int) -> tuple[str, int] | None:
    """Scan a POSIX $(...) or `...` command substitution starting at `start`."""
    n = len(text)
    if text[start:start + 2] == "$(":
        i = start + 2
        depth = 1
        in_single = False
        in_double = False
        escaped = False
        while i < n:
            ch = text[i]
            if escaped:
                escaped = False
                i += 1
                continue
            if in_single:
                if ch == "'":
                    in_single = False
                i += 1
                continue
            if ch == "\\":
                escaped = True
                i += 1
                continue
            if in_double:
                if ch == '"':
                    in_double = False
                i += 1
                continue
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text[start + 2:i], i + 1
            i += 1
        return None
    if start < n and text[start] == "`":
        i = start + 1
        escaped = False
        while i < n:
            ch = text[i]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "`":
                return text[start + 1:i], i + 1
            i += 1
    return None


def _scan_quoted_or_unquoted_subst(text: str, pos: int) -> tuple[str, int] | None:
    """Scan `$(...)`, `"$(...)"`, `` `...` ``, or `` "`...`" `` at `pos`."""
    if pos >= len(text):
        return None
    if text[pos] == '"':
        res = _scan_cmd_subst(text, pos + 1)
        if res is None:
            return None
        inner, end = res
        if end < len(text) and text[end] == '"':
            return inner, end + 1
        return None
    return _scan_cmd_subst(text, pos)


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _match_numeric_test(text: str, start: int) -> tuple[str, int] | None:
    """Match `test <subst> -op <num>` / `[ <subst> -op <num> ]` (or reversed) at `start`."""
    n = len(text)
    if text.startswith("test", start) and start + 4 < n and text[start + 4].isspace():
        is_bracket = False
        pos = _skip_ws(text, start + 4)
    elif text[start] == "[" and start + 1 < n and text[start + 1].isspace():
        is_bracket = True
        pos = _skip_ws(text, start + 1)
    else:
        return None

    def _finish(end_pos: int) -> int | None:
        if is_bracket:
            ws_pos = _skip_ws(text, end_pos)
            if ws_pos == end_pos or ws_pos >= n or text[ws_pos] != "]":
                return None
            end_pos = ws_pos + 1
        if end_pos < n and not (text[end_pos].isspace() or text[end_pos] in ";&|)"):
            return None
        return end_pos

    # Form 1: <subst> <op> <num>
    subst = _scan_quoted_or_unquoted_subst(text, pos)
    if subst is not None:
        inner, after_subst = subst
        op_pos = _skip_ws(text, after_subst)
        if op_pos > after_subst:
            op = text[op_pos:op_pos + 3]
            if op in _NUMERIC_TEST_OPS and op_pos + 3 < n and text[op_pos + 3].isspace():
                num_pos = _skip_ws(text, op_pos + 3)
                m = _INT_TOKEN_RE.match(text[num_pos:])
                if m is not None and inner.strip():
                    num = m.group(1) or m.group(2) or m.group(3)
                    end_pos = _finish(num_pos + m.end())
                    if end_pos is not None:
                        awk_op = _NUMERIC_TEST_OPS[op]
                        safe = (
                            f"({{ {inner.strip()}; }} | "
                            f"awk 'NR==1 && $1 ~ /^-?[0-9]+$/ {{v=$1+0; ok=1}} "
                            f"END {{exit !(ok && (v {awk_op} {num}))}}')"
                        )
                        return safe, end_pos

    # Form 2: <num> <op> <subst>
    m = _INT_TOKEN_RE.match(text[pos:])
    if m is not None:
        num = m.group(1) or m.group(2) or m.group(3)
        after_num = pos + m.end()
        op_pos = _skip_ws(text, after_num)
        if op_pos > after_num:
            op = text[op_pos:op_pos + 3]
            if op in _NUMERIC_TEST_OPS and op_pos + 3 < n and text[op_pos + 3].isspace():
                subst_pos = _skip_ws(text, op_pos + 3)
                subst = _scan_quoted_or_unquoted_subst(text, subst_pos)
                if subst is not None:
                    inner, after_subst = subst
                    if inner.strip():
                        end_pos = _finish(after_subst)
                        if end_pos is not None:
                            awk_op = _NUMERIC_TEST_OPS[op]
                            safe = (
                                f"({{ {inner.strip()}; }} | "
                                f"awk 'NR==1 && $1 ~ /^-?[0-9]+$/ {{v=$1+0; ok=1}} "
                                f"END {{exit !(ok && ({num} {awk_op} v))}}')"
                            )
                            return safe, end_pos
    return None


def _rewrite_numeric_test_substitutions(command: str) -> str:
    """Replace fragile `test "$(...)" -ge N` checks with safe POSIX awk comparisons."""
    out: list[str] = []
    i = 0
    n = len(command)
    in_single = False
    in_double = False
    escaped = False
    at_cmd_start = True

    while i < n:
        ch = command[i]
        if escaped:
            out.append(ch)
            escaped = False
            i += 1
            continue
        if in_single:
            out.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            i += 1
            continue
        if in_double:
            out.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        if at_cmd_start and ch in ("t", "["):
            matched = _match_numeric_test(command, i)
            if matched is not None:
                replacement, end_pos = matched
                out.append(replacement)
                i = end_pos
                at_cmd_start = False
                continue

        if ch == "'":
            in_single = True
            at_cmd_start = False
            out.append(ch)
            i += 1
        elif ch == '"':
            in_double = True
            at_cmd_start = False
            out.append(ch)
            i += 1
        elif ch.isspace():
            out.append(ch)
            i += 1
        elif ch in ";|&({!":
            out.append(ch)
            at_cmd_start = True
            i += 1
        else:
            out.append(ch)
            at_cmd_start = False
            i += 1
    return "".join(out)


def _is_guardable_path(token: str, expected_artifacts: frozenset[str]) -> bool:
    if not token or token in {"-", ".", ".."} or token.startswith("-") or token.startswith("/dev/"):
        return False
    if token.endswith("/"):
        return False
    if token in expected_artifacts:
        return True
    if any(ch in token for ch in "$*?[]`~"):
        return False
    return True


def _tokenize_shell(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";|&<>()")
        lexer.whitespace_split = False
        return list(lexer)
    except ValueError:
        return None


def _extract_file_content_paths(command: str, expected_artifacts: Sequence[str]) -> list[str]:
    """Identify file paths inspected by file-content commands in `command`."""
    expected_set = frozenset(item.strip() for item in expected_artifacts if isinstance(item, str) and item.strip())
    tokens = _tokenize_shell(command)
    if tokens is None:
        return [
            artifact for artifact in expected_artifacts
            if isinstance(artifact, str) and artifact.strip() and artifact.strip() in command
        ]

    # Split token stream into simple commands.
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in {"&&", "||", ";", "|", "(", ")", "{", "}"}:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)

    found: list[str] = []

    def _add(path: str) -> None:
        if _is_guardable_path(path, expected_set) and path not in found:
            found.append(path)

    for seg in segments:
        # Strip leading negation and shell variable assignments.
        idx = 0
        while idx < len(seg) and seg[idx] == "!":
            idx += 1
        while idx < len(seg) and _ENV_ASSIGN_RE.match(seg[idx]):
            idx += 1
        if idx >= len(seg):
            continue

        # Parse redirections and build argv.
        argv: list[str] = []
        j = idx
        while j < len(seg):
            tok = seg[j]
            if tok == "<":
                if j + 1 < len(seg):
                    _add(seg[j + 1])
                    j += 2
                    continue
            if tok in {">", ">>", "<>", ">&", "<&", "<<", "<<<"}:
                if argv and argv[-1].isdigit():
                    argv.pop()
                j += 2
                continue
            argv.append(tok)
            j += 1

        if not argv:
            continue
        prog = Path(argv[0]).name
        args = argv[1:]

        if prog in _SCRIPT_INTERPRETERS:
            joined = " ".join(argv)
            for artifact in expected_artifacts:
                art = artifact.strip() if isinstance(artifact, str) else ""
                if art and art in joined:
                    _add(art)
            continue

        if prog not in (_FILE_CONTENT_WITH_SCRIPT | _FILE_CONTENT_WITH_PATTERN | _FILE_CONTENT_DIRECT):
            continue

        opts_with_arg = _OPTS_WITH_ARG.get(prog, frozenset())
        script_flags = _SCRIPT_PROVIDING_FLAGS.get(prog, frozenset())
        has_script_flag = False
        is_recursive = False
        positionals: list[str] = []

        k = 0
        end_opts = False
        while k < len(args):
            arg = args[k]
            if end_opts:
                positionals.append(arg)
                k += 1
                continue
            if arg == "--":
                end_opts = True
                k += 1
                continue
            if arg.startswith("--"):
                flag_name = arg.split("=", 1)[0]
                if flag_name in script_flags:
                    has_script_flag = True
                if prog in _FILE_CONTENT_WITH_PATTERN and flag_name == "--recursive":
                    is_recursive = True
                if "=" not in arg and flag_name in opts_with_arg and k + 1 < len(args):
                    k += 2
                    continue
                k += 1
                continue
            if arg.startswith("-") and len(arg) > 1:
                if arg in script_flags:
                    has_script_flag = True
                if prog in _FILE_CONTENT_WITH_PATTERN and ("r" in arg[1:] or "R" in arg[1:]):
                    is_recursive = True
                if arg in opts_with_arg and k + 1 < len(args):
                    k += 2
                    continue
                # Handle attached short script flags like -efoo or -ffile
                if len(arg) > 2 and arg[:2] in script_flags:
                    has_script_flag = True
                k += 1
                continue
            positionals.append(arg)
            k += 1

        if prog in _FILE_CONTENT_WITH_SCRIPT:
            if prog in {"awk", "gawk", "mawk", "nawk"}:
                if has_script_flag:
                    candidates = [p for p in positionals if not _ENV_ASSIGN_RE.match(p)]
                else:
                    candidates = [p for p in positionals[1:] if not _ENV_ASSIGN_RE.match(p)]
            else:
                candidates = positionals if has_script_flag else positionals[1:]
            for candidate in candidates:
                _add(candidate)
        elif prog in _FILE_CONTENT_WITH_PATTERN:
            candidates = positionals if has_script_flag else positionals[1:]
            for candidate in candidates:
                if is_recursive and candidate not in expected_set:
                    continue
                _add(candidate)
        elif prog in _FILE_CONTENT_DIRECT:
            for candidate in positionals:
                _add(candidate)

    return found


def _split_top_level_and(command: str) -> list[str]:
    """Split `command` on top-level `&&` only (respecting quotes and grouping)."""
    parts: list[str] = []
    start = 0
    i = 0
    n = len(command)
    in_single = False
    in_double = False
    escaped = False
    paren_depth = 0
    brace_depth = 0

    while i < n:
        ch = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if in_double:
            if ch == '"':
                in_double = False
            i += 1
            continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "(":
            paren_depth += 1
        elif ch == ")" and paren_depth > 0:
            paren_depth -= 1
        elif ch == "{":
            brace_depth += 1
        elif ch == "}" and brace_depth > 0:
            brace_depth -= 1
        elif paren_depth == 0 and brace_depth == 0 and command[i:i + 2] == "&&":
            parts.append(command[start:i].strip())
            i += 2
            start = i
            continue
        i += 1
    parts.append(command[start:].strip())
    return parts


def _has_top_level_or_or_semi(command: str) -> bool:
    """Return True if `command` has a top-level `||` or `;` outside quotes/groups."""
    i = 0
    n = len(command)
    in_single = False
    in_double = False
    escaped = False
    paren_depth = 0
    brace_depth = 0

    while i < n:
        ch = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if in_double:
            if ch == '"':
                in_double = False
            i += 1
            continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "(":
            paren_depth += 1
        elif ch == ")" and paren_depth > 0:
            paren_depth -= 1
        elif ch == "{":
            brace_depth += 1
        elif ch == "}" and brace_depth > 0:
            brace_depth -= 1
        elif paren_depth == 0 and brace_depth == 0:
            if ch == ";" or command[i:i + 2] == "||":
                return True
        i += 1
    return False


def _parse_simple_file_test(clause: str) -> tuple[str, str] | None:
    """If `clause` is `test -s/-f <path>` or `[ -s/-f <path> ]`, return `(flag, path)`."""
    tokens = _tokenize_shell(clause)
    if not tokens:
        return None
    if len(tokens) == 3 and tokens[0] == "test" and tokens[1] in {"-s", "-f"}:
        return tokens[1], tokens[2]
    if len(tokens) == 4 and tokens[0] == "[" and tokens[3] == "]" and tokens[1] in {"-s", "-f"}:
        return tokens[1], tokens[2]
    return None


def guard_verification_command(command: str, expected_artifacts: Sequence[str] = ()) -> str:
    """Normalize and guard a shell verification command.

    1. Rewrites fragile numeric `test "$(...)" -op N` / `test $(...) -op N`
       checks so empty/missing output never raises `/bin/sh: test: Illegal number:`.
    2. Ensures file-content checks require `test -s <path> && ...` first.
    """
    normalized = _rewrite_numeric_test_substitutions(command.strip())
    paths = _extract_file_content_paths(normalized, expected_artifacts)
    if not paths:
        return normalized

    clauses = _split_top_level_and(normalized)
    guarded_paths: set[str] = set()
    upgraded_clauses = list(clauses)

    # Inspect leading `test -s` / `test -f` clauses (when followed by more clauses).
    if len(clauses) > 1:
        for idx, clause in enumerate(clauses[:-1]):
            parsed = _parse_simple_file_test(clause)
            if parsed is None:
                break
            flag, target = parsed
            if flag == "-s":
                guarded_paths.add(target)
            elif flag == "-f" and target in paths:
                upgraded_clauses[idx] = f"test -s {shlex.quote(target)}"
                guarded_paths.add(target)

    rebuilt = " && ".join(upgraded_clauses)
    missing = [path for path in paths if path not in guarded_paths]
    if not missing:
        return rebuilt

    prefix = " && ".join(f"test -s {shlex.quote(path)}" for path in missing)
    if _has_top_level_or_or_semi(rebuilt):
        return f"{prefix} && {{ {rebuilt}; }}"
    return f"{prefix} && {rebuilt}"


def _failure_signature(
    criteria: Sequence[CheckEvidence],
    artifacts: Sequence[CheckEvidence],
    commands: Sequence[CommandEvidence],
) -> str:
    payload = {
        "criteria": [[item.item, item.detail] for item in criteria if not item.passed],
        "artifacts": [[item.item, item.detail] for item in artifacts if not item.passed],
        "commands": [
            [item.command, item.returncode, item.stdout.strip(), item.stderr.strip()]
            for item in commands if not item.passed
        ],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _items(value: object, field: str) -> Sequence:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    return value


@dataclass(frozen=True)
class Criterion:
    description: str
    command: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "description", _text(self.description, "criterion description"))
        command = _items(self.command, "criterion command")
        object.__setattr__(self, "command", tuple(_text(arg, "command argument") for arg in command))

    @classmethod
    def from_dict(cls, value: Mapping) -> Criterion:
        if not isinstance(value, Mapping):
            raise ValueError("criterion must be an object")
        return cls(description=value.get("description"), command=value.get("command"))


@dataclass(frozen=True)
class PlannedTask:
    id: str
    description: str
    definition_of_done: tuple[Criterion, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "task id"))
        object.__setattr__(self, "description", _text(self.description, "task description"))
        criteria = _items(self.definition_of_done, "definition_of_done")
        if not all(isinstance(item, Criterion) for item in criteria):
            raise ValueError("definition_of_done must contain Criterion objects")
        object.__setattr__(self, "definition_of_done", tuple(criteria))

    @classmethod
    def from_dict(cls, value: Mapping) -> PlannedTask:
        if not isinstance(value, Mapping):
            raise ValueError("task must be an object")
        return cls(
            id=value.get("id"),
            description=value.get("description"),
            definition_of_done=tuple(
                Criterion.from_dict(item)
                for item in _items(value.get("definition_of_done"), "definition_of_done")
            ),
        )


@dataclass(frozen=True)
class Plan:
    goal: str
    tasks: tuple[PlannedTask, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "goal", _text(self.goal, "goal"))
        tasks = _items(self.tasks, "tasks")
        if not all(isinstance(task, PlannedTask) for task in tasks):
            raise ValueError("tasks must contain PlannedTask objects")
        if len({task.id for task in tasks}) != len(tasks):
            raise ValueError("task ids must be unique")
        object.__setattr__(self, "tasks", tuple(tasks))

    @classmethod
    def from_dict(cls, value: Mapping, *, goal: str | None = None) -> Plan:
        if not isinstance(value, Mapping):
            raise ValueError("plan must be an object")
        return cls(
            goal=goal if goal is not None else value.get("goal"),
            tasks=tuple(PlannedTask.from_dict(task) for task in _items(value.get("tasks"), "tasks")),
        )


@dataclass(frozen=True)
class VerificationResult:
    task_id: str
    criterion: Criterion
    passed: bool
    returncode: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Remediation:
    task_id: str
    instructions: str
    evidence: tuple[VerificationResult, ...]


@dataclass(frozen=True)
class AuditReport:
    passed: bool
    results: tuple[VerificationResult, ...]
    remediation: tuple[Remediation, ...]


@dataclass(frozen=True)
class ProjectReport:
    plan: Plan
    status: str
    hops: int
    audits: tuple[AuditReport, ...]
    worker_outputs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MilestoneDAG:
    milestones: tuple
    dependencies: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class CommandEvidence:
    command: str
    passed: bool
    returncode: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True)
class CheckEvidence:
    item: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class MilestoneAudit:
    status: str
    remediation_brief: str
    human_review_required: bool
    criteria_results: tuple[CheckEvidence, ...]
    artifact_results: tuple[CheckEvidence, ...]
    command_results: tuple[CommandEvidence, ...]


class ProjectManager:
    """Create structured plans and verify their explicit definition of done.

    Executors are async callables accepting a prompt and returning text. The
    optional standalone ``run`` counts one hop per worker dispatch.
    """

    guard_verification_command = staticmethod(guard_verification_command)

    def __init__(
        self,
        planner: Executor | None = None,
        worker: Executor | None = None,
        workspace: str | Path = ".",
        max_hops: int = 3,
        command_timeout: float = 60,
    ) -> None:
        if not isinstance(max_hops, int) or isinstance(max_hops, bool) or max_hops < 0:
            raise ValueError("max_hops must be a non-negative integer")
        if command_timeout <= 0:
            raise ValueError("command_timeout must be positive")
        self.planner = planner
        self.worker = worker
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        self.max_hops = max_hops
        self.command_timeout = command_timeout
        self._last_failure_signatures: dict[tuple[str, str, str], tuple[int, str]] = {}

    async def decompose_goal(
        self,
        task,
        executor: Executor | None = None,
        rolodex_text: str | None = None,
        skills_text: str = "",
    ) -> MilestoneDAG:
        """Ask the planner for a validated DAG of shared MilestoneTask models."""
        from dagger.schema import MilestoneTask

        planner = executor or self.planner
        if planner is None:
            raise ValueError("a planner executor is required")
        prompt = (
            "Decompose this project into at least two actionable milestone DAG nodes. Return ONLY JSON "
            'as {"milestones":[{"milestone_id":"unique-id","title":"...",'
            '"description":"...","required_skills":["..."],"depends_on":["id"],'
            '"dod":{"acceptance_criteria":["..."],"expected_artifacts":["relative/path"],'
            '"verification_commands":["executable arguments"]}}]}. '
            "Dependencies must reference other milestones and be acyclic. Give each "
            "acceptance criterion a corresponding executable verification command at "
            "the same array index; commands must exit zero only if the criterion is met. "
            "Commands execute as shell checks in the worktree; any expected artifact "
            "paths must be worktree-relative. Use an empty artifact list when none apply. "
            "File-content checks must guard each target file with `test -s <path> && ...` first. "
            "Never emit `test \"$(awk ...)\" -ge N` or unquoted `test $(awk ...) -ge N` "
            "comparisons that fail with `/bin/sh: test: Illegal number:`; use "
            "`test -s <path> && awk '... END { exit !(c+0 >= N) }' <path>` or "
            "`test -s <path> && grep -q ... <path>` instead. "
            "Include concrete, independently checkable criteria and commands.\n"
            + (f"Available Agent Rolodex:\n{rolodex_text}\n" if rolodex_text else "")
            + (f"Available Workspace Skills:\n{skills_text}\n" if skills_text else "")
            + json.dumps({"title": task.title, "description": task.description}, ensure_ascii=False)
        )
        response = await planner(prompt)
        try:
            value = json.loads(response)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("planner returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("milestone plan must be an object")
        raw_milestones = _items(value.get("milestones"), "milestones")
        if len(raw_milestones) < 2:
            raise ValueError("project decomposition must contain at least two milestones")
        milestones, dependencies = [], {}
        for raw in raw_milestones:
            if not isinstance(raw, dict):
                raise ValueError("milestone must be an object")
            data = dict(raw)
            milestone_id = _text(data.get("milestone_id"), "milestone_id")
            if milestone_id in dependencies:
                raise ValueError("milestone ids must be unique")
            deps = data.pop("depends_on", [])
            if not isinstance(deps, list) or not all(isinstance(dep, str) and dep.strip() for dep in deps):
                raise ValueError("depends_on must be an array of milestone ids")
            dependencies[milestone_id] = tuple(dep.strip() for dep in deps)
            milestone = MilestoneTask.model_validate(data)
            dod = milestone.dod
            if not dod.acceptance_criteria or len(dod.verification_commands) < len(dod.acceptance_criteria):
                raise ValueError("every acceptance criterion requires a verification command")
            for item in [
                *dod.acceptance_criteria,
                *dod.verification_commands,
                *dod.expected_artifacts,
                *milestone.allowed_write_paths,
            ]:
                _text(item, "definition of done entry")
            guarded_commands = [
                guard_verification_command(command, dod.expected_artifacts)
                for command in dod.verification_commands
            ]
            allowed_write_paths = list(
                dict.fromkeys([*milestone.allowed_write_paths, *dod.expected_artifacts])
            )
            milestone = milestone.model_copy(
                update={
                    "dod": dod.model_copy(update={"verification_commands": guarded_commands}),
                    "allowed_write_paths": allowed_write_paths,
                }
            )
            milestones.append(milestone)
        visiting, visited = set(), set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("milestone dependencies must be acyclic")
            if node in visited:
                return
            if node not in dependencies:
                raise ValueError(f"unknown milestone dependency: {node}")
            visiting.add(node)
            for dep in dependencies[node]:
                visit(dep)
            visiting.remove(node)
            visited.add(node)

        for node in dependencies:
            visit(node)
        return MilestoneDAG(tuple(milestones), dependencies)

    async def audit_milestone(
        self,
        milestone,
        worktree_path: str | Path,
        hop_count: int = 0,
        max_hops: int | None = None,
        *,
        run_id: str | None = None,
    ) -> MilestoneAudit:
        """Audit a milestone using command exits and workspace-relative artifacts.

        Criterion i is verified by command i. Missing evidence always fails;
        model-generated claims and worker output are never treated as proof.
        """
        limit = self.max_hops if max_hops is None else max_hops
        if (not isinstance(hop_count, int) or isinstance(hop_count, bool) or hop_count < 0
                or not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
            raise ValueError("hop_count and max_hops must be non-negative integers")
        worktree = Path(worktree_path).resolve()
        if not worktree.is_dir():
            raise ValueError("worktree_path must be an existing directory")
        checker = ProjectManager(workspace=worktree, command_timeout=self.command_timeout)
        commands = []
        for command in milestone.dod.verification_commands:
            try:
                guarded = guard_verification_command(
                    _text(command, "verification command"),
                    milestone.dod.expected_artifacts,
                )
                criterion = Criterion(guarded, ("/bin/sh", "-c", guarded))
            except ValueError as exc:
                commands.append(CommandEvidence(command, False, None, "", str(exc)))
                continue
            task = PlannedTask(milestone.milestone_id, milestone.title, (criterion,))
            result = (await checker.verify(task))[0]
            commands.append(CommandEvidence(guarded, result.passed, result.returncode, result.stdout, result.stderr))
        criteria = []
        for index, item in enumerate(milestone.dod.acceptance_criteria):
            passed = bool(item.strip()) and index < len(commands) and commands[index].passed
            detail = f"verified by command {index + 1}" if passed else f"command {index + 1} missing or failed"
            criteria.append(CheckEvidence(item, passed, detail))
        if not criteria:
            criteria.append(CheckEvidence("(missing acceptance criteria)", False, "No acceptance criteria supplied"))
        artifacts = []
        for item in milestone.dod.expected_artifacts:
            try:
                path = Path(item)
                target = (worktree / path).resolve()
                inside = bool(item.strip()) and not path.is_absolute() and target.is_relative_to(worktree)
                exists = inside and target.exists()
            except (OSError, RuntimeError, ValueError):
                exists = False
            artifacts.append(CheckEvidence(item, exists, "present" if exists else "missing or outside worktree"))
        unverifiable = (
            not milestone.dod.acceptance_criteria
            or any(not item.strip() for item in milestone.dod.acceptance_criteria)
            or len(commands) < len(milestone.dod.acceptance_criteria)
            or any(result.returncode in (None, 126, 127) for result in commands[:len(criteria)])
        )
        passed = not unverifiable and all(result.passed for result in (*criteria, *artifacts, *commands))
        failures = [
            f"{result.item}: {result.detail}"
            for result in (*criteria, *artifacts) if not result.passed
        ]
        failures.extend(
            f"command {result.command!r} failed (exit={result.returncode}): {result.stderr or result.stdout}"
            for result in commands if not result.passed
        )
        history_key = (run_id or "", str(worktree), milestone.milestone_id)
        duplicate_failure = False
        if passed:
            self._last_failure_signatures.pop(history_key, None)
            human_review_required = False
        else:
            signature = _failure_signature(criteria, artifacts, commands)
            previous = self._last_failure_signatures.get(history_key)
            duplicate_failure = (
                previous is not None
                and hop_count >= previous[0]
                and previous[1] == signature
            )
            human_review_required = unverifiable or hop_count >= limit or duplicate_failure
            if human_review_required:
                self._last_failure_signatures.pop(history_key, None)
            else:
                self._last_failure_signatures[history_key] = (hop_count, signature)
            if duplicate_failure:
                failures.append(
                    "Duplicate failure signature on consecutive attempts; escalating to human review."
                )
        return MilestoneAudit(
            "DOD_PASSED" if passed else "DOD_FAILED",
            "\n".join(failures),
            human_review_required,
            tuple(criteria), tuple(artifacts), tuple(commands),
        )

    async def create_plan(self, goal: str, executor: Executor | None = None) -> Plan:
        goal = _text(goal, "goal")
        planner = executor or self.planner
        if planner is None:
            raise ValueError("a planner executor is required")
        prompt = (
            "Create an actionable plan for the goal below. Return ONLY a JSON object "
            'with a non-empty "tasks" array. Each task must have a unique string "id", '
            'a concrete "description", and a non-empty "definition_of_done" array. '
            'Each criterion must have a "description" and a "command" array of '
            "executable and arguments. Commands must independently verify the criterion "
            "in the project workspace and exit zero only on success. "
            "Do not use prose "
            "as evidence or use shell strings.\nGoal:\n" + goal
        )
        response = await planner(prompt)
        if not isinstance(response, str):
            raise ValueError("planner must return a JSON string")
        try:
            value = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ValueError("planner returned invalid JSON") from exc
        return Plan.from_dict(value, goal=goal)

    async def verify(self, task: PlannedTask) -> tuple[VerificationResult, ...]:
        results = []
        for criterion in task.definition_of_done:
            env = {key: value for key, value in os.environ.items() if key not in SECRETS_ENV_VARS}
            try:
                process = await asyncio.create_subprocess_exec(
                    *criterion.command,
                    cwd=self.workspace,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=(os.name == "posix"),
                )
            except (OSError, ValueError) as exc:
                results.append(VerificationResult(task.id, criterion, False, None, "", str(exc)))
                continue
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), self.command_timeout)
                results.append(
                    VerificationResult(
                        task.id, criterion, process.returncode == 0, process.returncode,
                        stdout.decode("utf-8", errors="replace"),
                        stderr.decode("utf-8", errors="replace"),
                    )
                )
            except (TimeoutError, asyncio.CancelledError) as exc:
                if process.returncode is None:
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    except ProcessLookupError:
                        pass
                stdout, stderr = await process.communicate()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                results.append(
                    VerificationResult(
                        task.id, criterion, False, None,
                        stdout.decode("utf-8", errors="replace"),
                        stderr.decode("utf-8", errors="replace") + "\nVerification timed out",
                    )
                )
        return tuple(results)

    @staticmethod
    def instruction(task: PlannedTask, results: Sequence[VerificationResult] = ()) -> str:
        payload = {
            "task_id": task.id,
            "description": task.description,
            "definition_of_done": [
                {"description": item.description, "command": list(item.command)}
                for item in task.definition_of_done
            ],
        }
        failures = [
            {"criterion": result.criterion.description, "command": list(result.criterion.command),
             "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
            for result in results if result.task_id == task.id and not result.passed
        ]
        if failures:
            payload["verification_failures"] = failures
        return (
            "Implement the task and satisfy every definition-of-done criterion. "
            "If failures are provided, remediate them and preserve passing behavior.\n"
            + json.dumps(payload, ensure_ascii=False)
        )

    async def audit(self, plan: Plan, executor: Executor | None = None) -> AuditReport:
        # Verification, not a model's opinion, determines acceptance.
        all_results = []
        for task in plan.tasks:
            all_results.extend(await self.verify(task))
        results = tuple(all_results)
        remediation = tuple(
            Remediation(task.id, self.instruction(task, failures), failures)
            for task in plan.tasks
            if (failures := tuple(result for result in results if result.task_id == task.id and not result.passed))
        )
        return AuditReport(not remediation, results, remediation)

    async def run(self, goal: str) -> ProjectReport:
        if self.worker is None:
            raise ValueError("a worker executor is required")
        plan = await self.create_plan(goal)
        pending = list(plan.tasks)
        previous: dict[str, tuple[VerificationResult, ...]] = {}
        last_failure_sig: dict[str, tuple[tuple[str, int | None, str, str], ...]] = {}
        outputs: list[tuple[str, str]] = []
        audits: list[AuditReport] = []
        hops = 0
        while pending and hops < self.max_hops:
            task = pending.pop(0)
            hops += 1
            output = await self.worker(self.instruction(task, previous.get(task.id, ())))
            outputs.append((task.id, output))
            results = await self.verify(task)
            previous[task.id] = results
            if all(result.passed for result in results):
                last_failure_sig.pop(task.id, None)
            else:
                sig = tuple(
                    (r.criterion.description, r.returncode, r.stdout.strip(), r.stderr.strip())
                    for r in results if not r.passed
                )
                if last_failure_sig.get(task.id) == sig:
                    return ProjectReport(plan, "human_review_required", hops, tuple(audits), tuple(outputs))
                last_failure_sig[task.id] = sig
                pending.append(task)
            if not pending and len(previous) == len(plan.tasks):
                # Check all tasks again: later work may have broken earlier work.
                report = await self.audit(plan)
                audits.append(report)
                if report.passed:
                    return ProjectReport(plan, "passed", hops, tuple(audits), tuple(outputs))
                for remediation in report.remediation:
                    previous[remediation.task_id] = remediation.evidence
                    pending.extend(task for task in plan.tasks if task.id == remediation.task_id)
        return ProjectReport(plan, "hop_limit", hops, tuple(audits), tuple(outputs))
