import json
from pathlib import Path

from claude_usage.core import (
    cost_of,
    find_codex_sessions,
    main,
    parse_codex_rollout,
    parse_session,
    parse_subagent,
    price_for,
    short_model,
    Usage,
)


def test_current_openai_standard_pricing_and_model_aliases() -> None:
    expected = {
        "gpt-5.6": (5.0, 30.0),
        "gpt-5.6-sol": (5.0, 30.0),
        "gpt-5.6-terra": (2.5, 15.0),
        "gpt-5.6-luna": (1.0, 6.0),
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
    assert price_for("gpt-5.60") is None
    assert price_for("gpt-5.6-sol-preview") is None
    assert short_model("claude-haiku-4-5-20251001") == "haiku-4.5"
    usage = Usage(input=1_000_000, output=1_000_000, cache_read=1_000_000)
    assert cost_of({"gpt-5.6-sol": usage}) == 35.5


def test_report_allows_missing_claude_directory(tmp_path: Path, capsys) -> None:
    codex_dir = tmp_path / "codex" / "sessions"
    codex_dir.mkdir(parents=True)

    result = main(
        [
            "--projects-dir",
            str(tmp_path / "missing-claude"),
            "--gui-dir",
            str(tmp_path / "missing-gui"),
            "--codex-dir",
            str(codex_dir),
        ]
    )

    assert result == 0
    assert "No sessions with token usage found." in capsys.readouterr().out


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
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
                "payload": {"id": "parent", "cwd": "/work/project"},
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
    assert child.description == "Scout"
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
