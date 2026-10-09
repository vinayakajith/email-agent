"""Memory about the user: small topic-keyed notes with a full version history.

Two owners, never mixed:
- `user`: saved by the chat agent only when the user explicitly asks;
- `dream`: written by the nightly consolidation (the user's inferred picture).
A dream can never overwrite or delete a `user` note. Every change is recorded in
memory_versions, which is also how a dream is rolled back.
"""

import re
import uuid

from email_agent.db.pool import connection

MAX_CONTENT = 1_500
MAX_MEMORIES = 60
_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9_/-]{0,59}$")
# Cheap guard against storing secrets: card numbers and credential-like phrases.
_SECRET_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b|\b(password|passcode|otp|one[- ]time (?:code|password)|cvv|pin)\s*[:=]", re.I)


class MemoryError(ValueError):
    pass


def normalize_topic(topic: str) -> str:
    t = re.sub(r"\s+", "-", topic.strip().lower()).strip("/")
    if not _TOPIC_RE.match(t):
        raise MemoryError("Topic must be a short slug like 'preferences/answers' or 'people/priya'")
    return t


def _check_content(content: str) -> str:
    content = content.strip()
    if not content:
        raise MemoryError("Memory content is empty")
    if len(content) > MAX_CONTENT:
        raise MemoryError(f"Memory content is limited to {MAX_CONTENT} characters")
    if _SECRET_RE.search(content):
        raise MemoryError("That looks like a secret (card number, password or code); secrets are never stored")
    return content


async def _version(conn, account_id, topic, op, old, new, source, conversation_id, job_id) -> None:
    await conn.execute(
        """INSERT INTO memory_versions (account_id, topic, op, old_content, new_content, source, conversation_id, job_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (account_id, topic, op, old, new, source, conversation_id, job_id),
    )


async def save_in(conn, account_id: int, topic: str, content: str, source: str,
                  conversation_id: uuid.UUID | None = None, job_id: int | None = None) -> str:
    """Create or replace a note inside an open transaction. Returns 'create', 'update', 'unchanged' or 'skipped'."""
    topic, content = normalize_topic(topic), _check_content(content)
    row = await (await conn.execute(
        "SELECT content, source FROM memories WHERE account_id = %s AND topic = %s FOR UPDATE", (account_id, topic)
    )).fetchone()
    if row is None:
        count = (await (await conn.execute(
            "SELECT count(*) AS n FROM memories WHERE account_id = %s", (account_id,)
        )).fetchone())["n"]
        if count >= MAX_MEMORIES:
            raise MemoryError(f"Memory is full ({MAX_MEMORIES} notes); forget something first")
        await conn.execute(
            "INSERT INTO memories (account_id, topic, content, source) VALUES (%s, %s, %s, %s)",
            (account_id, topic, content, source),
        )
        await _version(conn, account_id, topic, "create", None, content, source, conversation_id, job_id)
        return "create"
    if source == "dream" and row["source"] == "user":
        return "skipped"  # the user's own notes are never touched by a dream
    if row["content"] == content and row["source"] == source:
        return "unchanged"
    await conn.execute(
        "UPDATE memories SET content = %s, source = %s, updated_at = now() WHERE account_id = %s AND topic = %s",
        (content, source, account_id, topic),
    )
    await _version(conn, account_id, topic, "update", row["content"], content, source, conversation_id, job_id)
    return "update"


async def delete_in(conn, account_id: int, topic: str, source: str, only_source: str | None = None,
                    conversation_id: uuid.UUID | None = None, job_id: int | None = None) -> bool:
    """Delete a note inside an open transaction; `only_source` restricts which owner's notes may go."""
    row = await (await conn.execute(
        "DELETE FROM memories WHERE account_id = %s AND topic = %s AND (%s::text IS NULL OR source = %s) RETURNING content",
        (account_id, normalize_topic(topic), only_source, only_source),
    )).fetchone()
    if row:
        await _version(conn, account_id, normalize_topic(topic), "delete", row["content"], None, source, conversation_id, job_id)
    return row is not None


async def save(account_id: int, topic: str, content: str, source: str = "user",
               conversation_id: uuid.UUID | None = None) -> str:
    async with connection() as conn:
        async with conn.transaction():
            return await save_in(conn, account_id, topic, content, source, conversation_id)


async def forget(account_id: int, topic: str, conversation_id: uuid.UUID | None = None) -> bool:
    """The user asked to forget: removes the note whoever wrote it."""
    async with connection() as conn:
        async with conn.transaction():
            return await delete_in(conn, account_id, topic, "user", conversation_id=conversation_id)


async def list_all(account_id: int) -> list[dict]:
    async with connection() as conn:
        return await (await conn.execute(
            "SELECT topic, content, source, updated_at FROM memories WHERE account_id = %s ORDER BY source DESC, topic",
            (account_id,),
        )).fetchall()


async def counts(account_id: int) -> dict[str, int]:
    async with connection() as conn:
        rows = await (await conn.execute(
            "SELECT source, count(*) AS n FROM memories WHERE account_id = %s GROUP BY source", (account_id,)
        )).fetchall()
    return {"user": 0, "dream": 0, **{r["source"]: r["n"] for r in rows}}


async def history(account_id: int, topic: str | None = None, limit: int = 50) -> list[dict]:
    async with connection() as conn:
        return await (await conn.execute(
            """SELECT id, topic, op, old_content, new_content, source, job_id, created_at FROM memory_versions
               WHERE account_id = %s AND (%s::text IS NULL OR topic = %s) ORDER BY id DESC LIMIT %s""",
            (account_id, topic, topic, limit),
        )).fetchall()


async def rollback_job(account_id: int, job_id: int) -> int:
    """Undo every memory change a job (a dream) made, newest first. Returns changes undone."""
    async with connection() as conn:
        async with conn.transaction():
            versions = await (await conn.execute(
                "SELECT * FROM memory_versions WHERE account_id = %s AND job_id = %s ORDER BY id DESC",
                (account_id, job_id),
            )).fetchall()
            for v in versions:
                if v["op"] == "create":
                    await conn.execute(
                        "DELETE FROM memories WHERE account_id = %s AND topic = %s AND source = 'dream'",
                        (account_id, v["topic"]),
                    )
                elif v["op"] == "update":
                    await conn.execute(
                        """UPDATE memories SET content = %s, updated_at = now()
                           WHERE account_id = %s AND topic = %s AND source = 'dream'""",
                        (v["old_content"], account_id, v["topic"]),
                    )
                else:  # delete: bring it back unless the user has since taken the topic
                    await conn.execute(
                        """INSERT INTO memories (account_id, topic, content, source) VALUES (%s, %s, %s, 'dream')
                           ON CONFLICT (account_id, topic) DO NOTHING""",
                        (account_id, v["topic"], v["old_content"]),
                    )
                inverse = {"create": "delete", "update": "update", "delete": "create"}[v["op"]]
                await _version(conn, account_id, v["topic"], inverse, v["new_content"], v["old_content"],
                               "rollback", None, None)
    return len(versions)
