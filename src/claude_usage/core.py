"""Parse and price local coding-agent usage for the web dashboard.

Supports Claude Code and Codex sessions:

* **Claude Code** stores each session as a JSONL transcript under
  ``~/.claude/projects/<encoded-project>/<session-id>.jsonl``. Every assistant
  turn carries a ``message.usage`` block; we sum usage per session,
  deduplicating by API message id so a resumed/edited log isn't double-counted.
  Subagents (Task/Agent tool) each get their own transcript under
  ``<session-id>/subagents/agent-*.jsonl`` and often run a different model than
  the base conversation, so they're parsed separately. A subagent can spawn
  further subagents; the on-disk layout stays flat, so the tree is rebuilt from
  each sidecar's spawning ``toolUseId``.

* **Codex** stores rollout transcripts under
  ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``. Each carries periodic
  ``token_count`` events whose ``total_token_usage`` is *cumulative*, so we
  just read the final running total — no dedup needed.

Each session is priced against the model that produced it.
"""

from __future__ import annotations

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
    "claude-opus-5": (5.0, 25.0),
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
    # OpenAI / Codex standard-tier list prices, USD/MTok, as of 2026-07.
    # Cached input is 10% of the input rate (== CACHE_READ_MULT). GPT-5.6 also
    # bills cache writes at 1.25x input; the generic Usage formula already does
    # that if a transcript begins reporting cache-write tokens.
    "gpt-5.6": (5.0, 30.0),  # alias for GPT-5.6 Sol
    "gpt-5.6-sol": (5.0, 30.0),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.2, 1.2),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.5-pro": (30.0, 180.0),
    "gpt-5.4": (2.5, 15.0),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-pro": (30.0, 180.0),
}

# ---------------------------------------------------------------------------
# Priority ("Fast") service tier.
#
# Codex can send a turn on OpenAI's *priority* tier, which its own model
# metadata calls "Fast" ("1.5x speed, increased usage"); a thread opts in via
# ``service_tier = "priority"`` in ~/.codex/config.toml. OpenAI prices that tier
# as a flat per-model multiple of the standard rate, applied alike to input,
# cached input, and output — so the cache multipliers above still hold and one
# factor per model is all we need.
#
# Verified against OpenAI's pricing docs (2026-07): the gpt-5.6 family and
# gpt-5.4 double, while gpt-5.5 is the odd one out at 2.5x
# ($5/$30 -> $12.50/$75). Anything unlisted falls back to 2x; the ``-pro``
# variants are unverified and gpt-5.4-nano is offered on the standard tier only,
# so a priority flag on it could only be a logging artifact and must not inflate
# its cost.
# ---------------------------------------------------------------------------
PRIORITY_TIER = "priority"
PRIORITY_MULT_DEFAULT = 2.0
PRIORITY_MULT: dict[str, float] = {
    "gpt-5.5": 2.5,
    "gpt-5.4-nano": 1.0,  # no priority tier offered
}

# Models we couldn't price (e.g. synthetic ids like "<synthetic>"); recorded so
# we can warn instead of silently treating their cost as zero.
_UNKNOWN_MODELS: set[str] = set()

# Models whose rollouts are dropped from the dashboard entirely. Codex runs an
# automatic post-turn review pass as its own thread under the model
# "codex-auto-review"; it's machine-internal, unpriced, and just noise here, so
# a rollout resolved to one of these is skipped rather than shown as a $0.00 row.
_IGNORED_MODELS: set[str] = {"codex-auto-review"}


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


def _pricing_key(model: str) -> str | None:
    """The :data:`PRICING` entry a model id resolves to, or None if unpriced.

    Kept separate from :func:`price_for` because the resolved key — not the id as
    logged — is what :data:`PRIORITY_MULT` is keyed on.
    """
    if model in PRICING:
        return model
    # Tolerate dated suffixes like "claude-haiku-4-5-20251001". Try the longest
    # (most specific) known id first so "gpt-5.4-mini-<date>" matches
    # "gpt-5.4-mini" rather than the shorter "gpt-5.4". The prefix test matters:
    # without it "gpt-5.5-<date>" slices at the right *length* for same-length
    # entries and resolves to whichever sorts first ("gpt-5.6"), silently pricing
    # one model as another.
    for known in sorted(PRICING, key=len, reverse=True):
        if not model.startswith(known):
            continue
        suffix = model[len(known) :]
        if re.fullmatch(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})", suffix):
            return known
    return _FAMILY_LATEST.get(model)


def price_for(model: str, *, priority: bool = False) -> tuple[float, float] | None:
    """Return (input_rate, output_rate) per MTok for a model id, or None.

    ``priority`` prices the turn on the priority ("Fast") service tier, scaling
    both rates by the model's :data:`PRIORITY_MULT` factor.
    """
    key = _pricing_key(model)
    if key is None:
        return None
    in_rate, out_rate = PRICING[key]
    if priority:
        mult = PRIORITY_MULT.get(key, PRIORITY_MULT_DEFAULT)
        return in_rate * mult, out_rate * mult
    return in_rate, out_rate


# Explicit short aliases for model ids too long to display comfortably. Empty
# for now (codex-auto-review, the former sole entry, is dropped upstream via
# _IGNORED_MODELS), but kept as the hook for any future unwieldy id.
_MODEL_ALIASES: dict[str, str] = {}


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
        parts = model[len("claude-") :].split("-")
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

    def minus(self, other: "Usage") -> "Usage":
        """Field-wise difference, clamped at zero.

        Used to recover the standard-tier slice of a worker whose usage partly
        billed on the fast tier, where only the total and the fast subset are
        stored.
        """
        return Usage(
            input=max(0, self.input - other.input),
            output=max(0, self.output - other.output),
            cache_read=max(0, self.cache_read - other.cache_read),
            cache_write_5m=max(0, self.cache_write_5m - other.cache_write_5m),
            cache_write_1h=max(0, self.cache_write_1h - other.cache_write_1h),
            cache_write_other=max(0, self.cache_write_other - other.cache_write_other),
        )

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


def cost_of(per_model: dict[str, Usage], *, priority: bool = False) -> float:
    """Price a per-model usage map, each slice at its own model's rate.

    ``priority`` bills the whole map on the priority ("Fast") tier. It's a
    per-worker flag rather than a per-slice one because Codex records the tier
    per *thread*, not per request.
    """
    total = 0.0
    for model, u in per_model.items():
        rates = price_for(model, priority=priority)
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


def cost_of_tiers(
    per_model: dict[str, Usage], priority_per_model: dict[str, Usage]
) -> float:
    """Price a worker whose usage split across service tiers.

    ``per_model`` is the worker's total and ``priority_per_model`` the fast-tier
    subset of it, so the standard-tier slice is their difference. A Codex thread
    can toggle fast mode between turns, so both halves can be non-empty at once;
    Claude workers always pass an empty subset and price entirely at standard.
    """
    if not priority_per_model:
        return cost_of(per_model)
    standard = {
        model: usage.minus(priority_per_model.get(model, Usage()))
        for model, usage in per_model.items()
    }
    return cost_of(standard) + cost_of(priority_per_model, priority=True)


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


def _context_tokens_from_usage(usage: dict) -> int:
    """Approximate context occupancy for one model response."""

    return sum(
        int(usage.get(key) or 0)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    )


@dataclass
class Turn:
    """One billed assistant response, kept until ownership is settled.

    A Claude transcript is aggregated in two steps because the same API response
    can appear in more than one file: forking or resuming a conversation writes
    its history into a *new* transcript. Turns are collected here first, then
    folded into per-model and per-segment totals once
    :func:`resolve_replays` has decided which transcript produced each one.
    """

    message_id: str  # API message.id; "" when the record carried none
    model: str
    usage: dict  # the raw message.usage block
    segment: int  # index into Session.parsed_segments
    written_by: str  # the record's own sessionId, which a replay leaves intact


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
    service_tier: str = ""  # OpenAI service tier, if recorded (Codex subagents)
    agent_id: str = ""  # opaque id from the agent-<id>.jsonl filename
    tool_use_id: str = ""  # the spawning tool_use's id; links this to its parent
    spawn_depth: int = 0  # 1 = spawned by the base, 2 = by a depth-1 subagent, ...
    # tool_use ids this agent emitted (its own Task/Agent spawns), used to find
    # which subagents are its children.
    emitted_tool_use_ids: set[str] = field(default_factory=set)
    models: set[str] = field(default_factory=set)
    per_model: dict[str, Usage] = field(default_factory=dict)
    # The fast-tier subset of per_model (see cost_of_tiers).
    priority_per_model: dict[str, Usage] = field(default_factory=dict)
    segments: list["Segment"] = field(default_factory=list)
    context_used_tokens: int = 0
    peak_context_tokens: int = 0
    context_window_tokens: int = 0
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
    def priority(self) -> bool:
        """True when any of this agent's usage billed on the fast tier."""
        return any(u.total_tokens for u in self.priority_per_model.values())

    @property
    def usage(self) -> Usage:
        return sum_usage(self.per_model)

    @property
    def priority_usage(self) -> Usage:
        return sum_usage(self.priority_per_model)

    @property
    def cost(self) -> float:
        return cost_of_tiers(self.per_model, self.priority_per_model)

    @property
    def primary_model(self) -> str:
        return primary_model_of(self.per_model, self.models)


@dataclass
class Segment:
    """One *context lifetime* of a base conversation, delimited by a
    compaction record. A session that never compacted has a single segment (not
    surfaced); each manual or automatic compaction starts a new one.

    Claude's ``compact_boundary`` supplies ``preTokens`` and a ``manual``/``auto``
    trigger. Codex supplies cumulative usage snapshots and per-turn context
    occupancy around its ``compacted`` record. The final segment has no trigger.
    These are context-window occupancy, a different figure from the per-turn
    billed usage summed into ``per_model``.
    """

    index: int = 0
    peak_tokens: int = 0  # preTokens of the terminating boundary; 0 if still live
    trigger: str = ""  # "manual"/"auto" of that boundary; "" if still live
    models: set[str] = field(default_factory=set)
    per_model: dict[str, Usage] = field(default_factory=dict)
    # The fast-tier subset of per_model (see cost_of_tiers). Accumulated per turn
    # within this slice, so a slice that spans a fast-mode toggle splits too.
    priority_per_model: dict[str, Usage] = field(default_factory=dict)
    context_tokens: int = 0

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
    def priority(self) -> bool:
        """True when any of this slice's usage billed on the fast tier."""
        return any(u.total_tokens for u in self.priority_per_model.values())

    @property
    def usage(self) -> Usage:
        return sum_usage(self.per_model)

    @property
    def cost(self) -> float:
        return cost_of_tiers(self.per_model, self.priority_per_model)

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
    # OpenAI service tier the thread ran on: "priority" (Codex's "Fast" mode),
    # "default", or "" when the rollout predates the record. Codex only; Claude
    # transcripts report "standard" on every turn and have no fast-mode marker.
    service_tier: str = ""
    # Codex subagent linkage, from the rollout's session_meta (empty otherwise);
    # used to fold a subagent rollout into its parent, then discarded.
    parent_id: str = ""
    agent_name: str = ""
    agent_role: str = ""
    models: set[str] = field(default_factory=set)
    # usage accumulated per model so each slice is priced at its own rate
    per_model: dict[str, Usage] = field(default_factory=dict)
    # The fast-tier subset of per_model (see cost_of_tiers).
    priority_per_model: dict[str, Usage] = field(default_factory=dict)
    # subagents spawned within this session, each with its own model/usage
    subagents: list[SubAgent] = field(default_factory=list)
    # context lifetimes split at compaction boundaries; empty unless the base
    # conversation compacted at least once (i.e. has 2+ non-empty segments)
    segments: list[Segment] = field(default_factory=list)
    # Every context lifetime as parsed, including those `segments` hides. Kept so
    # the totals can be recomputed after replayed turns are excluded.
    parsed_segments: list[Segment] = field(default_factory=list)
    context_used_tokens: int = 0
    peak_context_tokens: int = 0
    context_window_tokens: int = 0
    # --- Claude replay bookkeeping (see resolve_replays) ---
    # Billed responses, retained until ownership across transcripts is settled.
    turns: list[Turn] = field(default_factory=list)
    # When this transcript was opened, from its own `queue-operation` records:
    # replayed history keeps its original timestamps, so this is what tells an
    # original from the copy that replays it.
    opened_at: str = ""
    # The session id stamped on a verbatim replayed prefix, if any. A fork copies
    # its parent's records as they were, so they still name the parent.
    verbatim_parent_id: str = ""
    # Resolved by resolve_replays: where this session's replayed history was
    # credited, why it was replayed ("fork"/"resume"), and how much was excluded.
    replayed_from_id: str = ""
    replayed_from_name: str = ""
    replay_kind: str = ""
    replayed_tokens: int = 0

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
    def priority(self) -> bool:
        """True when any of the base conversation's usage billed on the fast tier.

        A thread can toggle fast mode mid-run, so this is "used fast mode at all",
        not "is currently set to fast" — that's :attr:`service_tier`.
        """
        return any(u.total_tokens for u in self.priority_per_model.values())

    @property
    def usage(self) -> Usage:
        """Base-conversation usage only (excludes subagents)."""
        return sum_usage(self.per_model)

    @property
    def priority_usage(self) -> Usage:
        """The base conversation's fast-tier usage only."""
        return sum_usage(self.priority_per_model)

    @property
    def cost(self) -> float:
        """Base-conversation cost only (excludes subagents)."""
        return cost_of_tiers(self.per_model, self.priority_per_model)

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
    def total_priority_usage(self) -> Usage:
        """Fast-tier usage across the whole conversation (base + subagents).

        The rollup counterpart to :attr:`priority_usage`, so it sits beside
        :attr:`total_usage` rather than mixing a base-only figure into a row whose
        other numbers cover everything.
        """
        total = sum_usage(self.priority_per_model)
        for sa in self.all_subagents:
            total.add(sa.priority_usage)
        return total

    @property
    def total_cost(self) -> float:
        # Each subagent prices on its own tier split: a forked child inherits the
        # parent's tier in practice, but nothing guarantees it.
        return cost_of_tiers(self.per_model, self.priority_per_model) + sum(
            sa.cost for sa in self.all_subagents
        )

    @property
    def total_context_used_tokens(self) -> int:
        return self.context_used_tokens + sum(
            sa.context_used_tokens for sa in self.all_subagents
        )

    @property
    def total_peak_context_tokens(self) -> int:
        return max(
            [
                self.peak_context_tokens,
                *(sa.peak_context_tokens for sa in self.all_subagents),
            ]
        )


def _finalize_contexts(
    entity: Session | SubAgent,
    segments: list[Segment],
    *,
    keep_empty: bool = False,
) -> None:
    """Attach aggregate per-context occupancy to a worker."""

    for segment in segments:
        segment.context_tokens = max(segment.context_tokens, segment.peak_tokens)

    # A lifetime with no billed request of its own describes context this worker
    # never occupied: a fork or resumed session opens on the boundary record of
    # the compaction its *parent* ran, whose preTokens belong to that parent's
    # row. Only the live tail is exempt — a slice that just compacted has a real,
    # freshly reset occupancy to report before its first request lands.
    tail = segments[-1] if segments else None
    counted = [s for s in segments if s.usage.total_tokens or s is tail]
    entity.context_used_tokens = sum(s.context_tokens for s in counted)
    entity.peak_context_tokens = max((s.context_tokens for s in counted), default=0)
    visible = (
        segments if keep_empty else [s for s in segments if s.usage.total_tokens > 0]
    )
    if len(visible) >= 2:
        for index, segment in enumerate(visible):
            segment.index = index
        entity.segments = visible


# A transcript's ``user`` records carry the tool-result payloads and are about
# half the bytes on disk, while contributing nothing to the dashboard but a
# timestamp. Decoding them is the single largest cost of a scan, so a cheap
# prefix test skips them without paying ``json.loads``.
#
# Bounds are measured, with headroom: across a 133MB corpus the nested
# ``"role":"assistant"`` marker never appeared past offset 271, and a top-level
# ``type`` never past 146.
_HEAD_BYTES = 512

# The value of the first ``"type":"..."`` in the head, for records we ignore
# wholesale, written with its closing quote so a prefix test matches the whole
# value. Only types that *are* genuinely top-level at that position belong here:
# an ``attachment`` exposes a nested content type first (e.g. ``task_reminder``),
# so it is deliberately absent and gets decoded normally. Kept as a tuple for
# ``str.startswith``, which takes one directly and stays in C.
_SKIP_TYPES = ('user"',)


def _prompt_name(prompt: str) -> str:
    """Best-effort session name from a user prompt, or "" if it has no text.

    Read from a ``last-prompt`` record rather than the ``user`` turns themselves,
    which :func:`_skippable` deliberately never decodes. That field holds only
    what the user typed, so unlike the Codex path there are no injected
    instruction blocks to skip past.
    """
    for line in prompt.splitlines():
        line = line.strip()
        if line:
            return _truncate(line, 60)
    return ""


def _skippable(line: str) -> bool:
    """True when ``line`` is positively identified as a record we ignore.

    Only a *positive* identification skips. An assistant turn is recognized by
    its nested ``"role":"assistant"`` and always decoded; a user turn by its
    leading top-level ``type``. Anything unclassifiable from the head falls
    through to a full decode, so an unfamiliar record shape costs a little speed
    and never accuracy.

    Takes the raw line (no ``strip()``) so a skipped record never pays for a
    copy of its payload; a line with leading whitespace simply fails to match
    and is decoded.
    """
    head = line[:_HEAD_BYTES]
    if '"role":"assistant"' in head:
        return False
    i = head.find('"type":"')
    if i < 0:
        return False
    return head.startswith(_SKIP_TYPES, i + 8)


def _fold_skipped_timestamp(line: str | None, current: str) -> str:
    """Fold a skipped record's timestamp into a running max.

    Called once per file with the *last* line :func:`_skippable` skipped.
    Transcripts are written in chronological order, so the final record carries
    the file's newest timestamp — the one thing a skipped record can still
    contribute (the Date column and ``--since`` both read it). Decoding one line
    per file recovers it exactly, instead of scanning every skipped line for a
    key that can sit hundreds of kilobytes in.
    """
    if not line:
        return current
    try:
        ts = json.loads(line).get("timestamp")
    except json.JSONDecodeError:
        return current
    return ts if ts and ts > current else current


def aggregate_session(
    session: Session, exclude: set[str] | frozenset[str] = frozenset()
) -> None:
    """Fold a session's turns into its per-model and per-segment totals.

    ``exclude`` names message ids another transcript is credited with, so calling
    this again after :func:`resolve_replays` recomputes every figure from the
    turns that remain. Aggregates are rebuilt rather than adjusted because
    per-lifetime occupancy is a maximum, which cannot be walked back.
    """
    session.models.clear()
    session.per_model.clear()
    for segment in session.parsed_segments:
        segment.models.clear()
        segment.per_model.clear()
        segment.context_tokens = 0
    replayed = Usage()

    for turn in session.turns:
        if turn.message_id and turn.message_id in exclude:
            _accumulate(turn.usage, replayed)
            continue
        session.models.add(turn.model)
        _accumulate(turn.usage, session.usage_for(turn.model))
        segment = session.parsed_segments[turn.segment]
        segment.models.add(turn.model)
        _accumulate(turn.usage, segment.usage_for(turn.model))
        segment.context_tokens = max(
            segment.context_tokens, _context_tokens_from_usage(turn.usage)
        )

    session.replayed_tokens = replayed.total_tokens
    session.segments = []
    _finalize_contexts(session, session.parsed_segments)


def resolve_replays(sessions: list[Session]) -> None:
    """Credit each billed response to the transcript that produced it.

    Claude Code writes a conversation's history into a *new* transcript when it
    is forked, and again when a session is resumed after compaction, so one API
    response can sit in several files. Deduping within a file (see
    :func:`parse_session`) cannot see across them, so every copy would be billed
    again — and a copied ``compact_boundary`` would add the parent's context
    lifetime to the copy's Context total as well.

    Ownership is decided per message id:

    * A fork copies its parent's records verbatim, so each one still names the
      parent in its own ``sessionId``. Only transcripts that recorded the
      response under their *own* id can claim it.
    * A resume re-serializes the history under the new session id, leaving
      nothing in the record to tell copy from original — so the transcript
      opened first is taken to be the one that produced the response.

    A response no surviving transcript claims (its producer was deleted) stays
    with whichever copy opened first, so its tokens are still reported once.
    """
    claude = [s for s in sessions if s.tool == "claude"]
    holders: dict[str, list[Session]] = {}
    claimants: dict[str, list[Session]] = {}
    for session in claude:
        for turn in session.turns:
            if not turn.message_id:
                continue
            holders.setdefault(turn.message_id, []).append(session)
            if turn.written_by == session.session_id:
                claimants.setdefault(turn.message_id, []).append(session)

    excluded: dict[str, set[str]] = {}
    credited: dict[str, Counter] = {}
    for message_id, shared in holders.items():
        if len(shared) < 2:
            continue
        keeper = min(
            claimants.get(message_id) or shared,
            key=lambda s: (s.opened_at, s.session_id),
        )
        for session in shared:
            if session is keeper:
                continue
            excluded.setdefault(session.session_id, set()).add(message_id)
            credited.setdefault(session.session_id, Counter())[keeper.session_id] += 1

    names = {s.session_id: s.name for s in claude}
    for session in claude:
        drop = excluded.get(session.session_id)
        if not drop:
            continue
        aggregate_session(session, drop)
        parent, _ = credited[session.session_id].most_common(1)[0]
        session.replayed_from_id = parent
        session.replayed_from_name = names.get(parent, "")
        # A verbatim prefix is how the app writes a fork; a rewritten one is what
        # resuming a session produces. Callers with the desktop app's metadata can
        # still upgrade a "resume" it recorded a fork parent for.
        session.replay_kind = "fork" if session.verbatim_parent_id else "resume"


def parse_session(path: Path) -> Session | None:
    """Parse one .jsonl transcript into a Session, or None if it has no usage."""
    session_id = path.stem
    project = path.parent.name
    # Name candidates, resolved after the scan: records arrive in write order,
    # not precedence order, so each tier is collected separately.
    custom_title = ""  # what Claude Code writes today
    ai_title = ""  # older transcripts
    summary_title = ""  # older transcripts
    prompt_title = ""  # last resort

    session = Session(name="", session_id=session_id, project=project, path=path)
    seen_message_ids: set[str] = set()
    # Assistant turns are folded into the current context lifetime; a
    # compact_boundary closes it (stamping its peak/trigger) and opens the next.
    segments: list[Segment] = [Segment()]
    last_skipped: str | None = None
    # Earliest timestamp on one of this transcript's own records, preferring a
    # `queue-operation` — those are written as the file is opened, while replayed
    # history keeps the timestamps it had in the transcript it came from.
    opened_at = ""
    first_own_record = ""

    try:
        fh = path.open(encoding="utf-8")
    except OSError as exc:
        print(f"warning: cannot open {path}: {exc}", file=sys.stderr)
        return None

    with fh:
        for line in fh:
            if _skippable(line):
                last_skipped = line
                continue
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

            # Records copied in by a fork still name the transcript they were
            # written to, which both dates this file and identifies the parent.
            written_by = rec.get("sessionId") or ""
            if written_by and written_by != session_id:
                if not session.verbatim_parent_id:
                    session.verbatim_parent_id = written_by
            elif written_by and ts:
                if rtype == "queue-operation":
                    if not opened_at or ts < opened_at:
                        opened_at = ts
                elif not first_own_record or ts < first_own_record:
                    first_own_record = ts

            # Session name candidates. A title can be rewritten mid-session, so
            # the newest title record wins; `summary` and the opening prompt are
            # single facts about the session, so the first one wins.
            if rtype == "custom-title":
                custom_title = rec.get("customTitle") or custom_title
            elif rtype == "ai-title":
                ai_title = rec.get("aiTitle") or ai_title
            elif rtype == "summary" and not summary_title:
                summary_title = rec.get("summary") or ""
            elif rtype == "last-prompt" and not prompt_title:
                prompt_title = _prompt_name(rec.get("lastPrompt") or "")

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
            session.turns.append(
                Turn(
                    message_id=mid or "",
                    model=model,
                    usage=usage,
                    segment=len(segments) - 1,
                    written_by=written_by,
                )
            )

    session.timestamp = _fold_skipped_timestamp(last_skipped, session.timestamp)
    session.opened_at = opened_at or first_own_record
    session.parsed_segments = segments
    aggregate_session(session)

    # Subagents live in a sibling directory named after the session id. They're
    # parsed flat, then reorganized into a tree (a subagent may spawn its own).
    flat_subagents = parse_subagents(path.parent / path.stem / "subagents")
    for sa in flat_subagents:
        if sa.timestamp > session.timestamp:
            session.timestamp = sa.timestamp
    session.subagents = nest_subagents(flat_subagents)

    if not session.turns and not flat_subagents:
        return None

    session.name = (
        custom_title or ai_title or summary_title or prompt_title or "(untitled)"
    )
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
        agent_id = agent_id[len("agent-") :]

    sa = SubAgent(
        agent_type=agent_type,
        description=description,
        agent_id=agent_id,
        tool_use_id=tool_use_id,
        spawn_depth=spawn_depth,
    )
    seen_message_ids: set[str] = set()
    saw_usage = False
    segments: list[Segment] = [Segment()]
    last_skipped: str | None = None

    try:
        fh = path.open(encoding="utf-8")
    except OSError as exc:
        print(f"warning: cannot open {path}: {exc}", file=sys.stderr)
        return None

    with fh:
        for line in fh:
            if _skippable(line):
                last_skipped = line
                continue
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

            if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
                meta = rec.get("compactMetadata") or {}
                segments[-1].peak_tokens = int(meta.get("preTokens") or 0)
                segments[-1].trigger = meta.get("trigger") or ""
                segments.append(Segment(index=len(segments)))
                continue

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
            if model == "<synthetic>":
                continue
            sa.models.add(model)
            _accumulate(usage, sa.usage_for(model))
            segment = segments[-1]
            segment.models.add(model)
            _accumulate(usage, segment.usage_for(model))
            segment.context_tokens = max(
                segment.context_tokens, _context_tokens_from_usage(usage)
            )
            saw_usage = True

    sa.timestamp = _fold_skipped_timestamp(last_skipped, sa.timestamp)
    _finalize_contexts(sa, segments)
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
    resolve_replays(sessions)
    _apply_gui_forks(sessions, gui_meta)
    return sessions


def _apply_gui_forks(sessions: list[Session], gui_meta: dict[str, dict]) -> None:
    """Name a fork's parent from the desktop app's ``forkedFromSessionId``.

    The app records the fork outright, which :func:`resolve_replays` can only
    infer, and it survives the case that hides the evidence: a fork that was
    later resumed writes a *third* transcript whose replayed prefix carries the
    new session id, leaving it looking like a plain resume.

    Fork parentage is recorded between the app's own session ids, so it resolves
    through the CLI transcript each of those currently points at.
    """
    cli_ids = {
        rec["sessionId"]: cli for cli, rec in gui_meta.items() if rec.get("sessionId")
    }
    titles = {
        rec["sessionId"]: rec.get("title") or ""
        for rec in gui_meta.values()
        if rec.get("sessionId")
    }
    names = {s.session_id: s.name for s in sessions}
    for session in sessions:
        forked_from = (gui_meta.get(session.session_id) or {}).get(
            "forkedFromSessionId"
        )
        if not forked_from:
            continue
        session.replay_kind = "fork"
        if session.replayed_from_id:
            continue
        # Nothing of this transcript's own history was replayed elsewhere, so
        # point at the parent conversation the app recorded.
        parent = cli_ids.get(forked_from, "")
        session.replayed_from_id = parent
        session.replayed_from_name = names.get(parent) or titles.get(forked_from, "")


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


def _codex_spawn_metadata(payload: dict) -> dict:
    """Return nested subagent spawn metadata when ``source`` is structured."""
    source = payload.get("source")
    if not isinstance(source, dict):
        return {}
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return {}
    spawn = subagent.get("thread_spawn")
    return spawn if isinstance(spawn, dict) else {}


def _codex_agent_name(payload: dict) -> str:
    """Prefer a subagent's stable task path over its random nickname."""
    spawn = _codex_spawn_metadata(payload)
    agent_path = payload.get("agent_path") or spawn.get("agent_path") or ""
    if agent_path:
        leaf = agent_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        label = leaf.replace("_", " ").strip()
        if label:
            return label[:1].upper() + label[1:]
    return payload.get("agent_nickname") or spawn.get("agent_nickname") or ""


def _codex_usage_delta(total: dict, baseline: dict | None = None) -> Usage:
    """Convert two cumulative Codex counters into one billed usage slice."""
    baseline = baseline or {}

    def delta(key: str) -> int:
        return max(0, int(total.get(key) or 0) - int(baseline.get(key) or 0))

    input_total = delta("input_tokens")
    cached = min(delta("cached_input_tokens"), input_total)
    return Usage(
        input=input_total - cached,
        cache_read=cached,
        output=delta("output_tokens"),
    )


def _attribute_by_tier(
    entity: Session | Segment, model: str, by_tier: dict[bool, Usage]
) -> None:
    """Record a tier-split usage bundle on a worker under one model.

    ``per_model`` gets the combined total (what the token columns report) and
    ``priority_per_model`` the fast-tier half of it, which is what makes the two
    halves price at different rates. The fast entry is omitted when empty so
    ``priority`` stays false for the ordinary standard-tier case.
    """
    combined = entity.usage_for(model)
    combined.add(by_tier[False])
    combined.add(by_tier[True])
    if by_tier[True].total_tokens:
        entity.priority_per_model[model] = by_tier[True]


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
    # service_tier from the latest thread_settings_applied that records one. Codex
    # re-emits the whole settings block whenever it applies them, so this ends up
    # as the tier the thread is *currently* set to — which is not necessarily the
    # tier its earlier turns billed on (see turn_priority below).
    service_tier = ""
    # token_count.total_token_usage is cumulative; keep the largest seen, which
    # doubles as the baseline each new turn's usage is measured against.
    best_total = 0
    best_usage: dict | None = None
    # Billed usage accumulated per turn and split by service tier, keyed by
    # "did this turn bill fast". Fast mode can be toggled mid-thread, so the split
    # has to be recorded as the turns go by — a single after-the-fact tier could
    # only ever be right for some of them.
    #
    # Fast mode applies to a whole stream, and a stream is never split here:
    # Codex emits thread_settings_applied *before* the turn it governs (in the
    # logs it's followed by that turn's turn_context within milliseconds), so a
    # toggle always lands between turns and each turn bills wholly on the tier
    # that was in effect when it ran.
    total_by_tier: dict[bool, Usage] = {False: Usage(), True: Usage()}
    segment_by_tier: dict[bool, Usage] = {False: Usage(), True: Usage()}
    peak_context_tokens = 0
    context_window_tokens = 0
    saw_session_meta = False
    # A forked subagent rollout starts with a verbatim copy of its parent's
    # history.  Codex marks the end of that replay with the structured
    # inter-agent trigger for the child's first turn.  Keep the copied
    # cumulative counter only as the child's billing baseline; none of the
    # replayed models, occupancy, or compaction boundaries belong to the child.
    replaying_fork = False
    fork_baseline_usage: dict | None = None
    pending_turn_context: dict | None = None
    pending_thread_settings: dict | None = None
    # Codex keeps billed usage cumulative across compaction, so each context
    # lifetime's own usage is the per-turn deltas booked while it was live, held
    # here with that lifetime's peak occupancy and the models it ran.
    segments_acc: list[tuple[dict[bool, Usage], int, set[str]]] = []
    segment_peak = 0
    segment_models: set[str] = set()

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
            if rtype == "session_meta" and saw_session_meta:
                continue
            ts = rec.get("timestamp")
            if ts and ts > timestamp:
                timestamp = ts

            # A forked subagent rollout can replay its parent's session_meta
            # later in the file. The first record identifies the rollout;
            # accepting a replay would erase the child linkage and surface the
            # subagent as a duplicate top-level thread.
            if rtype == "session_meta":
                saw_session_meta = True
                session_id = payload.get("id") or session_id
                cwd = payload.get("cwd") or ""
                originator = payload.get("originator") or ""
                # A subagent rollout links back to its parent thread and carries
                # a task path/nickname and role; base sessions leave these empty.
                spawn = _codex_spawn_metadata(payload)
                parent_id = (
                    payload.get("parent_thread_id")
                    or spawn.get("parent_thread_id")
                    or ""
                )
                agent_name = _codex_agent_name(payload)
                agent_role = payload.get("agent_role") or spawn.get("agent_role") or ""
                replaying_fork = bool(parent_id and payload.get("forked_from_id"))
            elif replaying_fork:
                if rtype == "turn_context":
                    # The child's own turn_context immediately precedes its
                    # inter-agent trigger, so the last one in the replay is the
                    # context to retain once the copied prefix ends.
                    pending_turn_context = payload
                elif (
                    rtype == "event_msg"
                    and payload.get("type") == "thread_settings_applied"
                ):
                    # Same story as turn_context: the replay carries the parent's
                    # settings, then the child's own just before the trigger.
                    pending_thread_settings = payload.get("thread_settings") or {}
                elif rtype == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    total = info.get("total_token_usage") or {}
                    if int(total.get("total_tokens") or 0) >= int(
                        (fork_baseline_usage or {}).get("total_tokens") or 0
                    ):
                        fork_baseline_usage = dict(total)
                elif rtype == "inter_agent_communication_metadata" and payload.get(
                    "trigger_turn"
                ):
                    replaying_fork = False
                    tier = (pending_thread_settings or {}).get("service_tier")
                    if tier:
                        service_tier = tier
                    payload = pending_turn_context or {}
                    m = payload.get("model")
                    if m:
                        models.append(m)
                        segment_models.add(m)
                    settings = (payload.get("collaboration_mode") or {}).get(
                        "settings"
                    ) or {}
                    e = settings.get("reasoning_effort")
                    if e:
                        effort = e
            elif rtype == "turn_context":
                m = payload.get("model")
                if m:
                    models.append(m)
                    segment_models.add(m)
                # reasoning_effort lives under collaboration_mode.settings and
                # may be null on older sessions; keep the last non-null value.
                settings = (payload.get("collaboration_mode") or {}).get(
                    "settings"
                ) or {}
                e = settings.get("reasoning_effort")
                if e:
                    effort = e
            elif (
                rtype == "event_msg"
                and payload.get("type") == "thread_settings_applied"
            ):
                # Codex's "Fast" mode is OpenAI's priority tier; it bills at a
                # multiple of the standard rate (see PRIORITY_MULT). The key can be
                # null on threads that never had a tier (e.g. codex-auto-review),
                # so only a real value overwrites.
                # This record precedes the turn it governs, so it changes the tier
                # for subsequent turns and leaves already-booked ones alone.
                tier = (payload.get("thread_settings") or {}).get("service_tier")
                if tier:
                    service_tier = tier
            elif rtype == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                tot = info.get("total_token_usage") or {}
                last = info.get("last_token_usage") or {}
                peak_context_tokens = max(
                    peak_context_tokens, int(last.get("total_tokens") or 0)
                )
                segment_peak = max(segment_peak, int(last.get("total_tokens") or 0))
                context_window_tokens = max(
                    context_window_tokens, int(info.get("model_context_window") or 0)
                )
                t = int(tot.get("total_tokens") or 0)
                if t >= best_total:
                    # This snapshot advances the cumulative counter, so its gain
                    # over the last accepted one is the turn that just finished.
                    # Book it against the tier that turn ran on before moving the
                    # baseline; a snapshot that doesn't advance is a replay of one
                    # already booked, and the turn stays in flight.
                    baseline = (
                        best_usage if best_usage is not None else fork_baseline_usage
                    )
                    turn_usage = _codex_usage_delta(tot, baseline)
                    fast = service_tier == PRIORITY_TIER
                    total_by_tier[fast].add(turn_usage)
                    segment_by_tier[fast].add(turn_usage)
                    best_total, best_usage = t, tot
            elif rtype == "compacted" and best_usage:
                segments_acc.append(
                    (segment_by_tier, segment_peak, set(segment_models))
                )
                segment_by_tier = {False: Usage(), True: Usage()}
                segment_peak = 0
                segment_models = set()
            elif rtype == "response_item" and payload.get("role") == "user":
                for c in payload.get("content") or []:
                    if isinstance(c, dict) and c.get("type") in ("input_text", "text"):
                        user_texts.append(c.get("text") or "")

    if not best_usage or best_total == 0:
        return None  # no recorded usage (e.g. local models that don't report it)

    # Pick the model the session mostly ran on for pricing.
    if models:
        counts = Counter(models)
        last_seen = {model: index for index, model in enumerate(models)}
        # Frequency is the best attribution available for a cumulative counter.
        # Prefer the latest model on a tie so the result is deterministic and
        # reflects the model active when the final usage was recorded.
        model = max(counts, key=lambda item: (counts[item], last_seen[item]))
    else:
        model = "<unknown>"

    # Drop machine-internal passes (e.g. codex-auto-review) outright: they're
    # unpriced noise, and dropping the rollout here also keeps the model out of
    # any parent's model set and silences the "no pricing" warning.
    if model in _IGNORED_MODELS:
        return None

    session = Session(
        name=index.get(session_id) or _codex_fallback_name(user_texts),
        session_id=session_id,
        project=cwd,
        path=path,
        tool="codex",
        gui="desktop" in originator.lower(),
        timestamp=timestamp,
        effort=effort,
        service_tier=service_tier,
        parent_id=parent_id,
        agent_name=agent_name,
        agent_role=agent_role,
        models=set(models) or {model},
        context_used_tokens=peak_context_tokens,
        peak_context_tokens=peak_context_tokens,
        context_window_tokens=context_window_tokens,
    )
    # The cumulative counter isn't split per model, so all of it is attributed to
    # the dominant model — but it *is* split per tier, which is what pricing needs.
    _attribute_by_tier(session, model, total_by_tier)

    if segments_acc:
        segments_acc.append((segment_by_tier, segment_peak, set(segment_models)))
        segments: list[Segment] = []
        for by_tier, peak, seen_models in segments_acc:
            segment = Segment(
                index=len(segments),
                peak_tokens=peak if len(segments) < len(segments_acc) - 1 else 0,
                models=seen_models or {model},
                context_tokens=peak,
            )
            _attribute_by_tier(segment, model, by_tier)
            segments.append(segment)
        # Keep the new live slice visible even if no billable request has
        # completed since compaction; its reset occupancy is still meaningful.
        _finalize_contexts(session, segments, keep_empty=True)
    return session


def _codex_subagent(s: Session) -> SubAgent:
    """Convert a parsed subagent rollout into a SubAgent for its parent.

    The subagent's task name is its display name, falling back to its nickname;
    a non-default role becomes the ``type:`` prefix (so the label renders like
    "reviewer: Aristotle").
    """
    role = s.agent_role if s.agent_role.lower() not in ("", "default") else ""
    return SubAgent(
        agent_type=role,
        description=s.agent_name,
        timestamp=s.timestamp,
        effort=s.effort,
        service_tier=s.service_tier,
        models=set(s.models),
        per_model=dict(s.per_model),
        priority_per_model=dict(s.priority_per_model),
        segments=list(s.segments),
        context_used_tokens=s.context_used_tokens,
        peak_context_tokens=s.peak_context_tokens,
        context_window_tokens=s.context_window_tokens,
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
        return (
            bool(s.parent_id) and s.parent_id in by_id and by_id[s.parent_id] is not s
        )

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


def _fmt_compact(n: int) -> str:
    """Human-readable token count for context labels: 142, 45.2K, 34.5M."""
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1_000:.1f}K"
    return f"{n / 1_000_000:.1f}M"


def fast_slice(model: str, priority_per_model: dict[str, Usage]) -> dict[str, Usage]:
    """One model's entry from a fast-tier map, or an empty map if it has none."""
    usage = priority_per_model.get(model)
    return {model: usage} if usage is not None else {}


def _truncate(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 3] + "..."


# ---------------------------------------------------------------------------
# Date filtering
# ---------------------------------------------------------------------------

# Units expressible as a fixed number of seconds (months are handled separately
# since their length varies). Both short and spelled-out forms are accepted.
_DURATION_SECONDS: dict[str, int] = {
    "h": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
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
    """Resolve a dashboard date filter to a timezone-aware UTC cutoff.

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
        raise ValueError(
            f"unknown duration unit {unit!r} in {spec!r}; use h, d, w, or mo"
        )
    dt = parse_iso(spec)
    if dt is not None:
        return dt
    raise ValueError(
        f"could not parse {spec!r}; use a duration like '7d', '24h', "
        "'2w', '3mo', or an absolute date like '2026-06-01'"
    )
