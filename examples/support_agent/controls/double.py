"""Negative control: the model call is wrapped by hand as well as traced automatically."""

import os

import weave
from _base import QUESTION, agent, trace_agent
from openai.resources.chat.completions import Completions

weave.init(os.environ["WEAVE_PROJECT"])
trace_agent(weave)
Completions.create = weave.op(Completions.create, name="my_model_call")
print(agent.answer(QUESTION))
