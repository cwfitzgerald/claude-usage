import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from claude_usage.core import Session, SubAgent, Usage
from claude_usage.service import ScanConfig, Snapshot, UsageService
from claude_usage.web import create_app


def make_session(tmp_path: Path, *, name: str, session_id: str, cost_tokens: int):
    session = Session(
        name=name,
        session_id=session_id,
        project="private-project",
        path=tmp_path / f"{session_id}.jsonl",
        timestamp="2026-07-01T10:00:10Z",
        models={"claude-opus-4-8"},
        context_used_tokens=123,
        peak_context_tokens=100,
    )
    session.per_model["claude-opus-4-8"] = Usage(input=cost_tokens, output=cost_tokens)
    return session


def make_agent(name: str, tokens: int, timestamp: str) -> SubAgent:
    agent = SubAgent(description=name, timestamp=timestamp, models={"gpt-5.4"})
    agent.per_model["gpt-5.4"] = Usage(input=tokens)
    return agent


def test_sessions_api_filters_sorts_and_does_not_expose_paths(tmp_path: Path):
    service = UsageService(ScanConfig(tmp_path, tmp_path, tmp_path))
    low = make_session(tmp_path, name="Alpha", session_id="a", cost_tokens=10)
    high = make_session(tmp_path, name="Beta", session_id="b", cost_tokens=100)
    high.subagents = [
        make_agent("Zulu", 20, "2026-07-01T10:00:01Z"),
        make_agent("Alpha", 10, "2026-07-01T10:00:02Z"),
    ]
    high.subagents[0].children = [
        make_agent("Nested Zulu", 1, "2026-07-01T10:00:03Z"),
        make_agent("Nested Alpha", 2, "2026-07-01T10:00:04Z"),
    ]
    service.snapshot = Snapshot(
        generation=1,
        sessions=(low, high),
        by_id={(item.tool, item.session_id): item for item in (low, high)},
    )

    async def exercise():
        transport = ASGITransport(app=create_app(service))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            index = await client.get("/")
            assert index.status_code == 200
            assert "Agent usage" in index.text

            status = await client.get("/api/v1/status")
            assert status.status_code == 200
            assert status.json()["memory_bytes"] > 0

            response = await client.get(
                "/api/v1/sessions?sort=tokens&order=desc&q=beta"
            )
            assert response.status_code == 200
            body = response.json()
            assert [item["session_id"] for item in body["items"]] == ["b"]
            assert body["meta"]["filtered_count"] == 1
            assert body["items"][0]["context_used_tokens"] == 123
            assert body["items"][0]["display_model"] == "opus-4.8"
            assert "wall_ms" not in body["items"][0]
            assert "path" not in response.text

            removed_timing_sort = await client.get("/api/v1/sessions?sort=wall")
            assert removed_timing_sort.status_code == 400

            hidden_project = await client.get("/api/v1/sessions?q=private-project")
            assert hidden_project.status_code == 200
            assert hidden_project.json()["items"] == []

            default_detail = await client.get("/api/v1/sessions/claude/b")
            assert [agent["label"] for agent in default_detail.json()["subagents"]] == [
                "Zulu",
                "Alpha",
            ]

            detail = await client.get("/api/v1/sessions/claude/b?subagent_sort=name")
            assert detail.status_code == 200
            assert detail.json()["base"]["peak_context_tokens"] == 100
            assert detail.json()["base"]["display_model"] == "opus-4.8"
            assert "agent_ms" not in detail.json()["base"]
            assert [agent["label"] for agent in detail.json()["subagents"]] == [
                "Alpha",
                "Zulu",
            ]
            assert [
                agent["label"] for agent in detail.json()["subagents"][1]["children"]
            ] == ["Nested Alpha", "Nested Zulu"]
            assert "path" not in detail.text

            invalid_detail = await client.get(
                "/api/v1/sessions/claude/b?subagent_sort=wall"
            )
            assert invalid_detail.status_code == 400

    asyncio.run(exercise())


def test_refresh_is_serialized_and_atomically_replaces_snapshot(tmp_path: Path):
    async def exercise():
        service = UsageService(ScanConfig(tmp_path, tmp_path, tmp_path))
        assert await service.start_refresh() is True
        assert await service.start_refresh() is False
        await service.wait_for_refresh()
        assert service.snapshot.generation == 1
        assert service.snapshot.sessions == ()
        assert service.last_error is None

    asyncio.run(exercise())
