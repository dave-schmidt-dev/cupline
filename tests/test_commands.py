"""One-shot CLI commands that touch disk."""

import asyncio

import commands
from sweeper_fakes import FakeApp, FakeSession, FakeTab, FakeWindow


class _NoRegistry:
    def __init__(self, _connection):
        self.states = {}

    async def discover(self, _app):
        pass


def test_capture_writes_only_redacted_content(monkeypatch, tmp_path):
    """``--capture`` is the one path that puts terminal content on disk.

    ``redact()`` is tested on its own in test_screen.py; this pins that the
    write path still calls it, so a raw home path, URL or token never reaches
    a fixture file.
    """
    token = "sk" + "A" * 40
    session = FakeSession(
        "s1",
        lines=[
            "cd /Users/someone/Documents/Projects/atlas",
            "fetching https://example.com/private/thing",
            f"export KEY={token}",
        ],
    )
    app = FakeApp([FakeWindow("w0", [FakeTab("t0", [session])])])
    monkeypatch.setattr(commands.sessionlib, "SessionRegistry", _NoRegistry)
    monkeypatch.setattr(commands, "IDLE_AFTER_SECONDS", 0.0)
    monkeypatch.chdir(tmp_path)

    asyncio.run(commands.cmd_capture(app, "s1", "waiting"))

    written = (tmp_path / "fixtures" / "unknown-waiting-01.txt").read_text(
        encoding="utf-8"
    )
    assert "/Users/someone" not in written
    assert "https://example.com" not in written
    assert token not in written
    assert "atlas" in written, "the capture wrote nothing from the screen at all"
