from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Fixed for v1: no provider or model switching for retrieval.
EMBED_MODEL = "voyage-4-large"
EMBED_DIM = 1024
RERANK_MODEL = "rerank-3"

GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_ALL_MAIL = "[Gmail]/All Mail"
KEYRING_SERVICE = "email-agent"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    voyage_api_key: str = ""
    # OpenAI only; the model name is configurable within OpenAI.
    openai_model: str = "gpt-6-luna"
    openai_enrich_model: str = "gpt-5-mini"
    database_url: str = "postgresql://email_agent:email_agent@localhost:5434/email_agent"

    # Fallback when the keychain is unavailable.
    gmail_address: str = ""
    gmail_app_password: str = ""

    enrich_concurrency: int = 8
    # Passed as reasoning.effort to the enrich model; empty string disables it for non-reasoning models.
    enrich_reasoning_effort: str = "low"
    agent_max_steps: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()
