"""Compare a coding agent on Weave tasks with and without the weaveConnect kit.

Each run copies the sample agent into a fresh folder, sets up the task's starting state, lets
Claude Code work on it headless and isolated, then scores the result with `weaveconnect check`.
Only the kit arm gets the skill and the `weaveconnect` command; the prompts are identical.

    uv run python scripts/compare.py --dry-run                 # prepare and score controls, $0
    uv run python scripts/compare.py --runs 3 --budget 1.00    # paid: Claude Code API usage
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from weaveconnect import settings
from weaveconnect.checks import check_run

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "examples" / "support_agent" / "agent.py"
SKILL = ROOT / "skills" / "weaveconnect"
VENV_BIN = ROOT / ".venv" / "bin"
FAKE_SECRET = "demo-not-a-real-secret-0042"
QUESTION = "Where is my order 1042?"


@dataclass(frozen=True)
class Task:
    key: str
    prompt: str
    start: str


TASKS = [
    Task(
        "add-tracing",
        "Add W&B Weave tracing to agent.py so each run records the agent, its tools and its "
        "model calls in the Weave project named by the WEAVE_PROJECT setting. Keep the change small.",
        "plain",
    ),
    Task(
        "nothing-arrives",
        "I added Weave to agent.py, but nothing shows up in my Weave project when I run it. Fix it.",
        "no_init",
    ),
    Task(
        "counted-twice",
        "Weave shows every model call from agent.py twice. Fix it so each one appears once.",
        "double",
    ),
    Task(
        "secret-in-trace",
        "Make sure no secrets from the environment end up in agent.py's Weave traces, without "
        "breaking the agent.",
        "leaky",
    ),
]


def starting_state(kind: str) -> str:
    """agent.py as the task begins: untouched, or with one realistic tracing mistake."""
    source = AGENT.read_text()
    if kind == "plain":
        return source
    source = source.replace(
        "from openai import OpenAI\n", "import weave\nfrom openai import OpenAI\n", 1
    )
    for name in ("lookup_order", "return_policy", "answer"):
        source = source.replace(f"\ndef {name}(", f"\n@weave.op\ndef {name}(", 1)
    init = '    load_dotenv()\n    weave.init(os.environ["WEAVE_PROJECT"])\n'
    if kind == "no_init":
        return source
    source = source.replace("    load_dotenv()\n", init, 1)
    if kind == "double":
        wrapper = '    create = weave.op(client.chat.completions.create, name="model_call")\n'
        source = source.replace("    messages = [{", wrapper + "    messages = [{", 1)
        return source.replace("client.chat.completions.create(model=", "create(model=", 1)
    if kind == "leaky":
        helper = (
            "\n\n@weave.op\ndef open_session(credentials: str) -> str:\n"
            '    return "session opened"\n'
        )
        source = source.replace("\n\ndef run_tool(", helper + "\n\ndef run_tool(", 1)
        return source.replace(
            "    messages = [{",
            '    open_session(os.environ.get("DEMO_SECRET_TOKEN", ""))\n    messages = [{',
            1,
        )
    raise ValueError(kind)


def prepare(task: Task, kit: bool, workspace: Path) -> Path:
    repo = workspace / f"{task.key}-{'kit' if kit else 'bare'}"
    repo.mkdir(parents=True)
    (repo / "agent.py").write_text(starting_state(task.start))
    shutil.copy(AGENT.parent / "cases.jsonl", repo / "cases.jsonl")
    (repo / ".gitignore").write_text(".env\n")
    settings.write_env(repo / ".env", {settings.PROJECT_NAME: os.environ[settings.PROJECT_NAME]})
    bin_dir = repo / ".bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(VENV_BIN / "python")
    if kit:
        shutil.copytree(SKILL, repo / ".claude" / "skills" / "weaveconnect")
        (bin_dir / "weaveconnect").symlink_to(VENV_BIN / "weaveconnect")
    for command in (["git", "init", "-q"], ["git", "add", "-A"]):
        subprocess.run(command, cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=compare",
            "-c",
            "user.email=compare@example.com",
            "commit",
            "-qm",
            "start",
        ],
        cwd=repo,
        check=True,
    )
    return repo


def run_harness(repo: Path, task: Task, budget: float, model: str) -> dict:
    env = {**os.environ, "PATH": f"{repo / '.bin'}:/usr/bin:/bin", "DEMO_SECRET_TOKEN": FAKE_SECRET}
    command = [
        "claude",
        "-p",
        task.prompt,
        "--bare",
        "--strict-mcp-config",
        "--setting-sources",
        "project",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        # Commands are limited to running the agent, the kit and read-only Git.
        "Bash(python *),Bash(weaveconnect *),Bash(ls *),Bash(cat *),Bash(git diff*),"
        "Bash(git status*),Read,Edit,Write,Glob,Grep,Skill",
        "--max-budget-usd",
        f"{budget:.2f}",
        "--model",
        model,
        "--output-format",
        "json",
        "--no-session-persistence",
    ]
    started = time.monotonic()
    completed = subprocess.run(
        command, cwd=repo, env=env, capture_output=True, text=True, timeout=900
    )
    seconds = round(time.monotonic() - started, 1)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        result = {"is_error": True, "result": completed.stdout[-500:] + completed.stderr[-500:]}
    return {
        "seconds": seconds,
        "cost_usd": result.get("total_cost_usd"),
        "turns": result.get("num_turns"),
        "harness_error": bool(result.get("is_error")),
        "used_weaveconnect": "weaveconnect" in json.dumps(result),
    }


def score(repo: Path) -> dict:
    os.environ["DEMO_SECRET_TOKEN"] = FAKE_SECRET
    report = check_run(
        f"{VENV_BIN / 'python'} agent.py '{QUESTION}'",
        repo,
        expect_ops=("lookup_order",),
        timeout=180,
    )
    return {
        "passed": report.status.value == "pass",
        "checks": {check.name: check.status.value for check in report.checks},
        "diff_lines": len(
            subprocess.run(
                ["git", "diff"], cwd=repo, capture_output=True, text=True
            ).stdout.splitlines()
        ),
    }


@dataclass
class Result:
    task: str
    arm: str
    run: int
    passed: bool
    checks: dict
    seconds: float | None = None
    cost_usd: float | None = None
    turns: int | None = None
    harness_error: bool = False
    used_weaveconnect: bool = False
    diff_lines: int = 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--budget", type=float, default=1.00, help="Dollar cap per agent run.")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--tasks", nargs="*", default=[task.key for task in TASKS])
    parser.add_argument("--dry-run", action="store_true", help="Score the starting states only.")
    args = parser.parse_args()

    saved = settings.read_env(ROOT / ".env")
    for name in (settings.KEY_NAME, settings.PROJECT_NAME):
        os.environ.setdefault(name, saved.get(name, ""))
    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY: isolated Claude Code runs bill the API, not a subscription.")
        return 2

    tasks = [task for task in TASKS if task.key in args.tasks]
    workspace = Path(tempfile.mkdtemp(prefix="weaveconnect-compare-"))
    results = []
    for task in tasks:
        for arm in ("bare", "kit"):
            for run in range(1, args.runs + 1):
                repo = prepare(task, arm == "kit", workspace / f"run{run}")
                harness = {} if args.dry_run else run_harness(repo, task, args.budget, args.model)
                scored = score(repo)
                result = Result(
                    task.key,
                    arm,
                    run,
                    scored["passed"],
                    scored["checks"],
                    diff_lines=scored["diff_lines"],
                    **harness,
                )
                results.append(result)
                print(
                    f"{task.key:16} {arm:4} run {run}: {'PASS' if result.passed else 'FAIL'} "
                    f"cost={result.cost_usd} seconds={result.seconds}",
                    flush=True,
                )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "comparison" / f"{'dry-run' if args.dry_run else 'run'}-{stamp}.json"
    out.parent.mkdir(exist_ok=True)
    secrets = settings.secret_values(dict(os.environ))
    out.write_text(settings.redact(json.dumps([asdict(r) for r in results], indent=2), secrets))
    print(f"\nSaved {out.relative_to(ROOT)}")
    for arm in ("bare", "kit"):
        arm_results = [r for r in results if r.arm == arm]
        passed = sum(r.passed for r in arm_results)
        print(f"{arm}: {passed}/{len(arm_results)} runs passed every check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
