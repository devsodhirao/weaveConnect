"""Positive control: agent spans from W&B's Session SDK, automatic tracing off. Checks pass."""

from _base import QUESTION
from _session import run

run(QUESTION, automatic_tracing=False)
