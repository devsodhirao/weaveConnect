"""The sample agent traced with W&B's Session SDK, which records agent spans instead of Calls."""

import json
import os

import weave
from _base import agent
from openai import OpenAI
from weave import Message, Usage
from weave.conversation import ToolCallPart


def to_messages(messages: list[dict]) -> list[Message]:
    converted = []
    for message in messages:
        role = message["role"]
        if role == "system":
            converted.append(Message.system(message["content"]))
        elif role == "user":
            converted.append(Message.user(message["content"]))
        elif role == "tool":
            converted.append(Message.tool_result(message["tool_call_id"], message["content"]))
        else:
            calls = [
                ToolCallPart(
                    id=call["id"],
                    name=call["function"]["name"],
                    arguments=call["function"]["arguments"],
                )
                for call in message.get("tool_calls") or []
            ]
            converted.append(
                Message.assistant(message.get("content") or "", tool_calls=calls or None)
            )
    return converted


def answer(question: str) -> str:
    client = OpenAI(
        base_url="https://api.inference.wandb.ai/v1",
        api_key=os.environ["WANDB_API_KEY"],
        project=os.environ.get("WEAVE_PROJECT"),
    )
    messages = [{"role": "system", "content": agent.SYSTEM}, {"role": "user", "content": question}]
    with weave.start_session(agent_name="support-agent") as session:
        with session.start_turn(user_message=question) as turn:
            for _ in range(4):
                with turn.llm(model=agent.MODEL, provider_name="wandb-inference") as llm:
                    response = client.chat.completions.create(
                        model=agent.MODEL, messages=messages, tools=agent.TOOLS
                    )
                    message = response.choices[0].message
                    reply = message.model_dump(exclude_none=True)
                    llm.record(
                        input_messages=to_messages(messages),
                        output_messages=to_messages([reply]),
                        usage=Usage(
                            input_tokens=response.usage.prompt_tokens,
                            output_tokens=response.usage.completion_tokens,
                        ),
                        finish_reasons=[response.choices[0].finish_reason],
                    )
                if not message.tool_calls:
                    return message.content or ""
                messages.append(reply)
                for call in message.tool_calls:
                    arguments = call.function.arguments or "{}"
                    with turn.tool(
                        name=call.function.name, arguments=arguments, tool_call_id=call.id
                    ) as tool:
                        result = agent.run_tool(call.function.name, json.loads(arguments))
                        tool.result = json.dumps(result)
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
                    )
    return "I could not finish that request."


def run(question: str, automatic_tracing: bool) -> None:
    weave.init(
        os.environ["WEAVE_PROJECT"],
        settings={"implicitly_patch_integrations": automatic_tracing},
    )
    print(answer(question))
