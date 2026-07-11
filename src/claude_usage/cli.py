"""Click command-line interface for the dashboard and terminal report."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from . import core


@click.group(help="Inspect local Claude Code and Codex usage.")
def cli() -> None:
    pass


@cli.command(help="Serve the local usage dashboard.")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=click.IntRange(1, 65535))
@click.option(
    "open_browser",
    "--open/--no-open",
    default=True,
    show_default=True,
    help="Open the dashboard in the default web browser.",
)
@click.option(
    "--projects-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path.home() / ".claude" / "projects",
    show_default=True,
)
@click.option(
    "--gui-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=core.default_gui_dir,
    show_default=True,
)
@click.option(
    "--codex-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=core.default_codex_dir,
    show_default=True,
)
def serve(
    host: str,
    port: int,
    open_browser: bool,
    projects_dir: Path,
    gui_dir: Path,
    codex_dir: Path,
) -> int:
    import uvicorn

    from .service import ScanConfig, UsageService
    from .web import create_app

    if host not in {"127.0.0.1", "localhost", "::1"}:
        click.echo(
            "warning: the dashboard exposes local session metadata without "
            "authentication on a non-loopback interface",
            err=True,
        )
    service = UsageService(ScanConfig(projects_dir, gui_dir, codex_dir))
    browser_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    browser_url = f"http://{browser_host}:{port}"
    uvicorn.run(
        create_app(
            service,
            open_browser_url=browser_url if open_browser else None,
        ),
        host=host,
        port=port,
        workers=1,
    )
    return 0


@cli.command(help="Print the compatible terminal or JSON report.")
@click.option(
    "--projects-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path.home() / ".claude" / "projects",
    show_default=True,
)
@click.option(
    "--gui-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=core.default_gui_dir,
    show_default=True,
)
@click.option(
    "--codex-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=core.default_codex_dir,
    show_default=True,
)
@click.option(
    "--sort",
    type=click.Choice(["cost", "tokens", "name", "date"]),
    default="cost",
    show_default=True,
)
@click.option("--since", metavar="WHEN")
@click.option(
    "--color",
    type=click.Choice(["auto", "always", "never"]),
    default="auto",
    show_default=True,
)
@click.option("json_output", "--json", is_flag=True, help="Emit JSON output.")
def report(
    projects_dir: Path,
    gui_dir: Path,
    codex_dir: Path,
    sort: str,
    since: str | None,
    color: str,
    json_output: bool,
) -> int:
    args = [
        "--projects-dir",
        str(projects_dir),
        "--gui-dir",
        str(gui_dir),
        "--codex-dir",
        str(codex_dir),
        "--sort",
        sort,
        "--color",
        color,
    ]
    if since:
        args.extend(["--since", since])
    if json_output:
        args.append("--json")
    return core.main(args)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        args = ["serve"]
    elif args[0] not in {"serve", "report", "--help", "-h"}:
        # Preserve historical option-only invocations such as `--json`.
        args.insert(0, "report")
    try:
        result = cli.main(args=args, prog_name="claude-usage", standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.exceptions.Exit as exc:
        return exc.exit_code
    return int(result or 0)
