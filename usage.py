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
  indented rows beneath a whole-conversation rollup line. A subagent can spawn
  further subagents; the on-disk layout stays flat, so the tree is rebuilt from
  each sidecar's spawning ``toolUseId`` and rendered nested to any depth.

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
    # Sonnet 5 has a dated price bump ($2/$10 through 2026-08-31, then $3/$15);
    # we price it at the higher, going-forward rate.
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-0": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-3-5": (0.80, 4.0),
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


# Bare family names (no version) show up in the logs when a session records the
# alias the user selected — e.g. "opus"/"fable" from fast mode — rather than the
# resolved id. Map each to the latest known version of that family so those turns
# are priced instead of silently dropped to $0.
_FAMILY_LATEST = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5",
    "mythos": "claude-mythos-5",
}


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
    if model in _FAMILY_LATEST:
        return PRICING[_FAMILY_LATEST[model]]
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
    holding its ``agentType``, ``description``, and the ``toolUseId`` of the
    spawning call. Subagents can themselves spawn subagents; a child is nested
    under whichever agent *emitted* the ``tool_use`` whose id matches its
    ``tool_use_id`` (see :func:`nest_subagents`). ``usage``/``cost`` here are the
    agent's *own* work only — its ``children`` are separate rows/entries.
    """

    agent_type: str = ""
    description: str = ""
    timestamp: str = ""
    effort: str = ""  # reasoning effort, if recorded (Codex subagents)
    agent_id: str = ""  # opaque id from the agent-<id>.jsonl filename
    tool_use_id: str = ""  # the spawning tool_use's id; links this to its parent
    spawn_depth: int = 0  # 1 = spawned by the base, 2 = by a depth-1 subagent, ...
    # tool_use ids this agent emitted (its own Task/Agent spawns), used to find
    # which subagents are its children.
    emitted_tool_use_ids: set[str] = field(default_factory=set)
    models: set[str] = field(default_factory=set)
    per_model: dict[str, Usage] = field(default_factory=dict)
    # subagents this one spawned, nested to arbitrary depth
    children: list["SubAgent"] = field(default_factory=list)

    def usage_for(self, model: str) -> Usage:
        return self.per_model.setdefault(model, Usage())

    def iter_tree(self):
        """Yield this subagent, then every descendant (depth-first)."""
        yield self
        for child in self.children:
            yield from child.iter_tree()

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
class Segment:
    """One *context lifetime* of a base conversation, delimited by a
    ``compact_boundary``. A session that never compacted has a single segment
    (not surfaced); each ``/compact`` or auto-compaction starts a new one.

    ``peak_tokens``/``trigger`` come from the boundary that *ended* this segment
    (its ``preTokens`` and ``manual``/``auto`` trigger); the final, still-live
    segment has neither. These are context-window occupancy, a different figure
    from the per-turn billed usage summed into ``per_model``.
    """

    index: int = 0
    peak_tokens: int = 0  # preTokens of the terminating boundary; 0 if still live
    trigger: str = ""     # "manual"/"auto" of that boundary; "" if still live
    models: set[str] = field(default_factory=set)
    per_model: dict[str, Usage] = field(default_factory=dict)

    def usage_for(self, model: str) -> Usage:
        return self.per_model.setdefault(model, Usage())

    @property
    def label(self) -> str:
        """``context 2 (→168K)`` for a compacted slice; ``context 3 (live)``
        for the final one. The arrow marks the peak occupancy it rolled over at."""
        if self.peak_tokens:
            return f"context {self.index + 1} (→{_fmt_compact(self.peak_tokens)})"
        return f"context {self.index + 1} (live)"

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
    # context lifetimes split at compaction boundaries; empty unless the base
    # conversation compacted at least once (i.e. has 2+ non-empty segments)
    segments: list[Segment] = field(default_factory=list)

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

    @property
    def all_subagents(self) -> list["SubAgent"]:
        """Every subagent in the tree, flattened (depth-first), for accounting."""
        out: list[SubAgent] = []
        for sa in self.subagents:
            out.extend(sa.iter_tree())
        return out

    # --- whole-conversation rollups (base + every subagent, nested or not) ---
    @property
    def all_models(self) -> set[str]:
        models = set(self.models)
        for sa in self.all_subagents:
            models |= sa.models
        return models

    @property
    def total_usage(self) -> Usage:
        total = sum_usage(self.per_model)
        for sa in self.all_subagents:
            total.add(sa.usage)
        return total

    @property
    def total_cost(self) -> float:
        return cost_of(self.per_model) + sum(sa.cost for sa in self.all_subagents)


def parse_session(path: Path) -> Session | None:
    """Parse one .jsonl transcript into a Session, or None if it has no usage."""
    session_id = path.stem
    project = path.parent.name
    name = ""

    session = Session(name="", session_id=session_id, project=project, path=path)
    seen_message_ids: set[str] = set()
    saw_usage = False
    # Assistant turns are folded into the current context lifetime; a
    # compact_boundary closes it (stamping its peak/trigger) and opens the next.
    segments: list[Segment] = [Segment()]

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

            # A compaction closes the current context lifetime and starts a new
            # one. Stamp the slice we're leaving with the boundary's occupancy
            # (preTokens) and trigger, then open the next segment.
            if rtype == "system" and rec.get("subtype") == "compact_boundary":
                meta = rec.get("compactMetadata") or {}
                segments[-1].peak_tokens = int(meta.get("preTokens") or 0)
                segments[-1].trigger = meta.get("trigger") or ""
                segments.append(Segment(index=len(segments)))
                continue

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
            # A "<synthetic>" turn is a locally-generated placeholder (e.g.
            # "No response requested.") with all-zero usage, not a real API
            # model — skip it so it doesn't register as a spurious extra model.
            if model == "<synthetic>":
                continue
            session.models.add(model)
            _accumulate(usage, session.usage_for(model))
            seg = segments[-1]
            seg.models.add(model)
            _accumulate(usage, seg.usage_for(model))
            saw_usage = True

    # Surface the split only when the base actually compacted. Drop empty
    # slices (e.g. a trailing boundary with no turns after it), then renumber
    # so labels read context 1..N; a lone remaining segment is just the whole
    # base and needs no breakdown.
    non_empty = [s for s in segments if s.usage.total_tokens > 0]
    if len(non_empty) >= 2:
        for i, s in enumerate(non_empty):
            s.index = i
        session.segments = non_empty

    # Subagents live in a sibling directory named after the session id. They're
    # parsed flat, then reorganized into a tree (a subagent may spawn its own).
    flat_subagents = parse_subagents(path.parent / path.stem / "subagents")
    for sa in flat_subagents:
        if sa.timestamp > session.timestamp:
            session.timestamp = sa.timestamp
    session.subagents = nest_subagents(flat_subagents)

    if not saw_usage and not flat_subagents:
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


def _collect_tool_use_ids(msg: dict, into: set[str]) -> None:
    """Add the id of every ``tool_use`` content block in ``msg`` to ``into``.

    A single assistant message is split across one JSONL line per content block
    (all sharing the message id), so this must run on *every* line — not just the
    first-seen id — or spawns emitted on later lines would be missed. Only real
    ``tool_use`` blocks count; text that merely mentions a ``<tool-use-id>`` does
    not, which is what keeps a child linked to its true parent.
    """
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            bid = block.get("id")
            if bid:
                into.add(bid)


def parse_subagent(path: Path) -> SubAgent | None:
    """Parse one subagent transcript (+ its .meta.json), or None if empty.

    The transcript records mirror the base session's assistant turns, so usage
    is summed per model and deduplicated by API message id the same way.
    """
    agent_type = ""
    description = ""
    tool_use_id = ""
    spawn_depth = 0
    try:
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        agent_type = meta.get("agentType") or ""
        description = meta.get("description") or ""
        tool_use_id = meta.get("toolUseId") or ""
        spawn_depth = int(meta.get("spawnDepth") or 0)
    except (OSError, json.JSONDecodeError):
        pass  # a missing/garbled sidecar just means an unnamed, unlinked subagent

    agent_id = path.stem
    if agent_id.startswith("agent-"):
        agent_id = agent_id[len("agent-"):]

    sa = SubAgent(
        agent_type=agent_type,
        description=description,
        agent_id=agent_id,
        tool_use_id=tool_use_id,
        spawn_depth=spawn_depth,
    )
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
            # Record spawn calls before the dedup below skips repeat lines.
            _collect_tool_use_ids(msg, sa.emitted_tool_use_ids)

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


def nest_subagents(flat: list[SubAgent]) -> list[SubAgent]:
    """Turn a flat list of subagents into a forest by spawn lineage.

    Each subagent's ``tool_use_id`` is the id of the ``tool_use`` that spawned
    it. Whichever agent *emitted* that id is its parent; a subagent nests under
    that parent's ``children``. Anything spawned by the base conversation (or
    whose parent transcript is missing) stays at the top level. The returned list
    is the top-level subagents; ``children`` are populated in place.
    """
    owner: dict[str, SubAgent] = {}
    for sa in flat:
        for tid in sa.emitted_tool_use_ids:
            owner[tid] = sa

    top: list[SubAgent] = []
    for sa in flat:
        parent = owner.get(sa.tool_use_id) if sa.tool_use_id else None
        if parent is not None and parent is not sa:
            parent.children.append(sa)
        else:
            top.append(sa)
    return top


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

    # Fold subagent rollouts into their parent thread, preserving nesting (a
    # subagent can spawn its own). A rollout is a subagent when it links to a
    # parent we actually parsed; convert each such rollout to a SubAgent once,
    # then wire the forest. A subagent whose parent wasn't found (filtered out,
    # or missing) stays a top-level row rather than vanishing.
    by_id = {s.session_id: s for s in parsed}

    def is_sub(s: Session) -> bool:
        return bool(s.parent_id) and s.parent_id in by_id and by_id[s.parent_id] is not s

    sa_of = {s.session_id: _codex_subagent(s) for s in parsed if is_sub(s)}

    tops: list[Session] = []
    for s in parsed:
        sa = sa_of.get(s.session_id)
        if sa is None:
            tops.append(s)  # a base thread (or an orphan whose parent is gone)
            continue
        parent_sa = sa_of.get(s.parent_id)
        if parent_sa is not None:
            parent_sa.children.append(sa)  # nest under a subagent parent
        else:
            by_id[s.parent_id].subagents.append(sa)  # attach to a base thread

    # Bubble each subtree's latest activity up to its root for the Date column.
    for s in tops:
        for sa in s.all_subagents:
            if sa.timestamp > s.timestamp:
                s.timestamp = sa.timestamp
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


def _per_model_json(per_model: dict[str, Usage]) -> list[dict]:
    """Break an agent's usage out by model, each slice priced at its own rate.

    Always present (even for a single-model agent, as a one-element list) so a
    consumer can attribute cost per model without re-deriving the split. The
    sum of these slices equals the agent's own usage/cost figure.
    """
    return [
        {"model": model, **_usage_json(u, cost_of({model: u}))}
        for model, u in per_model.items()
    ]


def _tree_connectors() -> tuple[str, str, str]:
    """Return the (mid, last, vert) tree glyphs the current stdout can encode.

    ``vert`` is the continuation prefix for a deeper level (segments nested
    under ``main``). Box-drawing glyphs read best, but a legacy console (e.g.
    Windows cp1252) can't encode them, so fall back to ASCII rather than crash.
    """
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "├└│─".encode(enc)
        return "├─ ", "└─ ", "│  "
    except (LookupError, UnicodeError):
        return "|- ", "`- ", "|  "


def _tree_label(label: str, last: bool, indent: str = "") -> str:
    """Prefix a child row's label with a tree connector, then truncate it.

    The connector keeps the grouping visible even when color is off (piped
    output), and marks the last child. ``indent`` nests a row one level deeper
    (e.g. a context segment beneath ``main``).
    """
    mid, end, _ = _tree_connectors()
    return _truncate(indent + (end if last else mid) + label, 42)


def _model_cell(primary_model: str, models: set[str], effort: str = "") -> str:
    """Format the Model column: short id, ``+`` if mixed, ``(effort)`` if any."""
    cell = short_model(primary_model) + ("+" if len(models) > 1 else "")
    if effort:
        cell += f" ({effort})"
    return cell


def _agg_model_cell(
    primary_model: str, models: set[str], per_model: dict[str, Usage], effort: str = ""
) -> str:
    """Model cell for an aggregate row (main / subagent / segment).

    When the agent's usage actually splits across >1 model it gets a per-model
    breakdown beneath it, so the aggregate row leaves the Model column blank —
    the same way the whole-conversation rollup line does — rather than naming one
    model with a ``+``. A ``+`` still appears for the Codex case of several models
    all attributed to one dominant slice (a single ``per_model`` entry, no
    breakdown), where naming that model is the only signal available.
    """
    if len(per_model) > 1:
        return f"({effort})" if effort else ""
    return _model_cell(primary_model, models, effort)


def _usage_cells(
    date: str, label: str, model: str, u: Usage, cost: float
) -> list[str]:
    """Build one table row from a usage bucket (shared by session/child rows)."""
    # Combine all cache-write buckets into one displayed column.
    cache_write = u.cache_write_5m + u.cache_write_1h + u.cache_write_other
    return [
        date,
        label,
        model,
        _fmt_compact(u.input),
        _fmt_compact(u.output),
        _fmt_compact(u.cache_read),
        _fmt_compact(cache_write),
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


def _sort_per_model(
    per_model: dict[str, Usage], sort_key: str
) -> list[tuple[str, Usage]]:
    """Order an agent's per-model slices for its breakdown rows.

    Models carry no timestamp, so ``date`` falls back to cost (like the default);
    ``name`` orders by the displayed short id, ``tokens`` by each slice's size.
    """
    items = list(per_model.items())
    if sort_key == "tokens":
        return sorted(items, key=lambda kv: kv[1].total_tokens, reverse=True)
    if sort_key == "name":
        return sorted(items, key=lambda kv: short_model(kv[0]))
    return sorted(items, key=lambda kv: cost_of({kv[0]: kv[1]}), reverse=True)


# ---------------------------------------------------------------------------
# Terminal styling
# ---------------------------------------------------------------------------

_RESET = "\033[0m"

# Column layout, referenced when styling individual cells.
_COST_COL = 7
_TOKEN_COLS = (3, 4, 5, 6)  # input, output, cache rd, cache wr

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
    "seg": ["2", "36"],    # dim cyan — a context lifetime of the base agent
    "model": ["2", "35"],  # dim magenta — one model's slice of a mixed agent
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

    _, _, vert = _tree_connectors()
    blank = " " * len(vert)

    def pm_child_rows(entity, child_prefix, siblings_after=0):
        # Break a mixed-model agent's *own* usage into one row per model, each
        # priced at its own rate, so a within-agent model switch (e.g. an opus
        # agent that stalled and resumed on fable) is visible and auditable
        # instead of hiding behind the aggregate row's "+". ``siblings_after``
        # is the count of other children at this level still to be emitted (e.g.
        # a subagent's nested subagents), so the last-child marker is correct.
        items = _sort_per_model(entity.per_model, sort_key)
        for k, (model, u) in enumerate(items):
            c = cost_of({model: u})
            last = siblings_after == 0 and k == len(items) - 1
            rows.append(body_row("model", _usage_cells(
                "", _tree_label(short_model(model), last=last, indent=child_prefix),
                short_model(model), u, c,
            ), c))

    def seg_rows(segs, indent):
        # Context lifetimes always read chronologically (context 1..N), never
        # reordered by --sort: a later context above an earlier one is nonsense.
        for j, sg in enumerate(segs):
            last_seg = j == len(segs) - 1
            rows.append(body_row("seg", _usage_cells(
                "", _tree_label(sg.label, last=last_seg, indent=indent),
                _agg_model_cell(sg.primary_model, sg.models, sg.per_model),
                sg.usage, sg.cost,
            ), sg.cost))
            # A segment that itself mixed models breaks down one level deeper.
            if len(sg.per_model) > 1:
                deeper = indent + (blank if last_seg else vert)
                pm_child_rows(sg, deeper)

    def emit_subagent(sa, prefix, last):
        # One row for this subagent's own usage, then its per-model breakdown (if
        # mixed) and its spawned children nested a level deeper. The prefix carries
        # the ancestor guide lines so the tree stays legible at any depth; children
        # follow the same --sort order.
        rows.append(body_row("sub", _usage_cells(
            "", _tree_label(sa.label, last=last, indent=prefix),
            _agg_model_cell(sa.primary_model, sa.models, sa.per_model, sa.effort),
            sa.usage, sa.cost,
        ), sa.cost))
        kids = _sort_subagents(sa.children, sort_key)
        child_prefix = prefix + (blank if last else vert)
        # Per-model rows precede the nested subagents at the same level, so they
        # count as non-last whenever this agent also spawned children.
        if len(sa.per_model) > 1:
            pm_child_rows(sa, child_prefix, siblings_after=len(kids))
        for i, k in enumerate(kids):
            emit_subagent(k, child_prefix, i == len(kids) - 1)

    rows = []
    grand = Usage()
    grand_cost = 0.0
    for s in sessions:
        grand.add(s.total_usage)
        grand_cost += s.total_cost

        segs = s.segments  # non-empty only when the base compacted (2+ slices)
        base_multi = len(s.per_model) > 1  # base itself switched models
        if not s.subagents and not segs and not base_multi:
            # Common case: one flat row for the whole (base-only, single-model)
            # conversation. A trailing "(effort)" shows the reasoning effort when
            # recorded.
            rows.append(body_row("flat", _usage_cells(
                s.date or "-",
                _truncate(s.name, 42),
                _model_cell(s.primary_model, s.models, s.effort),
                s.usage, s.cost,
            ), s.cost))
            continue

        # An expanded conversation (subagents, compaction segments, and/or a base
        # that mixed models): a rollup line for the whole thing, then its parts
        # indented beneath. The rollup shows a model only when the whole
        # conversation ran on one.
        models = s.all_models
        sum_model = short_model(next(iter(models))) if len(models) == 1 else ""
        rows.append(body_row("rollup", _usage_cells(
            s.date or "-", _truncate(s.name, 42), sum_model,
            s.total_usage, s.total_cost,
        ), s.total_cost))

        if s.subagents:
            # The base is its own "main" row; beneath it come its context segments
            # (if any) or — failing that — its own per-model breakdown when it
            # mixed models. The subagent forest follows at the base level, each
            # subagent's spawned children nested beneath it.
            subs = _sort_subagents(s.subagents, sort_key)
            rows.append(body_row("main", _usage_cells(
                "", _tree_label("main", last=False),
                _agg_model_cell(s.primary_model, s.models, s.per_model, s.effort),
                s.usage, s.cost,
            ), s.cost))
            if segs:
                seg_rows(segs, vert)
            elif base_multi:
                pm_child_rows(s, vert)
            for i, sa in enumerate(subs):
                emit_subagent(sa, "", i == len(subs) - 1)
        elif segs:
            # No subagents: the rollup *is* the base, so its context segments
            # (each further split by model if mixed) hang directly off it.
            seg_rows(segs, "")
        else:
            # Base-only but mixed models: break the rollup down by model.
            pm_child_rows(s, "")

    headers = ["Date", "Session", "Model", "Input", "Output", "Cache rd", "Cache wr", "Cost"]
    gw = grand.cache_write_5m + grand.cache_write_1h + grand.cache_write_other
    total_cells = [
        "TOTAL",
        "",
        "",
        _fmt_compact(grand.input),
        _fmt_compact(grand.output),
        _fmt_compact(grand.cache_read),
        _fmt_compact(gw),
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

    by_tool = Counter(s.tool for s in sessions)
    breakdown = ", ".join(f"{c} {tool}" for tool, c in sorted(by_tool.items()))
    parts = [f"${grand_cost / n:,.2f} per session {dim}({n}: {breakdown}){reset}"]
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
            # Left-align the date/name/model columns, right-align numbers.
            padded = cell.ljust(widths[i]) if i <= 2 else cell.rjust(widths[i])
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
            f"matched a scanned transcript - all sessions treated as 'cli'.",
            file=sys.stderr,
        )

    def _subagent_json(sa: SubAgent) -> dict:
        # Own usage/cost only; the whole subtree is captured via nested children.
        return {
            "agent_type": sa.agent_type,
            "description": sa.description,
            "agent_id": sa.agent_id or None,
            "spawn_depth": sa.spawn_depth or None,
            "primary_model": sa.primary_model,
            "models": sorted(sa.models),
            "effort": sa.effort or None,
            **_usage_json(sa.usage, sa.cost),
            # Own usage split by model (mixed only when >1 entry).
            "per_model": _per_model_json(sa.per_model),
            "children": [_subagent_json(c) for c in sa.children],
        }

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
                # Broken out so callers can attribute cost to base vs subagents;
                # per_model splits the base's own usage by model (each priced at
                # its own rate), surfacing any within-base model switch.
                "base": {
                    **_usage_json(s.usage, s.cost),
                    "per_model": _per_model_json(s.per_model),
                },
                # Top-level subagents only; each nests its own spawned children.
                "subagents": [_subagent_json(sa) for sa in s.subagents],
                # Base conversation split at compaction boundaries (empty unless
                # it compacted). peak_tokens/trigger describe the boundary that
                # ended each slice; these sum to "base", not to the top-level.
                "segments": [
                    {
                        "index": sg.index,
                        "peak_tokens": sg.peak_tokens or None,
                        "trigger": sg.trigger or None,
                        "primary_model": sg.primary_model,
                        "models": sorted(sg.models),
                        **_usage_json(sg.usage, sg.cost),
                        "per_model": _per_model_json(sg.per_model),
                    }
                    for sg in s.segments
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
