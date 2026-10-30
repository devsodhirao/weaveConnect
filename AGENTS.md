# Repository rules

The [README](README.md) holds the idea, setup and current state.

Use plain language in the app and public explanations. Explain unfamiliar terms and
state what a limitation means for the user. Name each metric and its denominator.

- Never print credentials. `.env` and personal notes stay outside Git; `.env.example`
  lists the names to set.
- Traces go to the Weave project named by `WEAVE_PROJECT`
  (`devfromlll-learning-layer-labs/weaveConnect`).
- A check must be able to fail. Keep the broken controls in `examples/support_agent/controls/`
  failing, and never report a step as working unless its `weaveconnect` command passed.
- Separate what was observed from what is proposed. Update "What has been checked" in the
  README only from a run you made, with the date.
- Local checks: `uv run pytest -q` and `uv run ruff check .`. Evaluations and agent runs use
  W&B Inference credits.
