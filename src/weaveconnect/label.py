"""Let a person judge evaluation answers, then report how often the AI judge agreed.

Labels must come from a person, so this only runs in an interactive terminal. Cases where the
judge and the code checks disagree come first, because that is where the judge is most likely
to be wrong.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weaveconnect import settings
from weaveconnect.evaluate import _case_text
from weaveconnect.records import object_name, query_calls
from weaveconnect.report import Report, Status

JUDGE = "Judge"


@dataclass
class Case:
    call_id: str
    question: str
    answer: str
    judge: bool
    reason: str
    checks: dict[str, bool]

    @property
    def disputed(self) -> bool:
        return bool(self.checks) and all(self.checks.values()) != self.judge


def agreement(labels: list[tuple[Case, bool]]) -> str:
    """How often the judge matched the person, and in which direction it missed."""
    agreed = sum(case.judge == human for case, human in labels)
    too_kind = sum(case.judge and not human for case, human in labels)
    too_harsh = sum(human and not case.judge for case, human in labels)
    return (
        f"The judge agreed with you on {agreed} of {len(labels)} cases: it passed {too_kind} "
        f"you failed and failed {too_harsh} you passed."
    )


def run_label(
    repo: Path,
    evaluation: str | None = None,
    limit: int = 10,
    save: bool = True,
    interactive: bool | None = None,
) -> Report:
    report = Report("Checking the judge against your judgment")
    if not (sys.stdin.isatty() if interactive is None else interactive):
        report.add(
            "Labels",
            Status.UNCHECKED,
            "Labels must come from a person. Run `weaveconnect label` in your own terminal "
            "(in Claude Code: `! weaveconnect label`).",
        )
        return report
    saved = settings.read_env(repo.resolve() / ".env")
    project = os.environ.get(settings.PROJECT_NAME) or saved.get(settings.PROJECT_NAME)
    key = settings.resolve_key(saved)
    if not project or not key:
        report.add("Setup", Status.UNCHECKED, "Run `weaveconnect connect` first.")
        return report
    os.environ[settings.KEY_NAME] = key
    import weave

    client = weave.init(
        project, settings={"print_call_link": False, "implicitly_patch_integrations": False}
    )
    run = next(
        (
            r
            for r in query_calls(client, roots_only=True, limit=300)
            if r.op == "Evaluation.evaluate"
            and (evaluation is None or object_name(r.inputs.get("self")) == evaluation)
        ),
        None,
    )
    if run is None:
        report.add(
            "Labels", Status.UNCHECKED, "No evaluation found. Run `weaveconnect eval` first."
        )
        return report
    cases = sorted(load_cases(client, run.id), key=lambda case: not case.disputed)[:limit]
    name = object_name(run.inputs.get("self"))
    print(f"Labelling up to {len(cases)} cases from `{name}`. Disputed cases come first.\n")
    labels = []
    for index, case in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {case.question}\n  Answer: {case.answer[:400]}")
        checks = ", ".join(f"{n} {'pass' if ok else 'fail'}" for n, ok in case.checks.items())
        print(
            f"  Judge: {'pass' if case.judge else 'fail'}: {case.reason}\n  Code checks: {checks}"
        )
        reply = input("  Is this answer acceptable? [y]es, [n]o, [s]kip, [q]uit: ").strip().lower()
        if reply.startswith("q"):
            break
        if reply[:1] in ("y", "n"):
            labels.append((case, reply.startswith("y")))
            if save:
                client.get_call(case.call_id).feedback.add_reaction(
                    "👍" if reply.startswith("y") else "👎"
                )
        print()
    if not labels:
        report.add("Labels", Status.UNCHECKED, "No cases were labelled.")
        return report
    where = "saved as Weave reactions on each case" if save else "not saved (--no-save)"
    report.add("Labels", Status.PASS, f"{len(labels)} case(s) labelled, {where}.")
    report.notes.append(agreement(labels))
    return report


def load_cases(client: Any, evaluate_call_id: str) -> list[Case]:
    cases, seen = [], set()
    for record in query_calls(client, parent_ids=[evaluate_call_id], limit=2000):
        if record.op != "Evaluation.predict_and_score" or not isinstance(record.output, dict):
            continue
        example = str(record.inputs.get("example") or "")
        if example in seen:
            continue  # with repeated trials, label each case once
        seen.add(example)
        scores = record.output.get("scores") or {}
        judge = scores.get(JUDGE) or {}
        if not isinstance(judge.get("pass"), bool) or judge.get("checked") is False:
            continue
        checks = {
            name: value["pass"]
            for name, value in scores.items()
            if name != JUDGE and isinstance(value, dict) and isinstance(value.get("pass"), bool)
        }
        cases.append(
            Case(
                record.id,
                _case_text(str(record.inputs.get("example") or "")),
                str(record.output.get("output") or ""),
                judge["pass"],
                str(judge.get("reason") or ""),
                checks,
            )
        )
    return cases
