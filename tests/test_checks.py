from datetime import UTC, datetime, timedelta

from weaveconnect.checks import duplicate_requests, find_leaks, judge_records, request_fingerprint
from weaveconnect.records import CallRecord
from weaveconnect.report import Report, Status

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
MESSAGES = [{"role": "user", "content": "Where is my order 1042?"}]


def call(op, start, end=1.0, parent=None, trace="t1", ident=None, **inputs):
    return CallRecord(
        id=ident or f"{op}-{start}",
        trace_id=trace,
        parent_id=parent,
        op=op,
        started_at=T0 + timedelta(seconds=start),
        ended_at=None if end is None else T0 + timedelta(seconds=start + end),
        inputs=inputs,
    )


def request(start, end=1.0, parent="root", **extra):
    extra.setdefault("self", "<client>")
    return call(
        "openai.chat.completions.create",
        start,
        end,
        parent,
        model="gpt-oss",
        messages=MESSAGES,
        **extra,
    )


def statuses(records, secrets=(), expect=()):
    report = Report("test")
    judge_records(report, records, list(secrets), tuple(expect))
    return {check.name: check.status for check in report.checks}, report


def test_nothing_arrived_fails_loudly():
    result, report = statuses([])
    assert result == {"Traces arrived": Status.FAIL}
    assert report.status is Status.FAIL


def test_a_healthy_run_passes_every_check():
    records = [call("answer", 0, 5, ident="root", question="q"), request(1), request(3)]
    result, report = statuses(records)
    assert set(result.values()) == {Status.PASS}
    assert report.exit_code == 0


def test_no_model_request_is_a_failure():
    result, _ = statuses([call("answer", 0, question="q")])
    assert result["Model calls recorded"] is Status.FAIL


def test_hand_wrapper_plus_automatic_tracing_is_a_duplicate():
    wrapper = call(
        "my_model_call", 1, 2, parent="root", ident="w", model="gpt-oss", messages=MESSAGES
    )
    child = request(1.1, 1.5, parent="w")
    records = [call("answer", 0, 5, ident="root"), wrapper, child]
    result, _ = statuses(records)
    assert result["Each model call recorded once"] is Status.FAIL
    assert len(duplicate_requests(records)) == 1


def test_a_retry_after_the_first_request_ended_is_not_a_duplicate():
    records = [call("answer", 0, 9, ident="root"), request(1, 1), request(3, 1)]
    assert duplicate_requests(records) == []


def test_fingerprint_ignores_client_objects_and_timeouts():
    first = request(1, self="client A", timeout=30)
    second = request(1, self="client B", timeout=None)
    assert request_fingerprint(first) == request_fingerprint(second)
    assert request_fingerprint(call("answer", 0, question="q")) is None


def test_an_unfinished_call_fails():
    result, _ = statuses([call("answer", 0, None, ident="root"), request(1)])
    assert result["Every call finished"] is Status.FAIL


def test_a_secret_from_settings_is_found_but_never_repeated():
    secret = "demo-not-a-real-secret-0042"
    records = [
        call("answer", 0, 5, ident="root"),
        request(1),
        call("open_session", 2, parent="root", config={"nested": [f"token={secret}"]}),
    ]
    result, report = statuses(records, secrets=[secret])
    assert result["No secrets in traces"] is Status.FAIL
    assert secret not in report.render()
    assert "inputs.config.nested[0]" in report.render()


def test_common_key_formats_are_found_without_settings():
    leaky = call("answer", 0, ident="root", note="sk-" + "a" * 30)
    assert find_leaks([leaky], [])[0][2] == "an OpenAI-style key"


def test_an_expected_step_must_appear():
    records = [call("answer", 0, 5, ident="root"), request(1)]
    result, _ = statuses(records, expect=["lookup_order"])
    assert result["Step `lookup_order` recorded"] is Status.FAIL
