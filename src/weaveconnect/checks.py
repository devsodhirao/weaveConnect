"""Run the developer's command, then check what actually reached Weave."""

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from weaveconnect import settings
from weaveconnect.records import CallRecord, fetch_since
from weaveconnect.report import Report, Status

SETUP_OP = "weaveconnect_setup_check"
# Calls are matched by time, so allow for a small difference between this clock and Weave's.
CLOCK_ALLOWANCE = timedelta(seconds=5)
REQUEST_FIELDS = ("messages", "input", "contents", "prompt")
KEY_PATTERNS = {
    "an OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    "a GitHub token": re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{30,}|\bgithub_pat_\w{30,}"),
    "an AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "a Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "a private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def check_run(
    command: str,
    repo: Path,
    project: str | None = None,
    expect_ops: tuple[str, ...] = (),
    timeout: int = 300,
    settle: float = 20.0,
) -> Report:
    repo = repo.resolve()
    report = Report(f"Checking what `{command}` sends to Weave")
    saved = settings.read_env(repo / ".env")
    project = project or os.environ.get(settings.PROJECT_NAME) or saved.get(settings.PROJECT_NAME)
    key = settings.resolve_key(saved)
    if not project or not key:
        report.add("Setup", Status.UNCHECKED, "Run `weaveconnect connect` first.")
        return report

    secrets = settings.secret_values(saved, dict(os.environ))
    started = datetime.now(UTC) - CLOCK_ALLOWANCE
    _run(report, command, repo, {**saved, **os.environ}, timeout, secrets)

    os.environ[settings.KEY_NAME] = key
    import weave

    try:
        client = weave.init(
            project,
            settings={"print_call_link": False, "implicitly_patch_integrations": False},
        )
        records = _settled(client, started, settle)
    except Exception as error:
        detail = settings.redact(f"{type(error).__name__}: {error}", secrets)
        report.add("Reading traces", Status.UNCHECKED, f"Could not read {project}: {detail}")
        return report
    judge_records(report, records, secrets, expect_ops)
    return report


def _run(report, command, repo, env, timeout, secrets) -> None:
    try:
        completed = subprocess.run(
            shlex.split(command), cwd=repo, env=env, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as error:
        report.add("Your command", Status.UNCHECKED, f"Could not start it: {error.filename}.")
        return
    except subprocess.TimeoutExpired:
        report.add("Your command", Status.FAIL, f"Still running after {timeout} seconds; stopped.")
        return
    if completed.returncode == 0:
        report.add("Your command", Status.PASS, "Finished normally.")
        return
    tail = settings.redact((completed.stderr or completed.stdout).strip()[-400:], secrets)
    report.add("Your command", Status.FAIL, f"Exited with code {completed.returncode}. {tail}")


def _settled(client: Any, since: datetime, settle: float) -> list[CallRecord]:
    """Poll until the number of new calls stops changing, or `settle` seconds pass."""
    deadline = time.monotonic() + settle
    previous = -1
    while True:
        records = [r for r in fetch_since(client, since) if r.op != SETUP_OP]
        if (records and len(records) == previous) or time.monotonic() > deadline:
            return records
        previous = len(records)
        time.sleep(2)


def judge_records(
    report: Report,
    records: list[CallRecord],
    secrets: list[str],
    expect_ops: tuple[str, ...] = (),
) -> None:
    """Add every trace check to `report`. Pure, so it is tested without Weave."""
    if not records:
        report.add(
            "Traces arrived",
            Status.FAIL,
            "Your command ran, but Weave received nothing from it. Make sure weave.init(...) runs "
            "before the agent starts and uses the same WEAVE_PROJECT.",
        )
        return
    traces = {record.trace_id for record in records}
    report.add("Traces arrived", Status.PASS, f"{len(records)} calls in {len(traces)} trace(s).")

    requests = [record for record in records if request_fingerprint(record)]
    if requests:
        report.add("Model calls recorded", Status.PASS, f"{len(requests)} model request(s).")
    else:
        report.add(
            "Model calls recorded",
            Status.FAIL,
            "No model request was recorded. Call weave.init before creating the model client, "
            "and leave Weave's automatic integrations on.",
        )

    ops = Counter(record.op for record in records)
    for name in expect_ops:
        if any(name in op for op in ops):
            report.add(f"Step `{name}` recorded", Status.PASS, "Found in the trace.")
        else:
            report.add(f"Step `{name}` recorded", Status.FAIL, "Not in the trace. Add @weave.op.")

    pairs = duplicate_requests(records)
    if pairs:
        first, second = pairs[0]
        report.add(
            "Each model request recorded once",
            Status.FAIL,
            f"{len(pairs)} request(s) were recorded twice, for example by `{first.op}` and "
            f"`{second.op}`. Keep one: usually remove the hand-written wrapper.",
        )
    elif requests:
        report.add("Each model request recorded once", Status.PASS, "No duplicates.")

    unfinished = [record for record in records if record.ended_at is None]
    if unfinished:
        names = ", ".join(sorted({record.op for record in unfinished}))
        report.add(
            "Every call finished",
            Status.FAIL,
            f"{len(unfinished)} call(s) never finished ({names}). The process may have stopped "
            "before Weave finished sending.",
        )
    else:
        report.add("Every call finished", Status.PASS, "All calls have an end time.")

    leaks = find_leaks(records, secrets)
    if leaks:
        record, path, kind = leaks[0]
        report.add(
            "No secrets in traces",
            Status.FAIL,
            f"{len(leaks)} place(s) hold a secret; first: {kind} in `{record.op}` at {path}. "
            "Keep it out of traced arguments, or redact it before Weave records it.",
        )
    else:
        report.add("No secrets in traces", Status.PASS, "None found.")

    report.notes.append("What Weave saw: " + ", ".join(f"{op} ×{n}" for op, n in ops.items()))
    errors = sorted({record.op for record in records if record.exception})
    if errors:
        report.notes.append("Errors recorded inside: " + ", ".join(errors))
    roots = [record for record in records if record.parent_id is None and record.url]
    if roots:
        report.notes.append(f"Open the trace: {roots[-1].url}")


def request_fingerprint(record: CallRecord) -> str | None:
    """Identify a model request by its model and prompt, ignoring client objects and timeouts."""
    model = record.inputs.get("model")
    field = next((name for name in REQUEST_FIELDS if record.inputs.get(name)), None)
    if not isinstance(model, str) or field is None:
        return None
    payload = json.dumps([model, record.inputs[field]], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def duplicate_requests(records: list[CallRecord]) -> list[tuple[CallRecord, CallRecord]]:
    """Pairs of calls that recorded the same model request at the same time."""
    groups: dict[str, list[CallRecord]] = defaultdict(list)
    for record in records:
        fingerprint = request_fingerprint(record)
        if fingerprint:
            groups[fingerprint].append(record)
    pairs = []
    for group in groups.values():
        group.sort(key=lambda record: record.started_at)
        for index, first in enumerate(group):
            for second in group[index + 1 :]:
                if _overlap(first, second):
                    pairs.append((first, second))
    return pairs


def _overlap(first: CallRecord, second: CallRecord) -> bool:
    end = first.ended_at or datetime.max.replace(tzinfo=UTC)
    return second.started_at < end


def find_leaks(records: list[CallRecord], secrets: list[str]) -> list[tuple[CallRecord, str, str]]:
    """Places where a secret value or a common key format appears. Never returns the secret."""
    leaks = []
    for record in records:
        fields = {"inputs": record.inputs, "output": record.output, "exception": record.exception}
        for path, text in _strings(fields):
            kind = next(("a value from your settings" for s in secrets if s in text), None)
            kind = kind or next((n for n, p in KEY_PATTERNS.items() if p.search(text)), None)
            if kind:
                leaks.append((record, path, kind))
    return leaks


def _strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path or "(value)", value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")
