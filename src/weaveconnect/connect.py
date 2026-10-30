"""Connect a project folder to Weave, then prove that a trace arrives."""

import getpass
import os
import secrets
import sys
import time
from pathlib import Path

import weave

from weaveconnect import settings
from weaveconnect.records import query_calls
from weaveconnect.report import Report, Status

AUTHORIZE_URL = "https://wandb.ai/authorize"
NO_KEY = (
    "No key found. Ask the person to run `weaveconnect connect` in their own terminal "
    f"(in Claude Code: `! weaveconnect connect`) or to set {settings.KEY_NAME}. "
    "Never paste a key into an agent chat."
)


@weave.op(name="weaveconnect_setup_check")
def _setup_check(nonce: str) -> dict:
    return {"nonce": nonce, "ok": True}


def connect(repo: Path, project: str | None = None, interactive: bool | None = None) -> Report:
    repo = repo.resolve()
    report = Report(f"Connecting {repo.name} to Weave")
    env_path = repo / ".env"
    saved = settings.read_env(env_path)
    interactive = sys.stdin.isatty() if interactive is None else interactive

    key = settings.resolve_key(saved)
    entered = False
    if not key and interactive:
        print(
            f"Open {AUTHORIZE_URL}, copy your W&B API key and paste it here. It will not be shown."
        )
        key = getpass.getpass("W&B API key: ").strip() or None
        entered = key is not None
    if not key:
        report.add("W&B key", Status.FAIL, NO_KEY)
        return report
    source = (
        "entered now"
        if entered
        else ("environment" if os.environ.get(settings.KEY_NAME) else ".env")
    )
    report.add("W&B key", Status.PASS, f"Found ({source}). It is never printed.")

    ignored, added = settings.ignore_env(repo)
    if ignored:
        report.add(
            "Key kept out of Git",
            Status.PASS,
            "Added .env to .gitignore." if added else ".env is ignored.",
        )
    else:
        report.add(
            "Key kept out of Git", Status.FAIL, "Git still tracks .env. Remove it from Git first."
        )
        return report

    project = settings.resolve_project(project, repo, saved)
    updates = {settings.PROJECT_NAME: project}
    if entered:
        updates[settings.KEY_NAME] = key
    settings.write_env(env_path, updates)

    os.environ[settings.KEY_NAME] = key
    try:
        client = weave.init(project, settings={"print_call_link": False})
    except Exception as error:
        report.add("Weave project", *_explain(error, [key]))
        return report
    full_name = f"{client.entity}/{client.project}"
    if full_name != project:
        settings.write_env(env_path, {settings.PROJECT_NAME: full_name})
    report.add(
        "Weave project", Status.PASS, f"{full_name}, saved as {settings.PROJECT_NAME} in .env."
    )

    nonce = secrets.token_hex(8)
    _, call = _setup_check.call(nonce)
    client.flush()
    record = _read_back(client, str(call.id), nonce)
    if record is None:
        report.add(
            "Test trace", Status.FAIL, "Sent, but Weave did not return it within 30 seconds."
        )
        return report
    report.add("Test trace", Status.PASS, f"Sent and read back: {record.url}")
    report.notes.append(f"Your project: https://wandb.ai/{full_name}/weave")
    report.notes.append(
        "Next: add tracing to your agent, then run `weaveconnect check --run '<command>'`."
    )
    return report


def _read_back(client, call_id: str, nonce: str, wait: float = 30.0):
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            record = next(query_calls(client, call_ids=[call_id], limit=1), None)
        except Exception:
            record = None
        if record and isinstance(record.output, dict) and record.output.get("nonce") == nonce:
            return record
        time.sleep(2)
    return None


def _explain(error: Exception, secrets_to_hide: list[str]) -> tuple[Status, str]:
    text = settings.redact(f"{type(error).__name__}: {error}", secrets_to_hide)
    lowered = text.lower()
    if any(word in lowered for word in ("401", "unauthorized", "invalid api key", "api key")):
        return (
            Status.FAIL,
            f"W&B rejected the key. Get a new one at {AUTHORIZE_URL}. ({text[:200]})",
        )
    if any(word in lowered for word in ("403", "permission", "not allowed")):
        return Status.FAIL, f"This key cannot create projects in that team. ({text[:200]})"
    return Status.UNCHECKED, f"Could not reach W&B. ({text[:200]})"
