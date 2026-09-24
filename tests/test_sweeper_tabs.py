"""Sweeper tab aggregation and streamer-failure tests."""

import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import AgentState  # noqa: E402
from sweeper_fakes import FakeSession, _go_quiet, build  # noqa: E402


def test_a_tab_says_how_many_agents_are_waiting_not_just_one(monkeypatch):
    """Three panes per tab is the normal layout here, and they finish together.

    The title can only name one project, so it has to say that there are more.
    Naming one and dropping the rest reported a third of the truth.
    """
    a = FakeSession("s1", lines=["$ ready", "done, over to you"])
    b = FakeSession("s2", lines=["$ ready", "also finished here"])
    c = FakeSession("s3", lines=["$ ready", "still going"])
    mon, app = build(monkeypatch, [a, b, c])
    tab = app.windows[0].tabs[0]

    asyncio.run(mon._sweep(0))
    for sid in ("s1", "s2"):
        _go_quiet(mon.registry.states[sid])
    asyncio.run(mon._sweep(1))

    title = mon.painter.titled[tab.tab_id]
    assert "+1" in title, f"a second waiting agent was invisible: {title!r}"

    # the third stops too; the count follows
    _go_quiet(mon.registry.states["s3"])
    asyncio.run(mon._sweep(2))
    assert "+2" in mon.painter.titled[tab.tab_id]


def test_a_single_waiting_agent_has_no_count_suffix(monkeypatch):
    """+0 would be noise on the common single-pane case."""
    only = FakeSession("s1", lines=["$ ready", "done, over to you"])
    mon, app = build(monkeypatch, [only])
    tab = app.windows[0].tabs[0]

    asyncio.run(mon._sweep(0))
    _go_quiet(mon.registry.states["s1"])
    asyncio.run(mon._sweep(1))

    assert "+" not in mon.painter.titled[tab.tab_id]


def test_only_the_stopped_pane_is_coloured_end_to_end(monkeypatch):
    """The reported bug, through the whole pipeline.

    One agent in a three-pane tab stops; the other two are still working. Every
    pane going amber told the user three agents wanted them. The active pane is
    the exception by design — it carries the tab aggregate so the tab bar shows
    the alert at all — so the assertion is on the two panes that are not focused.
    """
    stopped = FakeSession("s1", lines=["$ ready", "done, over to you"])
    busy_a = FakeSession("s2", lines=["$ ready", "still going"])
    busy_b = FakeSession("s3", lines=["$ ready", "also still going"])
    mon, app = build(monkeypatch, [stopped, busy_a, busy_b])
    tab = app.windows[0].tabs[0]
    tab.active = 0  # focus on the pane that stops

    asyncio.run(mon._sweep(0))
    _go_quiet(mon.registry.states["s1"])
    asyncio.run(mon._sweep(1))

    painted = mon.painter.pane_applied
    assert painted["s1"] is AgentState.WAITING
    assert painted["s2"] is AgentState.WORKING, "a working pane was painted amber"
    assert painted["s3"] is AgentState.WORKING, "a working pane was painted amber"


def test_the_focused_working_pane_still_carries_the_alert(monkeypatch):
    """Focus on a working pane must not hide a sibling's alert.

    iTerm2 draws the tab bar from the active session alone, so the focused pane
    is the only thing keeping the tab amber while a different pane waits.
    """
    busy = FakeSession("s1", lines=["$ ready", "still going"])
    stopped = FakeSession("s2", lines=["$ ready", "done, over to you"])
    mon, app = build(monkeypatch, [busy, stopped])
    tab = app.windows[0].tabs[0]
    tab.active = 0  # focus on the pane that keeps working

    asyncio.run(mon._sweep(0))
    _go_quiet(mon.registry.states["s2"])
    asyncio.run(mon._sweep(1))

    assert mon.painter.pane_applied["s1"] is AgentState.WAITING
    assert tab.tab_id in mon.painter.colored


def test_a_tab_whose_agent_quits_does_not_stay_coloured(monkeypatch):
    """Regression: the worst failure mode this tool has.

    Tabs are only repainted while something in them still votes, and votes come
    only from sessions that resolve as agents. Quit the harness back to a shell
    mid-alert and the tab stops voting: nothing visits it again, so the red
    stays until cupline exits. A stuck alert is indistinguishable from a real
    one and never clears, which is worse than never having raised it.
    """
    session = FakeSession("s1", lines=["$ ready", "Do you want to proceed? (y/n)"])
    mon, app = build(monkeypatch, [session])
    tab = app.windows[0].tabs[0]

    asyncio.run(mon._sweep(0))
    _go_quiet(mon.registry.states["s1"])
    asyncio.run(mon._sweep(1))
    assert tab.tab_id in mon.painter.colored, "precondition: the tab is alerting"

    # The user answers and quits claude; the pane is a plain shell now.
    mon.registry.states["s1"].agent = None
    asyncio.run(mon._sweep(2))

    assert tab.tab_id not in mon.painter.colored, (
        "the tab kept an alert for an agent that no longer exists"
    )


# -- the dead-streamer path -----------------------------------------------
# The redraw clock is the primary signal and the streamer is the only thing
# that advances it, so a streamer that dies does not degrade detection — it
# inverts it. These three cover the failure being loud, bounded, and not
# mistaken for a stop.


def test_a_dead_streamer_warns_and_backs_off(monkeypatch, caplog):
    """It used to fail silently and respawn forever.

    The failure was a single `log.debug` against a file handler set to WARNING,
    so nothing was recorded anywhere — while `_ensure_watcher` saw a finished
    task on every sweep and created another, a create-and-die loop at 2 Hz with
    no backoff and no ceiling.
    """
    async def scenario():
        session = FakeSession("s1")
        session.streamer_fails = True
        mon, _ = build(monkeypatch, [session])
        del mon._ensure_watcher          # use the real one, not the stub
        await mon.registry.discover(mon.app)

        with caplog.at_level(logging.WARNING, logger="cupline"):
            mon._ensure_watcher("s1")
            await mon.watchers["s1"]     # let it open, fail, and record
            first = mon.watchers["s1"]
            assert mon._watcher_failures["s1"] == 1

            mon._ensure_watcher("s1")    # the very next sweep
            assert mon.watchers["s1"] is first, "respawned while backing off"

        assert any(r.levelno >= logging.WARNING for r in caplog.records), \
            "a dead streamer left no record at WARNING or above"
        assert mon.registry.states["s1"].streamer_ok is False

    asyncio.run(scenario())


def test_the_backoff_grows_and_is_cleared_by_a_recovery(monkeypatch):
    async def scenario():
        session = FakeSession("s1")
        session.streamer_fails = True
        mon, _ = build(monkeypatch, [session])
        del mon._ensure_watcher
        await mon.registry.discover(mon.app)

        delays = []
        for _ in range(3):
            mon._watcher_retry_at.pop("s1", None)   # pretend the wait elapsed
            mon._ensure_watcher("s1")
            await mon.watchers["s1"]
            delays.append(mon._watcher_retry_at["s1"] - time.monotonic())
        assert delays[0] < delays[1] < delays[2], f"backoff did not grow: {delays}"

        # The streamer comes back: the next open clears the penalty entirely.
        session.streamer_fails = False
        mon._watcher_retry_at.pop("s1", None)
        mon._ensure_watcher("s1")
        await asyncio.sleep(0)
        assert "s1" not in mon._watcher_failures
        assert "s1" not in mon._watcher_retry_at
        assert mon.registry.states["s1"].streamer_ok is True
        mon._drop_watcher("s1")

    asyncio.run(scenario())


