"""weaveconnect: connect a project to Weave, prove each step worked, and show what is next."""

import argparse
import os
import sys
from pathlib import Path

from weaveconnect.report import EXIT_CODES, Status

EVAL_DEFAULT_JUDGE = "deepseek-ai/DeepSeek-V4.1-Flash"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="weaveconnect", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    connect = commands.add_parser(
        "connect", help="Save settings, create the project, prove a trace arrives."
    )
    check = commands.add_parser("check", help="Run your agent and check what reached Weave.")
    progress = commands.add_parser("next", help="Show the stage reached and the next step.")
    evaluate = commands.add_parser("eval", help="Run an evaluation from cases or recent traces.")
    for command in (connect, check, progress, evaluate):
        command.add_argument("--dir", default=".", help="Project folder (default: here).")
        command.add_argument("--project", help="Weave project, as team/name or name.")
    connect.add_argument("--no-prompt", action="store_true", help="Never ask for the key.")
    check.add_argument(
        "--run", required=True, metavar="COMMAND", help="The command that runs your agent."
    )
    check.add_argument("--expect-op", action="append", default=[], help="A step that must appear.")
    check.add_argument("--timeout", type=int, default=300, help="Seconds to let the command run.")
    evaluate.add_argument("--target", required=True, metavar="FILE:FUNCTION")
    source = evaluate.add_mutually_exclusive_group(required=True)
    source.add_argument("--cases", type=Path, help="JSON lines, one test case per line.")
    source.add_argument(
        "--from-recent", type=int, metavar="N", help="Use the last N traced inputs."
    )
    evaluate.add_argument("--judge-model", default=EVAL_DEFAULT_JUDGE)

    args = parser.parse_args(argv)
    repo = Path(args.dir)
    if args.command == "connect":
        from weaveconnect.connect import connect as run

        report = run(repo, args.project, interactive=False if args.no_prompt else None)
    elif args.command == "check":
        from weaveconnect.checks import check_run

        report = check_run(args.run, repo, args.project, tuple(args.expect_op), args.timeout)
    elif args.command == "eval":
        from weaveconnect.evaluate import run_eval

        cases = (repo / args.cases) if args.cases else None
        report = run_eval(
            repo, args.target, cases, args.from_recent, args.judge_model, args.project
        )
    else:
        return _next(repo, args.project)
    print(report.render())
    return report.exit_code


def _next(repo: Path, project: str | None) -> int:
    from weaveconnect import settings

    saved = settings.read_env(repo.resolve() / ".env")
    project = project or os.environ.get(settings.PROJECT_NAME) or saved.get(settings.PROJECT_NAME)
    key = settings.resolve_key(saved)
    if not project or not key:
        print("Run `weaveconnect connect` first.")
        return EXIT_CODES[Status.UNCHECKED]
    os.environ[settings.KEY_NAME] = key
    import weave

    from weaveconnect.stages import read_progress, render

    try:
        client = weave.init(
            project, settings={"print_call_link": False, "implicitly_patch_integrations": False}
        )
        print(render(f"{client.entity}/{client.project}", read_progress(client)))
    except Exception as error:
        print(f"Could not read {project}: {type(error).__name__}")
        return EXIT_CODES[Status.UNCHECKED]
    return 0


if __name__ == "__main__":
    sys.exit(main())
