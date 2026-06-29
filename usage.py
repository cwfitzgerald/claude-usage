#!/usr/bin/env python3
"""Summarize token usage and cost across local coding-agent sessions.

Supports multiple tools, shown side by side in one table (the **Tool** column):

* **Claude Code** stores each session as a JSONL transcript under
  ``~/.claude/projects/<encoded-project>/<session-id>.jsonl``. Every assistant
  turn carries a ``message.usage`` block; we sum usage per session,
  deduplicating by API message id so a resumed/edited log isn't double-counted.

* **Codex** stores rollout transcripts under
  ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``. Each carries periodic
  ``token_count`` events whose ``total_token_usage`` is *cumulative*, so we
  just read the final running total — no dedup needed.

Each session is priced against the model that produced it and printed in a
table.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
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
    # OpenAI / Codex. These reuse the same cost formula as the Claude models:
    # OpenAI's cached-input rate is 10% of the input rate (== CACHE_READ_MULT),
    # so codex "cached_input_tokens" map onto our cache_read bucket and price
    # correctly, and OpenAI has no cache-*write* surcharge (those buckets stay
    # zero). List prices, USD/MTok, as of 2026-06.
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.4": (2.5, 15.0),
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
    tool: str = "claude"  # which agent produced it: "claude" or "codex"
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


# ---------------------------------------------------------------------------
# Codex (~/.codex)
# ---------------------------------------------------------------------------

def default_codex_dir() -> Path:
    """Root of Codex's session storage (~/.codex/sessions)."""
    return Path(os.path.expanduser("~")) / ".codex" / "sessions"


def load_codex_index(codex_root: Path) -> dict[str, str]:
    """Map a Codex session id -> its curated thread name, from session_index.jsonl.

    The index lives next to the ``sessions/`` dir and only covers recent
    sessions, so callers must have a fallback for ids it doesn't list.
    """
    index: dict[str, str] = {}
    path = codex_root.parent / "session_index.jsonl"
    if not path.is_file():
        return index
    try:
        fh = path.open(encoding="utf-8")
    except OSError:
        return index
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid, name = rec.get("id"), rec.get("thread_name")
            if sid and name:
                index[sid] = name
    return index


def _codex_fallback_name(user_texts: list[str]) -> str:
    """Best-effort session name from the first *real* user prompt.

    Codex injects AGENTS.md / permission blocks as the first user messages, so
    we skip anything that looks like an instruction block and take the first
    genuine prompt.
    """
    for txt in user_texts:
        t = txt.strip()
        if not t or t.startswith(("#", "<")):
            continue
        first_line = t.splitlines()[0].strip()
        return _truncate(first_line, 60)
    return "(untitled)"


def parse_codex_rollout(path: Path, index: dict[str, str]) -> Session | None:
    """Parse one Codex rollout transcript, or None if it has no token usage."""
    session_id = path.stem
    cwd = ""
    originator = ""
    models: list[str] = []
    user_texts: list[str] = []
    # token_count.total_token_usage is cumulative; keep the largest seen.
    best_total = 0
    best_usage: dict | None = None

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
            payload = rec.get("payload") or {}

            if rtype == "session_meta":
                session_id = payload.get("id") or session_id
                cwd = payload.get("cwd") or ""
                originator = payload.get("originator") or ""
            elif rtype == "turn_context":
                m = payload.get("model")
                if m:
                    models.append(m)
            elif rtype == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                tot = info.get("total_token_usage") or {}
                t = int(tot.get("total_tokens") or 0)
                if t >= best_total:
                    best_total, best_usage = t, tot
            elif rtype == "response_item" and payload.get("role") == "user":
                for c in payload.get("content") or []:
                    if isinstance(c, dict) and c.get("type") in ("input_text", "text"):
                        user_texts.append(c.get("text") or "")

    if not best_usage or best_total == 0:
        return None  # no recorded usage (e.g. local models that don't report it)

    # Codex's input_tokens INCLUDE the cached ones; split them so the cached
    # slice is priced at the discounted (cache_read) rate and the rest at full.
    input_total = int(best_usage.get("input_tokens") or 0)
    cached = int(best_usage.get("cached_input_tokens") or 0)
    output = int(best_usage.get("output_tokens") or 0)  # already includes reasoning

    # Pick the model the session mostly ran on for pricing (a session may also
    # invoke internal models like codex-auto-review; we attribute the aggregate
    # total to the dominant one).
    model = max(set(models), key=models.count) if models else "<unknown>"

    session = Session(
        name=index.get(session_id) or _codex_fallback_name(user_texts),
        session_id=session_id,
        project=cwd,
        path=path,
        tool="codex",
        gui="desktop" in originator.lower(),
        models=set(models) or {model},
    )
    u = session.usage_for(model)
    u.input = max(0, input_total - cached)
    u.cache_read = cached
    u.output = output
    return session


def find_codex_sessions(codex_root: Path) -> list[Session]:
    if not codex_root.is_dir():
        return []
    index = load_codex_index(codex_root)
    sessions: list[Session] = []
    for path in sorted(codex_root.glob("**/rollout-*.jsonl")):
        s = parse_codex_rollout(path, index)
        if s is not None:
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
                s.tool,
                "gui" if s.gui else "cli",
                _fmt_int(u.input),
                _fmt_int(u.output),
                _fmt_int(u.cache_read),
                _fmt_int(cache_write),
                _fmt_int(u.total_tokens),
                f"${s.cost:,.2f}",
            ]
        )

    headers = ["Session", "Tool", "Src", "Input", "Output", "Cache rd", "Cache wr", "Total", "Cost"]
    gw = grand.cache_write_5m + grand.cache_write_1h + grand.cache_write_other
    by_tool = Counter(s.tool for s in sessions)
    breakdown = ", ".join(f"{n} {tool}" for tool, n in sorted(by_tool.items()))
    total_row = [
        f"TOTAL ({len(sessions)} sessions: {breakdown})",
        "",
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
            + " - their cost is reported as $0.00.",
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
            # Left-align the name + tool + source columns, right-align numbers.
            cells.append(cell.ljust(widths[i]) if i <= 2 else cell.rjust(widths[i]))
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
        "--codex-dir",
        type=Path,
        default=default_codex_dir(),
        help=f"Codex sessions directory (default: {default_codex_dir()})",
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

    claude_sessions = find_sessions(args.projects_dir, args.gui_dir)
    sessions = claude_sessions + find_codex_sessions(args.codex_dir)

    # A missing GUI metadata dir is a normal state (CLI-only machine), so we
    # stay quiet about it. Only warn when the dir IS present but nothing matched
    # — that's the surprising case worth flagging.
    if (
        claude_sessions
        and not any(s.gui for s in claude_sessions)
        and args.gui_dir.is_dir()
    ):
        n = len(load_gui_metadata(args.gui_dir))
        print(
            f"note: GUI metadata dir {args.gui_dir} has {n} entries but none "
            f"matched a scanned transcript - all sessions shown as 'cli'.",
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
                    "tool": s.tool,
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
