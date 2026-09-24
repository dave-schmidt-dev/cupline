"""Sweeper idle-path stability and stopped-session detection tests."""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import AgentState  # noqa: E402
from sweeper_fakes import FakeSession, _finish_a_turn, _go_quiet, build  # noqa: E402


# -- the idle path --------------------------------------------------------
# These go through the sweeper on purpose. Calling snapshot() directly at an
# arbitrary later time proves the arithmetic, not that anything ever calls it
# at that time — which is precisely how the defect below survived a green suite.


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


