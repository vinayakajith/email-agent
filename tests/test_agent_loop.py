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
        self.calls.append(kwargs)
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
    user_turns = [i["content"] for i in client.calls[1]["input"] if i.get("role") == "user"]
    assert user_turns == ["first", "second"]
