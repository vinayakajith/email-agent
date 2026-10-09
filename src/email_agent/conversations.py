"""Server-side conversation store: append-only Responses API input items per conversation."""

import uuid

from psycopg.types.json import Jsonb

from email_agent.db.pool import connection


async def create(account_id: int, title: str) -> uuid.UUID:
    conversation_id = uuid.uuid4()
    async with connection() as conn:
        await conn.execute(
            "INSERT INTO conversations (id, account_id, title) VALUES (%s, %s, %s)",
            (conversation_id, account_id, title[:120]),
        )
    return conversation_id


async def get(account_id: int, conversation_id: uuid.UUID) -> dict | None:
    async with connection() as conn:
        return await (await conn.execute(
            "SELECT * FROM conversations WHERE id = %s AND account_id = %s", (conversation_id, account_id)
        )).fetchone()


async def items(conversation_id: uuid.UUID, after: int = 0) -> list[dict]:
    """Items in order, skipping the first `after` (used to pick up items appended elsewhere)."""
    async with connection() as conn:
        rows = await (await conn.execute(
            "SELECT item FROM conversation_items WHERE conversation_id = %s AND seq > %s ORDER BY seq",
            (conversation_id, after),
        )).fetchall()
    return [r["item"] for r in rows]


async def append(conversation_id: uuid.UUID, new_items: list[dict]) -> None:
    if not new_items:
        return
    async with connection() as conn:
        async with conn.transaction():
            # Lock the conversation so concurrent writers (agent, approval from another device)
            # get consecutive sequence numbers.
            await conn.execute("SELECT 1 FROM conversations WHERE id = %s FOR UPDATE", (conversation_id,))
            last = (await (await conn.execute(
                "SELECT COALESCE(max(seq), 0) AS n FROM conversation_items WHERE conversation_id = %s",
                (conversation_id,),
            )).fetchone())["n"]
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO conversation_items (conversation_id, seq, item) VALUES (%s, %s, %s)",
                    [(conversation_id, last + i, Jsonb(item)) for i, item in enumerate(new_items, start=1)],
                )
            await conn.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", (conversation_id,))


async def recent(account_id: int, limit: int = 20) -> list[dict]:
    async with connection() as conn:
        return await (await conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE account_id = %s "
            "ORDER BY updated_at DESC LIMIT %s",
            (account_id, limit),
        )).fetchall()
