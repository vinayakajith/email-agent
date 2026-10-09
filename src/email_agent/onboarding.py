"""Onboarding: API keys -> Gmail -> profile -> first ingest.

Pure functions, so the CLI wizard today and a web/mobile flow later share one implementation.
"""

import voyageai
from openai import OpenAI

from email_agent import secrets_store
from email_agent.config import EMBED_DIM, EMBED_MODEL, get_settings
from email_agent.db.pool import connection

STEPS = ("api_keys", "gmail", "profile", "first_ingest")


class OnboardingError(Exception):
    pass


def verify_openai_key(key: str) -> None:
    try:
        OpenAI(api_key=key).models.retrieve(get_settings().openai_model)
    except Exception as e:
        raise OnboardingError(f"OpenAI key rejected: {type(e).__name__}: {e}") from e


def verify_voyage_key(key: str) -> None:
    try:
        voyageai.Client(api_key=key).embed(["ping"], model=EMBED_MODEL, input_type="query", output_dimension=EMBED_DIM)
    except Exception as e:
        raise OnboardingError(f"Voyage key rejected: {type(e).__name__}: {e}") from e


def save_api_keys(openai_key: str, voyage_key: str) -> None:
    """Check both keys with a cheap call, then store them in the keychain."""
    openai_key, voyage_key = openai_key.strip(), voyage_key.strip()
    verify_openai_key(openai_key)
    verify_voyage_key(voyage_key)
    secrets_store.set("openai_api_key", openai_key)
    secrets_store.set("voyage_api_key", voyage_key)
    get_settings.cache_clear()  # pick up the new keys


async def status() -> dict:
    """Which onboarding steps are done, and the next one to do."""
    s = get_settings()
    async with connection() as conn:
        row = await (await conn.execute(
            """SELECT a.id, a.email,
                      EXISTS (SELECT 1 FROM user_profile p WHERE p.account_id = a.id) AS has_profile,
                      EXISTS (SELECT 1 FROM sync_state s WHERE s.account_id = a.id) AS ingested,
                      EXISTS (SELECT 1 FROM ingest_jobs j WHERE j.account_id = a.id
                                AND j.mode IN ('full', 'since') AND j.status IN ('queued', 'running')) AS ingesting
               FROM accounts a ORDER BY a.id LIMIT 1"""
        )).fetchone()
    done = {
        "api_keys": bool(s.openai_api_key and s.voyage_api_key),
        "gmail": row is not None,
        "profile": bool(row and row["has_profile"]),
        "first_ingest": bool(row and row["ingested"]),
    }
    return {
        "steps": done,
        "next": next((step for step in STEPS if not done[step]), None),
        "account": row["email"] if row else None,
        "account_id": row["id"] if row else None,
        "ingest_running": bool(row and row["ingesting"]),
    }
