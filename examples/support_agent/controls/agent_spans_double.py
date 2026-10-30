"""Negative control: agent spans plus automatic tracing, so each model call is recorded twice."""

from _base import QUESTION
from _session import run

run(QUESTION, automatic_tracing=True)
