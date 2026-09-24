"""Tab-state per-pane painting, stale bookkeeping, and pane-change tests."""

from asyncio import run
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import AgentState, PaneVerdict  # noqa: E402
from tab_state import TabPainter  # noqa: E402
from tab_state_fakes import FakeApp, FakeSession, FakeTab, colors_of  # noqa: E402


def test_only_the_waiting_pane_is_coloured():
    tab = FakeTab("t1", pane_count=3, active="t1-p0")
    painter = TabPainter()
    verdicts = {
        "t1-p0": PaneVerdict(AgentState.WAITING, True),
        "t1-p1": PaneVerdict(AgentState.WORKING, True),
        "t1-p2": PaneVerdict(AgentState.WORKING, True),
    }
    assert run(painter.apply(tab, AgentState.WAITING, per_pane=verdicts)) is True
    assert colors_of(tab, painter) == [
        AgentState.WAITING, AgentState.WORKING, AgentState.WORKING,
    ]


def test_the_active_pane_carries_the_tab_aggregate():
    """Otherwise the tab bar goes dark whenever a working pane has focus.

    iTerm2 renders a tab's bar entry from its active session alone, so this is
    the one pane that cannot be allowed to tell the truth about itself.
    """
    tab = FakeTab("t1", pane_count=3, active="t1-p1")
    painter = TabPainter()
    verdicts = {
        "t1-p0": PaneVerdict(AgentState.ACTION, True),
        "t1-p1": PaneVerdict(AgentState.WORKING, True),
        "t1-p2": PaneVerdict(AgentState.WORKING, True),
    }
    run(painter.apply(tab, AgentState.ACTION, per_pane=verdicts))
    assert colors_of(tab, painter) == [
        AgentState.ACTION,   # genuinely wants you
        AgentState.ACTION,   # active: carries the aggregate for the tab bar
        AgentState.WORKING,  # working, and says so
    ]


def test_an_unwatched_pane_gets_no_colour():
    """A plain shell split has no verdict, so it makes no claim."""
    tab = FakeTab("t1", pane_count=3, active="t1-p0")
    painter = TabPainter()
    verdicts = {"t1-p0": PaneVerdict(AgentState.WORKING, True)}
    run(painter.apply(tab, AgentState.WORKING, per_pane=verdicts))
    assert colors_of(tab, painter) == [AgentState.WORKING] * 3


def test_an_unwatched_active_pane_still_carries_the_aggregate():
    """The active-pane rule cannot be gated on being a watched agent.

    A shell sitting in the focused split next to a waiting agent would otherwise
    leave nothing holding the tab colour, and the alert would vanish.
    """
    tab = FakeTab("t1", pane_count=2, active="t1-p1")
    painter = TabPainter()
    verdicts = {"t1-p0": PaneVerdict(AgentState.WAITING, True)}
    run(painter.apply(tab, AgentState.WAITING, per_pane=verdicts))
    assert colors_of(tab, painter) == [AgentState.WAITING, AgentState.WAITING]


def test_an_unknown_active_pane_falls_back_to_painting_everything():
    """Fail safe: a spurious colour is recoverable, a missed alert is not."""
    tab = FakeTab("t1", pane_count=3, active=None)
    painter = TabPainter()
    verdicts = {
        "t1-p0": PaneVerdict(AgentState.WAITING, True),
        "t1-p1": PaneVerdict(AgentState.WORKING, True),
    }
    run(painter.apply(tab, AgentState.WAITING, per_pane=verdicts))
    assert colors_of(tab, painter) == [AgentState.WAITING] * 3


def test_moving_focus_repaints_even_though_the_aggregate_did_not_change():
    """The regression the tab-level dedupe would have caused.

    Focus moving from the waiting pane to a working one leaves the tab's
    aggregate identical, so a dedupe keyed on the aggregate returns early — and
    the newly-active pane keeps no colour, dropping the tab bar to grey while an
    agent is still waiting.
    """
    painter = TabPainter()
    verdicts = {
        "t1-p0": PaneVerdict(AgentState.WAITING, True),
        "t1-p1": PaneVerdict(AgentState.WORKING, True),
    }
    first = FakeTab("t1", pane_count=2, active="t1-p0")
    run(painter.apply(first, AgentState.WAITING, per_pane=verdicts))

    moved = FakeTab("t1", pane_count=2, active="t1-p1")
    assert run(painter.apply(moved, AgentState.WAITING, per_pane=verdicts)) is True
    assert colors_of(moved, painter) == [AgentState.WAITING, AgentState.WAITING]


def test_a_settled_per_pane_picture_pushes_nothing():
    """Per-pane painting must keep the RPC suppression the tab cache gave us."""
    tab = FakeTab("t1", pane_count=3, active="t1-p0")
    painter = TabPainter()
    verdicts = {
        "t1-p0": PaneVerdict(AgentState.WAITING, True),
        "t1-p1": PaneVerdict(AgentState.WORKING, True),
        "t1-p2": PaneVerdict(AgentState.WORKING, True),
    }
    run(painter.apply(tab, AgentState.WAITING, per_pane=verdicts))
    before = tab.total_pushes
    for _ in range(5):
        assert run(painter.apply(tab, AgentState.WAITING, per_pane=verdicts)) is False
    assert tab.total_pushes == before


def test_one_pane_changing_repaints_only_that_pane():
    tab = FakeTab("t1", pane_count=3, active="t1-p0")
    painter = TabPainter()
    verdicts = {
        "t1-p0": PaneVerdict(AgentState.WAITING, True),
        "t1-p1": PaneVerdict(AgentState.WORKING, True),
        "t1-p2": PaneVerdict(AgentState.WORKING, True),
    }
    run(painter.apply(tab, AgentState.WAITING, per_pane=verdicts))
    before = [len(s.pushes) for s in tab.sessions]

    verdicts["t1-p2"] = PaneVerdict(AgentState.WAITING, True)
    assert run(painter.apply(tab, AgentState.WAITING, per_pane=verdicts)) is True
    after = [len(s.pushes) for s in tab.sessions]
    assert after[0] == before[0] and after[1] == before[1]
    assert after[2] == before[2] + 1


def test_a_pane_going_unknown_holds_its_own_colour():
    """The per-pane mirror of HOLD_ON_UNKNOWN: silence is not a state change."""
    tab = FakeTab("t1", pane_count=2, active="t1-p0")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.ACTION, per_pane={
        "t1-p0": PaneVerdict(AgentState.WORKING, True),
        "t1-p1": PaneVerdict(AgentState.ACTION, True),
    }))
    run(painter.apply(tab, AgentState.ACTION, per_pane={
        "t1-p0": PaneVerdict(AgentState.WORKING, True),
        "t1-p1": PaneVerdict(AgentState.UNKNOWN, True),
    }))
    assert colors_of(tab, painter)[1] is AgentState.ACTION


def test_a_pane_whose_evidence_expired_stops_holding():
    """A pane cannot keep a colour on a reading it can no longer point at."""
    tab = FakeTab("t1", pane_count=2, active="t1-p0")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.ACTION, per_pane={
        "t1-p0": PaneVerdict(AgentState.WORKING, True),
        "t1-p1": PaneVerdict(AgentState.ACTION, True),
    }))
    run(painter.apply(tab, AgentState.WORKING, per_pane={
        "t1-p0": PaneVerdict(AgentState.WORKING, True),
        "t1-p1": PaneVerdict(AgentState.UNKNOWN, False),
    }))
    assert colors_of(tab, painter)[1] is AgentState.UNKNOWN
    assert "t1" not in painter.colored


def test_clear_wipes_every_pane_not_just_the_active_one():
    """Shutdown must not strand a background pane's colour behind it."""
    tab = FakeTab("t1", pane_count=3, active="t1-p0")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.ACTION, per_pane={
        "t1-p0": PaneVerdict(AgentState.WORKING, True),
        "t1-p1": PaneVerdict(AgentState.ACTION, True),
        "t1-p2": PaneVerdict(AgentState.WAITING, True),
    }))
    run(painter.clear(tab))
    assert colors_of(tab, painter) == [AgentState.WORKING] * 3
    assert "t1" not in painter.colored


def test_reset_all_forgets_per_pane_bookkeeping():
    """All three per-pane dicts back `colored`; reset_all must clear every one.

    Forgetting ``pane_tab`` alone would leave `colored` computable again on the
    next paint from half-stale data -- session ids mapped to tabs that were
    reset, mixed with fresh applications.
    """
    tab = FakeTab("t1", pane_count=2, active="t1-p0")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.ACTION, per_pane={
        "t1-p0": PaneVerdict(AgentState.ACTION, True),
        "t1-p1": PaneVerdict(AgentState.WORKING, True),
    }))
    run(painter.reset_all(FakeApp([tab])))
    assert painter.pane_applied == {}
    assert painter.pane_tab == {}
    assert painter.colored == set()




def test_a_pane_opened_mid_tab_is_painted_without_disturbing_its_settled_siblings():
    """A new split pane appearing between sweeps must be painted on first
    sight, without forcing a repaint of panes whose verdict has not changed."""
    tab = FakeTab("t1", pane_count=2, active="t1-p0")
    painter = TabPainter()
    verdicts = {
        "t1-p0": PaneVerdict(AgentState.WAITING, True),
        "t1-p1": PaneVerdict(AgentState.WORKING, True),
    }
    run(painter.apply(tab, AgentState.WAITING, per_pane=verdicts))
    before = [len(s.pushes) for s in tab.sessions]

    new_pane = FakeSession("t1-p2")
    tab.sessions.append(new_pane)
    verdicts["t1-p2"] = PaneVerdict(AgentState.WORKING, True)
    assert run(painter.apply(tab, AgentState.WAITING, per_pane=verdicts)) is True
    assert [len(s.pushes) for s in tab.sessions[:2]] == before
    # WORKING carries no colour, and this pane has never been painted, so there
    # is nothing to push and nothing of ours to undo. Pushing a "clear" at it
    # anyway is what used to destroy a hand-set tab colour (Task 11).
    assert len(new_pane.pushes) == 0
    assert painter.pane_applied["t1-p2"] is AgentState.WORKING

    # It is painted the moment it actually needs a colour, though — the point
    # of the test is that a mid-tab pane is not missed.
    verdicts["t1-p2"] = PaneVerdict(AgentState.WAITING, True)
    run(painter.apply(tab, AgentState.WAITING, per_pane=verdicts))
    assert len(new_pane.pushes) == 1
    assert painter.pane_applied["t1-p2"] is AgentState.WAITING


def test_wanted_is_empty_for_a_tab_with_no_sessions():
    """Defensive: a real iTerm2 tab always has at least one pane -- closing the
    last one closes the tab -- but `_wanted` must not assume that. An empty
    ``tab.sessions`` must produce an empty verdict map rather than raising, in
    both the per-pane and whole-tab-fallback branches.
    """
    tab = FakeTab("t1", pane_count=0, active=None)
    painter = TabPainter()
    assert painter._wanted(tab, AgentState.ACTION, None) == {}
    assert painter._wanted(tab, AgentState.ACTION, {}) == {}


def test_apply_is_a_noop_on_a_sessionless_tab():
    """No sessions means nothing can be pushed, so nothing -- including the
    title -- should be touched."""
    tab = FakeTab("t1", pane_count=0, active=None)
    painter = TabPainter()
    assert run(painter.apply(tab, AgentState.ACTION, per_pane={})) is False
    assert tab.titles == []


def test_focus_moving_during_a_held_unknown_still_repaints():
    """Regression: the hold short-circuit silently dropped the alert.

    An UNKNOWN aggregate holds the tab's colour, which is right. But it used to
    do that by returning before painting, and the pane carrying the tab bar's
    colour is whichever one has focus. Move focus during a hold and the newly
    active pane was left with no colour, so the tab bar went grey while an agent
    was still waiting — the alert vanishing exactly when the user looked at it.
    """
    painter = TabPainter()
    waiting = {
        "t1-p0": PaneVerdict(AgentState.WAITING, True),
        "t1-p1": PaneVerdict(AgentState.WORKING, True),
    }
    first = FakeTab("t1", pane_count=2, active="t1-p0")
    run(painter.apply(first, AgentState.WAITING, per_pane=waiting))
    assert "t1" in painter.colored

    # p0's next reading is UNKNOWN with its evidence still current, so the tab
    # holds. Focus moves to the working pane in the same sweep.
    moved = FakeTab("t1", pane_count=2, active="t1-p1")
    held = {
        "t1-p0": PaneVerdict(AgentState.UNKNOWN, True),
        "t1-p1": PaneVerdict(AgentState.WORKING, True),
    }
    run(painter.apply(moved, AgentState.UNKNOWN, per_pane=held))

    active_state = painter.pane_applied["t1-p1"]
    assert active_state is AgentState.WAITING, (
        "the pane holding the tab bar lost the held colour, so the tab went grey"
    )
    assert "t1" in painter.colored


def test_a_backgrounded_pane_does_not_hold_an_aggregate_it_never_earned():
    """The active pane is painted with the tab's state, not its own.

    So `pane_applied` is the wrong thing to hold across an UNKNOWN for that
    pane: it would resurrect a sibling's alert as though this pane had raised
    it. A pane may only hold its own last confident reading.
    """
    painter = TabPainter()
    # p0 has focus and is WORKING, but p1 is ACTION, so p0 is painted red.
    focused = FakeTab("t1", pane_count=3, active="t1-p0")
    run(painter.apply(focused, AgentState.ACTION, per_pane={
        "t1-p0": PaneVerdict(AgentState.WORKING, True),
        "t1-p1": PaneVerdict(AgentState.ACTION, True),
        "t1-p2": PaneVerdict(AgentState.WORKING, True),
    }))
    assert painter.pane_applied["t1-p0"] is AgentState.ACTION  # carrying the tab

    # Focus leaves p0, and p0's own next reading is UNKNOWN.
    backgrounded = FakeTab("t1", pane_count=3, active="t1-p2")
    run(painter.apply(backgrounded, AgentState.ACTION, per_pane={
        "t1-p0": PaneVerdict(AgentState.UNKNOWN, True),
        "t1-p1": PaneVerdict(AgentState.ACTION, True),
        "t1-p2": PaneVerdict(AgentState.WORKING, True),
    }))
    assert painter.pane_applied["t1-p0"] is AgentState.WORKING, (
        "a background pane kept a red it had only ever carried on the tab's behalf"
    )


# -- a colour cupline did not set is not cupline's to remove ---------------
# `_profile_for(None)` switched all three `use_tab_color` flags off, and
# `_wanted` produces a state for *every* session in any tab holding an agent —
# including panes cupline does not watch. So a pane the user had coloured by
# hand was silently un-coloured on the first sweep and never given it back.



