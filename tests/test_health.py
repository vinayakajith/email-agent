import json

from typer.testing import CliRunner

from email_agent import health, jobs
from email_agent.db.pool import connection


async def _exec(sql, *params):
    async with connection() as conn:
        await conn.execute(sql, params)


def _by_name(checks):
    return {c.name: c for c in checks}


async def test_healthy_install_with_worker(ingested):
    await jobs.register_worker("w1")
    checks = _by_name(health.run())
    assert checks["database"].status == "ok" and checks["worker"].status == "ok"
    assert checks[f"sync {ingested.email}"].status == "ok"
    assert health.overall(list(checks.values())) == "ok"
    assert health.summary(list(checks.values())) == {"status": "ok", "message": "Up to date · synced just now"}


async def test_no_worker_is_a_warning_users_understand(ingested):
    checks = health.run()
    assert _by_name(checks)["worker"].status == "warn"
    assert health.summary(checks) == {"status": "attention", "message": "Background sync is paused, so new mail won't appear yet."}


async def test_stuck_mail_failed_jobs_and_stale_sync(ingested):
    await jobs.register_worker("w1")
    await _exec("UPDATE emails SET created_at = now() - interval '2 hours' WHERE stage <> 'embedded'")
    await _exec("UPDATE sync_state SET last_synced_at = now() - interval '3 hours'")
    await _exec(
        "INSERT INTO ingest_jobs (account_id, mode, status, error, finished_at) VALUES (%s, 'sync', 'failed', 'boom', now())",
        ingested.id,
    )
    checks = _by_name(health.run())
    assert checks["processing"].status == "warn" and "1 email(s)" in checks["processing"].detail
    assert checks["jobs"].status == "warn" and "sync: boom" in checks["jobs"].detail
    assert checks[f"sync {ingested.email}"].status == "warn"
    assert health.summary(list(checks.values()))["message"] == "Background work is failing right now; it will be retried."

    # A later success means the failure was resolved: no longer reported.
    await _exec(
        "INSERT INTO ingest_jobs (account_id, mode, status, finished_at) VALUES (%s, 'process', 'done', now() + interval '1 second')",
        ingested.id,
    )
    checks = _by_name(health.run())
    assert checks["jobs"].status == "ok"
    assert health.summary(list(checks.values()))["message"] == "Some mail couldn't be processed yet; it will be retried."


async def test_dream_overdue_and_drafts(ingested):
    from email_agent import conversations
    from email_agent.prompt_cache import text_message

    await jobs.register_worker("w1")
    conv = await conversations.create(ingested.id, "old chat")
    await conversations.append(conv, [text_message("user", "hello", breakpoint=False)])
    await _exec("UPDATE conversation_items SET created_at = now() - interval '30 hours'")
    await _exec(
        "INSERT INTO drafts (account_id, to_addrs, subject, body, status) VALUES (%s, '{a@x.com}', 's', 'b', 'awaiting_approval')",
        ingested.id,
    )
    checks = health.run()
    assert _by_name(checks)["dreaming"].status == "warn"
    assert "1 draft(s) waiting for your approval" in health.summary(checks)["message"]


async def test_pending_migration_and_unreachable_db(ingested, monkeypatch, tmp_path):
    (tmp_path / "999_future.sql").write_text("SELECT 1;")
    monkeypatch.setattr(health, "MIGRATIONS_DIR", tmp_path)
    checks = health.run()
    assert _by_name(checks)["database"].status == "fail"
    assert health.summary(checks)["message"] == "The assistant needs an update before it can continue."

    monkeypatch.setattr("email_agent.config.get_settings", lambda: type("S", (), {"database_url": "postgresql://nobody@localhost:1/x"})())
    monkeypatch.setattr(health, "get_settings", lambda: type("S", (), {"database_url": "postgresql://nobody@localhost:1/x"})())
    down = health.run()
    assert down[0].detail.startswith("unreachable") and health.summary(down)["status"] == "down"


def test_cli_json_and_exit_code(monkeypatch):
    from email_agent.cli import app

    monkeypatch.setattr(health, "run", lambda deep=False: [health.Check("database", "fail", "unreachable: x")])
    result = CliRunner().invoke(app, ["health", "--json"])
    assert result.exit_code == 1
    body = json.loads(result.output)
    assert body["status"] == "fail" and body["summary"]["status"] == "down"
    assert body["checks"] == [{"name": "database", "status": "fail", "detail": "unreachable: x"}]
