# weaveConnect

A command-line kit that a developer, or the coding agent working for them (Claude Code, Codex),
runs against a Weave project. It proves tracing works, reads what the traces show, turns failures
into test cases, and proves whether a fix helped, case by case.

[Coreweaving](https://github.com/devsodhirao/coreweaving) answers questions about the Weave docs.
weaveConnect works on what comes after a developer has their agent traced.

## Why this exists, and how it relates to W&B's own skill

W&B's [`weave-instrument`](https://github.com/wandb/skills/tree/main/skills/weave-instrument)
skill is about getting tracing right. It tells a coding agent how to instrument an agent and how
to verify the result: read traces back, treat empty results as unverified, and run known-bad
controls. Its verification is written as instructions for the agent to carry out.

weaveConnect does two things alongside it:

1. **It turns part of that verification into a command with an exit code.** `check` reads both
   kinds of Weave tracing (Calls from `@weave.op` and automatic integrations, and the agent spans
   W&B's skill recommends for agents). It gives the same verdict every time, and it also scans
   stored traces for secrets.
2. **It works on what happens after tracing works.** `next` reads recent runs and says what they
   show, with denominators. `eval --from-failures` turns failing runs into a test set. A rerun
   after a fix shows which cases were fixed and which broke, and `--min-pass` can fail a pull
   request.

That second part is the loop that improved Coreweaving: its traces exposed real bugs. Here the
loop runs as commands an agent can follow and cannot talk its way around.

## Commands

| Command | What it does |
|---|---|
| `weaveconnect connect` | Asks for the W&B key hidden (never printed or put in an agent chat), keeps `.env` out of Git, creates the Weave project, and proves a test trace arrives. |
| `weaveconnect check --run "<command>"` | Runs your agent, then reads back Calls and agent spans and checks them. Traces arrived; model calls were recorded; expected steps appear; each model call was recorded once, including a call recorded both as a span and as a Call; model and tool spans sit inside an agent turn; every step finished; no secrets are in the traces. |
| `weaveconnect next` | Reads recent runs and reports failures with denominators: errors, empty answers, runs that loop, tool calls written as text, model calls cut off at the token limit, calls to tools that do not exist, and the same tool called repeatedly with the same arguments. Tool error results are shown too, but not counted as failures. It shows where the time goes and gives one next step, chosen from that evidence. |
| `weaveconnect eval --target file.py:function` | Builds the test set with `--cases cases.jsonl`, `--from-recent N` or `--from-failures`, then runs a `weave.Evaluation` with code checks and a judge from a different model family on W&B Inference. It compares each case with the previous run and reports how many were fixed and how many broke. `--trials 3` runs each case three times. A case then counts as fixed only when every trial passes, and as flaky when its trials disagree. `--min-pass 0.8` exits 1 when any check passes fewer than that share. |
| `weaveconnect label` | Shows evaluation cases, disputed ones first, and asks you whether each answer is acceptable. It saves your answer as a Weave reaction and reports how often the judge agreed with you, and in which direction it missed. It refuses to run without a person at the terminal. |

Every check reports **pass**, **fail** or **could not check**, and a report with no checks never
passes. Exit codes: 0 when every check passed, 1 when one failed, 2 when something could not be
checked.

## The loop, on the sample agent

```
uv sync
uv run weaveconnect connect
uv run weaveconnect check --run "uv run python examples/support_agent/controls/traced.py"
AGENT_MODEL=meta-llama/Llama-3.3-70B-Instruct uv run python examples/support_agent/controls/traced.py "Where is my order 1042?"
uv run weaveconnect next
AGENT_MODEL=meta-llama/Llama-3.3-70B-Instruct uv run weaveconnect eval --target examples/support_agent/agent.py:answer --from-failures
uv run weaveconnect eval --target examples/support_agent/agent.py:answer --from-failures --min-pass 1.0
```

`examples/support_agent/agent.py` is a store-support agent with two tools and no Weave code. It
runs on W&B Inference, so the W&B key is the only key it needs. Its default model is
gpt-oss-120b. Llama 3.3 70B often writes its tool calls as text instead of calling the tools,
which is a real failure for `next` to find. `examples/support_agent/controls/` holds traced
versions: correct ones that use Calls and agent spans, and broken ones that must fail.

## Use it from Claude Code or Codex

1. Install the command: `uv tool install git+https://github.com/devsodhirao/weaveConnect`.
2. Give your agent the skill: copy `skills/weaveconnect/` into `.claude/skills/` for Claude Code,
   or point your `AGENTS.md` at `skills/weaveconnect/SKILL.md` for Codex.

The skill tells the agent to prove each step with a `weaveconnect` command, act on what `next`
finds, and never claim success unless the command passed. The person, not the agent, types the
key. The skill has not yet been measured in a live agent session.

## What has been checked

Observed on October 30, 2026, against `devfromlll-learning-layer-labs/weaveConnect`:

| Run | Result |
|---|---|
| A clean clone from GitHub, following the README | It installed, the tests passed and the command built from the GitHub URL. `connect` showed the hidden key prompt, never displayed the key and passed 4/4. `check` passed 6/6 on the traced control and failed on `no_init.py`. |
| `check` on the controls | Calls: correct passed every check. Without `weave.init` it failed ("Weave received nothing"). With a hand-written wrapper as well it failed, because each model call was recorded twice. A fake secret in a traced argument failed, with the value never printed. Agent spans (W&B's Session SDK): correct passed 8/8, including agent shape. With automatic integrations also on it failed, because each model call was recorded as a span and as a Call. |
| `next` on 21 recent runs | It found "3 of 21 runs: the model wrote a tool call as text instead of calling the tool (meta-llama/Llama-3.3-70B-Instruct)" and named the next command and file. |
| `eval --from-failures` | The three failing runs became a test set. On Llama, 0/3 passed the no-tool-call-as-text check. After switching the model to gpt-oss-120b, 3/3 passed: 3 fixed, 0 broke, and `--min-pass 1.0` passed. Back on Llama, `--min-pass 1.0` failed with exit 1 and named the two questions that broke. |
| `next` after the fix | It noted that the tests built from those failures now pass, and moved on to the next stage. |
| `eval --trials 3` (November 2) | The Llama failure set scored 0/9 runs. After switching to gpt-oss-120b it scored 9/9: 3 cases fixed (every trial passing), 0 broke, 0 flaky. |
| New `next` checks (November 2) | A real run asking about order 9999 appeared as a tool error result, reported but not counted as a failure. A model request recorded twice now counts once, and lone model calls are not counted as agent runs. |
| `label` (November 2) | It refused without an interactive terminal. In a simulated session with scripted answers and `--no-save`, it showed each case with the judge's verdict and reason and reported the agreement. No labels have been saved: real ones must come from a person. |
| Comparison with Claude Code (Sonnet, isolated), November 2 | 36 runs were planned. The Anthropic API credit ran out after run 22, so runs 23 to 36 are excluded. Valid runs for adding tracing and for fixing "nothing arrives": no skill 6/6, W&B's skill 0/6, W&B's skill plus weaveConnect 6/6. For "counted twice": no skill 3/3, W&B's skill 0/1. The W&B-skill runs finished in 11 to 19 seconds without a passing change. That is consistent with the skill's login check stopping the agent in this locked-down setup, but not confirmed, because final messages were not recorded then. With weaveConnect the agent ran its commands in 6/6 runs and passed, but took 71 to 80 seconds, against 12 to 15 with no skill, and cost about twice as much. Total spend: $0.53. |
| Offline tests | 38 passed; Ruff clean. |

Two lessons from building it:

- **The judge.** The first judge failed all six correct answers, because it decided the agent had
  stated "facts it could not have known". It cannot see the agent's tools, so it now grades only
  whether a reply answers the question, and code checks own the facts.
- **Run-to-run variation.** Llama passed 0/3 on one run of the failure tests and 1/3 on another,
  so a single run is a signal, not a verdict.

## Limits

- `check` matches calls and spans by time, so another process writing to the same project during
  the run would be counted too.
- A model Call is recognized by a `model` argument plus `messages`, `input`, `contents` or
  `prompt`. A Call and a span count as the same model call when they name the same model and
  overlap in time.
- `next` looks for specific failures: errors, empty answers, loops and tool calls written as
  text. It does not judge answer quality; evaluations do that.
- The secret scan looks for the values of settings whose names contain KEY, TOKEN, SECRET or
  PASSWORD, plus a few common key formats. It cannot find a secret it has never seen.
- Without `--trials`, each case runs once, and one run is a signal, not a verdict. The stage
  names in `src/weaveconnect/stages.py` are a draft.
- "Watch it" has no command yet. A monitor needs Weave's scorer extras and server-side
  setup; Coreweaving runs a live Signal of this kind, a monitor on completed turns scored on
  CoreWeave inference.
- What the comparison does and does not show: on these tasks a plain agent already passes, so it does not show weaveConnect helping. It shows the kit getting an agent past W&B's login check in a locked-down setup, at the cost of time. The harder tasks ("counted twice" with the kit, "secret in trace") have no valid runs yet. W&B's own [Skill Bench](https://github.com/wandb/skills/tree/main/bench) is the better tool for this question.
- Evaluations and the sample agent use W&B Inference credits.

Local checks: `uv run pytest -q` and `uv run ruff check .`
