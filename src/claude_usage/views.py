"""Dashboard response views and cached-query helpers for usage sessions."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from . import core


SORT_KEYS = {
    "cost",
    "tokens",
    "name",
    "date",
    "context",
}
SUBAGENT_SORT_KEYS = {"cost", "tokens", "name", "date"}


def usage_view(usage: core.Usage, cost: float) -> dict[str, Any]:
    return {
        "input_tokens": usage.input,
        "output_tokens": usage.output,
        "cache_read_tokens": usage.cache_read,
        "cache_write_tokens": (
            usage.cache_write_5m + usage.cache_write_1h + usage.cache_write_other
        ),
        "total_tokens": usage.total_tokens,
        "cost_usd": cost,
    }


def _context(entity: Any, *, rollup: bool = False) -> dict[str, Any]:
    if rollup:
        used = entity.total_context_used_tokens
        peak = entity.total_peak_context_tokens
        windows = [
            entity.context_window_tokens,
            *(agent.context_window_tokens for agent in entity.all_subagents),
        ]
        window = max(windows, default=0)
    else:
        used = getattr(entity, "context_used_tokens", 0)
        peak = getattr(entity, "peak_context_tokens", 0)
        window = getattr(entity, "context_window_tokens", 0)
    return {
        "context_used_tokens": used or None,
        "peak_context_tokens": peak or None,
        "context_window_tokens": window or None,
    }


def _models_view(
    per_model: dict[str, core.Usage],
    priority_per_model: dict[str, core.Usage] | None = None,
) -> list[dict[str, Any]]:
    # Each slice carries its share of the worker's fast-tier usage, so the slices
    # still sum to its cost when it toggled fast mode part-way through.
    fast = priority_per_model or {}
    return [
        {
            "model": model,
            "display_model": core.short_model(model),
            **usage_view(
                usage,
                core.cost_of_tiers({model: usage}, core.fast_slice(model, fast)),
            ),
        }
        for model, usage in sorted(per_model.items())
    ]


def _sort_subagents(
    subagents: Iterable[core.SubAgent], sort: str
) -> list[core.SubAgent]:
    if sort not in SUBAGENT_SORT_KEYS:
        raise ValueError(f"unsupported subagent sort key: {sort}")
    keys = {
        "cost": lambda agent: agent.cost,
        "tokens": lambda agent: agent.usage.total_tokens,
        "name": lambda agent: agent.label.casefold(),
        "date": lambda agent: agent.timestamp,
    }
    return sorted(
        subagents,
        key=keys[sort],
        reverse=sort in {"cost", "tokens", "date"},
    )


def agent_view(agent: core.SubAgent, *, subagent_sort: str = "cost") -> dict[str, Any]:
    return {
        "kind": "agent",
        "label": agent.label,
        "agent_type": agent.agent_type or None,
        "agent_id": agent.agent_id or None,
        "primary_model": agent.primary_model,
        "display_model": core.short_model(agent.primary_model),
        "models": sorted(agent.models),
        "effort": agent.effort or None,
        "service_tier": agent.service_tier or None,
        "priority_tokens": agent.priority_usage.total_tokens or None,
        **_context(agent),
        **usage_view(agent.usage, agent.cost),
        "per_model": _models_view(agent.per_model, agent.priority_per_model),
        "segments": [segment_view(segment) for segment in agent.segments],
        "children": [
            agent_view(child, subagent_sort=subagent_sort)
            for child in _sort_subagents(agent.children, subagent_sort)
        ],
    }


def segment_view(segment: core.Segment) -> dict[str, Any]:
    return {
        "kind": "segment",
        "label": segment.label,
        "index": segment.index,
        "peak_tokens": segment.peak_tokens or None,
        "trigger": segment.trigger or None,
        "primary_model": segment.primary_model,
        "display_model": core.short_model(segment.primary_model),
        "models": sorted(segment.models),
        "context_used_tokens": segment.context_tokens or None,
        "peak_context_tokens": segment.context_tokens or None,
        "context_window_tokens": None,
        "priority_tokens": core.sum_usage(segment.priority_per_model).total_tokens
        or None,
        **usage_view(segment.usage, segment.cost),
        "per_model": _models_view(segment.per_model, segment.priority_per_model),
    }


def session_summary(session: core.Session) -> dict[str, Any]:
    return {
        "name": session.name,
        "session_id": session.session_id,
        "project": session.project,
        "tool": session.tool,
        "source": "gui" if session.gui else "cli",
        "date": session.date,
        "timestamp": session.timestamp,
        "primary_model": session.primary_model,
        "display_model": core.short_model(session.primary_model),
        "models": sorted(session.all_models),
        "effort": session.effort or None,
        # "priority" is Codex's "Fast" mode, billed above the standard rate. This
        # is the tier the thread is currently set to; a thread that toggled it
        # mid-run has usage on both, so "priority_tokens" is what explains cost.
        "service_tier": session.service_tier or None,
        # A rollup, like the usage figures below it — the base's own share is
        # under "base" in the detail view.
        "priority_tokens": session.total_priority_usage.total_tokens or None,
        "subagent_count": len(session.all_subagents),
        **_context(session, rollup=True),
        **usage_view(session.total_usage, session.total_cost),
    }


def session_detail(
    session: core.Session, *, subagent_sort: str = "cost"
) -> dict[str, Any]:
    return {
        **session_summary(session),
        "base": {
            "kind": "main",
            "label": "main",
            "primary_model": session.primary_model,
            "display_model": core.short_model(session.primary_model),
            "models": sorted(session.models),
            "effort": session.effort or None,
            "service_tier": session.service_tier or None,
            "priority_tokens": session.priority_usage.total_tokens or None,
            **_context(session),
            **usage_view(session.usage, session.cost),
            "per_model": _models_view(session.per_model, session.priority_per_model),
            "segments": [segment_view(segment) for segment in session.segments],
        },
        "subagents": [
            agent_view(agent, subagent_sort=subagent_sort)
            for agent in _sort_subagents(session.subagents, subagent_sort)
        ],
    }


def filter_sessions(
    sessions: Iterable[core.Session],
    *,
    since: str | None = None,
    tool: str | None = None,
    query: str | None = None,
) -> list[core.Session]:
    result = list(sessions)
    if since and since != "all":
        cutoff = core.parse_since(since, datetime.now(timezone.utc))
        result = [
            session
            for session in result
            if (stamp := core.parse_iso(session.timestamp)) is not None
            and stamp >= cutoff
        ]
    if tool and tool != "all":
        result = [session for session in result if session.tool == tool]
    if query:
        needle = query.casefold()
        result = [
            session
            for session in result
            if needle
            in " ".join(
                [
                    session.name,
                    session.primary_model,
                    *session.all_models,
                ]
            ).casefold()
        ]
    return result


def sort_sessions(
    sessions: Iterable[core.Session], sort: str, order: str
) -> list[core.Session]:
    if sort not in SORT_KEYS:
        raise ValueError(f"unsupported sort key: {sort}")
    reverse = order == "desc"

    keys = {
        "cost": lambda s: s.total_cost,
        "tokens": lambda s: s.total_usage.total_tokens,
        "name": lambda s: s.name.casefold(),
        "date": lambda s: s.timestamp,
        "context": lambda s: s.total_context_used_tokens,
    }
    # Python's stable sort makes the identity key a deterministic tie-breaker.
    result = sorted(sessions, key=lambda s: (s.tool, s.session_id))
    return sorted(result, key=keys[sort], reverse=reverse)


def totals_view(sessions: Iterable[core.Session]) -> dict[str, Any]:
    items = list(sessions)
    usage = core.Usage()
    cost = 0.0
    context = 0
    for session in items:
        usage.add(session.total_usage)
        cost += session.total_cost
        context += session.total_context_used_tokens
    return {
        **usage_view(usage, cost),
        "context_used_tokens": context or None,
        "session_count": len(items),
        "by_tool": dict(Counter(session.tool for session in items)),
    }
