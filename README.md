# weaveConnect

[Coreweaving](https://github.com/devsodhirao/coreweaving) answers questions about the Weave docs.
weaveConnect takes the next step: it gets Weave working on a developer's own agent, from inside
the coding tool they already use (Claude Code, Codex), and checks every step by running it.

[Weave project](https://wandb.ai/devfromlll-learning-layer-labs/weaveConnect/weave)

## What it does

| Command | What it proves |
|---|---|
| `weaveconnect connect` | A W&B key exists (typed hidden, never printed), `.env` stays out of Git, the Weave project exists, and a test trace arrives and reads back. |
| `weaveconnect check --run "<command>"` | Runs your agent, then reads back what Weave received: traces arrived, model calls were recorded, expected steps appear, each model request was recorded once, every call finished, and no secrets are in the traces. |
| `weaveconnect next` | Reads the project and reports which stage it has reached and the one next step. |
| `weaveconnect eval --target file.py:function --cases cases.jsonl` | Saves the cases as a Weave dataset and runs a `weave.Evaluation` with code checks and a judge from a different model family, hosted on W&B Inference. |

Every check reports **pass**, **fail** or **could not check**, and a report with no checks never
passes. Exit codes: 0 when every check passed, 1 when one failed, 2 when something could not be
checked.

## Use it from Claude Code or Codex

1. Install the command: `uv tool install git+https://github.com/devsodhirao/weaveConnect`.
2. Give your agent the skill: copy `skills/weaveconnect/` into `.claude/skills/` for Claude Code,
   or point your `AGENTS.md` at `skills/weaveconnect/SKILL.md` for Codex.

The skill tells the agent to make the smallest tracing change, prove it with `weaveconnect check`,
and never claim success unless every check passed. W&B's own `weave-instrument` skill can write
the tracing code; weaveConnect checks that it works. The one step the agent never does is
handle the key: the person types it into `weaveconnect connect`, hidden.

## Try it here

```
uv sync
uv run weaveconnect connect
uv run weaveconnect check --run "uv run python examples/support_agent/controls/traced.py"
uv run weaveconnect next
uv run weaveconnect eval --target examples/support_agent/agent.py:answer --cases examples/support_agent/cases.jsonl
```

`examples/support_agent/agent.py` is a store-support agent with two tools and no Weave code. It
runs on W&B Inference, so the W&B key is the only key it needs. `examples/support_agent/controls/`
holds one correctly traced version and three broken on purpose, which show the checks can fail.

## What has been checked

Observed on October 30, 2026, against `devfromlll-learning-layer-labs/weaveConnect`:

| Run | Result |
|---|---|
| `connect`, here and in a fresh Git repo | 4/4 checks passed each time. In the fresh repo it added `.env` to `.gitignore` and left `.env` readable only by its owner. With no key anywhere, it failed with instructions and wrote nothing. |
| `check` on the four controls | Correct tracing: 6/6 passed. No `weave.init`: failed, "Weave received nothing". Model call wrapped by hand as well: failed, 2 requests recorded twice. A fake secret passed to a traced function: failed, with its location named and the value not printed. |
| A five-line tracing change in a fresh repo | 7/7 checks passed, including the expected `lookup_order` step. |
| `eval` on six cases | gpt-oss-120b agent: 6/6 mentioned the expected fact and the judge passed 6/6. Llama 3.3 70B agent: 1/6 and 1/6, because it wrote its tool calls as text instead of calling the tools. |
| `next` | Reported each of the six stages from what existed in the project. "Trust it" stays open until a person adds judgments. |
| Offline tests | 25 passed; Ruff clean. |

The first version of the judge failed all six correct answers, because it decided the agent had
stated "facts it could not have known". The judge cannot see the agent's tools, so it now grades
only whether a reply answers the question, and the expected phrases check the facts. The
disagreement between the code check (6/6) and that judge (0/6) is what exposed the problem.

## Limits

- `check` matches calls by time, so another process writing to the same project during the run
  would be counted too.
- A model request is recognized by a `model` argument plus `messages`, `input`, `contents` or
  `prompt`. Requests in other shapes are not checked for duplicates.
- The secret scan looks for the values of settings whose names contain KEY, TOKEN, SECRET or
  PASSWORD, plus a few common key formats. It cannot find a secret it has never seen.
- The stage names in `src/weaveconnect/stages.py` are a draft.
- Evaluations and the sample agent use W&B Inference credits.

Local checks: `uv run pytest -q` and `uv run ruff check .`
