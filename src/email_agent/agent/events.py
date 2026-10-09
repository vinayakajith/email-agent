"""Events emitted by the agent loop. The CLI renders them; a future UI streams them over SSE."""

from dataclasses import asdict, dataclass, field


@dataclass
class Event:
    def to_dict(self) -> dict:
        return {"type": type(self).__name__, **asdict(self)}


@dataclass
class Thought(Event):
    step: int
    text: str


@dataclass
class ToolCall(Event):
    step: int
    call_id: str
    name: str
    arguments: dict


@dataclass
class Observation(Event):
    step: int
    call_id: str
    name: str
    result: dict
    error: bool = False


@dataclass
class ConfirmationRequired(Event):
    step: int
    draft: dict


@dataclass
class FinalAnswer(Event):
    text: str
    cited_email_ids: list[int] = field(default_factory=list)
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0


# Streaming events: yielded live to clients, not stored in agent_runs traces.


@dataclass
class TextDelta(Event):
    step: int
    text: str


@dataclass
class ThoughtDelta(Event):
    step: int
    text: str


@dataclass
class ToolStarted(Event):
    """A tool call began streaming; its arguments are not complete yet."""

    step: int
    name: str


@dataclass
class StepPreamble(Event):
    """Text streamed in this step turned out to precede tool calls: it was a preamble, not the answer."""

    step: int


@dataclass
class AgentError(Event):
    message: str
