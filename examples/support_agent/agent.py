"""A small store-support agent with two tools. It has no Weave code yet.

Run it with `uv run python examples/support_agent/agent.py "Where is my order 1042?"`.
It uses W&B Inference, so the only key it needs is WANDB_API_KEY.
"""

import json
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

MODEL = os.environ.get("AGENT_MODEL", "openai/gpt-oss-120b")
ORDERS = {
    "1042": {"status": "shipped", "carrier": "UPS", "arrives": "November 3"},
    "1043": {"status": "processing", "carrier": "not assigned yet", "arrives": "November 6"},
    "1044": {"status": "delivered", "carrier": "USPS", "arrives": "October 28"},
}
POLICY = (
    "Unused items can be returned within 30 days of delivery for a full refund. "
    "Final-sale items cannot be returned."
)
SYSTEM = (
    "You are a store support agent. Use the tools for order facts and the return policy. "
    "If a tool has no answer, say so instead of guessing. Keep answers to two sentences."
)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_order",
            "description": "Look up an order's shipping status by its number.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "return_policy",
            "description": "Get the store's return policy.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def lookup_order(order_id: str) -> dict:
    return ORDERS.get(order_id.strip().lstrip("#"), {"error": "No order with that number."})


def return_policy() -> str:
    return POLICY


def run_tool(name: str, arguments: dict) -> object:
    if name == "lookup_order":
        return lookup_order(**arguments)
    if name == "return_policy":
        return return_policy()
    return {"error": f"Unknown tool {name}."}


def answer(question: str) -> str:
    client = OpenAI(
        base_url="https://api.inference.wandb.ai/v1",
        api_key=os.environ["WANDB_API_KEY"],
        project=os.environ.get("WEAVE_PROJECT"),
    )
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    for _ in range(4):
        response = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
        message = response.choices[0].message
        if not message.tool_calls:
            return message.content or ""
        messages.append(message.model_dump(exclude_none=True))
        for call in message.tool_calls:
            result = run_tool(call.function.name, json.loads(call.function.arguments or "{}"))
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
            )
    return "I could not finish that request."


if __name__ == "__main__":
    load_dotenv()
    print(answer(" ".join(sys.argv[1:]) or "Where is my order 1042?"))
