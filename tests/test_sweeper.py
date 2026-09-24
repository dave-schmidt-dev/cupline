"""Sweeper behaviour: the debounce race and the rediscovery fallback.

Both tests here exist because of real defects, and both are about *silent*
failure — a session that stops being classified, or one that is never seen at
all. Neither shows up as an error in the log; the tab just quietly stays wrong.
"""

import asyncio
import logging
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import IDLE_AFTER_SECONDS  # noqa: E402
from models import AgentState  # noqa: E402
import sessions as sessionlib  # noqa: E402
from sweeper_fakes import FakeSession, build  # noqa: E402


def test_an_event_during_the_fetch_is_not_lost(monkeypatch):
    """The bug: `dirty` cleared after the await erased a change we hadn't read.

    A session that changes during the fetch and then goes quiet emits no further
    events, so the lost flag meant it was never re-read — losing exactly the
    "agent finished and is waiting" transition.
    """
    session = FakeSession("s1")
    mon, _ = build(monkeypatch, [session])
    asyncio.run(mon._sweep(0))  # baseline reading

    state = mon.registry.states["s1"]
    state.dirty = False

    def event_lands():
        state.dirty = True  # the streamer, mid-await

    session.during_fetch = event_lands
    session.lines = ["$ ready", "Do you want to proceed? (y/n)"]
    asyncio.run(mon._read_and_classify(state, 100.0))

    assert state.dirty is True, "the mid-fetch event was silently dropped"
    assert mon._should_read(state, 200.0) is True


def test_a_failed_fetch_leaves_the_session_readable(monkeypatch):
    session = FakeSession("s1")
    mon, _ = build(monkeypatch, [session])
    asyncio.run(mon._sweep(0))

    state = mon.registry.states["s1"]
    state.dirty = True
    session.fail = True
    asyncio.run(mon._read_and_classify(state, 100.0))

    assert state.dirty is True, "a transient fetch failure retired the session"


def test_rediscovery_finds_a_session_the_monitor_never_announced(monkeypatch):
    """NewSessionMonitor must be an optimisation, not a single point of failure."""
    first = FakeSession("s1")
    mon, app = build(monkeypatch, [first])
    asyncio.run(mon._sweep(0))
    assert set(mon.registry.states) == {"s1"}

    # A session appears with no on_new_session call — a missed or late event.
    app.windows[0].tabs[0].sessions.append(FakeSession("s2"))

    asyncio.run(mon._sweep(1))
    assert "s2" not in mon.registry.states, "non-periodic ticks should not rediscover"

    asyncio.run(mon._sweep(8))  # the periodic branch
    assert "s2" in mon.registry.states
    assert mon.registry.states["s2"].is_agent()


def test_rediscovery_reaps_the_watcher_of_a_vanished_session(monkeypatch):
    """discover() prunes vanished sessions; their watcher tasks must go too."""
    async def scenario():
        session = FakeSession("s1")
        mon, app = build(monkeypatch, [session])
        await mon._sweep(0)

        parked = asyncio.create_task(asyncio.Event().wait())
        mon.watchers["s1"] = parked

        app.windows[0].tabs[0].sessions.clear()  # closed without an event
        await mon._sweep(8)

        assert "s1" not in mon.registry.states
        assert "s1" not in mon.watchers, "watcher task leaked"
        await asyncio.sleep(0)
        assert parked.cancelled()

    asyncio.run(scenario())


def test_one_vanished_pane_does_not_abandon_its_peers(monkeypatch):
    """Caught live at 22:21:33: one closed pane cost every pane its tick.

    `refresh_agents` enumerates the windows, then awaits per session. A pane
    that closes in between gets SESSION_NOT_FOUND, which used to propagate all
    the way out to the blanket handler in `sweep_forever` — abandoning the tick,
    so the *other* panes went unclassified and unpainted because one pane
    closed. All three variable reads are covered, not just `jobPid`.
    """
    doomed = FakeSession("s1")
    alive = FakeSession("s2", lines=["$ ready", "Do you want to proceed? (y/n)"])
    mon, _ = build(monkeypatch, [doomed, alive])
    asyncio.run(mon._sweep(0))            # baseline: both known and classified
    assert {"s1", "s2"} <= set(mon.registry.states)

    doomed.vanish = True
    pushes_before = len(alive.pushes)
    asyncio.run(mon._sweep(8))            # the periodic branch: refresh_agents runs

    assert "s1" not in mon.registry.states, "a vanished session must be dropped"
    peer = mon.registry.states["s2"]
    assert peer.agent == "claude", "the peer lost its identity to its neighbour"
    assert peer.previous_classification is not AgentState.UNKNOWN
    assert len(alive.pushes) >= pushes_before, "the peer went unpainted"


def test_a_vanished_pane_is_skipped_on_every_variable_read(monkeypatch):
    """`jobName` and `path` are read the same unguarded way as `jobPid`.

    The original code guarded only the `int()` cast, so a session surviving the
    first read and dying before the third still took the sweep down.
    """
    class DiesLate(FakeSession):
        def __init__(self, session_id):
            super().__init__(session_id)
            self.reads = 0

        async def async_get_variable(self, name):
            self.reads += 1
            if self.vanish and name == "path":
                raise RuntimeError("SESSION_NOT_FOUND")
            return await FakeSession.async_get_variable(self, name)

    doomed = DiesLate("s1")
    alive = FakeSession("s2")
    mon, _ = build(monkeypatch, [doomed, alive])
    asyncio.run(mon._sweep(0))

    doomed.vanish = True
    asyncio.run(mon._sweep(8))            # must not raise

    assert "s1" not in mon.registry.states
    assert "s2" in mon.registry.states


def test_sweep_survives_a_session_that_disappears_mid_tick(monkeypatch):
    session = FakeSession("s1")
    mon, app = build(monkeypatch, [session])
    asyncio.run(mon._sweep(0))

    app.windows[0].tabs[0].sessions.clear()
    mon.registry.states["s1"].dirty = True
    asyncio.run(mon._sweep(1))  # must not raise


def test_a_frozen_redraw_clock_is_never_read_as_a_stop(monkeypatch):
    """The bug this whole flag exists for.

    When the watcher dies nothing advances `last_event_at` again, so the clock
    sails past IDLE_AFTER_SECONDS by itself and every later reading says the
    agent stopped — permanently, and with no evidence behind it. The counter-
    case matters as much: the identical session with a live streamer must still
    report the stop, or the guard has simply disabled detection.
    """
    session = FakeSession("s1", lines=["$ ready", "all done"])
    mon, _ = build(monkeypatch, [session])
    asyncio.run(mon._sweep(0))
    state = mon.registry.states["s1"]
    assert state.streamer_ok is True

    later = time.monotonic() + IDLE_AFTER_SECONDS + 60

    state.streamer_ok = False
    mon._conclude(state, later, changed=False)
    assert state.previous_classification is AgentState.UNKNOWN, \
        "a session with no working streamer was reported stopped"

    state.streamer_ok = True
    mon._conclude(state, later, changed=False)
    assert state.previous_classification is AgentState.WAITING, \
        "the guard suppressed a real stop"


def test_a_session_that_vanishes_before_its_watcher_opens_backs_off(monkeypatch):
    """`get_session_by_id` returning None is a clean return, not an exception.

    It has the same consequence as a raising streamer — nothing will feed the
    clock — so it has to count as a failure, or that path keeps the 2 Hz
    respawn loop the backoff was added to stop.
    """
    async def scenario():
        session = FakeSession("s1")
        mon, _ = build(monkeypatch, [session])
        del mon._ensure_watcher
        await mon.registry.discover(mon.app)

        mon.app.windows[0].tabs[0].sessions.clear()  # closed before the open
        mon._ensure_watcher("s1")
        await mon.watchers["s1"]

        assert mon._watcher_failures["s1"] == 1
        assert "s1" in mon._watcher_retry_at
        assert mon.registry.states["s1"].streamer_ok is False

    asyncio.run(scenario())


def test_a_periodic_tick_reads_the_process_table_once(monkeypatch):
    """Two passes over the panes, one `ps`.

    `discover` and `refresh_agents` both refresh, and at a 2 s cache TTL
    against a 4 s period each used to fork its own read of a ~2500-process
    table, back to back, for the same answer. Production logs showed the pair
    plainly: 15 of 15 closely-spaced slow reads had the second starting within
    5-51 ms of the first finishing. Counting forks rather than asserting on the
    call that forces them keeps this honest if the refresh moves again.
    """
    async def scenario():
        mon, _ = build(monkeypatch, [FakeSession("s1"), FakeSession("s2")])

        forks = 0

        async def counting_ps():
            nonlocal forks
            forks += 1
            return "1 0 -zsh\n"

        monkeypatch.setattr(sessionlib, "_run_ps", counting_ps)
        monkeypatch.setattr(sessionlib, "_PS_CACHE", {}, raising=False)
        monkeypatch.setattr(sessionlib, "_PS_CACHE_AT", 0.0, raising=False)

        await mon._sweep(8)          # the periodic branch: both passes run

        assert forks == 1, f"the periodic tick forked ps {forks} times, not once"

    asyncio.run(scenario())


def test_a_process_table_failure_is_not_reported_as_vanished_panes(monkeypatch, caplog):
    """The refresh belongs outside the per-session guard, and this pins it there.

    `describe()` used to refresh the process table itself, which put the refresh
    inside discover()'s `except Exception` -> "session vanished" handler. Any
    error escaping the refresh would then have been attributed to every pane in
    turn as a pane that had closed — a whole-machine condition logged as a dozen
    unrelated local ones, and the sessions skipped rather than described.

    `refresh_process_table` swallows its own errors today, so this is a guard
    against a future edit rather than a live bug: it fails the moment the
    refresh moves back inside the guard.
    """
    async def scenario():
        mon, _ = build(monkeypatch, [FakeSession("s1"), FakeSession("s2")])

        async def exploding_refresh(force: bool = False):
            raise OSError("process table unavailable")

        monkeypatch.setattr(sessionlib, "refresh_process_table", exploding_refresh)

        with caplog.at_level(logging.INFO, logger="cupline"), pytest.raises(OSError):
            await mon.registry.discover(mon.app)

        assert not any("vanished" in r.getMessage() for r in caplog.records), \
            "a process-table failure was misreported as panes closing"

    asyncio.run(scenario())
