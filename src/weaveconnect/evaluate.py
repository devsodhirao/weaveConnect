"""Turn test cases or recent traces into a Weave evaluation, scored by code checks and a judge."""

import asyncio
import importlib.util
import inspect
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

import weave
from openai import OpenAI

from weaveconnect import findings, settings
from weaveconnect.records import object_name, query_calls
from weaveconnect.report import Report, Status

INFERENCE_URL = "https://api.inference.wandb.ai/v1"
DEFAULT_JUDGE = "deepseek-ai/DeepSeek-V4.1-Flash"
# The judge cannot see the agent's tools or data, so it must not grade facts: an earlier
# version failed 6/6 correct answers as "facts it could not have known". Code checks own facts.
JUDGE_PROMPT = """You review one reply from an AI agent. You cannot see the agent's tools or
data, so do not judge whether its facts are true; other checks do that.

Question: {question}
Reply: {answer}

Pass the reply if it answers the question directly, in plain language, without dodging the
question or asking for details the question already gave.
Reply with JSON only: {{"pass": true or false, "reason": "one short sentence"}}"""


def normalize(text: Any) -> str:
    """Lowercase, plain spaces and straight quotes, so formatting never decides a check."""
    text = unicodedata.normalize("NFKC", str(text))
    text = text.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'}))
    return re.sub(r"\s+", " ", text).strip().lower()


@weave.op
def answered(output: Any) -> dict:
    """Code check: the agent returned a non-empty answer."""
    return {"pass": isinstance(output, str) and bool(output.strip())}


@weave.op
def no_raw_tool_call(output: Any) -> dict:
    """Code check: the answer is not a tool call written out as text."""
    return {"pass": not findings.tool_call_as_text(output)}


@weave.op
def mentions_expected(expected: Any, output: Any) -> dict:
    """Code check: the answer contains at least one of the expected phrases."""
    phrases = [expected] if isinstance(expected, str) else list(expected)
    text = normalize(output)
    return {"pass": any(normalize(phrase) in text for phrase in phrases)}


class Judge(weave.Scorer):
    """A model from a different family than the agent decides whether the answer is acceptable."""

    model: str = DEFAULT_JUDGE

    @weave.op
    def score(self, question: str, output: Any) -> dict:
        client = OpenAI(
            base_url=INFERENCE_URL,
            api_key=os.environ[settings.KEY_NAME],
            project=os.environ.get(settings.PROJECT_NAME),
        )
        prompt = JUDGE_PROMPT.format(question=question, answer=output)
        try:
            response = client.chat.completions.create(
                model=self.model, messages=[{"role": "user", "content": prompt}], max_tokens=800
            )
            reply = response.choices[0].message.content or ""
        except Exception as error:
            return {"pass": False, "checked": False, "reason": f"{type(error).__name__}"}
        verdict = parse_verdict(reply)
        if verdict is None:
            snippet = " ".join(reply.split())[:120] or "an empty reply"
            return {"pass": False, "checked": False, "reason": f"No verdict in: {snippet}"}
        return {"pass": verdict[0], "checked": True, "reason": verdict[1]}


def parse_verdict(text: str) -> tuple[bool, str] | None:
    """Read `{"pass": bool, "reason": str}` from a reply that may wrap it in other text."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pass"), bool):
        return None
    return data["pass"], str(data.get("reason", ""))


def run_eval(
    repo: Path,
    target: str,
    cases: Path | None = None,
    from_recent: int | None = None,
    judge_model: str = DEFAULT_JUDGE,
    project: str | None = None,
    from_failures: bool = False,
    min_pass: float | None = None,
) -> Report:
    repo = repo.resolve()
    report = Report(f"Evaluating {target}")
    saved = settings.read_env(repo / ".env")
    project = project or os.environ.get(settings.PROJECT_NAME) or saved.get(settings.PROJECT_NAME)
    if not project or not settings.resolve_key(saved):
        report.add("Setup", Status.UNCHECKED, "Run `weaveconnect connect` first.")
        return report
    for name, value in saved.items():
        os.environ.setdefault(name, value)

    try:
        function = load_target(repo, target)
    except Exception as error:
        report.add(
            "Target", Status.FAIL, f"Could not load {target}: {type(error).__name__}: {error}"
        )
        return report
    name = getattr(function, "name", None) or function.__name__
    client = weave.init(project, settings={"print_call_link": False})

    if from_failures:
        parameter = _first_parameter(function)
        failed = findings.failing(findings.find(findings.load_runs(client, window=200)))
        rows = findings.failure_rows(failed, name, parameter) if parameter else []
        if not rows:
            report.add(
                "Test cases",
                Status.UNCHECKED,
                f"No failing runs of `{name}` were found in recent traces. Use --cases or "
                "--from-recent instead.",
            )
            return report
    else:
        rows = read_cases(cases) if cases else recent_inputs(client, name, from_recent or 5)
    question = _question_field(function, rows)
    if not rows or question is None:
        missing = "No cases found." if not rows else "Cases do not match the function's arguments."
        report.add("Test cases", Status.FAIL, missing)
        return report
    label = f"{name}-failures" if from_failures else f"{name}-cases"
    dataset = weave.Dataset(name=label, rows=rows)
    weave.publish(dataset)
    source = "failing runs" if from_failures else "cases"
    report.add("Test cases", Status.PASS, f"{len(rows)} {source} saved as dataset `{label}`.")

    scorers: list[Any] = [answered, no_raw_tool_call]
    if all("expected" in row for row in rows):
        scorers.append(mentions_expected)
    column_map = {} if question == "question" else {"question": question}
    scorers.append(Judge(model=judge_model, column_map=column_map))
    evaluation_name = f"{label.removesuffix('-cases')}-checks"
    evaluation = weave.Evaluation(name=evaluation_name, dataset=dataset, scorers=scorers)
    model = function if callable(getattr(function, "call", None)) else weave.op(function)
    summary = asyncio.run(evaluation.evaluate(model))
    client.flush()

    total = len(rows)
    passes = {s: _count(summary[s], "pass") for s in SCORER_NAMES if s in summary}
    for scorer, passed in passes.items():
        line = f"{scorer}: {passed}/{total} passed"
        if scorer == "Judge":
            line += f" ({_count(summary[scorer], 'checked')}/{total} judged by {judge_model})"
        report.notes.append(line)
    report.notes += compare_with_previous(client, evaluation_name, name, total)
    url = _latest_evaluation_url(client)
    report.add("Evaluation ran", Status.PASS, f"Scores recorded in Weave: {url or project}")
    if min_pass is not None:
        below = [f"{s} {p}/{total}" for s, p in passes.items() if p < min_pass * total]
        report.add(
            "Pass threshold",
            Status.FAIL if below else Status.PASS,
            f"Below {min_pass:.0%}: {', '.join(below)}."
            if below
            else f"Every check passed at least {min_pass:.0%} of cases.",
        )
    return report


SCORER_NAMES = ("answered", "no_raw_tool_call", "mentions_expected", "Judge")


def compare_with_previous(client: Any, evaluation: str, function: str, total: int) -> list[str]:
    """Case-by-case changes since the previous run of the same evaluation."""
    runs = [
        record
        for record in query_calls(client, roots_only=True, limit=300)
        if record.op == "Evaluation.evaluate"
        and object_name(record.inputs.get("self")) == evaluation
    ]
    if len(runs) < 2:
        return ["First run of this evaluation; the next run will be compared with it."]
    after = case_scores(client, runs[0], expected=total)
    before = case_scores(client, runs[1])
    changes = compare_cases(before, after)
    if not changes:
        return ["No cases in common with the previous run, so nothing to compare."]
    shared = next(iter(changes.values()))["shared"]
    lines = [
        f"Compared with the previous run ({_age(runs[1], runs[0])}), on {shared} shared cases:"
    ]
    broke = []
    for scorer, change in changes.items():
        lines.append(
            f"  {scorer}: {change['before']}/{shared} -> {change['after']}/{shared} "
            f"({len(change['fixed'])} fixed, {len(change['broke'])} broke)"
        )
        broke += [row for row in change["broke"] if row not in broke]
    for row in broke[:3]:
        lines.append(f"  Broke: {_case_text(after[row]['example'])}")
    return lines


def case_scores(client: Any, evaluate_call: Any, expected: int | None = None) -> dict[str, dict]:
    """Row id -> pass/fail per scorer, read from the evaluation's per-case calls."""
    for _ in range(10):
        cases = {}
        for record in query_calls(client, parent_ids=[evaluate_call.id], limit=2000):
            if record.op != "Evaluation.predict_and_score":
                continue
            row = str(record.inputs.get("example") or "").rsplit("/", 1)[-1]
            scores = (record.output or {}).get("scores") or {}
            cases[row] = {
                "id": record.id,
                "example": str(record.inputs.get("example") or ""),
                "scores": {
                    scorer: value.get("pass")
                    for scorer, value in scores.items()
                    if isinstance(value, dict) and isinstance(value.get("pass"), bool)
                },
            }
        if expected is None or len(cases) >= expected:
            return cases
        time.sleep(2)
    return cases


def compare_cases(before: dict[str, dict], after: dict[str, dict]) -> dict[str, dict]:
    """Per scorer, over the cases both runs share: passes before and after, fixed and broke."""
    shared = sorted(before.keys() & after.keys())
    if not shared:
        return {}
    scorers = sorted({scorer for row in shared for scorer in after[row]["scores"]})
    result = {}
    for scorer in scorers:
        old = {row: before[row]["scores"].get(scorer) for row in shared}
        new = {row: after[row]["scores"].get(scorer) for row in shared}
        result[scorer] = {
            "shared": len(shared),
            "before": sum(value is True for value in old.values()),
            "after": sum(value is True for value in new.values()),
            "fixed": [row for row in shared if old[row] is False and new[row] is True],
            "broke": [row for row in shared if old[row] is True and new[row] is False],
        }
    return result


def _case_text(example_ref: str) -> str:
    """The case's first text field, read from its dataset row reference (data, never code)."""
    try:
        row = weave.ref(example_ref).get()
        values = row.values() if hasattr(row, "values") else []
        return next((str(v)[:100] for v in values if isinstance(v, str)), example_ref[-24:])
    except Exception:
        return f"case {example_ref.rsplit('/', 1)[-1][:12]}"


def _age(earlier: Any, later: Any) -> str:
    minutes = (later.started_at - earlier.started_at).total_seconds() / 60
    if minutes < 90:
        return f"{minutes:.0f} minutes earlier"
    if minutes < 48 * 60:
        return f"{minutes / 60:.0f} hours earlier"
    return f"{minutes / 1440:.0f} days earlier"


def load_target(repo: Path, target: str) -> Any:
    """Import `path/to/file.py:function` from the developer's repository."""
    path_text, _, function_name = target.rpartition(":")
    if not path_text or not function_name:
        raise ValueError("use the form path/to/file.py:function")
    path = (repo / path_text).resolve()
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return getattr(module, function_name)


def read_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def recent_inputs(client: Any, op_name: str, limit: int) -> list[dict]:
    """Inputs of the most recent traced calls of `op_name`, without repeats."""
    rows, seen = [], set()
    for record in query_calls(client, roots_only=True, limit=500):
        if record.op != op_name:
            continue
        row = {key: value for key, value in record.inputs.items() if key != "self"}
        marker = json.dumps(row, sort_keys=True, default=str)
        if row and marker not in seen:
            seen.add(marker)
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _first_parameter(function: Any) -> str | None:
    target = inspect.unwrap(getattr(function, "resolve_fn", function))
    parameters = list(inspect.signature(target).parameters)
    return parameters[0] if parameters else None


def _question_field(function: Any, rows: list[dict]) -> str | None:
    """The argument the judge reads as the question: the function's first parameter."""
    parameter = _first_parameter(function)
    if parameter is None or not rows or any(parameter not in row for row in rows):
        return None
    return parameter


def _count(scores: Any, field: str) -> int:
    value = scores.get(field) if isinstance(scores, dict) else None
    return int(value.get("true_count", 0)) if isinstance(value, dict) else 0


def _latest_evaluation_url(client: Any) -> str | None:
    for record in query_calls(client, roots_only=True, limit=20):
        if record.op == "Evaluation.evaluate":
            return record.url
    return None
