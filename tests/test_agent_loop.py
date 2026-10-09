import copy
import json
from dataclasses import dataclass, field

from email_agent.agent.events import FinalAnswer, Observation, ToolCall
from email_agent.agent.loop import Agent
from email_agent.db.pool import connection


@dataclass
class Item:
    type: str
    summary: list = field(default_factory=list)
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    text: str = ""

    def model_dump(self, **kw):
        if self.type == "function_call":
            return {"type": "function_call", "call_id": self.call_id, "name": self.name, "arguments": self.arguments}
        if self.type == "reasoning":
            return {"type": "reasoning", "summary": [{"type": "summary_text", "text": s.text} for s in self.summary]}
        return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": self.text}]}


@dataclass
class Summary:
    text: str


@dataclass
class Usage:
    input_tokens: int = 100
    output_tokens: int = 10


@dataclass
class Resp:
    output: list
    usage: Usage = field(default_factory=Usage)

    @property
    def output_text(self):
        return "".join(i.text for i in self.output if i.type == "message")


class ScriptedClient:
    """Plays back a list of responses; when tool_choice='none' it must answer in text."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.responses = self

    async def create(self, **kwargs):
        # Snapshot: the agent keeps appending to the same history list.
        self.calls.append({**kwargs, "input": copy.deepcopy(kwargs["input"])})
        if kwargs["tool_choice"] == "none":
            return Resp([Item("message", text="Forced answer.")])
        return self.script.pop(0) if self.script else self.script_default()

    def script_default(self):
        return Resp([Item("function_call", call_id=f"c{len(self.calls)}", name="filter_emails", arguments="{}")])


def call(cid, name, **args):
    return Item("function_call", call_id=cid, name=name, arguments=json.dumps(args))


async def test_react_loop_thought_action_observation_answer(ingested):
    client = ScriptedClient([
        Resp([
            Item("reasoning", summary=[Summary("I should total the card statements for October.")]),
            call("c1", "aggregate_spend", kind="credit_card_bill", date_from="2026-10-01", date_to="2026-10-31"),
            call("c2", "filter_emails", needs_action=True),
        ]),
        Resp([Item("message", text="Your October credit card bills total INR 12,450 [#1].")]),
    ])
    agent = Agent(ingested, client=client)
    events = [e async for e in agent.run("total of my credit card bills this month")]

    kinds = [type(e).__name__ for e in events]
    assert kinds == ["Thought", "ToolCall", "ToolCall", "Observation", "Observation", "FinalAnswer"]
    obs = [e for e in events if isinstance(e, Observation)]
    assert obs[0].result["totals_by_currency"] == {"INR": 12450.0}
    final = events[-1]
    assert isinstance(final, FinalAnswer) and final.cited_email_ids == [1] and final.steps == 2

    # Observations went back to the model on step 2, after the function calls.
    second_input = client.calls[1]["input"]
    outputs = [i for i in second_input if i.get("type") == "function_call_output"]
    assert [o["call_id"] for o in outputs] == ["c1", "c2"]
    assert "12450" in outputs[0]["output"]

    async with connection() as conn:
        run = await (await conn.execute("SELECT * FROM agent_runs")).fetchone()
    assert run["answer"].startswith("Your October") and run["input_tokens"] == 200
    assert [e["type"] for e in run["events"]] == kinds


async def test_step_budget_forces_final_answer(ingested, monkeypatch):
    agent = Agent(ingested, client=ScriptedClient([]))
    agent.max_steps = 3
    events = [e async for e in agent.run("loop forever")]
    assert sum(isinstance(e, ToolCall) for e in events) == 3
    assert isinstance(events[-1], FinalAnswer) and events[-1].text == "Forced answer."
    assert agent.client.calls[-1]["tool_choice"] == "none"


async def test_multi_turn_keeps_history(ingested):
    client = ScriptedClient([Resp([Item("message", text="One.")]), Resp([Item("message", text="Two.")])])
    agent = Agent(ingested, client=client)
    [e async for e in agent.run("first")]
    [e async for e in agent.run("second")]
    user_turns = [i["content"][0]["text"] for i in client.calls[1]["input"] if i.get("role") == "user"]
    assert user_turns == ["first", "second"]


async def test_every_request_extends_the_previous_prefix(ingested):
    """Prompt caching needs each request's input to start with the previous request's input."""
    client = ScriptedClient([
        Resp([call("c1", "filter_emails", needs_action=True)]),
        Resp([Item("message", text="One.")]),
        Resp([Item("message", text="Two.")]),
    ])
    agent = Agent(ingested, client=client)
    assert agent.model == "gpt-6-luna"
    [e async for e in agent.run("first")]
    [e async for e in agent.run("second")]

    inputs = [c["input"] for c in client.calls]
    for prev, cur in zip(inputs, inputs[1:]):
        assert cur[: len(prev)] == prev
    assert all(c["prompt_cache_key"] == f"email-agent:agent:{ingested.id}" for c in client.calls)

    system, calendar, user = inputs[0][:3]
    assert system["role"] == "developer" and "autonomous research agent" in system["content"][0]["text"]
    assert calendar["role"] == "developer" and calendar["content"][0]["text"].startswith("Calendar")
    assert user["role"] == "user"
    # The time-dependent calendar note never sits before a breakpoint-marked stable block.
    assert "prompt_cache_breakpoint" not in calendar["content"][0]


async def test_requests_carry_cache_breakpoints_and_options(ingested):
    client = ScriptedClient([Resp([Item("message", text="ok")])])
    [e async for e in Agent(ingested, client=client).run("q")]
    req = client.calls[0]
    assert req["prompt_cache_options"] == {"mode": "implicit"}
    marked = [i["role"] for i in req["input"] if "prompt_cache_breakpoint" in i["content"][0]]
    assert marked == ["developer", "user"]  # end of system prompt, end of the question


def test_only_gpt6_class_models_are_accepted(monkeypatch):
    import pytest
    from pydantic import ValidationError

    from email_agent.config import Settings

    for ok in ("gpt-6-luna", "gpt-6.1-sol", "gpt-7"):
        assert Settings(openai_model=ok).openai_model == ok
    for bad in ("gpt-5-mini", "gpt-5.6", "o3", "gpt-4o"):
        with pytest.raises(ValidationError):
            Settings(openai_enrich_model=bad)


async def test_cached_tokens_are_tracked(ingested):
    @dataclass
    class Details:
        cached_tokens: int
        cache_write_tokens: int

    @dataclass
    class CachedUsage:
        input_tokens: int = 1000
        output_tokens: int = 10
        input_tokens_details: Details = field(default_factory=lambda: Details(800, 150))

    client = ScriptedClient([
        Resp([call("c1", "filter_emails")], usage=CachedUsage()),
        Resp([Item("message", text="done")], usage=CachedUsage()),
    ])
    events = [e async for e in Agent(ingested, client=client).run("q")]
    final = events[-1]
    assert (final.input_tokens, final.cached_tokens, final.cache_write_tokens) == (2000, 1600, 300)
    async with connection() as conn:
        run = await (await conn.execute("SELECT cached_tokens, cache_write_tokens FROM agent_runs")).fetchone()
    assert run == {"cached_tokens": 1600, "cache_write_tokens": 300}
