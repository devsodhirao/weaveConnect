"""Positive control: traced correctly. Every check should pass."""

import os

import weave
from _base import QUESTION, agent, trace_agent

weave.init(os.environ["WEAVE_PROJECT"])
trace_agent(weave)
print(agent.answer(QUESTION))
