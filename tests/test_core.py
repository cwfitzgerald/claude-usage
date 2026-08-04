import json
from pathlib import Path

from claude_usage.core import (
    _skippable,
    cost_of,
    find_codex_sessions,
    parse_codex_rollout,
    parse_session,
    parse_subagent,
    price_for,
    Session,
    short_model,
    SubAgent,
    Usage,
)


def test_current_openai_standard_pricing_and_model_aliases() -> None:
    expected = {
        "gpt-5.6": (5.0, 30.0),
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
    for model, rates in expected.items():
        assert price_for(model) == rates

    assert price_for("gpt-5.6-sol-2026-07-09") == (5.0, 30.0)
    # A dated id must resolve to *its own* entry. Same-length ids ("gpt-5.4" vs
    # "gpt-5.6") slice at the same offset, so matching the date suffix alone would
    # price this as whichever happens to sort first.
    assert price_for("gpt-5.4-2026-07-09") == (2.5, 15.0)
    assert price_for("gpt-5.60") is None
    assert price_for("gpt-5.6-sol-preview") is None
    assert short_model("claude-haiku-4-5-20251001") == "haiku-4.5"
    usage = Usage(input=1_000_000, output=1_000_000, cache_read=1_000_000)
    assert cost_of({"gpt-5.6-sol": usage}) == 35.5


def test_priority_tier_pricing_scales_per_model() -> None:
    """The priority ("Fast") tier bills at a per-model multiple of standard.

    Verified against OpenAI's pricing docs (2026-07): the gpt-5.6 family and
    gpt-5.4 double, gpt-5.5 is 2.5x, and gpt-5.4-nano has no priority tier at all
    — so a stray priority flag on it must not inflate its cost.
    """
    assert price_for("gpt-5.6-sol", priority=True) == (10.0, 60.0)
    assert price_for("gpt-5.6-terra", priority=True) == (4.0, 24.0)
    assert price_for("gpt-5.6-luna", priority=True) == (0.4, 2.4)
    assert price_for("gpt-5.4", priority=True) == (5.0, 30.0)
    assert price_for("gpt-5.5", priority=True) == (12.5, 75.0)
    assert price_for("gpt-5.4-nano", priority=True) == price_for("gpt-5.4-nano")
    # The multiplier keys off the *resolved* pricing entry, not the id as logged,
    # so a dated id gets its family's factor rather than the 2x fallback.
    assert price_for("gpt-5.5-2026-07-09", priority=True) == (12.5, 75.0)
    # An unpriced model stays unpriced rather than becoming "2x of nothing".
    assert price_for("gpt-5.60", priority=True) is None

    usage = Usage(input=1_000_000, output=1_000_000, cache_read=1_000_000)
    assert cost_of({"gpt-5.6-sol": usage}, priority=True) == 71.0


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def write_jsonl_compact(path: Path, records: list[dict]) -> None:
    """Write records byte-for-byte the way the tools do: no separator spaces.

    ``_skippable`` reads the raw line, so the on-disk layout decides whether a
    record takes the fast path. ``json.dumps`` defaults to ``", "``/``": "``,
    which no real transcript uses and which makes every record fall through to a
    full decode — correct, but it would hide the fast path from these tests.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def claude_assistant(
    message_id: str,
    timestamp: str,
    *,
    model: str = "claude-opus-4-8",
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> dict:
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "id": message_id,
            "model": model,
            "content": [{"type": "text", "text": "synthetic response"}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read_tokens,
                "cache_creation_input_tokens": cache_write_tokens,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cache_write_tokens,
                },
            },
        },
    }


def test_synthetic_subagent_does_not_create_usage_or_unknown_model(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "agent-synthetic.jsonl"
    write_jsonl(
        transcript,
        [
            claude_assistant(
                "synthetic-message",
                "2026-07-01T10:00:01Z",
                model="<synthetic>",
                input_tokens=0,
                output_tokens=0,
            )
        ],
    )

    assert parse_subagent(transcript) is None


def codex_token_count(
    timestamp: str,
    *,
    total_tokens: int,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    last_tokens: int | None = None,
) -> dict:
    return {
        "type": "event_msg",
        "timestamp": timestamp,
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "total_tokens": total_tokens,
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                },
                "last_token_usage": {
                    "total_tokens": (
                        total_tokens if last_tokens is None else last_tokens
                    ),
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                },
                "model_context_window": 1_050_000,
            },
        },
    }


def test_codex_model_ties_prefer_latest_and_cached_input_is_bounded(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "rollout-model-switch.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "turn_context",
                "timestamp": "2026-07-01T11:00:00Z",
                "payload": {"model": "gpt-5.4"},
            },
            {
                "type": "turn_context",
                "timestamp": "2026-07-01T11:00:01Z",
                "payload": {"model": "gpt-5.4-mini"},
            },
            codex_token_count(
                "2026-07-01T11:00:02Z",
                total_tokens=60,
                input_tokens=50,
                cached_input_tokens=80,
                output_tokens=10,
            ),
        ],
    )

    session = parse_codex_rollout(transcript, {})

    assert session is not None
    assert session.primary_model == "gpt-5.4-mini"
    assert session.usage.input == 0
    assert session.usage.cache_read == 50
    assert session.usage.output == 10


def test_parse_codex_rollout_splits_compacted_contexts(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout-compacted.jsonl"
    write_jsonl(
        transcript,
        [
            {"type": "turn_context", "payload": {"model": "gpt-5.4"}},
            codex_token_count(
                "2026-07-01T11:00:00Z",
                total_tokens=100,
                input_tokens=80,
                cached_input_tokens=20,
                output_tokens=20,
                last_tokens=70,
            ),
            {
                "type": "compacted",
                "timestamp": "2026-07-01T11:01:00Z",
                "payload": {"replacement_history": [], "window_number": 1},
            },
            codex_token_count(
                "2026-07-01T11:01:01Z",
                total_tokens=100,
                input_tokens=80,
                cached_input_tokens=20,
                output_tokens=20,
                last_tokens=5,
            ),
            codex_token_count(
                "2026-07-01T11:02:00Z",
                total_tokens=160,
                input_tokens=130,
                cached_input_tokens=30,
                output_tokens=30,
                last_tokens=25,
            ),
        ],
    )

    session = parse_codex_rollout(transcript, {})

    assert session is not None
    assert session.usage.total_tokens == 160
    assert len(session.segments) == 2
    first, second = session.segments
    assert first.usage == Usage(input=60, cache_read=20, output=20)
    assert first.label == "context 1 (→70)"
    assert second.usage == Usage(input=40, cache_read=10, output=10)
    assert second.label == "context 2 (live)"
    assert session.context_used_tokens == 95
    assert session.peak_context_tokens == 70


def test_codex_compaction_surfaces_before_next_billed_turn(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout-just-compacted.jsonl"
    write_jsonl(
        transcript,
        [
            {"type": "turn_context", "payload": {"model": "gpt-5.4"}},
            codex_token_count(
                "2026-07-01T11:00:00Z",
                total_tokens=100,
                input_tokens=80,
                cached_input_tokens=20,
                output_tokens=20,
                last_tokens=70,
            ),
            {"type": "compacted", "payload": {"replacement_history": []}},
            codex_token_count(
                "2026-07-01T11:01:00Z",
                total_tokens=100,
                input_tokens=80,
                cached_input_tokens=20,
                output_tokens=20,
                last_tokens=5,
            ),
        ],
    )

    session = parse_codex_rollout(transcript, {})

    assert session is not None
    assert len(session.segments) == 2
    assert session.segments[1].usage.total_tokens == 0
    assert session.segments[1].context_tokens == 5


def test_forked_codex_subagent_excludes_replayed_parent_compaction(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "rollout-forked-child.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-01T11:02:00Z",
                "payload": {
                    "id": "child",
                    "parent_thread_id": "parent",
                    "forked_from_id": "parent",
                    "agent_path": "/root/fix_tests",
                },
            },
            # Codex replays the parent's rollout after the child's metadata.
            {
                "type": "session_meta",
                "timestamp": "2026-07-01T11:02:01Z",
                "payload": {"id": "parent"},
            },
            {
                "type": "turn_context",
                "payload": {"model": "gpt-5.4"},
            },
            codex_token_count(
                "2026-07-01T11:02:02Z",
                total_tokens=100,
                input_tokens=80,
                cached_input_tokens=20,
                output_tokens=20,
                last_tokens=70,
            ),
            {"type": "compacted", "payload": {"replacement_history": []}},
            codex_token_count(
                "2026-07-01T11:02:03Z",
                total_tokens=100,
                input_tokens=80,
                cached_input_tokens=20,
                output_tokens=20,
                last_tokens=5,
            ),
            # The child's first turn_context is written just before the
            # structured marker that ends the inherited prefix.
            {
                "type": "turn_context",
                "payload": {
                    "model": "gpt-5.4-mini",
                    "collaboration_mode": {"settings": {"reasoning_effort": "high"}},
                },
            },
            {
                "type": "inter_agent_communication_metadata",
                "payload": {"trigger_turn": True},
            },
            codex_token_count(
                "2026-07-01T11:02:04Z",
                total_tokens=130,
                input_tokens=105,
                cached_input_tokens=25,
                output_tokens=25,
                last_tokens=20,
            ),
            {"type": "compacted", "payload": {"replacement_history": []}},
            codex_token_count(
                "2026-07-01T11:02:05Z",
                total_tokens=130,
                input_tokens=105,
                cached_input_tokens=25,
                output_tokens=25,
                last_tokens=4,
            ),
            codex_token_count(
                "2026-07-01T11:02:06Z",
                total_tokens=150,
                input_tokens=120,
                cached_input_tokens=30,
                output_tokens=30,
                last_tokens=10,
            ),
        ],
    )

    session = parse_codex_rollout(transcript, {})

    assert session is not None
    assert session.parent_id == "parent"
    assert session.agent_name == "Fix tests"
    assert session.primary_model == "gpt-5.4-mini"
    assert session.effort == "high"
    assert session.usage == Usage(input=30, cache_read=10, output=10)
    assert len(session.segments) == 2
    first, second = session.segments
    assert first.usage == Usage(input=20, cache_read=5, output=5)
    assert first.label == "context 1 (→20)"
    assert second.usage == Usage(input=10, cache_read=5, output=5)
    assert second.label == "context 2 (live)"
    assert session.context_used_tokens == 30
    assert session.peak_context_tokens == 20


def test_parse_claude_dedupes_compacts_and_loads_subagent(tmp_path: Path) -> None:
    transcript = tmp_path / "project" / "session-1.jsonl"
    first_turn = claude_assistant(
        "message-1",
        "2026-07-01T10:00:01Z",
        input_tokens=10,
        output_tokens=2,
        cache_read_tokens=3,
        cache_write_tokens=5,
    )
    write_jsonl(
        transcript,
        [
            {
                "type": "user",
                "timestamp": "2026-07-01T10:00:00Z",
                "promptId": "prompt-1",
                "message": {"content": [{"type": "text", "text": "test"}]},
            },
            {
                "type": "summary",
                "timestamp": "2026-07-01T10:00:00Z",
                "summary": "Test session",
            },
            first_turn,
            # Claude writes one copy of usage for each content block. The parser
            # must count a repeated API message id only once.
            {**first_turn, "timestamp": "2026-07-01T10:00:02Z"},
            {
                "type": "system",
                "subtype": "compact_boundary",
                "timestamp": "2026-07-01T10:00:03Z",
                "compactMetadata": {"preTokens": 20, "trigger": "manual"},
            },
            claude_assistant(
                "message-2",
                "2026-07-01T10:00:04Z",
                input_tokens=7,
                output_tokens=4,
            ),
            {
                "type": "system",
                "subtype": "stop_hook_summary",
                "timestamp": "2026-07-01T10:00:05Z",
            },
        ],
    )

    subagent_path = (
        transcript.parent / transcript.stem / "subagents" / "agent-child.jsonl"
    )
    write_jsonl(
        subagent_path,
        [
            claude_assistant(
                "subagent-message",
                "2026-07-01T10:00:05Z",
                model="claude-haiku-4-5",
                input_tokens=1,
                output_tokens=2,
            )
        ],
    )
    subagent_path.with_suffix(".meta.json").write_text(
        json.dumps(
            {
                "agentType": "Explore",
                "description": "Inspect fixtures",
                "toolUseId": "spawn-child",
                "spawnDepth": 1,
            }
        ),
        encoding="utf-8",
    )

    session = parse_session(transcript)

    assert session is not None
    assert session.name == "Test session"
    assert session.timestamp == "2026-07-01T10:00:05Z"
    assert session.usage.input == 17
    assert session.usage.output == 6
    assert session.usage.cache_read == 3
    assert session.usage.cache_write_5m == 5

    assert len(session.segments) == 2
    assert session.segments[0].usage.input == 10
    assert session.segments[0].peak_tokens == 20
    assert session.segments[0].trigger == "manual"
    assert session.segments[1].usage.input == 7
    assert session.segments[1].peak_tokens == 0
    assert session.segments[0].context_tokens == 20
    assert session.segments[1].context_tokens == 11
    assert session.context_used_tokens == 31
    assert session.peak_context_tokens == 20

    assert len(session.subagents) == 1
    subagent = session.subagents[0]
    assert subagent.label == "Inspect fixtures"
    assert subagent.agent_id == "child"
    assert subagent.spawn_depth == 1
    assert subagent.primary_model == "claude-haiku-4-5"
    assert subagent.usage.total_tokens == 3
    assert subagent.context_used_tokens == 3
    assert session.total_usage.total_tokens == 34


def test_skipping_user_records_preserves_usage_and_final_timestamp(
    tmp_path: Path,
) -> None:
    """The prefilter must skip ``user`` turns without changing any output.

    ``user`` records are about half the bytes on disk and hold nothing this
    dashboard needs except a timestamp, so they are skipped before ``json.loads``.
    The newest one still has to reach the Date column, and a record the head
    can't classify must fall through to a full decode.
    """
    transcript = tmp_path / "project" / "session-skip.jsonl"
    write_jsonl_compact(
        transcript,
        [
            {
                "parentUuid": "11111111-2222-3333-4444-555555555555",
                "type": "user",
                "timestamp": "2026-07-05T10:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "hi"}],
                },
            },
            claude_assistant(
                "message-skip",
                "2026-07-05T10:00:01Z",
                input_tokens=11,
                output_tokens=3,
            ),
            # An attachment leads with a *nested* content type, so the head can't
            # identify it; it must still be decoded rather than guessed at.
            {
                "parentUuid": "11111111-2222-3333-4444-555555555556",
                "attachment": {"type": "task_reminder", "content": "remember"},
                "type": "attachment",
                "timestamp": "2026-07-05T10:00:02Z",
            },
            # The newest record in the file is a skipped user turn: its timestamp
            # is what the Date column and --since must still see.
            {
                "parentUuid": "11111111-2222-3333-4444-555555555557",
                "type": "user",
                "timestamp": "2026-07-05T10:05:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "x" * 4096}],
                },
            },
        ],
    )

    session = parse_session(transcript)

    assert session is not None
    assert session.usage.input == 11
    assert session.usage.output == 3
    assert session.timestamp == "2026-07-05T10:05:00Z"

    # Guard the fast path itself: the assertions above stay green even if the
    # prefilter stops matching and every line is decoded, so check it directly.
    lines = transcript.read_text(encoding="utf-8").splitlines()
    assert [_skippable(line) for line in lines] == [True, False, False, True]


def codex_thread_settings(timestamp: str, service_tier: str | None) -> dict:
    """A ``thread_settings_applied`` event, the only record carrying the tier."""
    return {
        "type": "event_msg",
        "timestamp": timestamp,
        "payload": {
            "type": "thread_settings_applied",
            "thread_settings": {
                "model": "gpt-5.6-sol",
                "service_tier": service_tier,
                "reasoning_effort": "high",
            },
        },
    }


def test_codex_fast_tier_is_read_from_thread_settings_and_repriced(
    tmp_path: Path,
) -> None:
    """A ``priority`` tier is Codex's "Fast" mode and bills at 2x for gpt-5.6.

    Codex re-applies the whole settings block repeatedly, and a thread with no
    tier at all (e.g. codex-auto-review) writes an explicit null — which must not
    erase a real value seen earlier.
    """
    transcript = tmp_path / "rollout-fast.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-27T23:19:52Z",
                "payload": {"id": "fast", "cwd": "/work/project"},
            },
            {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
            codex_thread_settings("2026-07-27T23:19:53Z", "priority"),
            # A null tier is a "no tier recorded" marker, not a downgrade.
            codex_thread_settings("2026-07-27T23:19:54Z", None),
            codex_token_count(
                "2026-07-27T23:19:55Z",
                total_tokens=1_500_000,
                input_tokens=1_000_000,
                cached_input_tokens=1_000_000,
                output_tokens=500_000,
            ),
        ],
    )

    session = parse_codex_rollout(transcript, {})

    assert session is not None
    assert session.service_tier == "priority"
    assert session.priority is True
    # 1M cached input at $10/MTok * 0.1, plus 500K output at $60/MTok.
    assert session.cost == 1.0 + 30.0
    assert cost_of(session.per_model) == 0.5 + 15.0  # half, on the standard tier


def test_codex_tier_toggled_mid_thread_splits_per_turn(tmp_path: Path) -> None:
    """A thread that switches fast mode off bills each turn on the tier it ran.

    Codex's counter is cumulative, so the per-turn advance is the only way to
    attribute this. Taking a single tier for the whole thread would either
    overcharge the standard turns or undercharge the fast ones — and since the
    *last* recorded setting here is "default", a last-wins rule would price a
    mostly-fast thread entirely at standard.
    """
    transcript = tmp_path / "rollout-toggled.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-27T23:19:52Z",
                "payload": {"id": "toggled", "cwd": "/work/project"},
            },
            {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
            codex_thread_settings("2026-07-27T23:19:53Z", "priority"),
            # Two turns on the fast tier: 300K output total.
            codex_token_count(
                "2026-07-27T23:19:54Z",
                total_tokens=200_000,
                input_tokens=100_000,
                cached_input_tokens=0,
                output_tokens=100_000,
            ),
            codex_token_count(
                "2026-07-27T23:19:55Z",
                total_tokens=500_000,
                input_tokens=200_000,
                cached_input_tokens=0,
                output_tokens=300_000,
            ),
            # Fast mode off; everything after this is standard tier.
            codex_thread_settings("2026-07-27T23:19:56Z", "default"),
            codex_token_count(
                "2026-07-27T23:19:57Z",
                total_tokens=900_000,
                input_tokens=400_000,
                cached_input_tokens=0,
                output_tokens=500_000,
            ),
        ],
    )

    session = parse_codex_rollout(transcript, {})

    assert session is not None
    # The thread is *set* to default now, but two thirds of it billed fast.
    assert session.service_tier == "default"
    assert session.priority is True
    assert session.usage == Usage(input=400_000, output=500_000)
    assert session.priority_usage == Usage(input=200_000, output=300_000)
    # fast: 200K in @ $10 + 300K out @ $60 = $2 + $18
    # std:  200K in @ $5  + 200K out @ $30 = $1 + $6
    assert session.cost == (2.0 + 18.0) + (1.0 + 6.0)


def test_priority_usage_rollup_covers_subagents(tmp_path: Path) -> None:
    """``total_priority_usage`` is a rollup; ``priority_usage`` is base-only.

    The summary row's other figures are whole-conversation, so mixing a base-only
    fast-token count in beside them would undercount any session whose subagents
    ran fast — which is the normal case, since a fork inherits the tier.
    """
    session = Session(
        name="mixed",
        session_id="mixed",
        project="/work",
        path=tmp_path / "mixed.jsonl",
        tool="codex",
        models={"gpt-5.6-sol"},
    )
    session.per_model["gpt-5.6-sol"] = Usage(input=100, output=100)
    session.priority_per_model["gpt-5.6-sol"] = Usage(input=40, output=40)
    agent = SubAgent(description="child", models={"gpt-5.6-sol"})
    agent.per_model["gpt-5.6-sol"] = Usage(input=50, output=50)
    agent.priority_per_model["gpt-5.6-sol"] = Usage(input=50, output=50)
    session.subagents = [agent]

    assert session.priority_usage.total_tokens == 80
    assert session.total_priority_usage.total_tokens == 180
    assert session.total_usage.total_tokens == 300
    # The rollup can't exceed the usage it summarizes, nor undercut the base's.
    assert (
        session.priority_usage.total_tokens
        <= session.total_priority_usage.total_tokens
        <= session.total_usage.total_tokens
    )


def test_codex_settings_records_govern_only_later_turns(tmp_path: Path) -> None:
    """A settings record applies from the next turn on, never retroactively.

    Codex emits ``thread_settings_applied`` *before* the turn it governs, so a
    stream is never split across tiers: whatever the tier was when a turn ran is
    what that whole turn bills at. Consecutive records with no turn between them
    therefore collapse — only the last one reaches the next turn.
    """
    transcript = tmp_path / "rollout-midturn.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-27T23:19:52Z",
                "payload": {"id": "midturn", "cwd": "/work/project"},
            },
            {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
            codex_thread_settings("2026-07-27T23:19:53Z", "priority"),
            # This turn ran while fast mode was on, so it bills fast in full.
            codex_token_count(
                "2026-07-27T23:19:54Z",
                total_tokens=200_000,
                input_tokens=100_000,
                cached_input_tokens=0,
                output_tokens=100_000,
            ),
            # Switched off then straight back on with no turn in between. Neither
            # record can reach the turn above, and only the last one governs the
            # turn below — which therefore still bills fast.
            codex_thread_settings("2026-07-27T23:19:55Z", "default"),
            codex_thread_settings("2026-07-27T23:19:56Z", "priority"),
            codex_token_count(
                "2026-07-27T23:19:57Z",
                total_tokens=500_000,
                input_tokens=200_000,
                cached_input_tokens=0,
                output_tokens=300_000,
            ),
        ],
    )

    session = parse_codex_rollout(transcript, {})

    assert session is not None
    assert session.service_tier == "priority"
    # Both turns billed fast: nothing was left on the standard tier.
    assert (
        session.priority_usage == session.usage == Usage(input=200_000, output=300_000)
    )
    # 200K in @ $10/MTok + 300K out @ $60/MTok.
    assert session.cost == 2.0 + 18.0


def test_codex_default_tier_and_missing_record_price_at_standard(
    tmp_path: Path,
) -> None:
    """Only ``priority`` reprices; ``default`` and older tier-less rollouts don't."""
    for name, records in {
        "default": [codex_thread_settings("2026-07-15T10:00:01Z", "default")],
        "absent": [],
    }.items():
        transcript = tmp_path / f"rollout-{name}.jsonl"
        write_jsonl(
            transcript,
            [
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-15T10:00:00Z",
                    "payload": {"id": name, "cwd": "/work/project"},
                },
                {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
                *records,
                codex_token_count(
                    "2026-07-15T10:00:02Z",
                    total_tokens=600_000,
                    input_tokens=100_000,
                    cached_input_tokens=0,
                    output_tokens=500_000,
                ),
            ],
        )

        session = parse_codex_rollout(transcript, {})

        assert session is not None, name
        assert session.priority is False, name
        assert session.cost == 0.5 + 15.0, name

    assert parse_codex_rollout(tmp_path / "rollout-default.jsonl", {}) is not None


def test_forked_codex_subagent_adopts_its_own_tier_not_the_replayed_prefix(
    tmp_path: Path,
) -> None:
    """A fork replays the parent's settings before writing its own.

    The replayed prefix belongs to the parent, so the tier must come from the last
    settings record before the inter-agent trigger — the child's. Here the parent
    ran fast and the child was downgraded, so taking the wrong one would double
    the child's bill.
    """
    transcript = tmp_path / "rollout-forked-tier.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-27T23:23:47Z",
                "payload": {
                    "id": "child",
                    "parent_thread_id": "parent",
                    "forked_from_id": "parent",
                    "agent_path": "/root/correctness_review",
                },
            },
            # --- replayed parent prefix: its tier is not the child's ---
            codex_thread_settings("2026-07-27T23:23:48Z", "priority"),
            {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
            codex_token_count(
                "2026-07-27T23:23:49Z",
                total_tokens=300_000,
                input_tokens=200_000,
                cached_input_tokens=0,
                output_tokens=100_000,
            ),
            # --- the child's own settings/context, then the trigger ---
            codex_thread_settings("2026-07-27T23:23:50Z", "default"),
            {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
            {
                "type": "inter_agent_communication_metadata",
                "payload": {"trigger_turn": True},
            },
            codex_token_count(
                "2026-07-27T23:23:51Z",
                total_tokens=900_000,
                input_tokens=300_000,
                cached_input_tokens=0,
                output_tokens=600_000,
            ),
        ],
    )

    session = parse_codex_rollout(transcript, {})

    assert session is not None
    assert session.service_tier == "default"
    assert session.priority is False
    # Billed on the child's own turns only: 100K input + 500K output at standard.
    assert session.usage == Usage(input=100_000, output=500_000)
    assert session.cost == 0.5 + 15.0


def test_codex_segments_carry_their_own_tier_split(tmp_path: Path) -> None:
    """Each context slice prices on the tier its turns ran, and they sum to the
    thread's total."""
    transcript = tmp_path / "rollout-fast-compacted.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-27T23:19:52Z",
                "payload": {"id": "fast-compacted", "cwd": "/work/project"},
            },
            {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
            codex_thread_settings("2026-07-27T23:19:53Z", "priority"),
            codex_token_count(
                "2026-07-27T23:19:54Z",
                total_tokens=300_000,
                input_tokens=100_000,
                cached_input_tokens=0,
                output_tokens=200_000,
                last_tokens=90_000,
            ),
            {"type": "compacted", "payload": {"replacement_history": []}},
            codex_token_count(
                "2026-07-27T23:19:55Z",
                total_tokens=900_000,
                input_tokens=400_000,
                cached_input_tokens=0,
                output_tokens=500_000,
                last_tokens=20_000,
            ),
        ],
    )

    session = parse_codex_rollout(transcript, {})

    assert session is not None
    assert len(session.segments) == 2
    assert [segment.priority for segment in session.segments] == [True, True]
    assert sum(segment.cost for segment in session.segments) == session.cost
    # 400K input at $10/MTok + 500K output at $60/MTok, i.e. 2x standard.
    assert session.cost == 4.0 + 30.0


def test_find_codex_sessions_uses_peak_cumulative_usage_and_nests_child(
    tmp_path: Path,
) -> None:
    codex_root = tmp_path / "codex" / "sessions"
    (codex_root.parent / "session_index.jsonl").parent.mkdir(
        parents=True, exist_ok=True
    )
    (codex_root.parent / "session_index.jsonl").write_text(
        json.dumps({"id": "parent", "thread_name": "Indexed parent"}) + "\n",
        encoding="utf-8",
    )

    write_jsonl(
        codex_root / "2026" / "07" / "01" / "rollout-parent.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-01T11:00:00Z",
                "payload": {
                    "id": "parent",
                    "cwd": "/work/project",
                    "source": "cli",
                },
            },
            {
                "type": "turn_context",
                "timestamp": "2026-07-01T11:00:01Z",
                "payload": {
                    "model": "gpt-5.4",
                    "collaboration_mode": {"settings": {"reasoning_effort": "high"}},
                },
            },
            codex_token_count(
                "2026-07-01T11:00:02Z",
                total_tokens=100,
                input_tokens=80,
                cached_input_tokens=20,
                output_tokens=20,
            ),
            codex_token_count(
                "2026-07-01T11:00:03Z",
                total_tokens=250,
                input_tokens=200,
                cached_input_tokens=50,
                output_tokens=50,
            ),
            # A lower later counter must not replace the peak cumulative value.
            codex_token_count(
                "2026-07-01T11:00:04Z",
                total_tokens=200,
                input_tokens=160,
                cached_input_tokens=40,
                output_tokens=40,
            ),
        ],
    )
    write_jsonl(
        codex_root / "2026" / "07" / "01" / "rollout-child.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-01T11:01:00Z",
                "payload": {
                    "id": "child",
                    "cwd": "/work/project",
                    "parent_thread_id": "parent",
                    "agent_nickname": "Scout",
                    "agent_path": "/root/formats_surfaces_review",
                    "agent_role": "reviewer",
                },
            },
            {
                "type": "turn_context",
                "timestamp": "2026-07-01T11:01:01Z",
                "payload": {"model": "gpt-5.4-mini"},
            },
            codex_token_count(
                "2026-07-01T11:01:02Z",
                total_tokens=30,
                input_tokens=20,
                cached_input_tokens=5,
                output_tokens=10,
            ),
            # Forked rollouts can replay the parent's metadata. It must not
            # replace the child's identity or parent relationship.
            {
                "type": "session_meta",
                "timestamp": "2026-07-01T11:01:03Z",
                "payload": {"id": "parent", "cwd": "/work/project"},
            },
        ],
    )

    sessions = find_codex_sessions(codex_root)

    assert len(sessions) == 1
    parent = sessions[0]
    assert parent.session_id == "parent"
    assert parent.name == "Indexed parent"
    assert parent.effort == "high"
    # Cached input is included in Codex input_tokens and is split out by parser.
    assert parent.usage.input == 150
    assert parent.usage.cache_read == 50
    assert parent.usage.output == 50

    assert len(parent.subagents) == 1
    child = parent.subagents[0]
    assert child.description == "Formats surfaces review"
    assert child.agent_type == "reviewer"
    assert child.primary_model == "gpt-5.4-mini"
    assert child.usage.input == 15
    assert child.usage.cache_read == 5
    assert child.usage.output == 10
    assert parent.total_usage.total_tokens == 280
    assert parent.context_used_tokens == 250
    assert parent.total_context_used_tokens == 280
    assert parent.context_window_tokens == 1_050_000
    assert child.context_used_tokens == 30
    assert parent.timestamp == "2026-07-01T11:01:02Z"


def test_parse_codex_rollout_reads_nested_subagent_identity(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout-child.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-01T11:00:00Z",
                "payload": {
                    "id": "child",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "parent",
                                "agent_path": "/root/ci_review",
                                "agent_nickname": "Legacy nickname",
                                "agent_role": "reviewer",
                            }
                        }
                    },
                },
            },
            {"type": "turn_context", "payload": {"model": "gpt-5.4-mini"}},
            codex_token_count(
                "2026-07-01T11:00:01Z",
                total_tokens=10,
                input_tokens=8,
                cached_input_tokens=0,
                output_tokens=2,
            ),
        ],
    )

    session = parse_codex_rollout(transcript, {})

    assert session is not None
    assert session.parent_id == "parent"
    assert session.agent_name == "Ci review"
    assert session.agent_role == "reviewer"
