"""Health checks for every external dependency, so misconfiguration fails loudly up front."""

from dataclasses import dataclass

import psycopg
import voyageai
from openai import OpenAI

from email_agent.config import EMBED_DIM, EMBED_MODEL, RERANK_MODEL, get_settings


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _check_db() -> Check:
    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=5) as conn:
            exts = dict(conn.execute("SELECT extname, extversion FROM pg_extension").fetchall())
            migrated = conn.execute("SELECT to_regclass('public.emails') IS NOT NULL").fetchone()[0]
    except Exception as e:
        return Check("postgres", False, f"{type(e).__name__}: {e}")
    missing = [x for x in ("vector", "pg_search") if x not in exts]
    if missing:
        return Check("postgres", False, f"missing extensions {missing}; run `email-agent db migrate`")
    if not migrated:
        return Check("postgres", False, "schema not migrated; run `email-agent db migrate`")
    return Check("postgres", True, f"pgvector {exts['vector']}, pg_search {exts['pg_search']}")


def _check_openai(model: str, label: str) -> Check:
    s = get_settings()
    if not s.openai_api_key:
        return Check(label, False, "OPENAI_API_KEY not set")
    try:
        OpenAI(api_key=s.openai_api_key).models.retrieve(model)
    except Exception as e:
        return Check(label, False, f"{model}: {type(e).__name__}: {e}")
    return Check(label, True, model)


def _check_voyage() -> list[Check]:
    s = get_settings()
    if not s.voyage_api_key:
        return [Check("voyage", False, "VOYAGE_API_KEY not set")]
    client = voyageai.Client(api_key=s.voyage_api_key)
    checks = []
    try:
        res = client.embed(["ping"], model=EMBED_MODEL, input_type="query", output_dimension=EMBED_DIM)
        checks.append(Check("voyage embed", True, f"{EMBED_MODEL} dim={len(res.embeddings[0])}"))
    except Exception as e:
        checks.append(Check("voyage embed", False, f"{EMBED_MODEL}: {type(e).__name__}: {e}"))
    try:
        client.rerank("ping", ["pong", "ping"], model=RERANK_MODEL, top_k=1)
        checks.append(Check("voyage rerank", True, RERANK_MODEL))
    except Exception as e:
        checks.append(Check("voyage rerank", False, f"{RERANK_MODEL}: {type(e).__name__}: {e}"))
    return checks


def run_checks() -> list[Check]:
    s = get_settings()
    return [
        _check_db(),
        _check_openai(s.openai_model, "openai agent model"),
        _check_openai(s.openai_enrich_model, "openai enrich model"),
        *_check_voyage(),
    ]
