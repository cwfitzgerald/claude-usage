#!/usr/bin/env python3
"""Summarize token usage and cost across all local Claude Code sessions.

Claude Code stores each session as a JSONL transcript under
``~/.claude/projects/<encoded-project>/<session-id>.jsonl``. Every assistant
turn carries a ``message.usage`` block with the per-turn token counts. This
tool walks those transcripts, sums usage per session (deduplicating by API
message id so a resumed/edited log isn't double-counted), prices each session
against the model that produced it, and prints a table.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Pricing (USD per 1,000,000 tokens).
#
# input/output are the published rates. Cache pricing is derived with the
# standard Anthropic multipliers relative to the input rate:
#   - cache read              ~= 0.1x input
#   - cache write, 5-min TTL  ~= 1.25x input
#   - cache write, 1-hour TTL ~= 2.0x input
# Verified for the Opus 4.x / Sonnet 4.6 / Haiku 4.5 / Fable 5 families.
# ---------------------------------------------------------------------------
CACHE_READ_MULT = 0.10
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.00

PRICING: dict[str, tuple[float, float]] = {
    # model id            (input $/MTok, output $/MTok)
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4-0": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-0": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Models we couldn't price (e.g. synthetic ids like "<synthetic>"); recorded so
# we can warn instead of silently treating their cost as zero.
_UNKNOWN_MODELS: set[str] = set()


def price_for(model: str) -> tuple[float, float] | None:
    """Return (input_rate, output_rate) per MTok for a model id, or None."""
    if model in PRICING:
        return PRICING[model]
    # Tolerate dated suffixes like "claude-haiku-4-5-20251001".
    for known, rates in PRICING.items():
        if model.startswith(known):
            return rates
    return None


@dataclass
class Usage:
    """Accumulated token counts for one session (or a grand total)."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    # cache creation tokens not broken down into 1h/5m by the log
    cache_write_other: int = 0

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write_5m += other.cache_write_5m
        self.cache_write_1h += other.cache_write_1h
        self.cache_write_other += other.cache_write_other

    @property
    def total_tokens(self) -> int:
        return (
            self.input
            + self.output
            + self.cache_read
            + self.cache_write_5m
            + self.cache_write_1h
            + self.cache_write_other
        )


@dataclass
class Session:
    name: str
    session_id: str
    project: str
    path: Path
    gui: bool = False  # opened in the desktop GUI (has claude-code-sessions metadata)
    models: set[str] = field(default_factory=set)
    # usage accumulated per model so each slice is priced at its own rate
    per_model: dict[str, Usage] = field(default_factory=dict)

    def usage_for(self, model: str) -> Usage:
        return self.per_model.setdefault(model, Usage())

    @property
    def usage(self) -> Usage:
        total = Usage()
        for u in self.per_model.values():
            total.add(u)
        return total

    @property
    def cost(self) -> float:
        total = 0.0
        for model, u in self.per_model.items():
            rates = price_for(model)
            if rates is None:
                _UNKNOWN_MODELS.add(model)
                continue
            in_rate, out_rate = rates
            total += (u.input / 1e6) * in_rate
            total += (u.output / 1e6) * out_rate
            total += (u.cache_read / 1e6) * in_rate * CACHE_READ_MULT
            total += (u.cache_write_5m / 1e6) * in_rate * CACHE_WRITE_5M_MULT
            total += (u.cache_write_1h / 1e6) * in_rate * CACHE_WRITE_1H_MULT
            # Unbroken-down cache creation: price at the 5-min rate (the common case).
            total += (u.cache_write_other / 1e6) * in_rate * CACHE_WRITE_5M_MULT
        return total


def parse_session(path: Path) -> Session | None:
    """Parse one .jsonl transcript into a Session, or None if it has no usage."""
    session_id = path.stem
    project = path.parent.name
    name = ""

    session = Session(name="", session_id=session_id, project=project, path=path)
    seen_message_ids: set[str] = set()
    saw_usage = False

    try:
        fh = path.open(encoding="utf-8")
    except OSError as exc:
        print(f"warning: cannot open {path}: {exc}", file=sys.stderr)
        return None

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            rtype = rec.get("type")

            # Session name: prefer the AI-generated title; fall back to the
            # first user prompt if no title was ever written.
            if rtype == "ai-title":
                name = rec.get("aiTitle") or name
            elif rtype == "summary" and not name:
                name = rec.get("summary") or name

            if rtype != "assistant":
                continue

            msg = rec.get("message") or {}
            usage = msg.get("usage")
            if not usage:
                continue

            # Dedupe by API message id; the same response can appear on
            # multiple lines in resumed or replayed transcripts.
            mid = msg.get("id")
            if mid:
                if mid in seen_message_ids:
                    continue
                seen_message_ids.add(mid)

            model = msg.get("model") or "<unknown>"
            session.models.add(model)
            u = session.usage_for(model)

            u.input += int(usage.get("input_tokens") or 0)
            u.output += int(usage.get("output_tokens") or 0)
            u.cache_read += int(usage.get("cache_read_input_tokens") or 0)

            created = int(usage.get("cache_creation_input_tokens") or 0)
            breakdown = usage.get("cache_creation") or {}
            wrote_1h = int(breakdown.get("ephemeral_1h_input_tokens") or 0)
            wrote_5m = int(breakdown.get("ephemeral_5m_input_tokens") or 0)
            if wrote_1h or wrote_5m:
                u.cache_write_1h += wrote_1h
                u.cache_write_5m += wrote_5m
                # Any remainder the breakdown didn't account for.
                u.cache_write_other += max(0, created - wrote_1h - wrote_5m)
            else:
                u.cache_write_other += created

            saw_usage = True

    if not saw_usage:
        return None

    session.name = name or "(untitled)"
    return session


def default_gui_dir() -> Path:
    """Locate the desktop app's claude-code-sessions metadata dir.

    Tries known layouts and returns the first that exists:
      - Windows, normal install:  %APPDATA%\\Claude\\...
      - Windows, packaged (MSIX/Store) install: the app's %APPDATA% is
        redirected into its package container, so the data lives under
        %LOCALAPPDATA%\\Packages\\Claude*\\LocalCache\\Roaming\\Claude\\...
      - macOS: ~/Library/Application Support/Claude/...
    Falls back to the conventional path (for the --help text) if none exist.
    """
    candidates: list[Path] = []

    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Claude" / "claude-code-sessions")

    local = os.environ.get("LOCALAPPDATA")
    if local:
        # The publisher-hash suffix on the package name varies per install.
        candidates.extend(
            sorted(
                (Path(local) / "Packages").glob(
                    "Claude*/LocalCache/Roaming/Claude/claude-code-sessions"
                )
            )
        )

    candidates.append(
        Path(os.path.expanduser("~"))
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude-code-sessions"
    )

    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0] if candidates else Path("claude-code-sessions")


def load_gui_metadata(gui_dir: Path) -> dict[str, dict]:
    """Map a CLI session id -> the GUI's metadata for it (title, cwd, archived).

    The desktop app delegates to the bundled CLI, so its transcripts already
    live in ~/.claude/projects. These files only add a metadata layer keyed by
    `cliSessionId`.
    """
    meta: dict[str, dict] = {}
    if not gui_dir.is_dir():
        return meta
    for path in gui_dir.glob("*/*/local_*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cli_id = rec.get("cliSessionId")
        if cli_id:
            meta[cli_id] = rec
    return meta


def find_sessions(projects_dir: Path, gui_dir: Path | None = None) -> list[Session]:
    gui_meta = load_gui_metadata(gui_dir) if gui_dir else {}
    sessions: list[Session] = []
    for path in sorted(projects_dir.glob("*/*.jsonl")):
        s = parse_session(path)
        if s is None:
            continue
        info = gui_meta.get(s.session_id)
        if info is not None:
            s.gui = True
            # The app's curated title is what the user sees in the GUI; prefer it.
            if info.get("title"):
                s.name = info["title"]
        sessions.append(s)
    return sessions


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def print_table(sessions: list[Session], sort_key: str) -> None:
    if not sessions:
        print("No sessions with token usage found.")
        return

    if sort_key == "cost":
        sessions = sorted(sessions, key=lambda s: s.cost, reverse=True)
    elif sort_key == "tokens":
        sessions = sorted(sessions, key=lambda s: s.usage.total_tokens, reverse=True)
    elif sort_key == "name":
        sessions = sorted(sessions, key=lambda s: s.name.lower())

    rows = []
    grand = Usage()
    grand_cost = 0.0
    for s in sessions:
        u = s.usage
        grand.add(u)
        grand_cost += s.cost
        # Combine all cache-write buckets into one displayed column.
        cache_write = u.cache_write_5m + u.cache_write_1h + u.cache_write_other
        rows.append(
            [
                _truncate(s.name, 42),
                "gui" if s.gui else "cli",
                _fmt_int(u.input),
                _fmt_int(u.output),
                _fmt_int(u.cache_read),
                _fmt_int(cache_write),
                _fmt_int(u.total_tokens),
                f"${s.cost:,.2f}",
            ]
        )

    headers = ["Session", "Src", "Input", "Output", "Cache rd", "Cache wr", "Total", "Cost"]
    gw = grand.cache_write_5m + grand.cache_write_1h + grand.cache_write_other
    gui_count = sum(1 for s in sessions if s.gui)
    total_row = [
        f"TOTAL ({len(sessions)} sessions, {gui_count} gui)",
        "",
        _fmt_int(grand.input),
        _fmt_int(grand.output),
        _fmt_int(grand.cache_read),
        _fmt_int(gw),
        _fmt_int(grand.total_tokens),
        f"${grand_cost:,.2f}",
    ]

    _render(headers, rows, total_row)

    if _UNKNOWN_MODELS:
        print(
            "\nwarning: no pricing for "
            + ", ".join(sorted(_UNKNOWN_MODELS))
            + " — their cost is reported as $0.00.",
            file=sys.stderr,
        )


def _truncate(s: str, width: int) -> str:
    return s if len(s) <= width else s[: width - 3] + "..."


def _render(headers: list[str], rows: list[list[str]], total_row: list[str]) -> None:
    cols = len(headers)
    widths = [len(h) for h in headers]
    for row in rows + [total_row]:
        for i in range(cols):
            widths[i] = max(widths[i], len(row[i]))

    def fmt(row: list[str]) -> str:
        cells = []
        for i, cell in enumerate(row):
            # Left-align the name + source columns, right-align numbers/cost.
            cells.append(cell.ljust(widths[i]) if i <= 1 else cell.rjust(widths[i]))
        return "  ".join(cells)

    sep = "  ".join("-" * w for w in widths)
    print(fmt(headers))
    print(sep)
    for row in rows:
        print(fmt(row))
    print(sep)
    print(fmt(total_row))


def main(argv: list[str] | None = None) -> int:
    default_dir = Path(os.path.expanduser("~")) / ".claude" / "projects"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--projects-dir",
        type=Path,
        default=default_dir,
        help=f"Claude projects directory (default: {default_dir})",
    )
    parser.add_argument(
        "--gui-dir",
        type=Path,
        default=default_gui_dir(),
        help=(
            "desktop app's claude-code-sessions metadata dir, used to label GUI "
            "sessions and use their curated titles (auto-detected)"
        ),
    )
    parser.add_argument(
        "--sort",
        choices=["cost", "tokens", "name"],
        default="cost",
        help="sort order for the table (default: cost)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of a table",
    )
    args = parser.parse_args(argv)

    if not args.projects_dir.is_dir():
        print(f"error: {args.projects_dir} is not a directory", file=sys.stderr)
        return 1

    sessions = find_sessions(args.projects_dir, args.gui_dir)

    # A missing GUI metadata dir is a normal state (CLI-only machine), so we
    # stay quiet about it. Only warn when the dir IS present but nothing matched
    # — that's the surprising case worth flagging.
    if (
        sessions
        and not any(s.gui for s in sessions)
        and args.gui_dir.is_dir()
    ):
        n = len(load_gui_metadata(args.gui_dir))
        print(
            f"note: GUI metadata dir {args.gui_dir} has {n} entries but none "
            f"matched a scanned transcript — all sessions shown as 'cli'.",
            file=sys.stderr,
        )

    if args.json:
        out = []
        for s in sessions:
            u = s.usage
            out.append(
                {
                    "name": s.name,
                    "session_id": s.session_id,
                    "project": s.project,
                    "source": "gui" if s.gui else "cli",
                    "models": sorted(s.models),
                    "input_tokens": u.input,
                    "output_tokens": u.output,
                    "cache_read_tokens": u.cache_read,
                    "cache_write_tokens": (
                        u.cache_write_5m + u.cache_write_1h + u.cache_write_other
                    ),
                    "total_tokens": u.total_tokens,
                    "cost_usd": round(s.cost, 4),
                }
            )
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        if _UNKNOWN_MODELS:
            print(
                "warning: no pricing for " + ", ".join(sorted(_UNKNOWN_MODELS)),
                file=sys.stderr,
            )
        return 0

    print_table(sessions, args.sort)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
