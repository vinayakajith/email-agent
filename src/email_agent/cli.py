import asyncio
from datetime import date, timedelta

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(no_args_is_help=True, help="Email super agent: research your Gmail in plain English.")
db_app = typer.Typer(help="Database commands.")
app.add_typer(db_app, name="db")
console = Console()


def _run(coro):
    async def wrapper():
        from email_agent.db.pool import close_pool

        try:
            return await coro
        finally:
            await close_pool()

    from email_agent.accounts import AccountError

    try:
        return asyncio.run(wrapper())
    except AccountError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending database migrations."""
    from email_agent.db.migrate import migrate

    applied = migrate()
    console.print(f"Applied: {', '.join(applied)}" if applied else "Already up to date.")


@app.command()
def doctor() -> None:
    """Check the database, OpenAI and Voyage configuration."""
    from email_agent.doctor import run_checks

    table = Table("check", "status", "detail")
    checks = run_checks()
    for c in checks:
        table.add_row(c.name, "[green]ok[/]" if c.ok else "[red]FAIL[/]", c.detail)
    console.print(table)
    if not all(c.ok for c in checks):
        raise typer.Exit(1)


@app.command()
def connect(
    email: str = typer.Option(None, help="Gmail address"),
) -> None:
    """Connect a Gmail account with an app password (checks IMAP and SMTP)."""
    from email_agent.accounts import AccountError
    from email_agent.accounts import connect as do_connect

    console.print(
        "You need a Gmail [bold]app password[/]: turn on 2-Step Verification, then create one at\n"
        "https://myaccount.google.com/apppasswords . Also make sure IMAP is enabled in Gmail settings."
    )
    email = email or typer.prompt("Gmail address")
    password = typer.prompt("App password (16 characters)", hide_input=True)
    with console.status("Checking IMAP and SMTP login..."):
        try:
            account = _run(do_connect(email, password))
        except AccountError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(1)
    console.print(f"[green]Connected {account.email}[/]. Password saved to the keychain. Next: `email-agent ingest`.")


TIME_FRAMES = {"1": ("Last 30 days", 30), "2": ("Last 90 days", 90), "3": ("Last year", 365), "4": ("Custom start date", None), "5": ("Full mailbox", None)}
# Rough per-email token averages, only for the pre-flight estimate.
EST_ENRICH_TOKENS, EST_EMBED_TOKENS = 1_000, 700


def _pick_since() -> tuple[date | None, bool]:
    console.print("How much of your mailbox should be ingested?")
    for key, (label, _) in TIME_FRAMES.items():
        console.print(f"  [bold]{key}[/]. {label}")
    choice = typer.prompt("Choose", default="2")
    if choice == "5":
        return None, True
    if choice == "4":
        return date.fromisoformat(typer.prompt("Start date (YYYY-MM-DD)")), False
    days = TIME_FRAMES.get(choice, TIME_FRAMES["2"])[1]
    return date.today() - timedelta(days=days), False


@app.command()
def ingest(
    since: str = typer.Option(None, help="Ingest mail since YYYY-MM-DD"),
    full: bool = typer.Option(False, "--full", help="Ingest the entire mailbox"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
    account: str = typer.Option(None, help="Account email if several are connected"),
) -> None:
    """Ingest Gmail for a chosen time frame. Re-running resumes or widens a previous ingest."""
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

    from email_agent.accounts import get_account
    from email_agent.ingest.pipeline import plan_fetch, run_ingest

    if since and full:
        raise typer.BadParameter("Use either --since or --full, not both.")
    since_date: date | None = date.fromisoformat(since) if since else None
    if not since and not full:
        since_date, full = _pick_since()

    async def main():
        acct = await get_account(account)
        scope = "the full mailbox" if full else f"mail since {since_date}"
        with console.status(f"Scanning {scope} in {acct.email}..."):
            plan = await plan_fetch(acct, since_date)
        n = len(plan.to_fetch)
        console.print(f"{n:,} new emails to ingest ({plan.already_stored:,} already stored).")
        if n:
            console.print(
                f"Rough token estimate: ~{n * EST_ENRICH_TOKENS / 1e6:.1f}M OpenAI tokens (extraction), "
                f"~{n * EST_EMBED_TOKENS / 1e6:.1f}M Voyage tokens (embeddings)."
            )
        if not n or not (yes or typer.confirm("Start ingestion?", default=True)):
            return
        columns = (TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn())
        with Progress(*columns, console=console) as bar:
            tasks: dict[str, int] = {}

            def progress(stage: str, done: int, total: int) -> None:
                if stage not in tasks:
                    tasks[stage] = bar.add_task(stage, total=total)
                bar.update(tasks[stage], completed=done, total=total)

            await run_ingest(acct, plan, None if full else since_date, progress)
        console.print("[green]Ingestion finished.[/] Run `email-agent status` for details.")

    _run(main())


@app.command()
def status(account: str = typer.Option(None, help="Account email if several are connected")) -> None:
    """Show ingestion progress and sync state."""
    from email_agent.accounts import get_account
    from email_agent.ingest.pipeline import stage_counts

    async def main():
        acct = await get_account(account)
        return acct, await stage_counts(acct.id)

    acct, info = _run(main())
    table = Table("stage", "emails")
    for stage in ("fetched", "enriched", "embedded"):
        table.add_row(stage, f"{info['stages'].get(stage, 0):,}")
    console.print(f"[bold]{acct.email}[/]")
    console.print(table)
    console.print(f"chunks: {info['chunks']:,}")
    if sync := info["sync"]:
        covered = "full mailbox" if sync["since_date"] is None else f"since {sync['since_date']}"
        console.print(f"covered: {covered}; last synced {sync['last_synced_at']:%Y-%m-%d %H:%M}")
    if job := info["last_job"]:
        console.print(f"last job #{job['id']}: {job['status']}" + (f" ({job['error']})" if job["error"] else ""))


if __name__ == "__main__":
    app()
