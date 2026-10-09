"""Basic onboarding info about the user. Small and rarely changed: it lives in the system prompt."""

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from email_agent.db.pool import connection

MAX_NAME, MAX_ABOUT = 80, 300


class ProfileError(ValueError):
    pass


@dataclass
class Profile:
    account_id: int
    display_name: str
    timezone: str
    about: str


async def get(account_id: int) -> Profile | None:
    async with connection() as conn:
        row = await (await conn.execute(
            "SELECT account_id, display_name, timezone, about FROM user_profile WHERE account_id = %s", (account_id,)
        )).fetchone()
    return Profile(**row) if row else None


async def save(account_id: int, display_name: str, timezone: str, about: str = "") -> Profile:
    display_name, about = display_name.strip(), " ".join(about.split())
    if not display_name or len(display_name) > MAX_NAME:
        raise ProfileError(f"Name must be 1-{MAX_NAME} characters")
    if len(about) > MAX_ABOUT:
        raise ProfileError(f"'About' must be at most {MAX_ABOUT} characters")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ProfileError(f"Unknown timezone {timezone!r}; use an IANA name like Asia/Kolkata") from e
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO user_profile (account_id, display_name, timezone, about) VALUES (%s, %s, %s, %s)
               ON CONFLICT (account_id) DO UPDATE SET display_name = EXCLUDED.display_name,
                   timezone = EXCLUDED.timezone, about = EXCLUDED.about, updated_at = now()""",
            (account_id, display_name, timezone, about),
        )
    return Profile(account_id, display_name, timezone, about)


def render(p: Profile | None) -> str:
    """The profile block of the system prompt."""
    if p is None:
        return ""
    lines = ["About the user (from onboarding):", f"- Name: {p.display_name}", f"- Timezone: {p.timezone}"]
    if p.about:
        lines.append(f"- In their words: {p.about}")
    return "\n".join(lines)
