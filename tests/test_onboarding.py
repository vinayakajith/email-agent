import pytest

from email_agent import onboarding, profile, secrets_store
from email_agent.agent.loop import Agent
from email_agent.config import Settings, get_settings
from tests.test_agent_loop import Item, Resp, ScriptedClient


@pytest.fixture
def fake_keychain(monkeypatch):
    store = {}
    monkeypatch.setattr(secrets_store, "get", lambda name: store.get(name))
    monkeypatch.setattr(secrets_store, "set", lambda name, value: store.__setitem__(name, value))
    return store


def test_api_keys_fall_back_to_keychain(monkeypatch, fake_keychain):
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("VOYAGE_API_KEY")
    fake_keychain.update({"openai_api_key": "sk-from-keychain", "voyage_api_key": "pa-from-keychain"})
    s = Settings(_env_file=None)
    assert (s.openai_api_key, s.voyage_api_key) == ("sk-from-keychain", "pa-from-keychain")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert Settings(_env_file=None).openai_api_key == "sk-env"  # environment wins


def test_save_api_keys_verifies_before_storing(monkeypatch, fake_keychain):
    def reject(key):
        raise onboarding.OnboardingError("Voyage key rejected")

    monkeypatch.setattr(onboarding, "verify_openai_key", lambda key: None)
    monkeypatch.setattr(onboarding, "verify_voyage_key", reject)
    with pytest.raises(onboarding.OnboardingError):
        onboarding.save_api_keys("sk-1", "pa-bad")
    assert fake_keychain == {}

    monkeypatch.setattr(onboarding, "verify_voyage_key", lambda key: None)
    onboarding.save_api_keys(" sk-1 ", "pa-good")
    assert fake_keychain == {"openai_api_key": "sk-1", "voyage_api_key": "pa-good"}
    get_settings.cache_clear()


async def test_status_walks_through_the_steps(db, monkeypatch):
    st = await onboarding.status()
    assert st["steps"] == {"api_keys": True, "gmail": False, "profile": False, "first_ingest": False}
    assert st["next"] == "gmail"

    from email_agent.db.pool import connection

    async with connection() as conn:
        acct_id = (await (await conn.execute(
            "INSERT INTO accounts (email, imap_host, smtp_host) VALUES ('me@gmail.com', 'i', 's') RETURNING id"
        )).fetchone())["id"]
    assert (await onboarding.status())["next"] == "profile"
    await profile.save(acct_id, "Vinayak", "Asia/Kolkata")
    st = await onboarding.status()
    assert st["next"] == "first_ingest" and not st["ingest_running"]
    async with connection() as conn:
        await conn.execute("INSERT INTO ingest_jobs (account_id, mode) VALUES (%s, 'full')", (acct_id,))
    assert (await onboarding.status())["ingest_running"]


async def test_profile_validation(account):
    with pytest.raises(profile.ProfileError):
        await profile.save(account.id, "", "Asia/Kolkata")
    with pytest.raises(profile.ProfileError):
        await profile.save(account.id, "V", "Mars/Olympus")
    with pytest.raises(profile.ProfileError):
        await profile.save(account.id, "V", "UTC", "x" * 301)
    saved = await profile.save(account.id, "  Vinayak ", "Asia/Kolkata", "Founder at   OptiFlow")
    assert (saved.display_name, saved.about) == ("Vinayak", "Founder at OptiFlow")


async def test_profile_is_in_system_prompt_and_sets_timezone(ingested):
    await profile.save(ingested.id, "Vinayak", "America/New_York", "Prefers short answers")
    client = ScriptedClient([Resp([Item("message", text="ok")])])
    [e async for e in Agent(ingested, client=client).run("hi")]
    system = client.calls[0]["input"][0]["content"][0]["text"]
    assert "- Name: Vinayak" in system and "- In their words: Prefers short answers" in system
    context = client.calls[0]["input"][1]["content"][0]["text"]
    assert context.startswith("Calendar (America/New_York)")


async def test_without_profile_prompt_has_no_profile_block(ingested):
    client = ScriptedClient([Resp([Item("message", text="ok")])])
    [e async for e in Agent(ingested, client=client).run("hi")]
    assert "About the user" not in client.calls[0]["input"][0]["content"][0]["text"]
