import asyncio

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

    return asyncio.run(wrapper())


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


if __name__ == "__main__":
    app()
