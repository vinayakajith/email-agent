import os
import re
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Fixed for v1: no provider or model switching for retrieval.
EMBED_MODEL = "voyage-4-large"
EMBED_DIM = 1024
RERANK_MODEL = "rerank-3"

GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_ALL_MAIL = "[Gmail]/All Mail"
KEYRING_SERVICE = "email-agent"

# Request features used throughout (explicit prompt-cache breakpoints, prompt_cache_options,
# reasoning summaries) are built for GPT-6-class models; older families are not supported.
MIN_GPT_MAJOR = 6
_GPT_MAJOR = re.compile(r"^gpt-(\d+)")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    voyage_api_key: str = ""
    # OpenAI only, GPT-6 class (e.g. gpt-6-luna, gpt-6-sol, gpt-6.1-sol).
    openai_model: str = "gpt-6-luna"
    openai_enrich_model: str = "gpt-6-luna"
    database_url: str = "postgresql://email_agent:email_agent@localhost:5434/email_agent"

    # Fallback when the keychain is unavailable.
    gmail_address: str = ""
    gmail_app_password: str = ""

    # IANA name used for "this month" / date filters; detected from the OS when empty.
    timezone: str = ""
    enrich_concurrency: int = 8
    # Passed as reasoning.effort to the enrich model.
    enrich_reasoning_effort: str = "low"
    agent_max_steps: int = 15
    # Passed as reasoning.effort to the agent model.
    agent_reasoning_effort: str = "medium"

    @field_validator("openai_model", "openai_enrich_model")
    @classmethod
    def _gpt6_class(cls, v: str) -> str:
        m = _GPT_MAJOR.match(v)
        if not m or int(m.group(1)) < MIN_GPT_MAJOR:
            raise ValueError(f"{v!r} is not supported; use a GPT-{MIN_GPT_MAJOR}-class model such as gpt-6-luna")
        return v

    @property
    def tz(self) -> str:
        if self.timezone:
            return self.timezone
        target = os.path.realpath("/etc/localtime")
        return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else "UTC"


@lru_cache
def get_settings() -> Settings:
    return Settings()
