"""Turn test cases or recent traces into a Weave evaluation, scored by code checks and a judge."""

import asyncio
import importlib.util
import inspect
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

import weave
from openai import OpenAI

from weaveconnect import settings
from weaveconnect.records import query_calls
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

    rows = read_cases(cases) if cases else recent_inputs(client, name, from_recent or 5)
    question = _question_field(function, rows)
    if not rows or question is None:
        missing = "No cases found." if not rows else "Cases do not match the function's arguments."
        report.add("Test cases", Status.FAIL, missing)
        return report
    dataset = weave.Dataset(name=f"{name}-cases", rows=rows)
    weave.publish(dataset)
    report.add("Test cases", Status.PASS, f"{len(rows)} saved as dataset `{name}-cases`.")

    scorers: list[Any] = [answered]
    if all("expected" in row for row in rows):
        scorers.append(mentions_expected)
    column_map = {} if question == "question" else {"question": question}
    scorers.append(Judge(model=judge_model, column_map=column_map))
    evaluation = weave.Evaluation(name=f"{name}-checks", dataset=dataset, scorers=scorers)
    model = function if callable(getattr(function, "call", None)) else weave.op(function)
    summary = asyncio.run(evaluation.evaluate(model))

    total = len(rows)
    for scorer in ("answered", "mentions_expected", "Judge"):
        if scorer in summary:
            passed = _count(summary[scorer], "pass")
            line = f"{scorer}: {passed}/{total} passed"
            if scorer == "Judge":
                line += f" ({_count(summary[scorer], 'checked')}/{total} judged by {judge_model})"
            report.notes.append(line)
    url = _latest_evaluation_url(client)
    report.add("Evaluation ran", Status.PASS, f"Scores recorded in Weave: {url or project}")
    return report


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


def _question_field(function: Any, rows: list[dict]) -> str | None:
    """The argument the judge reads as the question: the function's first parameter."""
    target = inspect.unwrap(getattr(function, "resolve_fn", function))
    parameters = list(inspect.signature(target).parameters)
    if not parameters or not rows or any(parameters[0] not in row for row in rows):
        return None
    return parameters[0]


def _count(scores: Any, field: str) -> int:
    value = scores.get(field) if isinstance(scores, dict) else None
    return int(value.get("true_count", 0)) if isinstance(value, dict) else 0


def _latest_evaluation_url(client: Any) -> str | None:
    for record in query_calls(client, roots_only=True, limit=20):
        if record.op == "Evaluation.evaluate":
            return record.url
    return None
