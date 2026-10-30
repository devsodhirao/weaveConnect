"""Read a Weave project and say which stage it has reached and what to do next.

The stage names and order are a draft and are kept in one list so they are easy to replace.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Any

from weaveconnect.checks import SETUP_OP
from weaveconnect.records import object_name, query_calls

EVALUATE_OP = "Evaluation.evaluate"
HUMAN_FEEDBACK = ("wandb.reaction", "wandb.note", "wandb.annotation")


@dataclass(frozen=True)
class Stage:
    key: str
    name: str
    why: str
    next_step: str


STAGES = [
    Stage(
        "see",
        "See it",
        "You can see what your agent did, step by step, with cost and time.",
        "Add tracing (W&B's weave-instrument skill can write it), then prove it with "
        "`weaveconnect check --run '<command>'`.",
    ),
    Stage(
        "catch",
        "Catch it",
        "The problems you have seen become tests you keep.",
        "Save test cases and run them: `weaveconnect eval --target <file>:<function> "
        "--from-recent 5` (or `--cases cases.jsonl`).",
    ),
    Stage(
        "check",
        "Check it",
        "You know whether a change helped, not just whether one run looked fine.",
        "Run an evaluation: `weaveconnect eval --target <file>:<function> --cases cases.jsonl`.",
    ),
    Stage(
        "trust",
        "Trust it",
        "You know how much to believe the score.",
        "Open the evaluation in Weave and mark a few results right or wrong, then compare your "
        "marks with the judge's.",
    ),
    Stage(
        "improve",
        "Improve it",
        "You can show progress instead of claiming it.",
        "Change the prompt or model, run the same `weaveconnect eval` again, and compare the two "
        "runs in Weave.",
    ),
    Stage(
        "watch",
        "Watch it",
        "You hear about problems before your users report them.",
        "Add a monitor in Weave (Monitors, then New) that scores live answers with the same judge.",
    ),
]


@dataclass(frozen=True)
class Progress:
    stage: Stage
    reached: bool
    evidence: str


def read_progress(client: Any) -> list[Progress]:
    """Look only at what exists in the project; nothing here writes to Weave."""
    roots = list(query_calls(client, roots_only=True))
    own = [r for r in roots if r.op not in (SETUP_OP, EVALUATE_OP)]
    runs = [r for r in roots if r.op == EVALUATE_OP]
    datasets = _object_names(client, "Dataset")
    monitors = _object_names(client, "Monitor")
    feedback = [f for f in client.get_feedback(limit=200) if _is_human(f)]
    per_evaluation = Counter(object_name(r.inputs.get("self")) for r in runs)
    repeated = [name for name, count in per_evaluation.items() if count >= 2]
    found = {
        "see": (bool(own), f"{len(own)} trace(s) from your code" if own else "no traces yet"),
        "catch": (bool(datasets), _listing("dataset", datasets)),
        "check": (bool(runs), f"{len(runs)} evaluation run(s)" if runs else "no evaluation yet"),
        "trust": (bool(feedback), f"{len(feedback)} human judgment(s)" if feedback else "none yet"),
        "improve": (
            bool(repeated),
            f"{repeated[0]} ran {per_evaluation[repeated[0]]} times"
            if repeated
            else "an evaluation needs a second run to compare",
        ),
        "watch": (bool(monitors), _listing("monitor", monitors)),
    }
    return [Progress(stage, *found[stage.key]) for stage in STAGES]


def render(project: str, progress: list[Progress]) -> str:
    width = max(len(stage.name) for stage in STAGES)
    lines = [f"Progress for {project}", ""]
    lines += [
        f"  [{'x' if p.reached else ' '}] {p.stage.name:<{width}}  {p.evidence}" for p in progress
    ]
    upcoming = next((p.stage for p in progress if not p.reached), None)
    if upcoming is None:
        lines += ["", "Every stage has evidence in Weave."]
    else:
        lines += ["", f"Next: {upcoming.name}. {upcoming.why}", f"  {upcoming.next_step}"]
    return "\n".join(lines)


def _object_names(client: Any, base_class: str) -> list[str]:
    objects = client._objects(filter={"base_object_classes": [base_class], "latest_only": True})
    return sorted({str(getattr(obj, "object_id", obj)) for obj in objects})


def _is_human(feedback: Any) -> bool:
    kind = str(getattr(feedback, "feedback_type", "") or "")
    return kind.startswith(HUMAN_FEEDBACK)


def _listing(noun: str, names: list[str]) -> str:
    if not names:
        return f"no {noun} yet"
    return f"{len(names)} {noun}(s): {', '.join(names[:3])}"
