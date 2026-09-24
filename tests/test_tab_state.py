"""Tab painting: aggregation, redundant-push suppression, and restore.

The suppression tests exist because of a real bug: tracking only the *coloured*
tabs meant every "clear" state failed its own dedupe check and re-pushed a
profile update to every pane on every sweep tick.
"""

from asyncio import run
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import STATE_COLORS  # noqa: E402
from models import AgentState  # noqa: E402
from tab_state import TabPainter, _profile_for  # noqa: E402
from tab_state_fakes import FakeApp, FakeSession, FakeTab  # noqa: E402


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


