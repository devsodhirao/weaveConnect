"""Shared setup for the controls: load settings and import the untouched sample agent."""

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402

QUESTION = " ".join(sys.argv[1:]) or "Where is my order 1042?"


def trace_agent(weave) -> None:
    """The smallest correct change: ops on the agent and its tools; model calls traced automatically."""
    agent.answer = weave.op(agent.answer)
    agent.lookup_order = weave.op(agent.lookup_order)
    agent.return_policy = weave.op(agent.return_policy)
