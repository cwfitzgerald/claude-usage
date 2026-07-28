"""Launcher for the local usage dashboard."""

from __future__ import annotations

from pathlib import Path

import click

from . import core


@click.command(help="Serve the local Claude Code and Codex usage dashboard.")
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
def main(
    host: str,
    port: int,
    open_browser: bool,
    projects_dir: Path,
    gui_dir: Path,
    codex_dir: Path,
) -> None:
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
