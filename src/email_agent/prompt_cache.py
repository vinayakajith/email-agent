"""OpenAI prompt caching helpers.

Caching only works on an identical prefix, so requests are laid out stable-first:
tools -> static system prompt -> conversation history (append-only) -> newest items.
Anything that changes per request (like the current time) must come after the cached part.
"""

import re

_GPT_VERSION = re.compile(r"^gpt-(\d+)(?:\.(\d+))?")
# Explicit breakpoints and prompt_cache_options are documented for gpt-5.6 and later.
_MIN_EXPLICIT = (5, 6)


def supports_explicit_breakpoints(model: str) -> bool:
    m = _GPT_VERSION.match(model)
    return bool(m) and (int(m.group(1)), int(m.group(2) or 0)) >= _MIN_EXPLICIT


def cache_key(account_id: int, purpose: str) -> str:
    """Stable routing key so requests sharing a prefix land on the same cache."""
    return f"email-agent:{purpose}:{account_id}"


def text_message(role: str, text: str, breakpoint: bool) -> dict:
    block: dict = {"type": "input_text", "text": text}
    if breakpoint:
        # Marks the exact end of a reusable prefix; never removed later, so the prefix stays identical.
        block["prompt_cache_breakpoint"] = {"mode": "explicit"}
    return {"role": role, "content": [block]}


def usage_counts(usage) -> dict[str, int]:
    """Input, cached-read, cache-write and output tokens from a Responses API usage object."""
    if usage is None:
        return {"in": 0, "cached": 0, "cache_write": 0, "out": 0}
    details = getattr(usage, "input_tokens_details", None)
    return {
        "in": usage.input_tokens,
        "cached": getattr(details, "cached_tokens", 0) or 0,
        "cache_write": getattr(details, "cache_write_tokens", 0) or 0,
        "out": usage.output_tokens,
    }
