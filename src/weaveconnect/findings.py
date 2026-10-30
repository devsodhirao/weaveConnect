"""Read recent runs and say what they show: failures with denominators, and where time goes.

A run is one trace of the developer's agent, from either kind of tracing: a root Call (`@weave.op`
and automatic integrations) or an agent turn (W&B's Session SDK or OpenTelemetry GenAI spans).
Evaluation traces and weaveconnect's own test trace are not runs.
"""

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from weaveconnect.checks import SETUP_OP, request_fingerprint
from weaveconnect.records import CallRecord, SpanRecord, object_name, query_calls, query_spans

FAILURES = ("tool_text", "errors", "empty", "looping")
_TOOL_MARKERS = re.compile(r"<tool_call>|\[TOOL_CALLS\]|<\|python_tag\|>|<function=", re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "site-packages", "__pycache__"}


@dataclass(frozen=True)
class Reply:
    model: str | None
    text: str
    called_tool: bool


@dataclass
class Run:
    trace_id: str
    entry: str
    started_at: datetime
    seconds: float | None
    prompt: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    answer: Any = None
    replies: list[Reply] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    steps: list[tuple[str, float]] = field(default_factory=list)
    url: str | None = None


@dataclass(frozen=True)
class Finding:
    key: str
    summary: str
    runs: list[Run]
    advice: str


def tool_call_as_text(text: Any) -> bool:
    """True when a reply is a tool call written out as text instead of a real tool call."""
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if _TOOL_MARKERS.search(stripped):
        return True
    try:
        data = json.loads(_FENCE.sub("", stripped))
    except ValueError:
        return False
    items = data if isinstance(data, list) else [data]
    return bool(items) and all(isinstance(item, dict) and _looks_like_call(item) for item in items)


def _looks_like_call(item: dict) -> bool:
    if item.get("type") == "function" and isinstance(item.get("function"), dict):
        return True
    return "name" in item and ("parameters" in item or "arguments" in item)


def model_reply(output: Any) -> tuple[str, bool]:
    """(text, called a tool) from a recorded OpenAI, Responses or Anthropic model response."""
    if not isinstance(output, dict):
        return (output if isinstance(output, str) else ""), False
    choices = output.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        return str(message.get("content") or ""), bool(message.get("tool_calls"))
    blocks = (
        output.get("content") if isinstance(output.get("content"), list) else output.get("output")
    )
    texts, called = [], False
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        called = called or block.get("type") in ("tool_use", "function_call")
        if block.get("type") in ("text", "output_text"):
            texts.append(str(block.get("text") or ""))
        for inner in block.get("content") if isinstance(block.get("content"), list) else []:
            if isinstance(inner, dict) and inner.get("type") == "output_text":
                texts.append(str(inner.get("text") or ""))
    return " ".join(texts), called


def span_reply(messages: Any) -> tuple[str, bool]:
    """(text, called a tool) from an agent span's output messages, whose parts may be JSON text."""
    texts, called = [], False
    for message in messages if isinstance(messages, list) else []:
        parts = message.get("content") if isinstance(message, dict) else None
        if isinstance(parts, str):
            try:
                parts = json.loads(parts)
            except ValueError:
                parts = [{"type": "text", "content": parts}]
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict):
                continue
            called = called or part.get("type") in ("tool_call", "function_call", "tool_use")
            if part.get("type") == "text":
                texts.append(str(part.get("content") or part.get("text") or ""))
    return " ".join(texts), called


def _seconds(start: datetime, end: datetime | None) -> float | None:
    return (end - start).total_seconds() if end else None


def runs_from_calls(calls: list[CallRecord]) -> list[Run]:
    traces: dict[str, list[CallRecord]] = defaultdict(list)
    for call in calls:
        traces[call.trace_id].append(call)
    runs = []
    for trace_id, members in traces.items():
        root = next((call for call in members if call.parent_id is None), None)
        if root is None or root.op == SETUP_OP or root.op.startswith("Evaluation."):
            continue
        prompt = next((value for value in root.inputs.values() if isinstance(value, str)), None)
        run = Run(
            trace_id,
            root.op,
            root.started_at,
            _seconds(root.started_at, root.ended_at),
            prompt=prompt,
            inputs=root.inputs,
            answer=root.output,
            url=root.url,
        )
        for call in sorted(members, key=lambda c: c.started_at):
            if request_fingerprint(call):
                run.replies.append(Reply(call.inputs.get("model"), *model_reply(call.output)))
            if call.exception:
                run.errors.append((call.op, call.exception.strip().splitlines()[0][:120]))
            if call is not root and (duration := _seconds(call.started_at, call.ended_at)):
                run.steps.append((call.op, duration))
        runs.append(run)
    return runs


def runs_from_spans(spans: list[SpanRecord]) -> list[Run]:
    traces: dict[str, list[SpanRecord]] = defaultdict(list)
    for span in spans:
        traces[span.trace_id].append(span)
    runs = []
    for trace_id, members in traces.items():
        root = next(
            (s for s in members if s.parent_id is None and s.operation == "invoke_agent"), None
        )
        if root is None:
            continue
        run = Run(
            trace_id,
            root.agent or root.name,
            root.started_at,
            _seconds(root.started_at, root.ended_at),
            prompt=_first_user_text(root),
        )
        for span in sorted(members, key=lambda s: s.started_at):
            if span.operation == "chat":
                reply = Reply(span.model, *span_reply(span.payload.get("output_messages")))
                run.replies.append(reply)
                run.answer = reply.text
            if span.error:
                run.errors.append((span.name, span.error.splitlines()[0][:120]))
            if span is not root and (duration := _seconds(span.started_at, span.ended_at)):
                run.steps.append((span.name, duration))
        runs.append(run)
    return runs


def _first_user_text(span: SpanRecord) -> str | None:
    for message in span.payload.get("input_messages") or []:
        if isinstance(message, dict) and message.get("role") == "user":
            text, _ = span_reply([message])
            return text or None
    return None


def load_runs(client: Any, window: int = 50) -> list[Run]:
    """The most recent runs from both kinds of tracing, newest first."""
    runs = runs_from_calls(list(query_calls(client, limit=3000)))
    runs += runs_from_spans(list(query_spans(client, limit=1000)))
    return sorted(runs, key=lambda run: run.started_at, reverse=True)[:window]


def find(runs: list[Run]) -> list[Finding]:
    """Failures in recent runs, each with how many runs it affected out of all of them."""
    total = len(runs)
    findings = []
    texted = [
        r for r in runs if any(tool_call_as_text(x.text) and not x.called_tool for x in r.replies)
    ]
    if texted:
        models = sorted(
            {x.model for r in texted for x in r.replies if x.model and tool_call_as_text(x.text)}
        )
        named = f" ({', '.join(models)})" if models else ""
        findings.append(
            Finding(
                "tool_text",
                f"{len(texted)} of {total} runs: the model wrote a tool call as text instead of calling the tool{named}.",
                texted,
                "The model or provider is not returning real tool calls. Try another model, or check that "
                "tool calling is supported for it.",
            )
        )
    errored = [r for r in runs if r.errors]
    if errored:
        step, message = Counter(r.errors[0] for r in errored).most_common(1)[0][0]
        findings.append(
            Finding(
                "errors",
                f"{len(errored)} of {total} runs recorded an error, most often in `{step}`: {message}",
                errored,
                "Open one of these traces to see the step that failed and its inputs.",
            )
        )
    empty = [r for r in runs if r.replies and not str(r.answer or "").strip()]
    if empty:
        findings.append(
            Finding(
                "empty",
                f"{len(empty)} of {total} runs returned an empty answer.",
                empty,
                "Check what the last model call returned in one of these traces.",
            )
        )
    counts = [len(r.replies) for r in runs if r.replies]
    if len(counts) >= 3:
        usual = median(counts)
        limit = max(3, 2 * usual)
        looping = [r for r in runs if len(r.replies) > limit]
        if looping:
            findings.append(
                Finding(
                    "looping",
                    f"{len(looping)} of {total} runs made more than {limit:g} model calls; most make {usual:g}.",
                    looping,
                    "The agent may be looping. Open the longest trace and look for repeated steps.",
                )
            )
    return findings


def slowest_step(runs: list[Run]) -> str | None:
    durations: dict[str, list[float]] = defaultdict(list)
    for run in runs:
        for step, seconds in run.steps:
            durations[step].append(seconds)
    run_time = sum(run.seconds or 0 for run in runs)
    if not durations or run_time <= 0:
        return None
    step, times = max(durations.items(), key=lambda item: sum(item[1]))
    share = min(sum(times) / run_time, 1.0)
    return (
        f"Most time goes to `{step}`: median {median(times):.1f} s per call, "
        f"{share:.0%} of run time across {len(runs)} runs."
    )


def failing(findings: list[Finding]) -> list[Run]:
    seen, runs = set(), []
    for finding in findings:
        if finding.key in FAILURES:
            for run in finding.runs:
                if run.trace_id not in seen:
                    seen.add(run.trace_id)
                    runs.append(run)
    return runs


def locate(repo: Path, function: str) -> str | None:
    """`path/to/file.py:function` for the shallowest file that defines `function`."""
    pattern = re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(function)}\s*\(", re.MULTILINE)
    matches = []
    for path in repo.rglob("*.py"):
        if _SKIP_DIRS.intersection(path.relative_to(repo).parts):
            continue
        try:
            if pattern.search(path.read_text(errors="ignore")):
                matches.append(path.relative_to(repo))
        except OSError:
            continue
    if not matches:
        return None
    best = min(matches, key=lambda path: (len(path.parts), str(path)))
    return f"{best}:{function}"


def next_step(
    findings: list[Finding], repo: Path, covered: set[str] | frozenset = frozenset()
) -> str | None:
    """Turn failures into tests, unless tests built from them already pass (`covered`)."""
    runs = [run for run in failing(findings) if run.entry not in covered]
    if not runs:
        return None
    entry = Counter(run.entry for run in runs).most_common(1)[0][0]
    target = locate(repo, entry) or f"<file>:{entry}"
    return (
        f"Turn the {len(runs)} failing run(s) into test cases, so a fix can be proven: "
        f"`weaveconnect eval --target {target} --from-failures`"
    )


def passing_failure_tests(client: Any, entries: set[str]) -> set[str]:
    """Entries whose latest `<entry>-failures-checks` evaluation passed every check, every case."""
    latest: dict[str, CallRecord] = {}
    for record in query_calls(client, roots_only=True, limit=300):
        if record.op == "Evaluation.evaluate":
            latest.setdefault(object_name(record.inputs.get("self")), record)
    return {
        entry
        for entry in entries
        if (record := latest.get(f"{entry}-failures-checks")) and all_passed(record.output)
    }


def all_passed(summary: Any) -> bool:
    fractions = [
        scores["pass"]["true_fraction"]
        for scores in (summary or {}).values()
        if isinstance(scores, dict) and isinstance(scores.get("pass"), dict)
    ]
    return bool(fractions) and all(fraction == 1.0 for fraction in fractions)


def failure_rows(runs: list[Run], function: str, parameter: str) -> list[dict]:
    """Test-case inputs from failing runs of `function`, or of agent turns when none match."""
    matching = [run for run in runs if run.entry == function] or [r for r in runs if r.prompt]
    rows, seen = [], set()
    for run in matching:
        value = run.inputs.get(parameter, run.prompt)
        if isinstance(value, str) and value not in seen:
            seen.add(value)
            rows.append({parameter: value})
    return rows
