"""Tab painting: aggregation, redundant-push suppression, and restore.

The suppression tests exist because of a real bug: tracking only the *coloured*
tabs meant every "clear" state failed its own dedupe check and re-pushed a
profile update to every pane on every sweep tick.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import AgentState, PaneVerdict  # noqa: E402
from tab_state import TabPainter, _profile_for  # noqa: E402
from config import STATE_COLORS  # noqa: E402


class FakeSession:
    def __init__(self, session_id):
        self.session_id = session_id
        self.pushes = []

    async def async_set_profile_properties(self, profile):
        self.pushes.append(profile)


class FakeTab:
    def __init__(self, tab_id, pane_count=1, active=None):
        self.tab_id = tab_id
        self.sessions = [FakeSession(f"{tab_id}-p{i}") for i in range(pane_count)]
        self.titles = []
        #: Which pane iTerm2 considers active. None models a tab whose active
        #: pane could not be determined, which must fail safe rather than leave
        #: the tab bar uncoloured.
        self.active = active

    @property
    def current_session(self):
        if self.active is None:
            return None
        return next(s for s in self.sessions if s.session_id == self.active)

    async def async_set_title(self, title):
        self.titles.append(title)

    @property
    def total_pushes(self):
        return sum(len(s.pushes) for s in self.sessions)


def colors_of(tab, painter):
    """What each pane is currently painted, as the painter believes it."""
    return [painter.pane_applied.get(s.session_id) for s in tab.sessions]


class FakeWindow:
    def __init__(self, tabs):
        self.tabs = tabs


class FakeApp:
    def __init__(self, tabs):
        self.windows = [FakeWindow(tabs)]


run = asyncio.run


def test_paints_every_pane_the_same_when_no_per_pane_detail_is_given():
    """Without ``per_pane`` every pane is set to agree, not just the active one.

    This is the fallback path ``clear``/``reset_all``/the ``--demo`` command
    rely on: they are making the whole tab agree, not reporting on it, so there
    is no "active pane carries the aggregate" distinction to make here.
    """
    tab = FakeTab("t1", pane_count=4)
    painter = TabPainter()
    assert run(painter.apply(tab, AgentState.ACTION)) is True
    assert all(len(s.pushes) == 1 for s in tab.sessions)


def test_repeating_the_same_state_pushes_nothing():
    tab = FakeTab("t1", pane_count=3)
    painter = TabPainter()
    run(painter.apply(tab, AgentState.WAITING))
    before = tab.total_pushes
    for _ in range(5):
        assert run(painter.apply(tab, AgentState.WAITING)) is False
    assert tab.total_pushes == before


def test_repeating_a_clear_state_pushes_nothing():
    """The regression: WORKING clears colour, and must still dedupe."""
    tab = FakeTab("t1", pane_count=3)
    painter = TabPainter()
    run(painter.apply(tab, AgentState.WORKING))
    before = tab.total_pushes
    for _ in range(10):
        assert run(painter.apply(tab, AgentState.WORKING)) is False
    assert tab.total_pushes == before


def test_state_transition_pushes():
    tab = FakeTab("t1")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.WAITING))
    assert run(painter.apply(tab, AgentState.ACTION)) is True
    assert run(painter.apply(tab, AgentState.WORKING)) is True


def test_force_pushes_even_when_unchanged():
    tab = FakeTab("t1")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.ACTION))
    assert run(painter.apply(tab, AgentState.ACTION, force=True)) is True


def test_unknown_holds_an_existing_colour():
    """An UNKNOWN reading is not evidence the previous state ended."""
    tab = FakeTab("t1")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.ACTION))
    assert run(painter.apply(tab, AgentState.UNKNOWN)) is False
    assert "t1" in painter.colored


def test_working_clears_a_held_colour():
    tab = FakeTab("t1")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.ACTION))
    run(painter.apply(tab, AgentState.WORKING))
    assert "t1" not in painter.colored


def test_restore_clears_only_coloured_tabs():
    coloured, plain = FakeTab("t1"), FakeTab("t2")
    painter = TabPainter()
    run(painter.apply(coloured, AgentState.ACTION))
    run(painter.apply(plain, AgentState.WORKING))
    plain_pushes = plain.total_pushes

    run(painter.restore_painted(FakeApp([coloured, plain])))
    assert painter.colored == set()
    assert plain.total_pushes == plain_pushes  # untouched


def test_restore_is_a_noop_when_nothing_was_painted():
    tab = FakeTab("t1")
    painter = TabPainter()
    run(painter.restore_painted(FakeApp([tab])))
    assert tab.total_pushes == 0


def test_reset_all_touches_every_session():
    tabs = [FakeTab("t1", 2), FakeTab("t2", 3)]
    painter = TabPainter()
    assert run(painter.reset_all(FakeApp(tabs))) == 5


def test_a_failing_pane_does_not_block_its_siblings():
    class Broken(FakeSession):
        async def async_set_profile_properties(self, profile):
            raise RuntimeError("pane is gone")

    tab = FakeTab("t1", pane_count=3)
    tab.sessions[0] = Broken("broken")
    painter = TabPainter()
    assert run(painter.apply(tab, AgentState.ACTION)) is True
    assert tab.sessions[1].pushes and tab.sessions[2].pushes


def test_aggregate_uses_most_urgent():
    painter = TabPainter()
    assert painter.aggregate([AgentState.WORKING, AgentState.ACTION]) is AgentState.ACTION


def test_colour_states_are_distinct():
    """WAITING and ACTION must be visually different, not just different enums."""
    assert STATE_COLORS[AgentState.WAITING] != STATE_COLORS[AgentState.ACTION]
    assert AgentState.WORKING not in STATE_COLORS
    assert AgentState.UNKNOWN not in STATE_COLORS


def test_title_pairs_project_with_a_state_word():
    assert TabPainter.title_for(AgentState.WAITING, "cupline") == "cupline · your turn"
    assert TabPainter.title_for(AgentState.ACTION, "cupline") == "cupline · needs you"


def test_title_falls_back_to_the_word_without_a_project():
    assert TabPainter.title_for(AgentState.ACTION, None) == "needs you"


def test_states_needing_nothing_have_no_title():
    """No title means iTerm2's automatic name — usually the agent's task."""
    assert TabPainter.title_for(AgentState.WORKING, "cupline") is None
    assert TabPainter.title_for(AgentState.UNKNOWN, "cupline") is None


def test_title_is_set_on_paint():
    tab = FakeTab("t1", pane_count=2)
    painter = TabPainter()
    run(painter.apply(tab, AgentState.ACTION, project="harbor"))
    assert tab.titles == ["harbor · needs you"]


def test_title_is_cleared_when_state_needs_nothing():
    tab = FakeTab("t1")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.WAITING, project="harbor"))
    run(painter.apply(tab, AgentState.WORKING, project="harbor"))
    assert tab.titles == ["harbor · your turn", ""]


def test_a_manually_named_tab_is_not_wiped():
    """The first sweep must not clear titles this process never set."""
    tab = FakeTab("t1")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.WORKING, project="harbor"))
    assert tab.titles == []


def test_project_change_updates_the_title():
    """Aggregation can hand the tab to a different pane between sweeps."""
    tab = FakeTab("t1")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.ACTION, project="harbor"))
    assert run(painter.apply(tab, AgentState.ACTION, project="ridge")) is True
    assert tab.titles == ["harbor · needs you", "ridge · needs you"]


def test_same_state_and_project_does_not_retitle():
    tab = FakeTab("t1")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.ACTION, project="harbor"))
    run(painter.apply(tab, AgentState.ACTION, project="harbor"))
    assert tab.titles == ["harbor · needs you"]


def test_restore_clears_the_title():
    tab = FakeTab("t1")
    painter = TabPainter()
    run(painter.apply(tab, AgentState.ACTION, project="harbor"))
    run(painter.restore_painted(FakeApp([tab])))
    assert tab.titles[-1] == ""


def test_a_failing_title_does_not_fail_the_paint():
    class NoTitle(FakeTab):
        async def async_set_title(self, title):
            raise RuntimeError("tab is gone")

    tab = NoTitle("t1", pane_count=2)
    painter = TabPainter()
    assert run(painter.apply(tab, AgentState.ACTION, project="harbor")) is True
    assert all(s.pushes for s in tab.sessions)


def test_profile_sets_light_and_dark_variants():
    """Profiles with separate light/dark colours ignore the plain setter alone."""
    keys = set(_profile_for((1, 2, 3)).values.keys())
    assert {"Tab Color", "Tab Color (Dark)", "Tab Color (Light)"} <= keys
    assert {"Use Tab Color", "Use Tab Color (Dark)", "Use Tab Color (Light)"} <= keys


# --- Per-pane painting -------------------------------------------------------
#
# The reported bug: one agent stops in a three-pane tab and all three panes go
# amber, so the colour says "three agents want you" when one does. Verified
# against the live terminal before these tests were written — painting a single
# background pane amber left the tab bar grey and that pane's own title bar
# amber, which is what makes per-pane truth possible at all.


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
    assert len(new_pane.pushes) == 1
    assert painter.pane_applied["t1-p2"] is AgentState.WORKING


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
