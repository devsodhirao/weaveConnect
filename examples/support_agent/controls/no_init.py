"""Negative control: ops are added but weave.init is never called, so nothing is sent."""

import weave
from _base import QUESTION, agent, trace_agent

trace_agent(weave)
print(agent.answer(QUESTION))
