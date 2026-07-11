"""In-process, stale-while-refresh usage snapshot service."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import core


def process_rss_bytes() -> int | None:
    """Return this server process's resident memory without extra dependencies."""

    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("page_fault_count", wintypes.DWORD),
                    ("peak_working_set_size", ctypes.c_size_t),
                    ("working_set_size", ctypes.c_size_t),
                    ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                    ("quota_paged_pool_usage", ctypes.c_size_t),
                    ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                    ("quota_non_paged_pool_usage", ctypes.c_size_t),
                    ("pagefile_usage", ctypes.c_size_t),
                    ("peak_pagefile_usage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            get_current_process = ctypes.windll.kernel32.GetCurrentProcess
            get_current_process.argtypes = []
            get_current_process.restype = wintypes.HANDLE
            get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
            get_process_memory_info.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            get_process_memory_info.restype = wintypes.BOOL
            if get_process_memory_info(
                get_current_process(), ctypes.byref(counters), counters.cb
            ):
                return int(counters.working_set_size)
            return None

        statm = Path("/proc/self/statm")
        if statm.is_file():
            resident_pages = int(statm.read_text(encoding="ascii").split()[1])
            return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))

        # macOS exposes peak RSS through getrusage rather than a current-RSS
        # standard-library API. It is still a useful conservative fallback.
        import resource

        rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return rss if sys.platform == "darwin" else rss * 1024
    except (AttributeError, IndexError, OSError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ScanConfig:
    projects_dir: Path
    gui_dir: Path
    codex_dir: Path


@dataclass(frozen=True)
class Snapshot:
    generation: int = 0
    scanned_at: str | None = None
    scan_ms: int | None = None
    sessions: tuple[core.Session, ...] = ()
    warnings: tuple[str, ...] = ()
    by_id: dict[tuple[str, str], core.Session] = field(default_factory=dict)


class UsageService:
    """Own the current immutable snapshot and one serialized refresh task."""

    def __init__(self, config: ScanConfig):
        self.config = config
        self.snapshot = Snapshot()
        self.last_error: str | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def refreshing(self) -> bool:
        return self._refresh_task is not None and not self._refresh_task.done()

    def status(self) -> dict[str, object]:
        snap = self.snapshot
        return {
            "generation": snap.generation,
            "scanned_at": snap.scanned_at,
            "scan_ms": snap.scan_ms,
            "memory_bytes": process_rss_bytes(),
            "refreshing": self.refreshing,
            "session_count": len(snap.sessions),
            "warnings": list(snap.warnings),
            "last_error": self.last_error,
        }

    async def start_refresh(self) -> bool:
        async with self._lock:
            if self.refreshing:
                return False
            self._refresh_task = asyncio.create_task(self._run_refresh())
            return True

    async def wait_for_refresh(self) -> None:
        task = self._refresh_task
        if task is not None:
            await task

    async def _run_refresh(self) -> None:
        try:
            snapshot = await asyncio.to_thread(self._scan, self.snapshot.generation + 1)
        except Exception as exc:  # retain the previous good snapshot
            self.last_error = f"{type(exc).__name__}: {exc}"
        else:
            self.snapshot = snapshot
            self.last_error = None

    def _scan(self, generation: int) -> Snapshot:
        started = time.perf_counter()
        stderr = io.StringIO()
        core._UNKNOWN_MODELS.clear()
        with contextlib.redirect_stderr(stderr):
            claude_sessions = (
                core.find_sessions(self.config.projects_dir, self.config.gui_dir)
                if self.config.projects_dir.is_dir()
                else []
            )
            codex_sessions = (
                core.find_codex_sessions(self.config.codex_dir)
                if self.config.codex_dir.is_dir()
                else []
            )
        sessions = tuple(claude_sessions + codex_sessions)
        # Force pricing once so unknown models become structured snapshot
        # warnings instead of appearing later as a request-time side effect.
        for session in sessions:
            _ = session.total_cost
        warning_lines = [
            line.strip() for line in stderr.getvalue().splitlines() if line.strip()
        ]
        if core._UNKNOWN_MODELS:
            warning_lines.append(
                "No pricing for " + ", ".join(sorted(core._UNKNOWN_MODELS))
            )
        elapsed = round((time.perf_counter() - started) * 1000)
        return Snapshot(
            generation=generation,
            scanned_at=datetime.now(timezone.utc).isoformat(),
            scan_ms=elapsed,
            sessions=sessions,
            warnings=tuple(warning_lines),
            by_id={(s.tool, s.session_id): s for s in sessions},
        )
