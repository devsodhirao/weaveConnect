"""Local settings: the .env file, the Git rule that keeps it private, and secret values."""

import fnmatch
import os
import re
import subprocess
from pathlib import Path

KEY_NAME = "WANDB_API_KEY"
PROJECT_NAME = "WEAVE_PROJECT"

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.IGNORECASE)
_PROJECT_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def read_env(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines; comments and blank lines are ignored."""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        match = _LINE.match(line)
        if match and not line.lstrip().startswith("#"):
            values[match.group(1)] = _unquote(match.group(2).strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Set keys in place, keep every other line, and leave the file readable only by its owner."""
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(updates)
    for index, line in enumerate(lines):
        match = _LINE.match(line)
        if match and match.group(1) in remaining:
            lines[index] = f"{match.group(1)}={remaining.pop(match.group(1))}"
    lines += [f"{name}={value}" for name, value in remaining.items()]
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    path.chmod(0o600)


def git_ignores(repo: Path, name: str = ".env") -> bool | None:
    """Ask Git whether it ignores `name`. None means the folder is not a Git repository."""
    try:
        result = subprocess.run(["git", "check-ignore", "-q", name], cwd=repo, capture_output=True)
    except FileNotFoundError:
        return None
    return {0: True, 1: False}.get(result.returncode)


def ignore_env(repo: Path, name: str = ".env") -> tuple[bool, bool]:
    """Make sure the settings file stays out of Git. Returns (ignored, rule_added)."""
    if git_ignores(repo, name) or _listed(repo / ".gitignore", name):
        return True, False
    gitignore = repo / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    separator = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(
        f"{existing}{separator}# Local secrets: keep API keys out of Git\n{name}\n"
    )
    verdict = git_ignores(repo, name)
    return (verdict if verdict is not None else _listed(gitignore, name)), True


def _listed(gitignore: Path, name: str) -> bool:
    if not gitignore.exists():
        return False
    patterns = [line.strip() for line in gitignore.read_text().splitlines()]
    return any(p and not p.startswith(("#", "!")) and fnmatch.fnmatch(name, p) for p in patterns)


def resolve_project(explicit: str | None, repo: Path, saved: dict[str, str]) -> str:
    """Explicit choice, then the environment, then .env, then the folder name."""
    chosen = explicit or os.environ.get(PROJECT_NAME) or saved.get(PROJECT_NAME)
    return chosen or _PROJECT_CHARS.sub("-", repo.resolve().name).strip("-") or "weave-project"


def resolve_key(saved: dict[str, str]) -> str | None:
    return os.environ.get(KEY_NAME) or saved.get(KEY_NAME) or None


def secret_values(*sources: dict[str, str]) -> list[str]:
    """Values of settings whose names look secret and are long enough not to match by chance."""
    found = {
        value
        for source in sources
        for name, value in source.items()
        if _SECRET_NAME.search(name) and len(value) >= 8
    }
    return sorted(found, key=len, reverse=True)


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[secret removed]")
    return text
