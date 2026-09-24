"""Tab-state failed-paint and manual-colour restoration tests."""

from asyncio import run
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import iterm2  # noqa: E402

from models import AgentState, PaneVerdict  # noqa: E402
from tab_state import TabPainter  # noqa: E402
from tab_state_fakes import FakeApp, FakeProfile, FakeSession, FakeTab, colors_of  # noqa: E402


def test_a_pane_whose_paint_failed_is_still_restored_on_shutdown():
    """The bug a derived `colored` set prevents.

    One pane's profile push raises while its siblings succeed. A tab-level
    "no longer coloured" flag would already have been cleared, so the shutdown
    restore would walk past the tab and leave that pane amber with no process
    left to explain it.
    """
    class Flaky(FakeSession):
        fail = True

        async def async_set_profile_properties(self, profile):
            if Flaky.fail:
                raise RuntimeError("pane is busy")
            await super().async_set_profile_properties(profile)

    tab = FakeTab("t1", pane_count=2, active="t1-p0")
    tab.sessions[1] = Flaky("t1-p1")
    painter = TabPainter()

    # p0 takes amber; p1's push fails, so nothing is recorded for it.
    run(painter.apply(tab, AgentState.ACTION, per_pane={
        "t1-p0": PaneVerdict(AgentState.ACTION, True),
        "t1-p1": PaneVerdict(AgentState.WORKING, True),
    }))
    assert "t1" in painter.colored

    # Both panes now report WORKING, but p1 still cannot be written.
    run(painter.apply(tab, AgentState.WORKING, per_pane={
        "t1-p0": PaneVerdict(AgentState.WORKING, True),
        "t1-p1": PaneVerdict(AgentState.WORKING, True),
    }))
    assert "t1" not in painter.colored, "no pane holds a colour, so nothing to undo"

    # And the inverse: a pane that kept a colour keeps the tab on the restore
    # list even after its siblings are cleared.
    Flaky.fail = False
    run(painter.apply(tab, AgentState.ACTION, per_pane={
        "t1-p0": PaneVerdict(AgentState.WORKING, True),
        "t1-p1": PaneVerdict(AgentState.ACTION, True),
    }))
    assert "t1" in painter.colored
    run(painter.restore_painted(FakeApp([tab])))
    assert painter.colored == set()
    assert colors_of(tab, painter) == [AgentState.WORKING, AgentState.WORKING]


# --- Stale bookkeeping: closed and moved panes -------------------------------
#
# `pane_tab` is written only when `apply` actually pushes a profile to a
# session (tab_state.py:212-219); it is never pruned. These tests probe what
# happens when the world moves out from under that assumption: a session
# closes, or a pane is dragged to a different tab.


def test_colored_converges_to_empty_after_restore_painted_clears_every_reachable_tab():
    """Regression: a closed pane's bookkeeping outlives the pane itself.

    `restore_painted`'s own contract is "undo only what this process
    coloured" -- once every tab it knows about has been walked and cleared,
    nothing should be left to undo. Closing a coloured pane's session breaks
    that: the pane is gone from `tab.sessions`, so the clear that follows only
    ever touches the *live* sessions on the tab, and the closed pane's entry in
    `pane_applied`/`pane_tab` is never touched by anything again. `colored`
    then keeps reporting the tab as coloured forever, even though the tab
    itself has nothing left to clear.
    """
    tab = FakeTab("t1", pane_count=2, active="t1-p0")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.ACTION, per_pane={
        "t1-p0": PaneVerdict(AgentState.WORKING, True),
        "t1-p1": PaneVerdict(AgentState.ACTION, True),
    }))
    assert "t1" in painter.colored

    # t1-p1 closes. iTerm2 stops reporting it; nothing tells the painter.
    tab.sessions = [s for s in tab.sessions if s.session_id != "t1-p1"]

    run(painter.restore_painted(FakeApp([tab])))
    assert painter.colored == set(), (
        "a closed pane's stale bookkeeping kept its last tab listed as coloured"
    )


def test_restore_painted_recovers_a_pane_that_moved_to_a_different_tab():
    """Regression: moving a pane to a new tab can strand real colour behind it.

    `apply`'s redundant-push skip (tab_state.py:212-214) ``continue``s before
    ``pane_tab[sid]`` is rewritten at line 219. So when a pane moves to a new
    tab and its *desired* colour has not changed -- exactly what happens when
    the pane is the sole, active pane of its new tab and its own verdict is
    unchanged by the move -- the skip fires, no push happens, and `pane_tab`
    keeps pointing at the tab the pane left. `restore_painted` then clears the
    old (already-clean) tab and never reaches the session's real, current tab,
    leaving amber on a live pane after what the log calls a clean shutdown.
    """
    tab_a = FakeTab("A", pane_count=2, active="A-p0")
    painter = TabPainter()
    mover = tab_a.sessions[1]  # "A-p1"
    run(painter.apply(tab_a, AgentState.ACTION, per_pane={
        "A-p0": PaneVerdict(AgentState.WORKING, True),
        "A-p1": PaneVerdict(AgentState.ACTION, True),
    }))
    assert painter.colored == {"A"}

    # The pane moves to tab B, becoming its sole and active pane. Its own
    # verdict is unchanged by the move, so the aggregate for the new tab is the
    # same ACTION colour it already carries.
    tab_a.sessions = tab_a.sessions[:1]
    tab_b = FakeTab("B", pane_count=0, active="A-p1")
    tab_b.sessions = [mover]
    run(painter.apply(tab_b, AgentState.ACTION, per_pane={
        "A-p1": PaneVerdict(AgentState.ACTION, True),
    }))

    run(painter.restore_painted(FakeApp([tab_a, tab_b])))
    assert painter.pane_applied.get("A-p1") is AgentState.WORKING, (
        "the moved pane's colour was never cleared on shutdown"
    )




# -- a colour cupline did not set is not cupline's to remove ---------------
# `_profile_for(None)` switched all three `use_tab_color` flags off, and
# `_wanted` produces a state for *every* session in any tab holding an agent —
# including panes cupline does not watch. So a pane the user had coloured by
# hand was silently un-coloured on the first sweep and never given it back.


def _last_push(session):
    return {k: json.loads(v) for k, v in session.pushes[-1].values.items()}


def test_a_hand_set_tab_colour_survives_being_painted_and_restored():
    """The headline case: paint over a manual colour, then give it back.

    Both halves are restored. The flag alone is not enough — painting also
    overwrites the stored colour, so a pane restored to `use=True` with amber
    still in the slot would come back the wrong colour rather than its own.
    """
    manual = iterm2.Color(10, 20, 30)
    tab = FakeTab("t1", pane_count=1, active="t1-p0")
    tab.sessions[0].profile = FakeProfile(use=True, color=manual)
    painter = TabPainter()

    run(painter.apply(tab, AgentState.WAITING))
    assert tab.sessions[0].pushes, "the pane was never painted amber to begin with"

    run(painter.restore_painted(FakeApp([tab])))

    restored = _last_push(tab.sessions[0])
    assert restored["Use Tab Color"] is True
    assert restored["Use Tab Color (Dark)"] is True
    assert restored["Use Tab Color (Light)"] is True
    assert restored["Tab Color"] == manual.get_dict()
    assert restored["Tab Color (Dark)"] == manual.get_dict()


def test_a_pane_with_no_colour_of_its_own_is_still_returned_to_none():
    """The ordinary case must not regress into leaving amber behind."""
    tab = FakeTab("t1", pane_count=1, active="t1-p0")
    painter = TabPainter()

    run(painter.apply(tab, AgentState.WAITING))
    run(painter.restore_painted(FakeApp([tab])))

    restored = _last_push(tab.sessions[0])
    assert restored["Use Tab Color"] is False
    assert restored["Use Tab Color (Dark)"] is False
    assert restored["Use Tab Color (Light)"] is False


def test_a_pane_cupline_never_painted_is_left_entirely_alone():
    """An unwatched pane sharing a tab with an agent used to be pushed a clear.

    The active pane stays a deliberate exception: it carries the tab aggregate
    so the tab bar is correct whichever pane has focus, and that is documented
    at the top of tab_state.py.
    """
    tab = FakeTab("t1", pane_count=2, active="t1-p0")
    stranger = tab.sessions[1]
    stranger.profile = FakeProfile(use=True, color=iterm2.Color(9, 9, 9))
    painter = TabPainter()

    run(painter.apply(tab, AgentState.WAITING, per_pane={
        "t1-p0": PaneVerdict(AgentState.WAITING, True),
        # t1-p1 has no verdict at all — unwatched, or never read.
    }))

    assert stranger.pushes == [], "an unwatched pane had its profile rewritten"


def test_the_prior_appearance_is_read_before_the_first_push():
    """Reading it later would capture cupline's own colour as the user's."""
    tab = FakeTab("t1", pane_count=1, active="t1-p0")
    session = tab.sessions[0]
    session.profile = FakeProfile(use=True, color=iterm2.Color(7, 7, 7))

    pushes_at_read = []
    original = session.async_get_profile

    async def spy():
        pushes_at_read.append(len(session.pushes))
        return await original()

    session.async_get_profile = spy
    run(TabPainter().apply(tab, AgentState.WAITING))

    assert pushes_at_read == [0], \
        f"profile read after {pushes_at_read} pushes; it must precede the first"


def test_an_unreadable_profile_falls_back_to_clearing(caplog):
    """No basis for claiming a colour, so assume the common case: none."""
    tab = FakeTab("t1", pane_count=1, active="t1-p0")
    session = tab.sessions[0]

    async def broken():
        raise RuntimeError("profile unavailable")

    session.async_get_profile = broken
    painter = TabPainter()

    run(painter.apply(tab, AgentState.WAITING))
    run(painter.restore_painted(FakeApp([tab])))

    assert _last_push(session)["Use Tab Color"] is False


def test_reset_all_is_still_deliberately_unconditional():
    """Crash recovery captured nothing, so it has nothing to put back.

    `--reset` runs in a fresh process that painted no pane. Restoring is not
    available to it, and leaving stranded amber behind would defeat the whole
    command — so it stays blunt, and nothing calls it automatically.
    """
    tab = FakeTab("t1", pane_count=1, active="t1-p0")
    tab.sessions[0].profile = FakeProfile(use=True, color=iterm2.Color(4, 5, 6))

    run(TabPainter().reset_all(FakeApp([tab])))

    assert _last_push(tab.sessions[0])["Use Tab Color"] is False

