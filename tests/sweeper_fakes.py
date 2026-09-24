"""Shared fakes and helpers for sweeper tests."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessions as sessionlib  # noqa: E402
from config import IDLE_AFTER_SECONDS  # noqa: E402
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



def _stopped_and_focused(monkeypatch, lines=("$ ready", "all done")):
    """One agent pane, stopped, with iTerm2 in front and that pane focused."""
    session = FakeSession("s1", lines=list(lines))
    mon, app = build(monkeypatch, [session])
    asyncio.run(mon._sweep(0))          # baseline reading
    _go_quiet(mon.registry.states["s1"])
    app.app_active = True               # s1 is the tab's active pane by default
    return mon, app, session

