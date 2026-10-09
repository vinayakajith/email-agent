"""Hand-written ReAct loop on the OpenAI Responses API.

Each step: the model reasons (Thought), calls tools (Action), and reads the results
(Observation), until it answers without calling a tool. The loop is an async generator of
events so the same code drives the CLI now and a streaming UI later.
"""

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from openai import AsyncOpenAI
from psycopg.types.json import Jsonb

from email_agent.accounts import Account
from email_agent.agent import tools as agent_tools
from email_agent.agent.events import ConfirmationRequired, Event, FinalAnswer, Observation, Thought, ToolCall
from email_agent.agent.prompts import system_prompt
from email_agent.config import get_settings
from email_agent.db.pool import connection

CITATION_RE = re.compile(r"\[#(\d+)\]")
ConfirmFn = Callable[[dict], Awaitable[bool]]


class Agent:
    def __init__(self, account: Account, confirm: ConfirmFn = agent_tools.deny_all, client: AsyncOpenAI | None = None):
        s = get_settings()
        self.account = account
        self.client = client or AsyncOpenAI(api_key=s.openai_api_key)
        self.model = s.openai_model
        self.max_steps = s.agent_max_steps
        self.reasoning = {"effort": s.agent_reasoning_effort, "summary": "auto"} if s.agent_reasoning_effort else None
        self.tz = s.tz
        self.ctx = agent_tools.ToolContext(account, confirm)
        self.conversation_id = uuid.uuid4()
        # Full Responses API input, kept across turns for multi-turn chat.
        self.history: list[dict] = []

    async def _respond(self, final: bool = False):
        kwargs = {}
        if self.reasoning:
            kwargs["reasoning"] = self.reasoning
            # Stateless (store=False) multi-step reasoning needs the encrypted reasoning items back.
            kwargs["include"] = ["reasoning.encrypted_content"]
        return await self.client.responses.create(
            model=self.model,
            instructions=system_prompt(self.account.email, self.tz),
            input=self.history,
            tools=agent_tools.TOOL_SCHEMAS,
            tool_choice="none" if final else "auto",
            parallel_tool_calls=True,
            store=False,
            **kwargs,
        )

    async def run(self, question: str) -> AsyncIterator[Event]:
        started = time.monotonic()
        self.history.append({"role": "user", "content": question})
        events: list[Event] = []
        usage = {"in": 0, "out": 0}

        def emit(e: Event) -> Event:
            events.append(e)
            return e

        answer = None
        try:
            for step in range(1, self.max_steps + 2):
                final = step > self.max_steps  # budget spent: force an answer without tools
                resp = await self._respond(final=final)
                if resp.usage:
                    usage["in"] += resp.usage.input_tokens
                    usage["out"] += resp.usage.output_tokens
                self.history.extend(item.model_dump(mode="json", exclude_none=True) for item in resp.output)

                calls = [item for item in resp.output if item.type == "function_call"]
                for item in resp.output:
                    if item.type == "reasoning":
                        text = "\n".join(s.text for s in (item.summary or []) if s.text).strip()
                        if text:
                            yield emit(Thought(step, text))
                # Text emitted alongside tool calls is a preamble, i.e. part of the reasoning trace.
                if calls and resp.output_text.strip():
                    yield emit(Thought(step, resp.output_text.strip()))

                if not calls:
                    answer = resp.output_text.strip()
                    cited = sorted({int(m) for m in CITATION_RE.findall(answer)})
                    yield emit(FinalAnswer(answer, cited, step, usage["in"], usage["out"]))
                    return

                for c in calls:
                    try:
                        args = json.loads(c.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {"_raw": c.arguments}
                    yield emit(ToolCall(step, c.call_id, c.name, args))

                # Tools needing human confirmation run one at a time; the rest run concurrently.
                results: dict[str, dict] = {}
                parallel = [c for c in calls if c.name not in agent_tools.CONFIRM_TOOLS]
                outputs = await asyncio.gather(
                    *(agent_tools.call_tool(self.ctx, c.name, c.arguments) for c in parallel)
                )
                results.update({c.call_id: o for c, o in zip(parallel, outputs)})
                for c in calls:
                    if c.name in agent_tools.CONFIRM_TOOLS:
                        preview = await agent_tools.confirmation_preview(self.ctx, c.name, c.arguments)
                        if preview is not None:
                            yield emit(ConfirmationRequired(step, preview))
                        results[c.call_id] = await agent_tools.call_tool(self.ctx, c.name, c.arguments)

                for c in calls:
                    result = results[c.call_id]
                    yield emit(Observation(step, c.call_id, c.name, result, error="error" in result))
                    self.history.append(
                        {"type": "function_call_output", "call_id": c.call_id,
                         "output": agent_tools.render_observation(result)}
                    )
        finally:
            await self._save(question, events, answer, usage, started)

    async def _save(self, question: str, events: list[Event], answer: str | None, usage: dict, started: float) -> None:
        try:
            async with connection() as conn:
                await conn.execute(
                    """INSERT INTO agent_runs (conversation_id, question, events, answer, input_tokens,
                                               output_tokens, latency_ms)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (self.conversation_id, question, Jsonb([e.to_dict() for e in events], dumps=lambda o: json.dumps(o, default=str)),
                     answer, usage["in"], usage["out"], int((time.monotonic() - started) * 1000)),
                )
        except Exception as e:  # tracing must never break an answer
            print(f"could not save agent run: {e}")
