#!/usr/bin/env python3
"""Compatibility entry point for the packaged claude-usage application."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "src"))

from claude_usage.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
