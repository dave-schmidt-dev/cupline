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

import sessions as sessionlib  # noqa: E402
from config import IDLE_AFTER_SECONDS  # noqa: E402
from models import AgentState  # noqa: E402
from cupline import Cupline  # noqa: E402


class FakeLine:
    def __init__(self, text):
        self.string = text


class FakeContents:
    def __init__(self, lines):
        self._lines = lines

    @property
    def number_of_lines(self):
        return len(self._lines)

    def line(self, i):
        return FakeLine(self._lines[i])


class FakeSession:
    def __init__(self, session_id, lines=("$ ready",)):
        self.session_id = session_id
        self.lines = list(lines)
        self.fetches = 0
        self.fail = False
        #: when True, every variable read raises SESSION_NOT_FOUND
        self.vanish = False
        #: when True, opening the screen streamer raises — the Task 15 case
        self.streamer_fails = False
        #: called mid-await, to simulate a screen event landing during the fetch
        self.during_fetch = None
        self.pushes = []

    async def async_get_variable(self, name):
        if self.vanish:
            # What iTerm2 actually answers for a pane that closed between the
            # caller's enumeration and this RPC.
            raise RuntimeError("SESSION_NOT_FOUND")
        return {"jobPid": "1234", "jobName": "node", "path": "/tmp/demo",
                "tty": "/dev/ttys001"}.get(name)

    async def async_get_screen_contents(self):
        self.fetches += 1
        await asyncio.sleep(0)  # a real await, so the race is reproducible
        if self.during_fetch is not None:
            self.during_fetch()
        if self.fail:
            raise RuntimeError("session went away mid-fetch")
        return FakeContents(self.lines)

    async def async_set_profile_properties(self, profile):
        self.pushes.append(profile)

    def get_screen_streamer(self, want_contents=False):
        return FakeStreamerCM(self.streamer_fails)


class FakeStreamerCM:
    """iTerm2's screen streamer is a sync call returning an async CM."""

    def __init__(self, fails):
        self.fails = fails

    async def __aenter__(self):
        if self.fails:
            raise RuntimeError("streamer refused")
        return self

    async def __aexit__(self, *exc):
        return False

    async def async_get(self):
        await asyncio.Event().wait()  # park; tests inject events directly


class FakeTab:
    def __init__(self, tab_id, sessions, active=0):
        self.tab_id = tab_id
        self.sessions = sessions
        #: Index of the focused pane. A real tab always has one, and the painter
        #: gives it the tab aggregate so the tab bar stays correct — so the
        #: default here matters: leaving it unset would run every sweeper test
        #: through the paint-everything fallback instead of the real path.
        self.active = active

    @property
    def current_session(self):
        return self.sessions[self.active] if self.sessions else None

    async def async_set_title(self, title):
        pass


class FakeWindow:
    def __init__(self, window_id, tabs, current=0):
        self.window_id = window_id
        self.tabs = tabs
        self.current = current

    @property
    def current_tab(self):
        return self.tabs[self.current] if self.tabs else None


class FakeApp:
    def __init__(self, windows):
        self.windows = windows
        #: Mirrors ``iterm2.App``: whether iTerm2 is the frontmost application.
        #: False by default, which is the "nobody is looking" case — so a test
        #: that says nothing about focus acknowledges nothing, and the focus
        #: rule has to be opted into explicitly.
        self.app_active = False

    @property
    def current_window(self):
        return self.windows[0] if self.windows else None

    def get_session_by_id(self, session_id):
        for w in self.windows:
            for t in w.tabs:
                for s in t.sessions:
                    if s.session_id == session_id:
                        return s
        return None


def build(monkeypatch, sessions):
    """A Cupline wired to fakes, with every session resolving as an agent."""
    monkeypatch.setattr(sessionlib, "resolve_agent", lambda pid, **kw: "claude")
    app = FakeApp([FakeWindow("w0", [FakeTab("t0", sessions)])])
    mon = Cupline(connection=None, app=app, show_tail=False, debounce=1.5)

    def stub_watcher(session_id):
        """Stand in for a streamer that opened successfully.

        There are no real streamers here, but marking the session's redraw clock
        as fed is exactly what a live watcher does the moment it opens — and the
        classifier abstains when that clock is not being fed, so a stub that
        skips it would run the whole suite through the dead-streamer path
        instead of the one under test.
        """
        state = mon.registry.states.get(session_id)
        if state is not None:
            state.streamer_ok = True

    mon._ensure_watcher = stub_watcher
    return mon, app


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


# -- the idle path --------------------------------------------------------
# These go through the sweeper on purpose. Calling snapshot() directly at an
# arbitrary later time proves the arithmetic, not that anything ever calls it
# at that time — which is precisely how the defect below survived a green suite.

def _go_quiet(state, seconds=None):
    """Rewind the redraw clock so the pane reads as having stopped.

    A fresh SessionState dates ``last_event_at`` from construction, so it is
    WORKING until it has been observably silent. Tests that care about a stopped
    pane have to age it rather than just assert on its text — which is the whole
    point of the signal.
    """
    state.last_event_at -= (IDLE_AFTER_SECONDS * 2 if seconds is None else seconds)
    return state


def _finish_a_turn(mon, session, at):
    """Drive a session through 'agent printed its last line and went quiet'."""
    state = mon.registry.states[session.session_id]
    session.lines = ["$ ready", "All done. What would you like next?"]
    state.note_event(at)
    asyncio.run(mon._read_and_classify(state, at))
    return state


def test_a_quiet_session_is_read_again_so_stability_can_be_confirmed(monkeypatch):
    """A settled session emits no events, so only the sweeper can revisit it.

    `stable_since` is only set by a *second* reading that finds the screen
    unchanged. With no re-read there is no second reading, so a session that
    finished its turn reported zero stable seconds for as long as it sat there.
    """
    session = FakeSession("s1")
    mon, _ = build(monkeypatch, [session])
    asyncio.run(mon._sweep(0))
    state = _finish_a_turn(mon, session, 100.0)

    assert state.dirty is False and state.stable_since is None
    assert mon._should_read(state, 100.5) is False, "no point re-reading immediately"
    assert mon._should_read(state, 110.0) is True, "a quiet session is never revisited"

    asyncio.run(mon._read_and_classify(state, 110.0))
    assert state.stable_since == 110.0


def test_the_classifier_sees_stability_grow_while_the_screen_is_quiet(monkeypatch):
    """The seam the whole spike exists to prove.

    classify() used to run only when the content had just moved — and that same
    call pinned both content clocks to *now*, so every snapshot it ever received
    read `seconds_since_change == 0` and `seconds_stable == 0`. The temporal
    half of the classifier input was structurally unreachable.
    """
    import classifier

    seen = []
    monkeypatch.setattr(classifier, "classify",
                        lambda snap: seen.append(snap) or classifier.AgentState.UNKNOWN)

    session = FakeSession("s1")
    mon, _ = build(monkeypatch, [session])
    asyncio.run(mon._sweep(0))
    state = _finish_a_turn(mon, session, 100.0)

    now = 100.0
    for _ in range(6):
        now += 5.0
        if mon._should_read(state, now):
            asyncio.run(mon._read_and_classify(state, now))

    assert len(seen) >= 3, f"classifier ran {len(seen)} times over 30 quiet seconds"
    assert seen[-1].seconds_since_change >= 25.0
    assert seen[-1].seconds_stable >= 20.0
    assert seen[-1].seconds_since_redraw >= 25.0
    # and it must still be monotonic, not sawtoothing on each re-read
    stable = [s.seconds_stable for s in seen]
    assert stable == sorted(stable), f"stability went backwards: {stable}"


def test_a_content_change_resets_the_accumulated_stability(monkeypatch):
    """The agent starting work again must not inherit the idle period's clock."""
    session = FakeSession("s1")
    mon, _ = build(monkeypatch, [session])
    asyncio.run(mon._sweep(0))
    state = _finish_a_turn(mon, session, 100.0)
    asyncio.run(mon._read_and_classify(state, 130.0))  # confirms stability
    asyncio.run(mon._read_and_classify(state, 160.0))  # lets it accumulate
    # dated from the confirming read at 130, not from the change at 100: between
    # two readings the screen could have moved and moved back unobserved
    assert state.snapshot(160.0).seconds_stable == 30.0
    assert state.snapshot(160.0).seconds_since_change == 60.0

    session.lines = ["$ ready", "Editing config.py"]  # the human replied
    state.note_event(161.0)
    asyncio.run(mon._read_and_classify(state, 163.0))

    snap = state.snapshot(163.0)
    assert snap.seconds_stable == 0.0
    assert snap.seconds_since_change == 0.0


def test_a_state_does_not_outlive_the_screen_that_justified_it(monkeypatch):
    """Observed live: a prompt matched for 6.7s, was answered, tab stayed red.

    Every reading after the prompt cleared was UNKNOWN, and UNKNOWN holds the
    previous state — so the red became permanent. Holding is right while the
    evidence is still on screen and wrong once it has scrolled away.
    """
    session = FakeSession("s1", lines=["$ ready", "Do you want to proceed? (y/n)"])
    mon, app = build(monkeypatch, [session])
    tab = app.windows[0].tabs[0]
    asyncio.run(mon._sweep(0))

    # A pane only reads as blocked once it has been observably silent; a prompt
    # that just appeared is still a repainting screen.
    state = mon.registry.states["s1"]
    _go_quiet(state)
    asyncio.run(mon._sweep(1))
    assert state.previous_classification is AgentState.ACTION
    assert tab.tab_id in mon.painter.colored, "the prompt should have painted the tab"
    assert state.evidence_is_current() is True, "the prompt is still on screen"
    prompt_hash = state.confident_hash

    # the human answers: the screen changes and the pane starts repainting again
    session.lines = ["$ ready", "Applying the edit now", "  wrote config.py"]
    state.note_event(time.monotonic())
    state.last_read_at -= 10.0  # bring the max-unread ceiling due
    asyncio.run(mon._sweep(2))

    assert state.previous_classification is AgentState.WORKING
    assert state.confident_hash != prompt_hash, \
        "the state is still anchored to the answered prompt"
    assert tab.tab_id not in mon.painter.colored, "red outlived the prompt that caused it"


def test_panes_with_no_state_of_their_own_do_not_hold_a_tab_red(monkeypatch):
    """Observed live on a 3-pane tab: the red survived the pane that earned it.

    Only one pane ever hit ACTION. The other two had never been classified
    confidently — and under the first version of the staleness rule, "never
    confident" counted as "nothing has gone stale", so they voted to hold. Two
    panes with no state of their own kept a colour the third had moved past.
    """
    prompt = FakeSession("s1", lines=["$ ready", "Continue? (y/n)"])
    quiet_a = FakeSession("s2", lines=["$ ready", "some unremarkable output"])
    quiet_b = FakeSession("s3", lines=["$ ready", "more unremarkable output"])
    mon, app = build(monkeypatch, [prompt, quiet_a, quiet_b])
    tab = app.windows[0].tabs[0]
    asyncio.run(mon._sweep(0))

    states = mon.registry.states
    # The bystanders never go quiet, so they never earn a confident state --
    # which is exactly the condition that used to make them vote to hold.
    _go_quiet(states["s1"])
    asyncio.run(mon._sweep(1))
    assert states["s1"].previous_classification is AgentState.ACTION
    assert mon.painter.applied[tab.tab_id] is AgentState.ACTION
    # The bystanders are busy, so their own state is WORKING -- they have no
    # claim on the red and must not be able to keep it alive.
    assert states["s2"].previous_classification is AgentState.WORKING

    # The prompt is answered and that pane goes unreadable, so it abstains.
    # Nothing on the tab can point at a screen justifying red any more.
    prompt.lines = ["   ", "  "]
    states["s1"].note_event(time.monotonic())
    states["s1"].last_read_at -= 10.0  # bring the max-unread ceiling due
    asyncio.run(mon._sweep(2))

    assert states["s1"].previous_classification is AgentState.WORKING
    assert mon.painter.applied[tab.tab_id] is not AgentState.ACTION, \
        "bystander panes held a red that nothing on screen supports"


def test_an_unknown_still_holds_while_the_prompt_is_on_screen(monkeypatch):
    """The flicker case the hold exists for must survive the staleness fix."""
    session = FakeSession("s1", lines=["$ ready", "Do you want to proceed? (y/n)"])
    mon, app = build(monkeypatch, [session])
    tab = app.windows[0].tabs[0]
    asyncio.run(mon._sweep(0))
    _go_quiet(mon.registry.states["s1"])
    asyncio.run(mon._sweep(1))
    assert tab.tab_id in mon.painter.colored

    import classifier
    monkeypatch.setattr(classifier, "classify", lambda snap: AgentState.UNKNOWN)

    state = mon.registry.states["s1"]
    for tick in range(2, 13):  # screen unchanged; classifier now abstains
        asyncio.run(mon._sweep(tick))

    assert state.evidence_is_current() is True
    assert tab.tab_id in mon.painter.colored, "hold released while the prompt was visible"


def test_idle_rechecks_do_not_re_log_an_unchanged_state(monkeypatch, caplog):
    """Re-reading a quiet session must not turn the log into a heartbeat."""
    import logging
    session = FakeSession("s1")
    mon, _ = build(monkeypatch, [session])
    asyncio.run(mon._sweep(0))
    state = _finish_a_turn(mon, session, 100.0)

    with caplog.at_level(logging.INFO, logger="cupline"):
        now = 100.0
        for _ in range(10):
            now += 5.0
            if mon._should_read(state, now):
                asyncio.run(mon._read_and_classify(state, now))

    # One line is correct and wanted: the tick where the pane crossed from
    # working to stopped. What must not happen is a line per recheck after it.
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1, f"an idle session logged on every recheck: {messages}"
    assert "state=waiting (was working)" in messages[0]


def test_a_pane_that_stops_repainting_is_reported_without_reading_its_text(monkeypatch):
    """The requirement, end to end: tell me when an agent stopped, whatever the reason.

    Deliberately uses a tail that matches no rule at all. Under the text-only
    classifier this session was UNKNOWN forever and its tab was never painted —
    the agent could sit finished all night in silence. The only thing that
    changes between the two assertions is elapsed redraw time.
    """
    session = FakeSession("s1", lines=["$ ready", "some output with no prompt in it"])
    mon, app = build(monkeypatch, [session])
    tab = app.windows[0].tabs[0]

    asyncio.run(mon._sweep(0))
    assert mon.registry.states["s1"].previous_classification is AgentState.WORKING
    assert tab.tab_id not in mon.painter.colored, "a busy pane must not be painted"

    _go_quiet(mon.registry.states["s1"])
    asyncio.run(mon._sweep(1))

    assert mon.registry.states["s1"].previous_classification is AgentState.WAITING
    assert tab.tab_id in mon.painter.colored, "a stopped agent went unreported"


def test_a_hung_pane_still_claiming_to_be_busy_is_reported(monkeypatch):
    """"esc to interrupt" on a frozen screen is a stop, not a reassurance."""
    session = FakeSession("s1", lines=["$ ready", "Thinking... (esc to interrupt)"])
    mon, app = build(monkeypatch, [session])
    tab = app.windows[0].tabs[0]

    asyncio.run(mon._sweep(0))
    _go_quiet(mon.registry.states["s1"])
    asyncio.run(mon._sweep(1))

    assert mon.registry.states["s1"].previous_classification is AgentState.WAITING
    assert tab.tab_id in mon.painter.colored, "a hung agent was reported as busy"


def test_the_stop_verdict_needs_no_screen_fetch(monkeypatch):
    """Detection must not wait on an RPC, or it inherits the recheck interval."""
    session = FakeSession("s1", lines=["$ ready", "output"])
    mon, app = build(monkeypatch, [session])

    asyncio.run(mon._sweep(0))
    state = mon.registry.states["s1"]
    fetches_before = session.fetches

    _go_quiet(state)
    state.dirty = False
    state.last_read_at = time.monotonic()  # nothing is due to be re-read
    assert mon._should_read(state, time.monotonic()) is False
    asyncio.run(mon._sweep(1))

    assert session.fetches == fetches_before, "classified via an unnecessary fetch"
    assert state.previous_classification is AgentState.WAITING


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


def _stopped_and_focused(monkeypatch, lines=("$ ready", "all done")):
    """One agent pane, stopped, with iTerm2 in front and that pane focused."""
    session = FakeSession("s1", lines=list(lines))
    mon, app = build(monkeypatch, [session])
    asyncio.run(mon._sweep(0))          # baseline reading
    _go_quiet(mon.registry.states["s1"])
    app.app_active = True               # s1 is the tab's active pane by default
    return mon, app, session


def test_a_pane_you_are_looking_at_is_not_reported(monkeypatch):
    """The alert answers "something happened you do not know about".

    A pane holding keyboard focus while iTerm2 is frontmost is on screen in
    front of the user, so amber on it the moment they switch tabs is the tool
    reporting a screen they just read.
    """
    mon, _, _ = _stopped_and_focused(monkeypatch)
    asyncio.run(mon._sweep(1))
    assert mon.registry.states["s1"].previous_classification is AgentState.WAITING, (
        "the classifier's own verdict must be untouched; only the report changes"
    )
    assert mon.painter.pane_applied["s1"] is AgentState.WORKING
    assert mon.painter.applied["t0"] is AgentState.WORKING


def test_nothing_is_acknowledged_while_iterm2_is_in_the_background(monkeypatch):
    """The counter-case, and the reason the app-active gate exists.

    The library goes on reporting a current window, tab and session when iTerm2
    is not in front -- that is the last pane to hold focus, not one anybody is
    reading. Without this gate every pane the user touched before switching to a
    browser would be treated as watched, which is the whole alert.
    """
    mon, app, _ = _stopped_and_focused(monkeypatch)
    app.app_active = False
    asyncio.run(mon._sweep(1))
    assert mon.painter.pane_applied["s1"] is AgentState.WAITING


def test_an_unknown_focus_state_acknowledges_nothing(monkeypatch):
    """``app_active`` is None until a focus notification has been seen."""
    mon, app, _ = _stopped_and_focused(monkeypatch)
    app.app_active = None
    asyncio.run(mon._sweep(1))
    assert mon.painter.pane_applied["s1"] is AgentState.WAITING


def test_the_acknowledgement_dies_with_the_screen_it_was_given_for(monkeypatch):
    """Look at a pane, leave, and the *next* stop must still alert.

    This is what keeps the rule from being an off switch. The acknowledgement is
    keyed on the screen hash, so anything the agent does voids it -- the user
    acknowledged a screen, not a session.
    """
    mon, app, session = _stopped_and_focused(monkeypatch)
    asyncio.run(mon._sweep(1))
    assert mon.painter.pane_applied["s1"] is AgentState.WORKING

    app.app_active = False              # user switches to another application
    state = mon.registry.states["s1"]
    session.lines = ["$ ready", "all done", "and here is something new"]
    state.dirty = True                  # the agent worked, then stopped again
    asyncio.run(mon._sweep(2))

    assert mon.painter.pane_applied["s1"] is AgentState.WAITING
    assert "s1" not in mon.acked, "a spent acknowledgement must not linger"


def test_looking_at_a_control_does_not_answer_it(monkeypatch):
    """Scoped to amber on purpose.

    Red means an agent is blocked at a control. Having seen the prompt is not
    the same as having answered it, and a blocked agent that stops asking is the
    worst failure this tool has -- so ACTION is never acknowledged.
    """
    mon, _, _ = _stopped_and_focused(
        monkeypatch, lines=["$ ready", "Proceed with this change? (y/n)"])
    asyncio.run(mon._sweep(1))
    assert mon.painter.pane_applied["s1"] is AgentState.ACTION


def test_a_closed_pane_takes_its_acknowledgement_with_it(monkeypatch):
    mon, _, _ = _stopped_and_focused(monkeypatch)
    asyncio.run(mon._sweep(1))
    assert "s1" in mon.acked
    asyncio.run(mon.on_session_gone("s1"))
    assert "s1" not in mon.acked


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
