"""State aggregation and per-session temporal bookkeeping."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import AgentState, SessionState, most_urgent  # noqa: E402


def test_action_wins_over_waiting_and_working():
    """A tab holding several panes must surface the one needing a human."""
    assert most_urgent([AgentState.WORKING, AgentState.ACTION, AgentState.WAITING]) is AgentState.ACTION


def test_waiting_wins_over_working():
    assert most_urgent([AgentState.WORKING, AgentState.WAITING]) is AgentState.WAITING


def test_unknown_never_overrides_a_confident_sibling():
    assert most_urgent([AgentState.UNKNOWN, AgentState.WORKING]) is AgentState.WORKING


def test_all_unknown_stays_unknown():
    assert most_urgent([AgentState.UNKNOWN, AgentState.UNKNOWN]) is AgentState.UNKNOWN


def test_empty_is_unknown():
    assert most_urgent([]) is AgentState.UNKNOWN


def _state():
    return SessionState(session_id="s1", tab_id="t1", window_id="w1")


def test_note_change_reports_movement():
    state = _state()
    assert state.note_change("aaa", "tail one", 100.0) is True
    assert state.note_change("aaa", "tail one", 101.0) is False
    assert state.note_change("bbb", "tail two", 102.0) is True


def test_stable_since_set_only_while_unchanged():
    state = _state()
    state.note_change("aaa", "x", 100.0)
    assert state.stable_since is None
    state.note_change("aaa", "x", 101.0)
    assert state.stable_since == 101.0
    state.note_change("bbb", "y", 102.0)
    assert state.stable_since is None


def test_redraw_noise_does_not_reset_the_content_clock():
    """The bug: the streamer reset stable_since on every raw frame.

    A spinning agent emits ~26 redraws a second, so a session that was visibly
    idle behind an animation reported zero stable seconds forever — starving the
    exact temporal heuristic that would have called it WAITING.
    """
    state = _state()
    state.note_change("aaa", "x", 100.0)
    state.note_change("aaa", "x", 101.0)
    assert state.stable_since == 101.0

    for frame in range(26):  # one second of spinner
        state.note_event(101.0 + frame / 26)

    assert state.stable_since == 101.0, "redraw noise reset the content clock"
    assert state.snapshot(105.0).seconds_stable == 4.0


def test_snapshot_separates_redraw_from_content_change():
    state = _state()
    state.note_change("aaa", "x", 100.0)
    state.note_event(103.0)  # spinner frame, no content movement

    snapshot = state.snapshot(104.0)
    assert snapshot.seconds_since_change == 4.0, "content clock moved on a redraw"
    assert snapshot.seconds_since_redraw == 1.0


def test_note_event_marks_the_session_for_reading():
    state = _state()
    state.note_event(100.0)
    assert state.dirty is True
    assert state.last_event_at == 100.0


def test_a_confident_state_records_the_screen_that_justified_it():
    state = _state()
    state.note_change("aaa", "Do you want to proceed?", 100.0)
    state.note_classification(AgentState.ACTION)
    assert state.confident_hash == "aaa"
    assert state.evidence_is_current() is True, "the prompt is still on screen"


def test_an_unknown_reading_does_not_claim_new_evidence():
    """UNKNOWN means "I can't tell", so it must not overwrite what we do know."""
    state = _state()
    state.note_change("aaa", "Do you want to proceed?", 100.0)
    state.note_classification(AgentState.ACTION)

    state.note_change("bbb", "some other output", 110.0)
    previous = state.note_classification(AgentState.UNKNOWN)

    assert previous is AgentState.ACTION
    assert state.confident_hash == "aaa", "an abstention overwrote the evidence"
    assert state.evidence_is_current() is False, "the screen has moved on from the prompt"


def test_a_never_classified_pane_has_no_state_to_protect():
    """It must not vote to hold a colour some other pane in the tab put there."""
    state = _state()
    state.note_change("aaa", "unrecognisable output", 100.0)
    state.note_classification(AgentState.UNKNOWN)
    assert state.confident_hash is None
    assert state.evidence_is_current() is False


def test_snapshot_carries_temporal_fields():
    state = _state()
    state.agent = "codex"
    state.note_change("aaa", "some tail", 100.0)
    state.note_change("aaa", "some tail", 101.0)
    state.previous_classification = AgentState.WAITING

    snapshot = state.snapshot(104.0)
    assert snapshot.agent == "codex"
    assert snapshot.tail == "some tail"
    assert snapshot.seconds_since_change == 4.0
    assert snapshot.seconds_stable == 3.0
    assert snapshot.previous_state is AgentState.WAITING


def test_has_been_read_is_false_before_first_reading():
    state = _state()
    assert state.has_been_read() is False
    state.note_change("aaa", "x", 100.0)
    assert state.has_been_read() is True


def test_is_agent_gates_on_resolved_agent():
    state = _state()
    assert state.is_agent() is False
    state.agent = "claude"
    assert state.is_agent() is True
