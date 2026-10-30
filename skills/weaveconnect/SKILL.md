---
name: weaveconnect
description: Prove that W&B Weave tracing works, read what the traces show, turn failing runs into tests and prove whether a fix helped. Use when the user asks to add or check Weave tracing, asks why their agent fails, or asks what to do next with Weave.
---

# weaveConnect

Every step ends with a command that can fail. Never tell the user a Weave step worked, or that a
fix helped, unless the matching `weaveconnect` command printed "Every check passed."

The `weaveconnect` command must be installed
(`uv tool install git+https://github.com/devsodhirao/weaveConnect`).

## 1. Connect

Run `weaveconnect connect --no-prompt`.

- If it reports **No key found**, stop. Ask the user to run `weaveconnect connect` in their own
  terminal (in Claude Code they can type `! weaveconnect connect`), where the key is typed
  hidden. Never ask for the key in chat, print it, or write it into a file yourself.

## 2. Add tracing, then prove it

If W&B's `weave-instrument` skill is installed, follow it for the code change. Otherwise make
the smallest change: call `weave.init(os.environ["WEAVE_PROJECT"])` once at startup, add
`@weave.op` to the agent's entry function and its tools, and let Weave's automatic integration
record model calls. Never wrap the model call by hand as well, and never pass keys or tokens as
arguments to a traced function. If you record agent spans with the Session SDK, turn automatic
integrations off: `weave.init(..., settings={"implicitly_patch_integrations": False})`.

Run `weaveconnect check --run "<the command that runs the agent>"`, adding
`--expect-op <tool name>` for each tool the run should use. If a check fails, fix the cause it
names and run it again. Report the result in plain words with the trace link.

## 3. Read what the traces show

Run `weaveconnect next`. Report each finding with its denominator ("3 of 21 runs…"), then the one
next step it names. Do not invent findings it did not print.

## 4. Turn failures into tests, then prove the fix

- When `next` reports failures: `weaveconnect eval --target <file>:<function> --from-failures`.
  This records a baseline.
- Make the fix, then run the same command again. Report what the comparison prints: for each
  check, the counts before and after, how many cases were fixed and how many broke. If any case
  broke, say which.
- With known answers instead: `--cases cases.jsonl`, one JSON object per line with the
  function's argument names and an `expected` list of acceptable phrases.
- In CI, add `--min-pass 0.8` (or the agreed share) so the command fails below it.

The judge cannot see the agent's tools or data. It grades whether replies answer the question;
the `expected` phrases and the code checks own the facts. One run of each case is a signal, not a
verdict, so rerun before claiming a small change.
