"""Run the developer's command, then check what actually reached Weave.

Both kinds of tracing are read: Calls (`@weave.op` and automatic integrations) and agent spans
(W&B's Session SDK and OpenTelemetry GenAI), which W&B recommends for agent-shaped traces.
"""

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
from weaveconnect.records import CallRecord, SpanRecord, fetch_since, fetch_spans_since
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
        records, spans = _settled(client, started, settle)
    except Exception as error:
        detail = settings.redact(f"{type(error).__name__}: {error}", secrets)
        report.add("Reading traces", Status.UNCHECKED, f"Could not read {project}: {detail}")
        return report
    judge_records(report, records, secrets, expect_ops, spans)
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


def _settled(
    client: Any, since: datetime, settle: float
) -> tuple[list[CallRecord], list[SpanRecord]]:
    """Poll until the number of new calls and spans stops changing, or `settle` seconds pass."""
    deadline = time.monotonic() + settle
    previous = (-1, -1)
    while True:
        records = [r for r in fetch_since(client, since) if r.op != SETUP_OP]
        spans = fetch_spans_since(client, since)
        counts = (len(records), len(spans))
        if (sum(counts) and counts == previous) or time.monotonic() > deadline:
            return records, spans
        previous = counts
        time.sleep(2)


def judge_records(
    report: Report,
    records: list[CallRecord],
    secrets: list[str],
    expect_ops: tuple[str, ...] = (),
    spans: list[SpanRecord] | tuple = (),
) -> None:
    """Add every trace check to `report`. Pure, so it is tested without Weave."""
    spans = list(spans)
    if not records and not spans:
        report.add(
            "Traces arrived",
            Status.FAIL,
            "Your command ran, but Weave received nothing from it. Make sure weave.init(...) runs "
            "before the agent starts and uses the same WEAVE_PROJECT.",
        )
        return
    traces = {record.trace_id for record in records} | {span.trace_id for span in spans}
    parts = [f"{len(records)} calls"] if records else []
    parts += [f"{len(spans)} agent spans"] if spans else []
    report.add("Traces arrived", Status.PASS, f"{' and '.join(parts)} in {len(traces)} trace(s).")

    requests = [record for record in records if request_fingerprint(record)]
    chats = [span for span in spans if span.operation == "chat"]
    if requests or chats:
        report.add(
            "Model calls recorded", Status.PASS, f"{len(requests) + len(chats)} model call(s)."
        )
    else:
        report.add(
            "Model calls recorded",
            Status.FAIL,
            "No model call was recorded. Call weave.init before creating the model client and "
            "leave automatic integrations on, or record each call with the Session SDK's turn.llm.",
        )

    ops = Counter(record.op for record in records)
    span_names = Counter(span.name for span in spans)
    for name in expect_ops:
        found = any(name in op for op in ops) or any(
            name in str(span.tool or span.name) for span in spans
        )
        report.add(
            f"Step `{name}` recorded",
            Status.PASS if found else Status.FAIL,
            "Found in the trace." if found else "Not in the trace. Trace that step as well.",
        )

    pairs = duplicate_requests(records)
    crossed = recorded_both_ways(records, chats)
    if pairs:
        first, second = pairs[0]
        report.add(
            "Each model call recorded once",
            Status.FAIL,
            f"{len(pairs)} request(s) were recorded twice, for example by `{first.op}` and "
            f"`{second.op}`. Keep one: usually remove the hand-written wrapper.",
        )
    elif crossed:
        report.add(
            "Each model call recorded once",
            Status.FAIL,
            f"{len(crossed)} model call(s) were recorded both as an agent span and as a Call. When "
            "you record spans yourself, turn automatic integrations off: weave.init(..., "
            'settings={"implicitly_patch_integrations": False}).',
        )
    elif requests or chats:
        report.add("Each model call recorded once", Status.PASS, "No duplicates.")

    if spans:
        orphans = agent_shape_problems(spans)
        if orphans:
            names = ", ".join(sorted({span.name for span in orphans}))
            report.add(
                "Agent shape",
                Status.FAIL,
                f"{len(orphans)} span(s) have no agent turn above them ({names}). Start each model "
                "and tool span inside a turn.",
            )
        else:
            turns = sum(1 for span in spans if span.operation == "invoke_agent")
            report.add(
                "Agent shape", Status.PASS, f"Model and tool spans sit inside {turns} turn(s)."
            )

    unfinished = [r.op for r in records if r.ended_at is None]
    unfinished += [s.name for s in spans if s.ended_at is None]
    if unfinished:
        report.add(
            "Every call finished",
            Status.FAIL,
            f"{len(unfinished)} step(s) never finished ({', '.join(sorted(set(unfinished)))}). The "
            "process may have stopped before Weave finished sending.",
        )
    else:
        report.add("Every call finished", Status.PASS, "Every step has an end time.")

    leaks = find_leaks(records, secrets) + find_span_leaks(spans, secrets)
    if leaks:
        where, path, kind = leaks[0]
        report.add(
            "No secrets in traces",
            Status.FAIL,
            f"{len(leaks)} place(s) hold a secret; first: {kind} in `{where}` at {path}. Keep it "
            "out of traced arguments, or redact it before Weave records it.",
        )
    else:
        report.add("No secrets in traces", Status.PASS, "None found.")

    if ops:
        report.notes.append("Calls: " + ", ".join(f"{op} ×{n}" for op, n in ops.items()))
    if span_names:
        report.notes.append("Agent spans: " + ", ".join(f"{n} ×{c}" for n, c in span_names.items()))
    errors = sorted({r.op for r in records if r.exception} | {s.name for s in spans if s.error})
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
                if _overlap(first.started_at, first.ended_at, second.started_at, second.ended_at):
                    pairs.append((first, second))
    return pairs


def recorded_both_ways(records: list[CallRecord], chats: list[SpanRecord]) -> list[SpanRecord]:
    """Model-call spans that a Call for the same model also recorded, at the same time."""
    requests = [record for record in records if request_fingerprint(record)]
    return [
        chat
        for chat in chats
        if any(
            record.inputs.get("model") == chat.model
            and _overlap(record.started_at, record.ended_at, chat.started_at, chat.ended_at)
            for record in requests
        )
    ]


def agent_shape_problems(spans: list[SpanRecord]) -> list[SpanRecord]:
    """Model and tool spans with no agent turn above them in the same trace."""
    by_id = {span.id: span for span in spans}
    problems = []
    for span in spans:
        if span.operation not in ("chat", "execute_tool"):
            continue
        parent = by_id.get(span.parent_id or "")
        while parent is not None and parent.operation != "invoke_agent":
            parent = by_id.get(parent.parent_id or "")
        if parent is None and span.parent_id is None:
            problems.append(span)
    return problems


def _overlap(start_a, end_a, start_b, end_b) -> bool:
    latest = datetime.max.replace(tzinfo=UTC)
    return start_a < (end_b or latest) and start_b < (end_a or latest)


def find_leaks(records: list[CallRecord], secrets: list[str]) -> list[tuple[str, str, str]]:
    """Places where a secret value or a common key format appears. Never returns the secret."""
    leaks = []
    for record in records:
        fields = {"inputs": record.inputs, "output": record.output, "exception": record.exception}
        leaks += [(record.op, path, kind) for path, kind in _secret_paths(fields, secrets)]
    return leaks


def find_span_leaks(spans: list[SpanRecord], secrets: list[str]) -> list[tuple[str, str, str]]:
    return [
        (span.name, path, kind)
        for span in spans
        for path, kind in _secret_paths(span.payload, secrets)
    ]


def _secret_paths(fields: Any, secrets: list[str]) -> Iterator[tuple[str, str]]:
    for path, text in _strings(fields):
        kind = next(("a value from your settings" for s in secrets if s in text), None)
        kind = kind or next((n for n, p in KEY_PATTERNS.items() if p.search(text)), None)
        if kind:
            yield path, kind


def _strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path or "(value)", value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")
