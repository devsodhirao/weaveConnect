"""Negative control: a traced helper receives a secret, so it lands in the trace.

Run it with DEMO_SECRET_TOKEN set to a fake value; never use a real key here.
"""

import os

import weave
from _base import QUESTION, agent, trace_agent


@weave.op
def open_session(credentials: str) -> str:
    return "session opened"


weave.init(os.environ["WEAVE_PROJECT"])
trace_agent(weave)
open_session(os.environ["DEMO_SECRET_TOKEN"])
print(agent.answer(QUESTION))
