#!/usr/bin/env python3
"""Summarize token usage and cost across local coding-agent sessions.

Supports multiple tools, shown side by side in one table (the **Tool** column):

* **Claude Code** stores each session as a JSONL transcript under
  ``~/.claude/projects/<encoded-project>/<session-id>.jsonl``. Every assistant
  turn carries a ``message.usage`` block; we sum usage per session,
  deduplicating by API message id so a resumed/edited log isn't double-counted.
  Subagents (Task/Agent tool) each get their own transcript under
  ``<session-id>/subagents/agent-*.jsonl`` and often run a different model than
  the base conversation, so they're parsed separately and shown as their own
  indented rows beneath a whole-conversation rollup line.

* **Codex** stores rollout transcripts under
  ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``. Each carries periodic
  ``token_count`` events whose ``total_token_usage`` is *cumulative*, so we
  just read the final running total — no dedup needed.

Each session is priced against the model that produced it and printed in a
table.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    # zero). Standard-tier list prices, USD/MTok (there's also a pricier
    # "priority" tier we don't model), as of 2026-07.
    "gpt-5.5": (2.5, 15.0),
    "gpt-5.5-pro": (15.0, 90.0),
    "gpt-5.4": (1.25, 7.5),
    "gpt-5.4-mini": (0.375, 2.25),
    "gpt-5.4-nano": (0.10, 0.625),
    "gpt-5.4-pro": (15.0, 90.0),
}

# Models we couldn't price (e.g. synthetic ids like "<synthetic>"); recorded so
# we can warn instead of silently treating their cost as zero.
_UNKNOWN_MODELS: set[str] = set()


def price_for(model: str) -> tuple[float, float] | None:
    """Return (input_rate, output_rate) per MTok for a model id, or None."""
    if model in PRICING:
        return PRICING[model]
    # Tolerate dated suffixes like "claude-haiku-4-5-20251001". Try the longest
    # (most specific) known id first so "gpt-5.4-mini-<date>" matches
    # "gpt-5.4-mini" rather than the shorter "gpt-5.4".
    for known in sorted(PRICING, key=len, reverse=True):
        if model.startswith(known):
            return PRICING[known]
    return None


# Explicit short aliases for model ids too long to display comfortably.
_MODEL_ALIASES = {
    "codex-auto-review": "cdx-ar",
}


def short_model(model: str) -> str:
    """Compact a model id for display: claude-opus-4-8 -> opus-4.8.

    Strips the vendor prefix and any trailing date suffix, then renders the
    version components with dots (the first token is the family name). Non-Claude
    ids (e.g. gpt-5.5) are already short and pass through unchanged, except for a
    few overly long ids that get an explicit short alias.
    """
    if model in _MODEL_ALIASES:
        return _MODEL_ALIASES[model]
    if model.startswith("claude-"):
        parts = model[len("claude-"):].split("-")
        # Drop a trailing date suffix like "20251001".
        if parts and len(parts[-1]) == 8 and parts[-1].isdigit():
            parts.pop()
        name, ver = parts[0], ".".join(parts[1:])
        return f"{name}-{ver}" if ver else name
    return model


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


def sum_usage(per_model: dict[str, Usage]) -> Usage:
    """Collapse a per-model usage map into one aggregate Usage."""
    total = Usage()
    for u in per_model.values():
        total.add(u)
    return total


def cost_of(per_model: dict[str, Usage]) -> float:
    """Price a per-model usage map, each slice at its own model's rate."""
    total = 0.0
    for model, u in per_model.items():
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


def primary_model_of(per_model: dict[str, Usage], models: set[str]) -> str:
    """The model that produced the most tokens (used for display/pricing)."""
    if per_model:
        return max(per_model.items(), key=lambda kv: kv[1].total_tokens)[0]
    return next(iter(sorted(models)), "<unknown>")


def _accumulate(usage: dict, u: Usage) -> None:
    """Fold one assistant turn's ``message.usage`` block into ``u``."""
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


@dataclass
class SubAgent:
    """One Task/Agent subagent spawned within a session, with its own model.

    Claude Code stores each subagent's transcript under
    ``<session-id>/subagents/agent-*.jsonl`` next to a ``.meta.json`` sidecar
    holding its ``agentType`` and ``description``.
    """

    agent_type: str = ""
    description: str = ""
    timestamp: str = ""
    effort: str = ""  # reasoning effort, if recorded (Codex subagents)
    models: set[str] = field(default_factory=set)
    per_model: dict[str, Usage] = field(default_factory=dict)

    def usage_for(self, model: str) -> Usage:
        return self.per_model.setdefault(model, Usage())

    @property
    def label(self) -> str:
        """Human-facing name: the subagent's description, falling back to its
        type (e.g. ``Explore``) only when no description was recorded."""
        desc = self.description.strip()
        return desc or self.agent_type or "(subagent)"

    @property
    def usage(self) -> Usage:
        return sum_usage(self.per_model)

    @property
    def cost(self) -> float:
        return cost_of(self.per_model)

    @property
    def primary_model(self) -> str:
        return primary_model_of(self.per_model, self.models)


@dataclass
class Session:
    name: str
    session_id: str
    project: str
    path: Path
    tool: str = "claude"  # which agent produced it: "claude" or "codex"
    gui: bool = False  # opened in the desktop GUI (has claude-code-sessions metadata)
    timestamp: str = ""  # ISO 8601 of the last activity seen (for the Date column)
    effort: str = ""  # reasoning effort, if the tool records one (Codex only)
    # Codex subagent linkage, from the rollout's session_meta (empty otherwise);
    # used to fold a subagent rollout into its parent, then discarded.
    parent_id: str = ""
    agent_name: str = ""
    agent_role: str = ""
    models: set[str] = field(default_factory=set)
    # usage accumulated per model so each slice is priced at its own rate
    per_model: dict[str, Usage] = field(default_factory=dict)
    # subagents spawned within this session, each with its own model/usage
    subagents: list[SubAgent] = field(default_factory=list)

    def usage_for(self, model: str) -> Usage:
        return self.per_model.setdefault(model, Usage())

    @property
    def primary_model(self) -> str:
        """The base conversation's dominant model (used for display/pricing)."""
        return primary_model_of(self.per_model, self.models)

    @property
    def date(self) -> str:
        """Just the YYYY-MM-DD of the last activity, or '' if unknown."""
        return self.timestamp[:10]

    @property
    def usage(self) -> Usage:
        """Base-conversation usage only (excludes subagents)."""
        return sum_usage(self.per_model)

    @property
    def cost(self) -> float:
        """Base-conversation cost only (excludes subagents)."""
        return cost_of(self.per_model)

    # --- whole-conversation rollups (base + every subagent) ---
    @property
    def all_models(self) -> set[str]:
        models = set(self.models)
        for sa in self.subagents:
            models |= sa.models
        return models

    @property
    def total_usage(self) -> Usage:
        total = sum_usage(self.per_model)
        for sa in self.subagents:
            total.add(sa.usage)
        return total

    @property
    def total_cost(self) -> float:
        return cost_of(self.per_model) + sum(sa.cost for sa in self.subagents)


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

            ts = rec.get("timestamp")
            if ts and ts > session.timestamp:
                session.timestamp = ts

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
            _accumulate(usage, session.usage_for(model))
            saw_usage = True

    # Subagents live in a sibling directory named after the session id.
    session.subagents = parse_subagents(path.parent / path.stem / "subagents")
    for sa in session.subagents:
        if sa.timestamp > session.timestamp:
            session.timestamp = sa.timestamp

    if not saw_usage and not session.subagents:
        return None

    session.name = name or "(untitled)"
    return session


def parse_subagents(subagents_dir: Path) -> list[SubAgent]:
    """Parse every ``agent-*.jsonl`` under a session's ``subagents/`` dir."""
    if not subagents_dir.is_dir():
        return []
    result: list[SubAgent] = []
    for jsonl in sorted(subagents_dir.glob("agent-*.jsonl")):
        sa = parse_subagent(jsonl)
        if sa is not None:
            result.append(sa)
    # Default to spawn order (the glob is by opaque agent-id, not time).
    result.sort(key=lambda sa: sa.timestamp)
    return result


def parse_subagent(path: Path) -> SubAgent | None:
    """Parse one subagent transcript (+ its .meta.json), or None if empty.

    The transcript records mirror the base session's assistant turns, so usage
    is summed per model and deduplicated by API message id the same way.
    """
    agent_type = ""
    description = ""
    try:
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        agent_type = meta.get("agentType") or ""
        description = meta.get("description") or ""
    except (OSError, json.JSONDecodeError):
        pass  # a missing/garbled sidecar just means an unnamed subagent

    sa = SubAgent(agent_type=agent_type, description=description)
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

            ts = rec.get("timestamp")
            if ts and ts > sa.timestamp:
                sa.timestamp = ts

            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            usage = msg.get("usage")
            if not usage:
                continue

            mid = msg.get("id")
            if mid:
                if mid in seen_message_ids:
                    continue
                seen_message_ids.add(mid)

            model = msg.get("model") or "<unknown>"
            sa.models.add(model)
            _accumulate(usage, sa.usage_for(model))
            saw_usage = True

    return sa if saw_usage else None


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
    parent_id = ""
    agent_name = ""
    agent_role = ""
    models: list[str] = []
    user_texts: list[str] = []
    timestamp = ""
    effort = ""  # reasoning_effort from the latest turn_context that records one
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

            ts = rec.get("timestamp")
            if ts and ts > timestamp:
                timestamp = ts

            if rtype == "session_meta":
                session_id = payload.get("id") or session_id
                cwd = payload.get("cwd") or ""
                originator = payload.get("originator") or ""
                # A subagent rollout links back to its parent thread and carries
                # a nickname/role; base sessions leave these empty.
                parent_id = payload.get("parent_thread_id") or ""
                agent_name = payload.get("agent_nickname") or ""
                agent_role = payload.get("agent_role") or ""
            elif rtype == "turn_context":
                m = payload.get("model")
                if m:
                    models.append(m)
                # reasoning_effort lives under collaboration_mode.settings and
                # may be null on older sessions; keep the last non-null value.
                settings = (payload.get("collaboration_mode") or {}).get("settings") or {}
                e = settings.get("reasoning_effort")
                if e:
                    effort = e
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
        timestamp=timestamp,
        effort=effort,
        parent_id=parent_id,
        agent_name=agent_name,
        agent_role=agent_role,
        models=set(models) or {model},
    )
    u = session.usage_for(model)
    u.input = max(0, input_total - cached)
    u.cache_read = cached
    u.output = output
    return session


def _codex_subagent(s: Session) -> SubAgent:
    """Convert a parsed subagent rollout into a SubAgent for its parent.

    The subagent's nickname is its display name; a non-default role becomes the
    ``type:`` prefix (so the label renders like "reviewer: Aristotle").
    """
    role = s.agent_role if s.agent_role.lower() not in ("", "default") else ""
    return SubAgent(
        agent_type=role,
        description=s.agent_name,
        timestamp=s.timestamp,
        effort=s.effort,
        models=set(s.models),
        per_model=dict(s.per_model),
    )


def find_codex_sessions(codex_root: Path) -> list[Session]:
    if not codex_root.is_dir():
        return []
    index = load_codex_index(codex_root)
    parsed: list[Session] = []
    for path in sorted(codex_root.glob("**/rollout-*.jsonl")):
        s = parse_codex_rollout(path, index)
        if s is not None:
            parsed.append(s)

    # Fold subagent rollouts into their parent thread. A subagent whose parent
    # wasn't found (filtered out, or missing) stays a top-level row rather than
    # vanishing.
    by_id = {s.session_id: s for s in parsed}
    tops: list[Session] = []
    for s in parsed:
        parent = by_id.get(s.parent_id) if s.parent_id else None
        if parent is not None and parent is not s:
            parent.subagents.append(_codex_subagent(s))
            if s.timestamp > parent.timestamp:
                parent.timestamp = s.timestamp
        else:
            tops.append(s)
    for s in tops:
        s.subagents.sort(key=lambda sa: sa.timestamp)
    return tops


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_compact(n: int) -> str:
    """Human-readable token count for the table: 142, 45.2K, 34.5M.

    Keeps small counts exact and abbreviates thousands/millions so the wide
    cache columns stay scannable. JSON output uses the exact integer instead.
    """
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1_000:.1f}K"
    return f"{n / 1_000_000:.1f}M"


def _usage_json(u: Usage, cost: float) -> dict:
    """Serialize a usage bucket + its cost to the JSON field shape."""
    return {
        "input_tokens": u.input,
        "output_tokens": u.output,
        "cache_read_tokens": u.cache_read,
        "cache_write_tokens": (
            u.cache_write_5m + u.cache_write_1h + u.cache_write_other
        ),
        "total_tokens": u.total_tokens,
        "cost_usd": round(cost, 4),
    }


def _tree_connectors() -> tuple[str, str]:
    """Return the (mid, last) child connectors the current stdout can encode.

    Box-drawing glyphs read best, but a legacy console (e.g. Windows cp1252)
    can't encode them, so fall back to ASCII rather than crash on write.
    """
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "├└─".encode(enc)
        return "├─ ", "└─ "
    except (LookupError, UnicodeError):
        return "|- ", "`- "


def _tree_label(label: str, last: bool) -> str:
    """Prefix a child row's label with a tree connector, then truncate it.

    The connector keeps the base/subagent grouping visible even when color is
    off (piped output), and marks the last child of the conversation.
    """
    mid, end = _tree_connectors()
    return _truncate((end if last else mid) + label, 42)


def _model_cell(primary_model: str, models: set[str], effort: str = "") -> str:
    """Format the Model column: short id, ``+`` if mixed, ``(effort)`` if any."""
    cell = short_model(primary_model) + ("+" if len(models) > 1 else "")
    if effort:
        cell += f" ({effort})"
    return cell


def _usage_cells(
    label: str, model: str, src: str, date: str, u: Usage, cost: float
) -> list[str]:
    """Build one table row from a usage bucket (shared by session/child rows)."""
    # Combine all cache-write buckets into one displayed column.
    cache_write = u.cache_write_5m + u.cache_write_1h + u.cache_write_other
    return [
        label,
        model,
        src,
        date,
        _fmt_compact(u.input),
        _fmt_compact(u.output),
        _fmt_compact(u.cache_read),
        _fmt_compact(cache_write),
        _fmt_compact(u.total_tokens),
        f"${cost:,.2f}",
    ]


def _sort_subagents(subagents: list[SubAgent], sort_key: str) -> list[SubAgent]:
    """Order a session's subagents by the same key as the top-level table.

    ``main`` is emitted separately and always pinned first, so this only orders
    the subagents among themselves. Unknown keys keep the spawn-time default.
    """
    if sort_key == "cost":
        return sorted(subagents, key=lambda sa: sa.cost, reverse=True)
    if sort_key == "tokens":
        return sorted(subagents, key=lambda sa: sa.usage.total_tokens, reverse=True)
    if sort_key == "name":
        return sorted(subagents, key=lambda sa: sa.label.lower())
    if sort_key == "date":
        return sorted(subagents, key=lambda sa: sa.timestamp, reverse=True)
    return subagents


# ---------------------------------------------------------------------------
# Terminal styling
# ---------------------------------------------------------------------------

_RESET = "\033[0m"

# Column layout, referenced when styling individual cells.
_COST_COL = 9
_TOKEN_COLS = (4, 5, 6, 7, 8)  # input, output, cache rd, cache wr, total

# Base SGR parameters per row kind, so the parts of a conversation read at a
# glance: the bold rollup is the whole-conversation headline, cyan is the base
# ("main") agent, and the dimmed rows beneath it are its subagents. The header
# is bold + underlined to sit apart from the body.
_ROW_PARAMS = {
    "header": ["1", "4"],  # bold + underline
    "sep": ["2"],          # dim
    "flat": [],            # a subagent-free conversation: plain default color
    "rollup": ["1"],       # bold  — the whole-conversation total
    "main": ["36"],        # cyan  — the base agent
    "sub": ["2"],          # dim   — an indented subagent
    "total": ["1"],        # bold  — the grand total
}


def _sgr(params: list[str]) -> str:
    """Build an SGR escape from parameters, or "" for no styling."""
    return f"\033[{';'.join(params)}m" if params else ""


def _cost_params(cost: float) -> list[str]:
    """Traffic-light color for a cost cell: cheap → green, pricey → red.

    A true $0.00 (e.g. an unpriced model) is dimmed rather than colored, so it
    reads as "no figure" instead of "cheap".
    """
    if cost <= 0:
        return ["2"]          # dim
    if cost < 5:
        return ["32"]         # green
    if cost < 25:
        return ["33"]         # yellow
    return ["31"]             # red


def _row_styles(kind: str, cells: list[str], cost: float) -> list[str]:
    """Per-cell SGR codes for a body/total row: kind color, plus a cost tint
    and dimmed zeros layered on top of it."""
    base = _ROW_PARAMS[kind]
    bold_kind = kind in ("rollup", "total")
    styles = []
    for i, cell in enumerate(cells):
        if i == _COST_COL:
            # Keep the rollup/total bold so the tinted cost still reads as a total.
            params = _cost_params(cost)
            styles.append(_sgr(["1"] + params if bold_kind else params))
        elif i in _TOKEN_COLS and cell == "0":
            styles.append(_sgr(["2"]))  # dim the always-zero noise (e.g. Codex cache wr)
        else:
            styles.append(_sgr(base))
    return styles


def _enable_windows_ansi() -> None:
    """Turn on ANSI escape processing for the Windows console (no-op elsewhere)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def want_color(mode: str) -> bool:
    """Resolve --color {auto,always,never} against the environment/TTY."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR"):  # https://no-color.org/
        return False
    return sys.stdout.isatty()


def print_table(sessions: list[Session], sort_key: str, color: bool = False) -> None:
    if not sessions:
        print("No sessions with token usage found.")
        return

    if sort_key == "cost":
        sessions = sorted(sessions, key=lambda s: s.total_cost, reverse=True)
    elif sort_key == "tokens":
        sessions = sorted(
            sessions, key=lambda s: s.total_usage.total_tokens, reverse=True
        )
    elif sort_key == "name":
        sessions = sorted(sessions, key=lambda s: s.name.lower())
    elif sort_key == "date":
        sessions = sorted(sessions, key=lambda s: s.timestamp, reverse=True)

    # Each row carries its plain cells plus a parallel list of per-cell SGR
    # codes, so _render can pad on visible width and colorize independently.
    def body_row(kind, cells, cost):
        return (cells, _row_styles(kind, cells, cost))

    rows = []
    grand = Usage()
    grand_cost = 0.0
    for s in sessions:
        grand.add(s.total_usage)
        grand_cost += s.total_cost

        if not s.subagents:
            # Common case: one flat row for the whole (base-only) conversation.
            # A "+" marks a session that mixed models (priced by the dominant one);
            # a trailing "(effort)" shows the reasoning effort when recorded.
            rows.append(body_row("flat", _usage_cells(
                _truncate(s.name, 42),
                _model_cell(s.primary_model, s.models, s.effort),
                "gui" if s.gui else "cli",
                s.date or "-", s.usage, s.cost,
            ), s.cost))
            continue

        # A conversation with subagents: a rollup line for the whole thing,
        # then the base and each subagent indented beneath it. The rollup shows
        # a model only when the whole conversation ran on a single one.
        models = s.all_models
        sum_model = short_model(next(iter(models))) if len(models) == 1 else ""
        rows.append(body_row("rollup", _usage_cells(
            _truncate(s.name, 42), sum_model, "gui" if s.gui else "cli",
            s.date or "-", s.total_usage, s.total_cost,
        ), s.total_cost))
        subs = _sort_subagents(s.subagents, sort_key)
        rows.append(body_row("main", _usage_cells(
            _tree_label("main", last=not subs),
            _model_cell(s.primary_model, s.models, s.effort),
            "", "", s.usage, s.cost,
        ), s.cost))
        for i, sa in enumerate(subs):
            rows.append(body_row("sub", _usage_cells(
                _tree_label(sa.label, last=i == len(subs) - 1),
                _model_cell(sa.primary_model, sa.models, sa.effort),
                "", "", sa.usage, sa.cost,
            ), sa.cost))

    headers = ["Session", "Model", "Src", "Date", "Input", "Output", "Cache rd", "Cache wr", "Total", "Cost"]
    gw = grand.cache_write_5m + grand.cache_write_1h + grand.cache_write_other
    by_tool = Counter(s.tool for s in sessions)
    breakdown = ", ".join(f"{n} {tool}" for tool, n in sorted(by_tool.items()))
    total_cells = [
        f"TOTAL ({len(sessions)} sessions: {breakdown})",
        "",
        "",
        "",
        _fmt_compact(grand.input),
        _fmt_compact(grand.output),
        _fmt_compact(grand.cache_read),
        _fmt_compact(gw),
        _fmt_compact(grand.total_tokens),
        f"${grand_cost:,.2f}",
    ]
    total_row = body_row("total", total_cells, grand_cost)

    _render(headers, rows, total_row, color)
    _print_averages(sessions, grand_cost, color)

    if _UNKNOWN_MODELS:
        print(
            "\nwarning: no pricing for "
            + ", ".join(sorted(_UNKNOWN_MODELS))
            + " - their cost is reported as $0.00.",
            file=sys.stderr,
        )


def _print_averages(sessions: list[Session], grand_cost: float, color: bool) -> None:
    """Print per-session and per-day cost averages beneath the table.

    Two day rates are shown: over *active* days (distinct dates that had a
    session) and over the full *calendar* span (first to last date, idle days
    included) — the two answer "cost on a day I use it" vs "run-rate".
    """
    n = len(sessions)
    if not n:
        return
    dim = _sgr(["2"]) if color else ""
    reset = _RESET if color else ""

    parts = [f"${grand_cost / n:,.2f} per session"]
    dates = sorted(
        dt.date() for s in sessions if (dt := parse_iso(s.timestamp)) is not None
    )
    if dates:
        active = len(set(dates))
        span = (dates[-1] - dates[0]).days + 1
        parts.append(f"${grand_cost / active:,.2f} per active day {dim}({active}){reset}")
        parts.append(f"${grand_cost / span:,.2f} per calendar day {dim}({span}d span){reset}")

    print(f"{dim}Averages{reset}  " + f" {dim}·{reset} ".join(parts))


def _truncate(s: str, width: int) -> str:
    return s if len(s) <= width else s[: width - 3] + "..."


def _render(
    headers: list[str],
    rows: list[tuple[list[str], list[str]]],
    total_row: tuple[list[str], list[str]],
    color: bool = False,
) -> None:
    cols = len(headers)
    widths = [len(h) for h in headers]
    for cells, _ in [*rows, total_row]:
        for i in range(cols):
            widths[i] = max(widths[i], len(cells[i]))

    def fmt(cells: list[str], styles: list[str]) -> str:
        out = []
        for i, cell in enumerate(cells):
            # Left-align the name/model/source/date columns, right-align numbers.
            padded = cell.ljust(widths[i]) if i <= 3 else cell.rjust(widths[i])
            # Pad first, then wrap in the escape, so widths count visible text
            # only and each cell is colored independently of its neighbours.
            code = styles[i] if color else ""
            out.append(f"{code}{padded}{_RESET}" if code else padded)
        return "  ".join(out)

    header_style = _sgr(_ROW_PARAMS["header"]) if color else ""
    sep_style = _sgr(_ROW_PARAMS["sep"]) if color else ""
    sep_cells = ["-" * w for w in widths]

    print(fmt(headers, [header_style] * cols))
    print(fmt(sep_cells, [sep_style] * cols))
    for cells, styles in rows:
        print(fmt(cells, styles))
    print(fmt(sep_cells, [sep_style] * cols))
    total_cells, total_styles = total_row
    print(fmt(total_cells, total_styles))


# ---------------------------------------------------------------------------
# Date filtering (--since)
# ---------------------------------------------------------------------------

# Units expressible as a fixed number of seconds (months are handled separately
# since their length varies). Both short and spelled-out forms are accepted.
_DURATION_SECONDS: dict[str, int] = {
    "h": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}
_MONTH_UNITS = {"mo", "month", "months"}


def parse_iso(ts: str) -> datetime | None:
    """Parse an ISO 8601 timestamp into a timezone-aware datetime, or None.

    Transcript timestamps are UTC with a trailing 'Z'; a naive timestamp (no
    offset) is assumed to be UTC so comparisons against the cutoff are sound.
    """
    if not ts:
        return None
    s = ts.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _subtract_months(dt: datetime, months: int) -> datetime:
    """Go back `months` calendar months, clamping the day to the target month."""
    idx = dt.year * 12 + (dt.month - 1) - months
    year, month = divmod(idx, 12)
    month += 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def parse_since(spec: str, now: datetime) -> datetime:
    """Resolve a --since value to a cutoff datetime (timezone-aware, UTC).

    Accepts a relative duration like '7d', '24h', '2w', or '3mo' (units:
    h/hours, d/days, w/weeks, mo/months) measured back from `now`, or an
    absolute date/datetime such as '2026-06-01' or '2026-06-01T12:00:00'.
    """
    m = re.fullmatch(r"(\d+)\s*([a-zA-Z]+)", spec.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit in _MONTH_UNITS:
            return _subtract_months(now, n)
        if unit in _DURATION_SECONDS:
            return now - timedelta(seconds=n * _DURATION_SECONDS[unit])
        raise argparse.ArgumentTypeError(
            f"unknown duration unit {unit!r} in --since {spec!r}; "
            "use h, d, w, or mo"
        )
    dt = parse_iso(spec)
    if dt is not None:
        return dt
    raise argparse.ArgumentTypeError(
        f"could not parse --since {spec!r}; use a duration like '7d', '24h', "
        "'2w', '3mo', or an absolute date like '2026-06-01'"
    )


def main(argv: list[str] | None = None) -> int:
    # Prefer UTF-8 output so the tree glyphs render on a legacy Windows console;
    # harmless where stdout is already UTF-8 or can't be reconfigured.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

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
        choices=["cost", "tokens", "name", "date"],
        default="cost",
        help=(
            "sort order for the table (default: cost); also orders subagents "
            "within a conversation, with 'main' always pinned first"
        ),
    )
    parser.add_argument(
        "--since",
        metavar="WHEN",
        help=(
            "only include sessions active at or after WHEN: a relative duration "
            "like '7d', '24h', '2w', '3mo' (units h/d/w/mo), or an absolute date "
            "like '2026-06-01'"
        ),
    )
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help=(
            "colorize the table to distinguish rollup / main / subagent rows "
            "(default: auto — on only when writing to a terminal; also honors "
            "NO_COLOR)"
        ),
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

    if args.since is not None:
        try:
            cutoff = parse_since(args.since, datetime.now(timezone.utc))
        except argparse.ArgumentTypeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        before = len(sessions)
        # A session with no parseable timestamp can't be placed in the window,
        # so it's excluded rather than silently kept.
        sessions = [
            s
            for s in sessions
            if (dt := parse_iso(s.timestamp)) is not None and dt >= cutoff
        ]
        print(
            f"note: --since {args.since} -> showing {len(sessions)} of {before} "
            f"sessions active since {cutoff.date()}.",
            file=sys.stderr,
        )

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
            entry = {
                "name": s.name,
                "session_id": s.session_id,
                "project": s.project,
                "tool": s.tool,
                "source": "gui" if s.gui else "cli",
                "date": s.date,
                "timestamp": s.timestamp,
                "primary_model": s.primary_model,
                "models": sorted(s.all_models),
                "effort": s.effort or None,
                # Top-level numbers are the whole conversation (base + subagents).
                **_usage_json(s.total_usage, s.total_cost),
                # Broken out so callers can attribute cost to base vs subagents.
                "base": _usage_json(s.usage, s.cost),
                "subagents": [
                    {
                        "agent_type": sa.agent_type,
                        "description": sa.description,
                        "primary_model": sa.primary_model,
                        "models": sorted(sa.models),
                        "effort": sa.effort or None,
                        **_usage_json(sa.usage, sa.cost),
                    }
                    for sa in s.subagents
                ],
            }
            out.append(entry)
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        if _UNKNOWN_MODELS:
            print(
                "warning: no pricing for " + ", ".join(sorted(_UNKNOWN_MODELS)),
                file=sys.stderr,
            )
        return 0

    color = want_color(args.color)
    if color:
        _enable_windows_ansi()
    print_table(sessions, args.sort, color)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
