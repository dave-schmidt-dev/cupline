"""Headless evidence transitions through the real watcher, registry and painter."""

import asyncio
import contextlib
import time

import iterm2
import pytest

import sessions
from config import IDLE_AFTER_SECONDS
from models import AgentState
from sweeper_fakes import FakeSession, FakeTab, _go_quiet, build


class EventSession(FakeSession):
    def __init__(self, sid):
        super().__init__(sid)
        self.events = asyncio.Queue()
        self.opened = asyncio.Event()
        self.delivered = asyncio.Event()
        self.opens = 0

    def get_screen_streamer(self, want_contents=False):
        return self

    async def __aenter__(self):
        self.opens += 1
        self.opened.set()
        return self

    async def __aexit__(self, *exc):
        return False

    async def async_get(self):
        event = await self.events.get()
        if isinstance(event, Exception):
            raise event
        self.delivered.set()
        return event


def test_event_reaches_classification_and_failed_stream_restarts(monkeypatch):
    async def scenario():
        pane = EventSession("s1")
        mon, _ = build(monkeypatch, [pane])
        del mon._ensure_watcher
        try:
            await mon._sweep(0)
            await asyncio.wait_for(pane.opened.wait(), 1)
            state = mon.registry.states["s1"]
            pane.lines = ["Proceed? (y/n)"]
            pane.events.put_nowait(object())
            await asyncio.wait_for(pane.delivered.wait(), 1)
            assert state.dirty
            _go_quiet(state)
            await mon._sweep(1)
            assert state.previous_classification is AgentState.ACTION
            assert mon.painter.pane_applied["s1"] is AgentState.ACTION

            pane.events.put_nowait(RuntimeError("stream ended"))
            await asyncio.wait_for(mon.watchers["s1"], 1)
            assert not state.streamer_ok
            pane.opened.clear()
            mon._watcher_retry_at["s1"] = 0
            await mon._sweep(2)
            await asyncio.wait_for(pane.opened.wait(), 1)
            assert pane.opens == 2
            assert state.streamer_ok
            assert time.monotonic() - state.last_event_at < IDLE_AFTER_SECONDS
            await mon._sweep(3)
            assert state.previous_classification is AgentState.WORKING
            assert not mon.painter.colored
        finally:
            task = mon.watchers.get("s1")
            mon._drop_watcher("s1")
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        assert not state.streamer_ok
    asyncio.run(scenario())


@pytest.mark.parametrize("old_state", [AgentState.ACTION, AgentState.WAITING])
def test_process_replacement_releases_old_alert_even_when_read_fails(monkeypatch, old_state):
    async def scenario():
        pane = FakeSession("s1", ["Proceed? (y/n)" if old_state is AgentState.ACTION else "done"])
        mon, app = build(monkeypatch, [pane])
        await mon._sweep(0)
        state = mon.registry.states["s1"]
        _go_quiet(state)
        await mon._sweep(1)
        assert mon.painter.pane_applied["s1"] is old_state
        generation = state.evidence_generation
        monkeypatch.setattr(sessions, "resolve_agent", lambda pid: ("claude", 9999))
        pane.fail = True
        await mon._sweep(8)
        assert state.evidence_generation == generation + 1
        assert not state.has_been_read()
        assert not state.evidence_is_current()
        assert not mon.painter.colored
        assert not mon.painter.titled
    asyncio.run(scenario())


def test_new_agent_with_identical_screen_does_not_inherit_ack(monkeypatch):
    async def scenario():
        pane = FakeSession("s1", ["done"])
        mon, app = build(monkeypatch, [pane])
        app.app_active = True
        await mon._sweep(0)
        _go_quiet(mon.registry.states["s1"])
        await mon._sweep(1)
        assert "s1" in mon.acked
        app.app_active = False
        monkeypatch.setattr(sessions, "resolve_agent", lambda pid: ("claude", 9999))
        await mon._sweep(8)
        _go_quiet(mon.registry.states["s1"])
        await mon._sweep(9)
        assert mon.painter.pane_applied["s1"] is AgentState.WAITING
    asyncio.run(scenario())


def test_helper_changes_preserve_evidence_but_new_agent_invalidates_it(monkeypatch):
    async def scenario():
        pane = FakeSession("s1")
        resolve = sessions.resolve_agent
        mon, _ = build(monkeypatch, [pane])
        await mon._sweep(0)
        state = mon.registry.states["s1"]
        # Use the actual shared ancestry walk for the refresh path.
        monkeypatch.setattr(sessions, "resolve_agent", resolve)
        monkeypatch.setattr(sessions, "_PS_CACHE", {1234: (100, "node"), 100: (1, "claude")})
        await mon.registry._refresh_one(pane, state)
        state.note_change("same", "done", 10)
        state.note_classification(AgentState.WAITING)
        generation = state.evidence_generation
        monkeypatch.setattr(sessions, "_PS_CACHE", {1234: (99, "zsh"), 99: (100, "tool"), 100: (1, "claude")})
        await mon.registry._refresh_one(pane, state)
        assert state.evidence_generation == generation
        assert state.previous_classification is AgentState.WAITING
        monkeypatch.setattr(sessions, "_PS_CACHE", {1234: (200, "node"), 200: (1, "claude")})
        await mon.registry._refresh_one(pane, state)
        assert state.evidence_generation == generation + 1
        assert state.previous_classification is AgentState.UNKNOWN
        assert not state.has_been_read()
    asyncio.run(scenario())


def test_changed_evidence_and_failed_read_release_stale_red(monkeypatch):
    async def scenario():
        pane = FakeSession("s1", ["Proceed? (y/n)"])
        mon, _ = build(monkeypatch, [pane])
        await mon._sweep(0)
        state = mon.registry.states["s1"]
        _go_quiet(state)
        await mon._sweep(1)
        assert mon.painter.pane_applied["s1"] is AgentState.ACTION
        state.note_event(time.monotonic() - IDLE_AFTER_SECONDS * 2)
        pane.fail = True
        await mon._sweep(2)
        assert state.previous_classification is AgentState.UNKNOWN
        assert not mon.painter.colored
    asyncio.run(scenario())


def test_moved_pane_keeps_original_appearance_and_old_title_clears(monkeypatch):
    async def scenario():
        pane, sibling = FakeSession("s1"), FakeSession("s2")
        original = iterm2.Color(11, 22, 33)
        pane.profile.use_tab_color = True
        pane.profile.tab_color = original
        mon, app = build(monkeypatch, [pane, sibling])
        await mon._sweep(0)
        _go_quiet(mon.registry.states["s1"])
        await mon._sweep(1)
        prior = mon.painter.pane_prior["s1"]
        old = app.windows[0].tabs[0]
        old.sessions = [sibling]
        app.windows[0].tabs.append(FakeTab("t1", [pane]))
        await mon._sweep(8)
        assert mon.painter.pane_prior["s1"] is prior
        assert mon.painter.pane_tab["s1"] == "t1"
        assert "t0" not in mon.painter.titled
        await mon.painter.restore_painted(app)
        assert not mon.painter.colored
        assert mon.painter.pane_prior["s1"].color is original
    asyncio.run(scenario())


def test_agent_exit_drops_watcher_and_ack_and_dead_tab_is_forgotten(monkeypatch):
    async def scenario():
        pane = FakeSession("s1")
        mon, app = build(monkeypatch, [pane])
        await mon._sweep(0)
        _go_quiet(mon.registry.states["s1"])
        await mon._sweep(1)
        parked = asyncio.create_task(asyncio.Event().wait())
        mon.watchers["s1"] = parked
        mon.acked["s1"] = (0, "old")
        monkeypatch.setattr(sessions, "resolve_agent", lambda pid: None)
        await mon._sweep(8)
        await asyncio.sleep(0)
        assert parked.cancelled()
        assert not mon.watchers and not mon.acked
        assert not mon.painter.colored
        app.windows.clear()
        await mon._sweep(16)
        assert not mon.painter.pane_prior
        assert not mon.painter.pane_tab
        assert not mon.painter.titled
    asyncio.run(scenario())


def test_unknown_sibling_cannot_hold_an_expired_alert(monkeypatch):
    async def scenario():
        alert, sibling = FakeSession("s1", ["Proceed? (y/n)"]), FakeSession("s2")
        mon, _ = build(monkeypatch, [alert, sibling])
        await mon._sweep(0)
        _go_quiet(mon.registry.states["s1"])
        await mon._sweep(1)
        assert mon.painter.pane_applied["s1"] is AgentState.ACTION
        # Both clocks become unavailable. The alert's content changes, while
        # the sibling still has current evidence for its own WORKING reading.
        mon._ensure_watcher = lambda sid: None
        for state in mon.registry.states.values():
            state.note_streamer(False, time.monotonic())
            _go_quiet(state)
        alert.lines = []
        mon.registry.states["s1"].dirty = True
        await mon._read_and_classify(mon.registry.states["s1"], time.monotonic())
        await mon._sweep(2)
        assert mon.registry.states["s2"].evidence_is_current()
        assert not mon.painter.colored
        assert not mon.painter.titled
    asyncio.run(scenario())


def test_failed_quiet_reread_keeps_current_evidence(monkeypatch):
    async def scenario():
        pane = FakeSession("s1", ["Proceed? (y/n)"])
        mon, _ = build(monkeypatch, [pane])
        await mon._sweep(0)
        state = mon.registry.states["s1"]
        _go_quiet(state)
        await mon._sweep(1)
        mon._ensure_watcher = lambda sid: None
        state.note_streamer(False, time.monotonic())
        pane.fail = True
        state.last_read_at = 0
        await mon._sweep(2)
        assert state.previous_classification is AgentState.UNKNOWN
        assert state.evidence_is_current()
        assert mon.painter.pane_applied["s1"] is AgentState.ACTION
    asyncio.run(scenario())
