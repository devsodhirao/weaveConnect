from datetime import UTC, datetime
from types import SimpleNamespace

from weaveconnect import cli
from weaveconnect.connect import connect
from weaveconnect.evaluate import (
    _question_field,
    load_target,
    mentions_expected,
    normalize,
    parse_verdict,
)
from weaveconnect.report import Status
from weaveconnect.stages import read_progress, render

EVAL_REF = "weave:///team/proj/object/answer-checks:abc"


class FakeServer:
    def __init__(self, rows):
        self.rows = rows

    def calls_query_stream(self, request):
        roots_only = request["filter"].get("trace_roots_only")
        return [row for row in self.rows if not roots_only or row["parent_id"] is None]


class FakeClient:
    entity, project, project_id = "team", "proj", "team/proj"

    def __init__(self, rows, datasets=(), monitors=(), feedback=()):
        self.server = FakeServer(rows)
        self.objects = {"Dataset": datasets, "Monitor": monitors}
        self.feedback = feedback

    def _objects(self, filter):
        names = self.objects[filter["base_object_classes"][0]]
        return [SimpleNamespace(object_id=name) for name in names]

    def get_feedback(self, limit):
        return [SimpleNamespace(feedback_type=kind) for kind in self.feedback]


def row(op, ident, parent=None, **inputs):
    return {
        "id": ident,
        "trace_id": ident,
        "parent_id": parent,
        "op_name": f"weave:///team/proj/op/{op}:digest",
        "started_at": datetime(2026, 10, 30, tzinfo=UTC),
        "ended_at": datetime(2026, 10, 30, tzinfo=UTC),
        "inputs": inputs,
        "output": None,
        "exception": None,
    }


def test_progress_starts_at_see_it_for_an_empty_project():
    progress = read_progress(FakeClient([]))
    assert [p.reached for p in progress] == [False] * 6
    assert "Next: See it" in render("team/proj", progress)


def test_progress_reads_each_stage_from_what_exists():
    rows = [
        row("answer", "a"),
        row("weaveconnect_setup_check", "s"),
        row("Evaluation.evaluate", "e1", self=EVAL_REF),
        row("Evaluation.evaluate", "e2", self=EVAL_REF),
    ]
    client = FakeClient(rows, datasets=["answer-cases"], feedback=["wandb.runnable.Judge"])
    reached = {p.stage.key: p for p in read_progress(client)}
    assert reached["see"].evidence == "1 recent run(s) of your agent"
    assert reached["catch"].reached and reached["check"].reached
    assert reached["improve"].evidence == "answer-checks ran 2 times"
    # A scorer's feedback is not a human judgment.
    assert not reached["trust"].reached
    assert "Next: Trust it" in render("team/proj", list(reached.values()))


def test_text_checks_ignore_spacing_quotes_and_case():
    assert normalize("Within 30 days — can’t") == "within 30 days — can't"
    assert mentions_expected(["30 days"], "Returns within 30 days.")["pass"] is True
    assert mentions_expected("no order", "I couldn't find it.")["pass"] is False


def test_verdicts_are_read_from_wrapped_replies_and_rejected_when_unclear():
    assert parse_verdict('```json\n{"pass": true, "reason": "Direct."}\n```') == (True, "Direct.")
    assert parse_verdict('{"pass": "yes"}') is None
    assert parse_verdict("I think it passes.") is None


def test_targets_load_from_a_file_and_name_their_question(tmp_path):
    (tmp_path / "bot.py").write_text("def reply(prompt, tone='plain'):\n    return prompt\n")
    function = load_target(tmp_path, "bot.py:reply")
    assert function("hi") == "hi"
    assert _question_field(function, [{"prompt": "a"}]) == "prompt"
    assert _question_field(function, [{"question": "a"}]) is None


def test_connect_without_a_key_refuses_to_guess(tmp_path, monkeypatch):
    monkeypatch.delenv("WANDB_API_KEY", raising=False)
    report = connect(tmp_path, interactive=False)
    assert report.status is Status.FAIL
    assert "Never paste a key" in report.render()
    assert not (tmp_path / ".env").exists()


def test_next_without_setup_says_what_to_do(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("WANDB_API_KEY", raising=False)
    monkeypatch.delenv("WEAVE_PROJECT", raising=False)
    assert cli.main(["next", "--dir", str(tmp_path)]) == 2
    assert "weaveconnect connect" in capsys.readouterr().out
