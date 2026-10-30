import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from weaveconnect import findings
from weaveconnect.checks import agent_shape_problems, judge_records, recorded_both_ways
from weaveconnect.evaluate import compare_cases, no_raw_tool_call
from weaveconnect.records import CallRecord, SpanRecord
from weaveconnect.report import Report, Status

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
LLAMA_TEXT = (
    '{"type": "function", "function": {"name": "lookup_order", "parameters": {"order_id": "1"}}}'
)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def span(op, ident, parent=None, start=0, end=1, trace="t", **extra):
    return SpanRecord(
        ident, trace, parent, op, f"{op} {ident}", at(start), at(start + end), **extra
    )


def call(op, ident, parent=None, start=0, end=1, trace="t", output=None, exception=None, **inputs):
    return CallRecord(
        ident, trace, parent, op, at(start), at(start + end), inputs, output, exception
    )


def reply(text, tool_calls=None):
    return {"choices": [{"message": {"content": text, "tool_calls": tool_calls}}]}


def test_tool_calls_written_as_text_are_recognised():
    assert findings.tool_call_as_text(LLAMA_TEXT)
    assert findings.tool_call_as_text('```json\n{"name": "x", "arguments": {}}\n```')
    assert findings.tool_call_as_text("<tool_call>lookup_order</tool_call>")
    assert not findings.tool_call_as_text("Your order shipped via UPS.")
    assert not findings.tool_call_as_text('{"status": "shipped"}')
    assert no_raw_tool_call(LLAMA_TEXT)["pass"] is False


def test_a_run_whose_model_writes_tool_calls_as_text_is_a_failure():
    good = [
        call("answer", "a", trace="good", output="Shipped.", question="q1"),
        call(
            "openai.chat.completions.create",
            "b",
            "a",
            trace="good",
            output=reply(None, [{"id": 1}]),
            model="gpt-oss",
            messages=[1],
        ),
        call(
            "openai.chat.completions.create",
            "c",
            "a",
            trace="good",
            output=reply("Shipped."),
            model="gpt-oss",
            messages=[2],
        ),
    ]
    bad = [
        call("answer", "d", trace="bad", output=LLAMA_TEXT, question="q2"),
        call(
            "openai.chat.completions.create",
            "e",
            "d",
            trace="bad",
            output=reply(LLAMA_TEXT),
            model="llama",
            messages=[3],
        ),
    ]
    runs = findings.runs_from_calls(good + bad)
    found = findings.find(runs)
    assert [f.key for f in found] == ["tool_text"]
    assert found[0].summary.startswith("1 of 2 runs") and "(llama)" in found[0].summary
    assert findings.failure_rows(findings.failing(found), "answer", "question") == [
        {"question": "q2"}
    ]


def test_errors_empty_answers_and_loops_are_counted_with_denominators():
    runs = []
    for index in range(4):
        trace = f"t{index}"
        members = [call("answer", f"r{index}", trace=trace, output="ok", question=f"q{index}")]
        calls = 9 if index == 3 else 1
        members += [
            call(
                "openai.chat.completions.create",
                f"m{index}-{n}",
                f"r{index}",
                trace=trace,
                output=reply("ok"),
                model="gpt-oss",
                messages=[index, n],
            )
            for n in range(calls)
        ]
        runs += members
    runs[0] = call("answer", "r0", trace="t0", output="", question="q0")
    runs.append(call("lookup_order", "x", "r1", trace="t1", exception="KeyError: '9'\nTraceback"))
    found = {f.key: f for f in findings.find(findings.runs_from_calls(runs))}
    assert (
        found["errors"].summary
        == "1 of 4 runs recorded an error, most often in `lookup_order`: KeyError: '9'"
    )
    assert found["empty"].summary == "1 of 4 runs returned an empty answer."
    assert found["looping"].summary.startswith("1 of 4 runs made more than 3 model calls")


def test_agent_turns_from_spans_become_runs_and_failures():
    output = [
        {
            "role": "assistant",
            "content": json.dumps([{"type": "text", "content": LLAMA_TEXT}]),
        }
    ]
    spans = [
        span(
            "invoke_agent",
            "root",
            agent="support-agent",
            payload={
                "input_messages": [
                    {"role": "user", "content": '[{"type": "text", "content": "Where is 1?"}]'}
                ]
            },
        ),
        span("chat", "c1", "root", model="llama", payload={"output_messages": output}),
    ]
    runs = findings.runs_from_spans(spans)
    assert runs[0].entry == "support-agent" and runs[0].prompt == "Where is 1?"
    assert findings.find(runs)[0].key == "tool_text"


def test_spans_satisfy_the_checks_and_shape_problems_fail():
    spans = [
        span("invoke_agent", "turn"),
        span("chat", "c", "turn", model="m"),
        span("execute_tool", "tool", "turn", tool="lookup_order"),
    ]
    report = Report("spans")
    judge_records(report, [], [], ("lookup_order",), spans)
    assert report.status is Status.PASS
    orphan = span("chat", "lost", None, trace="other")
    assert agent_shape_problems(spans + [orphan]) == [orphan]


def test_a_model_call_recorded_as_a_span_and_a_call_is_flagged():
    chat = span("chat", "c", "turn", start=1, end=2, model="gpt-oss")
    patched = call(
        "openai.chat.completions.create", "x", start=1.1, end=1.5, model="gpt-oss", messages=[1]
    )
    assert recorded_both_ways([patched], [chat]) == [chat]
    later = call(
        "openai.chat.completions.create", "y", start=9, end=1, model="gpt-oss", messages=[1]
    )
    assert recorded_both_ways([later], [chat]) == []


def test_secrets_in_span_payloads_are_found():
    report = Report("leak")
    leaky = span(
        "execute_tool", "t", "turn", payload={"tool_call_arguments": "token=abcdefgh12345"}
    )
    judge_records(report, [], ["abcdefgh12345"], (), [span("invoke_agent", "turn"), leaky])
    assert "abcdefgh12345" not in report.render()
    assert {c.name: c.status for c in report.checks}["No secrets in traces"] is Status.FAIL


def test_case_comparison_counts_fixed_and_broken_cases():
    before = {
        "a": {"scores": {"no_raw_tool_call": False}},
        "b": {"scores": {"no_raw_tool_call": True}},
        "c": {"scores": {"no_raw_tool_call": False}},
    }
    after = {
        "a": {"scores": {"no_raw_tool_call": True}},
        "b": {"scores": {"no_raw_tool_call": False}},
        "c": {"scores": {"no_raw_tool_call": False}},
        "new": {"scores": {"no_raw_tool_call": True}},
    }
    change = compare_cases(before, after)["no_raw_tool_call"]
    assert (change["shared"], change["before"], change["after"]) == (3, 1, 1)
    assert change["fixed"] == ["a"] and change["broke"] == ["b"]


def test_next_step_names_the_file_that_defines_the_failing_function(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "bot.py").write_text("def answer(question):\n    return question\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "copy.py").write_text("def answer(question):\n    pass\n")
    run = findings.Run("t", "answer", T0, 1.0, errors=[("answer", "boom")])
    found = findings.find([run])
    step = findings.next_step(found, tmp_path)
    assert "--target pkg/bot.py:answer --from-failures" in step
