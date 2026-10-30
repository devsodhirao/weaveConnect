---
name: weaveconnect
description: Connect this project to W&B Weave, prove that tracing really works, and find the next useful step. Use when the user asks to add Weave, trace or evaluate an agent, check whether Weave is working, or asks what to do next with Weave.
---

# weaveConnect

Every step ends with a check that fails loudly. Never tell the user a Weave step worked
unless the matching `weaveconnect` command printed "Every check passed."

The `weaveconnect` command must be installed (`uv tool install git+https://github.com/devsodhirao/weaveConnect`).

## 1. Connect

Run `weaveconnect connect --no-prompt`.

- If it reports **No key found**, stop. Ask the user to run `weaveconnect connect` in their own
  terminal (in Claude Code they can type `! weaveconnect connect`), where the key is typed
  hidden. Never ask for the key in chat, print it, or write it into a file yourself.
- It saves `WEAVE_PROJECT` in `.env`, keeps `.env` out of Git and proves a test trace arrives.

## 2. Add tracing

Make the smallest change. If W&B's `weave-instrument` skill is installed, follow it for the code.

- Call `weave.init(os.environ["WEAVE_PROJECT"])` once, at startup, before the agent runs.
- Add `@weave.op` to the agent's entry function and to each tool function.
- Let Weave's automatic integration record model calls. Do not wrap the model call yourself:
  that records every request twice.
- Never pass keys, tokens or passwords as arguments to a traced function.

## 3. Prove it

Run `weaveconnect check --run "<the command that runs the agent>"`, adding
`--expect-op <tool name>` for each tool the run should use.

- If any check fails, fix the cause it names and run it again.
- Report the result in plain words and include the trace link it prints.

## 4. Next step

Run `weaveconnect next` and tell the user the stage reached and the one next step it names.

## 5. Evaluate, when asked or when `next` suggests it

- With known answers: `weaveconnect eval --target <file>:<function> --cases <cases.jsonl>`,
  one JSON object per line with the function's argument names and an `expected` list of
  acceptable phrases.
- From recent traces: `weaveconnect eval --target <file>:<function> --from-recent 5`.

Report each score with its denominator, for example "5/6 mentioned the expected fact". The
judge cannot see the agent's tools or data, so it grades whether replies answer the question,
not whether facts are true; the `expected` phrases check facts.
