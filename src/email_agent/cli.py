import asyncio
from datetime import date, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(no_args_is_help=True, help="Email super agent: research your Gmail in plain English.")
db_app = typer.Typer(help="Database commands.")
app.add_typer(db_app, name="db")
console = Console()


def setup_logging(default: str = "WARNING") -> None:
    import logging

    from rich.logging import RichHandler

    from email_agent.config import get_settings

    level = (get_settings().log_level or default).upper()
    logging.basicConfig(
        level=level, format="%(name)s: %(message)s", force=True,
        handlers=[RichHandler(console=console, show_path=False)],
    )
    # Library chatter (HTTP requests, IMAP) stays out unless explicitly asked for.
    for noisy in ("httpx", "httpcore", "openai", "imapclient"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, logging.getLogger().level))


@app.callback()
def _main() -> None:
    setup_logging()


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


@app.command()
def setup() -> None:
    """Guided onboarding: API keys → Gmail → your profile → first ingest. Resumes where you left off."""
    from email_agent import onboarding
    from email_agent.onboarding import OnboardingError

    st = _run(onboarding.status())
    labels = {"api_keys": "API keys", "gmail": "Gmail", "profile": "Your profile", "first_ingest": "First ingest"}
    for i, step in enumerate(onboarding.STEPS, start=1):
        console.print(f"  {i}. {labels[step]}: {'[green]done[/]' if st['steps'][step] else 'to do'}")
    if st["next"] is None:
        console.print("[green]Everything is set up.[/]")
        return

    if not st["steps"]["api_keys"]:
        console.print("\n[bold]API keys[/] (stored in your keychain; environment variables still take precedence)")
        while True:
            openai_key = typer.prompt("OpenAI API key", hide_input=True)
            voyage_key = typer.prompt("Voyage API key", hide_input=True)
            try:
                with console.status("Checking the keys..."):
                    onboarding.save_api_keys(openai_key, voyage_key)
                console.print("[green]Keys verified and saved.[/]")
                break
            except OnboardingError as e:
                console.print(f"[red]{e}[/]")
                if not typer.confirm("Try again?", default=True):
                    raise typer.Exit(1)

    if not st["steps"]["gmail"]:
        console.print("\n[bold]Gmail[/]")
        connect(email=None)

    if not st["steps"]["profile"]:
        console.print("\n[bold]About you[/] (kept short; it is part of every conversation)")
        _edit_profile(name=None, timezone=None, about=None)

    if not st["steps"]["first_ingest"]:
        if st["ingest_running"]:
            console.print("\nYour first ingest is already running; `email-agent status` shows progress.")
        else:
            console.print("\n[bold]First ingest[/]")
            ingest(since=None, full=False, yes=False, account=None)
    console.print("\n[green]You're set.[/] Keep `email-agent worker` running for new mail, then try `email-agent chat`.")


def _edit_profile(name: str | None, timezone: str | None, about: str | None) -> None:
    from email_agent import profile
    from email_agent.accounts import get_account
    from email_agent.config import get_settings

    async def load():
        acct = await get_account()
        return acct, await profile.get(acct.id)

    acct, current = _run(load())
    while True:
        name = name or typer.prompt("What should I call you?", default=current.display_name if current else None)
        timezone = timezone or typer.prompt("Your timezone", default=current.timezone if current else get_settings().tz)
        if about is None:
            about = typer.prompt(
                "Anything I should always know about you? (optional, one line)",
                default=current.about if current else "", show_default=False,
            )
        try:
            saved = _run(profile.save(acct.id, name, timezone, about))
            break
        except profile.ProfileError as e:
            console.print(f"[red]{e}[/]")
            name = timezone = about = None
    console.print(f"[green]Saved.[/] {saved.display_name} · {saved.timezone}" + (f" · {saved.about}" if saved.about else ""))


@app.command("profile")
def profile_cmd(
    name: str = typer.Option(None),
    timezone: str = typer.Option(None, help="IANA name, e.g. Asia/Kolkata"),
    about: str = typer.Option(None, help="One line the assistant should always know"),
) -> None:
    """Show or edit your basic profile (applies to new conversations)."""
    _edit_profile(name, timezone, about)


memory_app = typer.Typer(help="What the assistant remembers about you.", no_args_is_help=True)
app.add_typer(memory_app, name="memory")


def _with_account(fn):
    from email_agent.accounts import get_account

    async def main():
        return await fn(await get_account())

    return _run(main())


@memory_app.command("list")
def memory_list() -> None:
    """Your saved notes and the insights from past conversations."""
    from email_agent import memory

    notes = _with_account(lambda a: memory.list_all(a.id))
    if not notes:
        console.print("Nothing remembered yet. Ask the assistant to \"remember …\" in chat.")
        return
    table = Table("topic", "from", "updated", "note")
    for n in notes:
        label = "you" if n["source"] == "user" else "dream"
        table.add_row(n["topic"], label, f"{n['updated_at']:%Y-%m-%d}", n["content"])
    console.print(table)


@memory_app.command("show")
def memory_show(topic: str) -> None:
    """Show one note in full."""
    from email_agent import memory

    notes = {n["topic"]: n for n in _with_account(lambda a: memory.list_all(a.id))}
    note = notes.get(memory.normalize_topic(topic))
    console.print(note["content"] if note else f"[red]No note with topic {topic!r}[/]")


@memory_app.command("forget")
def memory_forget(topic: str) -> None:
    """Delete a note (yours or an insight)."""
    from email_agent import memory

    removed = _with_account(lambda a: memory.forget(a.id, topic))
    console.print("Forgotten." if removed else f"[red]No note with topic {topic!r}[/]")


@memory_app.command("history")
def memory_history(topic: str = typer.Argument(None), limit: int = typer.Option(30)) -> None:
    """Every change to memory, newest first (job = the dream that made it)."""
    from email_agent import memory

    rows = _with_account(lambda a: memory.history(a.id, memory.normalize_topic(topic) if topic else None, limit))
    table = Table("when", "topic", "change", "by", "job", "content")
    for r in rows:
        content = r["new_content"] if r["new_content"] is not None else f"(was) {r['old_content']}"
        table.add_row(f"{r['created_at']:%m-%d %H:%M}", r["topic"], r["op"], r["source"], str(r["job_id"] or ""), content)
    console.print(table)


@memory_app.command("rollback")
def memory_rollback(job_id: int) -> None:
    """Undo everything one dream changed."""
    from email_agent import memory

    n = _with_account(lambda a: memory.rollback_job(a.id, job_id))
    console.print(f"Undid {n} change(s) from job #{job_id}." if n else f"Job #{job_id} made no memory changes.")


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
    from email_agent.accounts import get_account
    from email_agent.ingest.pipeline import plan_fetch

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
        if not n:
            console.print("Nothing new to fetch. If an earlier run was interrupted, run `email-agent resume`.")
            return
        if not (yes or typer.confirm("Start ingestion?", default=True)):
            return
        await _submit(acct, "full" if full else "since", None if full else since_date)

    _run(main())


async def _run_inline(job_id: int, worker_id: str) -> None:
    """No worker running: execute the job in this process through the same runner."""
    from email_agent import jobs
    from email_agent.worker.runner import run_now

    await jobs.requeue_stale()
    while (job := await jobs.get(job_id))["status"] not in ("done", "failed"):
        # Claim fails while another job for this account runs or a retry is scheduled later.
        if job["status"] != "queued" or await run_now(job_id, worker_id) is None:
            await asyncio.sleep(2)


async def _submit(acct, mode: str, since: date | None = None) -> None:
    """Queue an ingestion job and follow its progress from the database.

    The background worker runs it when one is alive; otherwise this process does.
    """
    import contextlib

    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

    from email_agent import jobs

    job_id = await jobs.enqueue(acct.id, mode, since)
    inline = None
    if await jobs.live_workers():
        console.print(f"Job #{job_id} queued for the background worker (Ctrl-C stops watching; the job continues).")
    else:
        console.print(f"Job #{job_id}: no worker running, so it runs here. Start `email-agent worker` to run jobs in the background.")
        inline = asyncio.create_task(_run_inline(job_id, jobs.new_worker_id("cli")))
    columns = (TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn())
    try:
        with Progress(*columns, console=console) as bar:
            task = bar.add_task("queued", total=None)
            while True:
                job = await jobs.get(job_id)
                if job["status"] == "running" and job["stage"]:
                    bar.update(task, description=job["stage"], completed=job["stage_done"], total=job["stage_total"])
                elif job["status"] == "queued":
                    bar.update(task, description="queued" if not job["error"] else f"retry scheduled: {job['error'][:60]}")
                if job["status"] in ("done", "failed"):
                    break
                await asyncio.sleep(1)
    finally:
        if inline is not None and not inline.done():
            inline.cancel()  # the runner hands the job back to the queue
            with contextlib.suppress(asyncio.CancelledError):
                await inline
    if job["status"] == "failed":
        console.print(f"[red]Job #{job_id} failed:[/] {job['error']}")
        raise typer.Exit(1)
    console.print(
        f"[green]Job #{job_id} done.[/] fetched {job['fetched']:,} · extracted {job['enriched']:,} · embedded {job['embedded']:,}"
    )


@app.command()
def resume(account: str = typer.Option(None, help="Account email if several are connected")) -> None:
    """Finish extraction and embedding for emails left behind by an interrupted ingest."""
    from email_agent.accounts import get_account

    async def main():
        await _submit(await get_account(account), "process")

    _run(main())


@app.command()
def reenrich(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
    account: str = typer.Option(None, help="Account email if several are connected"),
) -> None:
    """Re-extract facts made by an older extractor version (after the extraction schema changes)."""
    from email_agent.accounts import get_account
    from email_agent.db.pool import connection
    from email_agent.ingest.enrich import EXTRACTOR_VERSION

    async def main():
        acct = await get_account(account)
        async with connection() as conn:
            stale = (await (await conn.execute(
                """SELECT count(*) AS n FROM email_facts f JOIN emails e ON e.id = f.email_id
                   WHERE e.account_id = %s AND f.extractor_version < %s""",
                (acct.id, EXTRACTOR_VERSION),
            )).fetchone())["n"]
        if not stale:
            console.print(f"All facts are already at extractor version {EXTRACTOR_VERSION}.")
            return
        console.print(
            f"{stale:,} emails have facts from an older extractor; they will be re-extracted (OpenAI) "
            f"and re-embedded (Voyage)."
        )
        if not (yes or await asyncio.to_thread(typer.confirm, "Continue?", default=True)):
            return
        await _submit(acct, "reenrich")

    _run(main())


@app.command()
def sync(account: str = typer.Option(None, help="Account email if several are connected")) -> None:
    """Fetch and process mail that arrived since the last ingest or sync."""
    from email_agent.accounts import get_account

    async def main():
        await _submit(await get_account(account), "sync")

    _run(main())


@app.command("eval-retrieval")
def eval_retrieval(
    golden: Path = typer.Argument(Path("evals/retrieval_golden.yaml")),
    k: int = typer.Option(10),
    account: str = typer.Option(None, help="Account email if several are connected"),
) -> None:
    """Compare BM25, vector, hybrid and hybrid+rerank on hand-labelled questions."""
    from email_agent.accounts import get_account
    from email_agent.retrieval_eval import run_eval

    if not golden.exists():
        console.print(f"[red]{golden} not found.[/] Start from evals/retrieval_golden.example.yaml.")
        raise typer.Exit(1)

    async def main():
        return await run_eval((await get_account(account)).id, golden, k)

    scores, warnings = _run(main())
    for w in warnings:
        console.print(f"[yellow]warning:[/] {w}")
    table = Table("mode", f"recall@{k}", "MRR")
    for name, sc in scores.items():
        table.add_row(name, f"{sc.recall:.3f}", f"{sc.mrr:.3f}")
    console.print(table)


@app.command()
def worker() -> None:
    """Run the background worker: watches for new mail and runs queued ingestion jobs.

    Keep it running in its own terminal (Ctrl-C stops it cleanly).
    """
    from email_agent.worker.main import Worker

    setup_logging("INFO")
    _run(Worker().run())


@app.command()
def search(
    query: str,
    mode: str = typer.Option("hybrid", help="bm25 | vector | hybrid"),
    no_rerank: bool = typer.Option(False, "--no-rerank"),
    k: int = typer.Option(10),
    sender: str = typer.Option(None),
    category: str = typer.Option(None),
    since: str = typer.Option(None, help="YYYY-MM-DD"),
    until: str = typer.Option(None, help="YYYY-MM-DD, inclusive"),
    account: str = typer.Option(None, help="Account email if several are connected"),
) -> None:
    """Debug view of retrieval: per-stage ranks for each hit."""
    from email_agent.accounts import get_account
    from email_agent.search.filters import SearchFilters
    from email_agent.search.hybrid import hybrid_search

    filters = SearchFilters(
        date_from=date.fromisoformat(since) if since else None,
        date_to=date.fromisoformat(until) if until else None,
        sender=sender,
        category=category,
    )
    async def main():
        acct = await get_account(account)
        return await hybrid_search(acct.id, query, filters, k=k, mode=mode, rerank=not no_rerank)

    hits = _run(main())
    table = Table("#", "score", "ranks", "date", "from", "subject")
    for i, h in enumerate(hits, start=1):
        ranks = " ".join(f"{name}={rank}" for name, rank in h.ranks.items() if rank is not None)
        table.add_row(str(i), f"{h.score:.3f}", ranks, f"{h.sent_at:%Y-%m-%d}" if h.sent_at else "", h.from_addr, h.subject)
    console.print(table)


def _summarize_observation(result: dict) -> str:
    from rich.markup import escape

    if "error" in result:
        return f"[red]error: {escape(str(result['error']))}[/]"
    for key in ("results", "emails", "line_items", "messages", "paid_services"):
        if key in result:
            extra = f", totals {result['totals_by_currency']}" if "totals_by_currency" in result else ""
            return f"{len(result[key])} {key.replace('_', ' ')}{extra}"
    if "subject" in result:
        return f"read: {result['subject']}"
    return "ok"


async def _decide_drafts(account, drafts: list[dict]) -> None:
    """The user's approve/reject decision. A web or mobile client would show buttons instead."""
    from rich.panel import Panel

    from email_agent.drafts import DraftError, approve_draft, reject_draft

    for draft in drafts:
        lines = [f"[bold]To:[/] {', '.join(draft['to'])}"]
        if draft.get("cc"):
            lines.append(f"[bold]Cc:[/] {', '.join(draft['cc'])}")
        lines += [f"[bold]Subject:[/] {draft['subject']}", "", draft["body"]]
        console.print(Panel("\n".join(lines), title=f"Send draft #{draft['draft_id']}?", border_style="yellow"))
        approved = await asyncio.to_thread(typer.confirm, "Send it", default=False)
        try:
            if approved:
                await approve_draft(account, draft["draft_id"])
                console.print("[green]Sent.[/]")
            else:
                await reject_draft(account, draft["draft_id"])
                console.print("Not sent.")
        except DraftError as e:
            console.print(f"[red]{e}[/]")


async def _render_turn(agent, question: str, verbose: bool) -> None:
    from rich.markdown import Markdown
    from rich.markup import escape

    from email_agent.agent.events import ConfirmationRequired, FinalAnswer, Observation, Thought, ToolCall
    from email_agent.db.pool import connection

    status = console.status("Thinking...")
    status.start()
    approvals: list[dict] = []
    try:
        async for event in agent.run(question):
            if isinstance(event, ConfirmationRequired):
                approvals.append(event.draft)
            elif isinstance(event, Thought):
                console.print(Markdown(event.text, style="dim italic"))
            elif isinstance(event, ToolCall):
                args = ", ".join(f"{k}={v!r}" for k, v in event.arguments.items() if v is not None)
                console.print(f"[cyan]-> {event.name}({escape(args)})[/]")
            elif isinstance(event, Observation):
                console.print(f"[dim]<- {event.name}: {_summarize_observation(event.result)}[/]")
                if verbose:
                    console.print_json(data=event.result)
            elif isinstance(event, FinalAnswer):
                status.stop()
                console.print()
                console.print(Markdown(event.text))
                if event.cited_email_ids:
                    async with connection() as conn:
                        rows = await (await conn.execute(
                            "SELECT id, subject, from_name, from_addr, sent_at FROM emails WHERE id = ANY(%s) ORDER BY id",
                            (event.cited_email_ids,),
                        )).fetchall()
                    console.print("\n[bold]Sources[/]")
                    for r in rows:
                        when = f"{r['sent_at']:%Y-%m-%d}" if r["sent_at"] else ""
                        line = f"  [#{r['id']}] {r['subject']} — {r['from_name'] or r['from_addr']}, {when}"
                        console.print(escape(line))
                hit = event.cached_tokens / event.input_tokens if event.input_tokens else 0
                console.print(
                    f"[dim]{event.steps} steps · {event.input_tokens:,} in ({event.cached_tokens:,} cached, "
                    f"{hit:.0%}) / {event.output_tokens:,} out tokens[/]"
                )
            if not isinstance(event, FinalAnswer):
                status.start()
    finally:
        status.stop()
    if approvals:
        await _decide_drafts(agent.account, approvals)


@app.command()
def ask(
    question: str,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print full tool observations"),
    account: str = typer.Option(None, help="Account email if several are connected"),
) -> None:
    """Ask one question about your mailbox."""
    from email_agent.accounts import get_account
    from email_agent.agent.loop import Agent

    async def main():
        agent = Agent(await get_account(account))
        await _render_turn(agent, question, verbose)

    _run(main())


@app.command()
def chat(
    resume: str = typer.Option(None, "--resume", help="Continue a saved conversation by id"),
    list_: bool = typer.Option(False, "--list", help="List recent conversations"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print full tool observations"),
    account: str = typer.Option(None, help="Account email if several are connected"),
) -> None:
    """Multi-turn conversation with the agent (saved; resumable). Type 'exit' to quit."""
    from email_agent import conversations
    from email_agent.accounts import get_account
    from email_agent.agent.loop import Agent
    from email_agent.drafts import pending_drafts

    async def main():
        acct = await get_account(account)
        if list_:
            table = Table("updated", "title")
            table.add_column("id (use with --resume)", no_wrap=True, min_width=36)
            for c in await conversations.recent(acct.id):
                table.add_row(f"{c['updated_at']:%Y-%m-%d %H:%M}", c["title"], str(c["id"]))
            console.print(table)
            return
        agent = Agent(acct, conversation_id=resume)
        console.print(f"Chatting about [bold]{acct.email}[/]. Type 'exit' to quit.")
        if waiting := await pending_drafts(acct):
            console.print(f"{len(waiting)} draft(s) are waiting for your decision.")
            await _decide_drafts(acct, waiting)
        while True:
            question = (await asyncio.to_thread(console.input, "\n[bold green]you>[/] ")).strip()
            if question.lower() in {"exit", "quit", ":q"}:
                break
            if question:
                await _render_turn(agent, question, verbose)
        if agent.conversation_id:
            console.print(f"[dim]Resume with: email-agent chat --resume {agent.conversation_id}[/]")

    _run(main())


@app.command()
def status(account: str = typer.Option(None, help="Account email if several are connected")) -> None:
    """Show ingestion progress and sync state."""
    from email_agent.accounts import get_account
    from email_agent.ingest.pipeline import stage_counts

    from email_agent import jobs

    async def main():
        acct = await get_account(account)
        return acct, await stage_counts(acct.id), await jobs.live_workers(), await jobs.open_jobs(acct.id)

    acct, info, workers, open_jobs = _run(main())
    table = Table("stage", "emails")
    for stage in ("fetched", "enriched", "embedded"):
        table.add_row(stage, f"{info['stages'].get(stage, 0):,}")
    console.print(f"[bold]{acct.email}[/]")
    console.print(table)
    console.print(f"chunks: {info['chunks']:,}")
    if sync := info["sync"]:
        covered = "full mailbox" if sync["since_date"] is None else f"since {sync['since_date']}"
        console.print(f"covered: {covered}; last synced {sync['last_synced_at']:%Y-%m-%d %H:%M}")
    console.print(f"worker: {'[green]running[/]' if workers else '[yellow]not running[/] (`email-agent worker`)'}")
    for j in open_jobs:
        detail = f"{j['stage']} {j['stage_done']}/{j['stage_total']}" if j["status"] == "running" and j["stage"] else ""
        console.print(f"  job #{j['id']} {j['mode']}: {j['status']} {detail}".rstrip())
    if job := info["last_job"]:
        console.print(f"last job #{job['id']}: {job['status']}" + (f" ({job['error']})" if job["error"] else ""))
        if job["enrich_input_tokens"]:
            hit = job["enrich_cached_tokens"] / job["enrich_input_tokens"]
            console.print(
                f"extraction: {job['enrich_input_tokens']:,} in ({job['enrich_cached_tokens']:,} cached, {hit:.0%}) / "
                f"{job['enrich_output_tokens']:,} out tokens"
            )


if __name__ == "__main__":
    app()
