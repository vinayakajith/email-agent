from contextlib import asynccontextmanager

from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from email_agent.config import get_settings

_pool: AsyncConnectionPool | None = None


async def _configure(conn: AsyncConnection) -> None:
    try:
        await register_vector_async(conn)
    except Exception:
        # Before migration 001 the vector type does not exist yet.
        pass
    # Autocommit first: configure must leave the connection idle, not inside a transaction.
    await conn.set_autocommit(True)
    # Date filters ("this month") are interpreted in the user's local timezone.
    await conn.execute("SELECT set_config('TimeZone', %s, false)", (get_settings().tz,))


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            get_settings().database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            configure=_configure,
            open=False,
        )
        await _pool.open()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def connection():
    pool = await get_pool()
    async with pool.connection() as conn:
        yield conn
