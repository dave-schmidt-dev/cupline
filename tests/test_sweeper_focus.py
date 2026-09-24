"""Sweeper focus acknowledgement and screen-change tests."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import AgentState  # noqa: E402
from sweeper_fakes import _stopped_and_focused  # noqa: E402


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


def test_a_screen_differing_only_in_digits_is_a_different_screen(monkeypatch):
    """The regression for a hash shared between two different questions.

    ``screen_hash`` flattens plain numbers so a token counter ticking up cannot
    read as progress -- correct for the debounce, and it made
    ``Done. 3 tests failed.`` and ``Done. 5 tests failed.`` the same screen. A
    glance at the first therefore suppressed the second, which is a real stop
    the user never hears about. Fails against the shared hash.
    """
    mon, app, session = _stopped_and_focused(
        monkeypatch, lines=["$ ready", "Done. 3 tests failed."])
    state = mon.registry.states["s1"]
    asyncio.run(mon._sweep(1))
    assert mon.painter.pane_applied["s1"] is AgentState.WORKING, "the glance counts"
    seen_hash = state.last_screen_hash

    app.app_active = False              # user switches away
    session.lines = ["$ ready", "Done. 5 tests failed."]
    state.dirty = True                  # the agent ran again and stopped again
    asyncio.run(mon._sweep(2))

    assert state.last_screen_hash == seen_hash, (
        "premise: the debounce hash cannot tell these two screens apart, which "
        "is what made sharing it with the acknowledgement lose an alert"
    )
    assert mon.painter.pane_applied["s1"] is AgentState.WAITING, (
        "a different number is different content, and this stop is news"
    )


def test_the_acknowledgement_still_survives_a_moving_spinner(monkeypatch):
    """The other half of the same choice, and why the ack hash is not the raw text.

    The screen acknowledged is captured while the pane is still animating; the
    stop it has to match arrives after the animation has moved on. Keep spinner
    frames and elapsed counters in the ack hash and the rule never fires at all.
    """
    mon, app, session = _stopped_and_focused(
        monkeypatch, lines=["$ ready", "\u280b Thinking (3s)"])
    asyncio.run(mon._sweep(1))
    assert mon.painter.pane_applied["s1"] is AgentState.WORKING

    app.app_active = False
    state = mon.registry.states["s1"]
    session.lines = ["$ ready", "\u2819 Thinking (41s)"]
    state.dirty = True
    asyncio.run(mon._sweep(2))
    assert mon.painter.pane_applied["s1"] is AgentState.WORKING, (
        "animation is not content; this is the same screen the user read"
    )


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


def test_a_hang_you_glanced_at_is_not_reported_and_that_is_the_known_cost(monkeypatch):
    """Pinning an accepted hole, not asserting a desirable outcome.

    Every other way out of an acknowledgement is the screen changing, and a
    hang with nobody typing into it changes nothing. Glance at a pane inside
    IDLE_AFTER_SECONDS of it freezing and the acknowledged screen is the same
    screen that reads WAITING five seconds later, so the stop this tool most
    wants to catch is the one it swallows.

    Deliberately left open. The obvious gate -- only acknowledge a pane already
    reading as stopped -- closes this and undoes the rule, because the user's own
    typing is redraw activity: the pane they just left reads WORKING at the exact
    moment focus would record it, and is never acknowledged at all. So this test
    exists to make a future session argue with the trade-off rather than discover
    it. If you are here because you just fixed the hang case, check the pane the
    user actually complained about still stays dark.
    """
    mon, _, _ = _stopped_and_focused(
        monkeypatch, lines=["$ ready", "Refactoring the parser… (esc to interrupt)"])
    asyncio.run(mon._sweep(1))
    assert mon.registry.states["s1"].previous_classification is AgentState.WAITING, (
        "text never overrules the redraw clock: a frozen pane is still a stop"
    )
    assert mon.painter.pane_applied["s1"] is AgentState.WORKING, (
        "documented cost: the glance suppressed a hang"
    )



