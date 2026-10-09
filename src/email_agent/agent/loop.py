"""Hand-written ReAct loop on the OpenAI Responses API.

Each step: the model reasons (Thought), calls tools (Action), and reads the results
(Observation), until it answers without calling a tool. The loop is an async generator of
events so the same code drives the CLI now and a streaming UI later.
"""

import asyncio
import json
import logging
import random
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, InternalServerError, RateLimitError
from psycopg.types.json import Jsonb

from email_agent import conversations, profile
from email_agent.accounts import Account
from email_agent.agent import tools as agent_tools
from email_agent.agent.events import (
    ConfirmationRequired,
    Event,
    FinalAnswer,
    Observation,
    StepPreamble,
    TextDelta,
    Thought,
    ThoughtDelta,
    ToolCall,
    ToolStarted,
)
from email_agent.agent.prompts import system_prompt, turn_context
from email_agent.config import get_settings
from email_agent.db.pool import connection
from email_agent.prompt_cache import CACHE_OPTIONS, cache_key, text_message, usage_counts

log = logging.getLogger(__name__)
RETRYABLE = (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)
MAX_ATTEMPTS = 6


def _retry_delay(attempt: int) -> float:
    return min(20.0, random.uniform(0.5, 1.5) * 2**attempt)


# Client-side conveniences the streaming SDK adds to output items; the API rejects them as input.
_SDK_ONLY_FIELDS = {"parsed_arguments", "parsed"}


def _strip(value):
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in _SDK_ONLY_FIELDS}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def as_input_item(item) -> dict:
    """An output item as it must be sent back in `input` on the next step."""
    return _strip(item.model_dump(mode="json", exclude_none=True))


@dataclass
class _StepDone:
    response: object


CITATION_RE = re.compile(r"\[#(\d+)\]")


class Agent:
    """One conversation with the agent, persisted server-side.

    `Agent(account)` starts a new conversation; `Agent(account, conversation_id)` resumes one
    from any process or device. History is append-only and only committed at consistent
    points (a step's tool calls together with their outputs), so a crash mid-step never
    leaves a stored conversation the API would reject.
    """

    def __init__(self, account: Account, conversation_id: uuid.UUID | str | None = None, client: AsyncOpenAI | None = None):
        s = get_settings()
        self.account = account
        self.client = client or AsyncOpenAI(api_key=s.openai_api_key)
        self.model = s.openai_model
        self.max_steps = s.agent_max_steps
        self.reasoning = {"effort": s.agent_reasoning_effort, "summary": "auto"} if s.agent_reasoning_effort else None
        self.tz = s.tz
        self.conversation_id = uuid.UUID(str(conversation_id)) if conversation_id else None
        self.ctx = agent_tools.ToolContext(account, self.conversation_id)
        # Full Responses API input: the static system prompt first (ending in a cache breakpoint
        # shared by all conversations of the account), then every turn, append-only.
        self.history: list[dict] = []
        self._committed = 0
        self._loaded = False

    async def _open(self, question: str) -> None:
        """Create, load, or catch up the conversation before a turn."""
        user = None if self._loaded else await profile.get(self.account.id)
        if user:
            self.tz = user.timezone
        if self.conversation_id is None:
            self.conversation_id = await conversations.create(self.account.id, question)
            prompt = system_prompt(self.account.email, profile.render(user))
            self.history = [text_message("developer", prompt, breakpoint=True)]
            await conversations.append(self.conversation_id, self.history)
        elif not self._loaded:
            if await conversations.get(self.account.id, self.conversation_id) is None:
                raise ValueError(f"No conversation {self.conversation_id} for this account")
            self.history = await conversations.items(self.conversation_id)
        else:
            # Items appended elsewhere, e.g. the outcome of a draft approved on another device.
            self.history += await conversations.items(self.conversation_id, after=self._committed)
        self._committed = len(self.history)
        self._loaded = True
        self.ctx.conversation_id = self.conversation_id

    async def _commit(self) -> None:
        await conversations.append(self.conversation_id, self.history[self._committed:])
        self._committed = len(self.history)

    def _params(self, final: bool) -> dict:
        kwargs = {}
        if self.reasoning:
            kwargs["reasoning"] = self.reasoning
            # Stateless (store=False) multi-step reasoning needs the encrypted reasoning items back.
            kwargs["include"] = ["reasoning.encrypted_content"]
        return dict(
            model=self.model,
            input=self.history,
            prompt_cache_key=cache_key(self.account.id, "agent"),
            prompt_cache_options=CACHE_OPTIONS,
            tools=agent_tools.TOOL_SCHEMAS,
            tool_choice="none" if final else "auto",
            parallel_tool_calls=True,
            store=False,
            **kwargs,
        )

    async def _stream_step(self, step: int, final: bool) -> AsyncIterator[Event | _StepDone]:
        """Stream one model call: live deltas, then the completed response as `_StepDone`.

        Background ingestion shares the OpenAI rate limits, so a failure *before* anything was
        streamed is retried with backoff. After deltas have reached the client, it is not: the
        same text must never be streamed twice.
        """
        for attempt in range(MAX_ATTEMPTS):
            streamed = False
            try:
                async with self.client.responses.stream(**self._params(final)) as stream:
                    async for ev in stream:
                        if ev.type == "response.output_text.delta":
                            streamed = True
                            yield TextDelta(step, ev.delta)
                        elif ev.type == "response.reasoning_summary_text.delta":
                            streamed = True
                            yield ThoughtDelta(step, ev.delta)
                        elif ev.type == "response.output_item.added" and ev.item.type == "function_call":
                            streamed = True
                            yield ToolStarted(step, ev.item.name)
                    response = await stream.get_final_response()
                yield _StepDone(response)
                return
            except RETRYABLE:
                if streamed or attempt == MAX_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(_retry_delay(attempt))

    async def run(self, question: str) -> AsyncIterator[Event]:
        started = time.monotonic()
        await self._open(question)
        self.history.append(text_message("developer", await turn_context(self.account.id, self.tz), breakpoint=False))
        # Breakpoint after the question: later steps and turns all extend this prefix.
        self.history.append(text_message("user", question, breakpoint=True))
        events: list[Event] = []
        usage = {"in": 0, "cached": 0, "cache_write": 0, "out": 0}

        def emit(e: Event) -> Event:
            events.append(e)
            return e

        answer = None
        try:
            for step in range(1, self.max_steps + 2):
                final = step > self.max_steps  # budget spent: force an answer without tools
                resp = None
                streamed_text = False
                async for item in self._stream_step(step, final):
                    if isinstance(item, _StepDone):
                        resp = item.response
                    else:
                        streamed_text |= isinstance(item, TextDelta)
                        yield item  # live only; deltas are not stored in the trace
                for k, v in usage_counts(resp.usage).items():
                    usage[k] += v
                self.history.extend(as_input_item(item) for item in resp.output)

                calls = [item for item in resp.output if item.type == "function_call"]
                for item in resp.output:
                    if item.type == "reasoning":
                        text = "\n".join(s.text for s in (item.summary or []) if s.text).strip()
                        if text:
                            yield emit(Thought(step, text))
                # Text emitted alongside tool calls is a preamble, i.e. part of the reasoning trace.
                if calls and resp.output_text.strip():
                    if streamed_text:
                        yield StepPreamble(step)
                    yield emit(Thought(step, resp.output_text.strip()))

                if not calls:
                    answer = resp.output_text.strip()
                    await self._commit()
                    cited = sorted({int(m) for m in CITATION_RE.findall(answer)})
                    yield emit(FinalAnswer(answer, cited, step, usage["in"], usage["out"], usage["cached"], usage["cache_write"]))
                    return

                for c in calls:
                    try:
                        args = json.loads(c.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {"_raw": c.arguments}
                    yield emit(ToolCall(step, c.call_id, c.name, args))

                outputs = await asyncio.gather(*(agent_tools.call_tool(self.ctx, c.name, c.arguments) for c in calls))
                for c, result in zip(calls, outputs):
                    yield emit(Observation(step, c.call_id, c.name, result, error="error" in result))
                    self.history.append(
                        {"type": "function_call_output", "call_id": c.call_id,
                         "output": agent_tools.render_observation(result)}
                    )
                await self._commit()
                for result in outputs:
                    if agent_tools.USER_APPROVAL_KEY in result:
                        yield emit(ConfirmationRequired(step, result[agent_tools.USER_APPROVAL_KEY]))
        finally:
            # Anything not committed (error, cancelled turn) is dropped so memory matches the store.
            del self.history[self._committed:]
            await self._save(question, events, answer, usage, started)

    async def _save(self, question: str, events: list[Event], answer: str | None, usage: dict, started: float) -> None:
        try:
            async with connection() as conn:
                await conn.execute(
                    """INSERT INTO agent_runs (account_id, conversation_id, question, events, answer, input_tokens,
                                               cached_tokens, cache_write_tokens, output_tokens, latency_ms)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (self.account.id, self.conversation_id, question, Jsonb([e.to_dict() for e in events], dumps=lambda o: json.dumps(o, default=str)),
                     answer, usage["in"], usage["cached"], usage["cache_write"], usage["out"],
                     int((time.monotonic() - started) * 1000)),
                )
        except Exception:  # tracing must never break an answer
            log.exception("could not save agent run")
