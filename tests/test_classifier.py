"""Classifier interface and fixture replay.

The temporal rule -- redraw silence past ``IDLE_AFTER_SECONDS`` -- is the real
signal the tool rests on, and is tested as such below, not as a placeholder. What
remains a placeholder is the *text* discrimination that only runs once a pane
is already known to have stopped: deciding WAITING versus ACTION. These tests
pin that as the *contract* the future rules/LLM implementation must keep, plus
whatever fixtures have been captured so far.
"""

import glob
import re
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import classifier  # noqa: E402
from config import IDLE_AFTER_SECONDS  # noqa: E402
from models import AgentState, TerminalSnapshot  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def snap(tail, session_id="s1", agent="claude", stable=2.0, previous=AgentState.UNKNOWN,
         since_redraw=None):
    """Build a snapshot of a *stopped* agent by default.

    ``since_redraw`` defaults to comfortably past ``IDLE_AFTER_SECONDS`` because
    that is the only state in which the text rules run at all — a pane that is
    still repainting is WORKING and its text is never consulted. Tests that care
    about the working path pass a small value explicitly.
    """
    return TerminalSnapshot(
        session_id=session_id,
        agent=agent,
        tail=tail,
        seconds_since_change=stable,
        seconds_stable=stable,
        previous_state=previous,
        seconds_since_redraw=(IDLE_AFTER_SECONDS * 2 if since_redraw is None
                              else since_redraw),
    )


@pytest.fixture(autouse=True)
def _clean_overrides():
    classifier.clear_overrides()
    yield
    classifier.clear_overrides()


def test_returns_an_agent_state():
    assert isinstance(classifier.classify(snap("anything")), AgentState)


def test_blank_screen_is_unknown():
    assert classifier.classify(snap("   \n  ")) is AgentState.UNKNOWN


def test_a_stopped_pane_with_ordinary_text_is_waiting():
    """Ambiguous *text* is no longer an ambiguous *state*.

    This asserted UNKNOWN while text was the only input, because "Reading file
    src/main.py" genuinely cannot tell you whether the agent is mid-turn or
    finished. Redraw timing answers that without reading the words at all: this
    pane has repainted nothing for longer than the threshold, so it has stopped.
    Abstaining here now would withhold the one signal the tool exists to give.
    """
    assert classifier.classify(snap("Reading file src/main.py")) is AgentState.WAITING


def test_a_repainting_pane_is_working_whatever_it_says():
    """Redraw activity outranks text, in both directions.

    The same tail that reads WAITING when still reads WORKING when the pane is
    repainting -- the text is identical and only the timing differs.
    """
    tail = "Reading file src/main.py"
    assert classifier.classify(snap(tail, since_redraw=0.1)) is AgentState.WORKING
    assert classifier.classify(snap(tail)) is AgentState.WAITING


@pytest.mark.parametrize("tail", [
    "Proceed with this change? (y/n)",
    "Overwrite the file? [y/N]",
    "Press enter to continue",
    "  ❯ 1. Yes\n    2. No",
    "Waiting for your approval",
    "API key: ",
    # the real shape: prose *plus* the control it renders with
    "Do you want to proceed?\n❯ 1. Yes\n  2. No, tell Claude what to do differently",
])
def test_human_decision_shapes_are_action(tail):
    assert classifier.classify(snap(tail)) is AgentState.ACTION


@pytest.mark.parametrize("tail", [
    # Caught live: painted a tab red for over a minute. The agent had finished
    # its turn and asked a question — WAITING, not blocked at a control.
    "How do you want to finish this? I can either land it now or split it out.",
    "Do you want to review the plan before I start? Let me know either way.",
    "Tell me whether to allow the retry, or if I should stop here?",
])
def test_question_shaped_prose_is_not_an_action(tail):
    """Match the control, not the prose.

    An agent that is genuinely blocked renders an input widget; one that ends
    its turn with a question renders English. Treating the second as ACTION
    spends the red on a session that needs nothing urgent, and a red that cries
    wolf is worse than no red at all.
    """
    assert classifier.classify(snap(tail)) is not AgentState.ACTION


@pytest.mark.parametrize("tail", [
    "Thinking... (esc to interrupt)",
    "Running tests, ctrl+c to cancel",
])
def test_text_claiming_to_be_busy_does_not_override_a_stopped_screen(tail):
    """A frozen harness still says "esc to interrupt".

    These strings used to return WORKING on their own. That rule would now hide
    the failure the user most wants reported: an agent that hung mid-turn leaves
    this text on screen while repainting nothing at all. The screen has stopped,
    so cupline says stopped, whatever the leftover words claim.
    """
    assert classifier.classify(snap(tail)) is AgentState.WAITING
    assert classifier.classify(snap(tail, since_redraw=0.1)) is AgentState.WORKING


def test_override_takes_precedence():
    classifier.set_override("s1", AgentState.WAITING)
    assert classifier.classify(snap("Proceed? (y/n)", session_id="s1")) is AgentState.WAITING


def test_override_is_scoped_to_its_session():
    classifier.set_override("s1", AgentState.WAITING)
    other = classifier.classify(snap("nothing here", session_id="s2", since_redraw=0.1))
    assert other is AgentState.WORKING


def test_override_can_be_cleared():
    classifier.set_override("s1", AgentState.ACTION)
    classifier.set_override("s1", None)
    back = classifier.classify(snap("nothing here", session_id="s1", since_redraw=0.1))
    assert back is AgentState.WORKING


def test_classifier_does_not_import_iterm2():
    """The seam that keeps the classifier swappable and testable off-terminal.

    Matched as a regex over both import forms. A plain ``"import iterm2" not in
    source`` substring check — what this was — passes happily on
    ``from iterm2 import Color``, so the guard could be walked straight through
    by the more idiomatic of the two spellings it exists to prevent.
    """
    import models  # noqa: F401
    forbidden = re.compile(r"^\s*(?:import\s+iterm2|from\s+iterm2\b)", re.MULTILINE)
    for module in (classifier, sys.modules["models"]):
        with open(module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        assert not forbidden.search(source), (
            f"{os.path.basename(module.__file__)} imports iterm2; "
            "the classifier layer must stay off-terminal"
        )


def _fixtures():
    return sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.txt")))


@pytest.mark.parametrize("path", _fixtures() or [pytest.param(None, marks=pytest.mark.skip(
    reason="no fixtures captured yet — use cupline.py --capture"))])
def test_fixture_expectations(path):
    """Replay captured terminal tails against their expected label.

    UNKNOWN is accepted for any fixture: the placeholder classifier is allowed
    to abstain. It is not allowed to be confidently wrong.
    """
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    header, _, body = text.partition("---\n")
    expected_line = next(
        (line for line in header.splitlines() if line.startswith("# expected:")), None
    )
    assert expected_line, f"{path} has no '# expected:' header"
    expected = AgentState(expected_line.split(":", 1)[1].strip().lower())
    redraw_line = next(
        (line for line in header.splitlines() if line.startswith("# redrawing:")), None
    )
    redrawing = redraw_line.split(":", 1)[1].strip() if redraw_line else "unmeasured"

    # A static tail cannot exercise the rule that actually decides WORKING,
    # because that rule reads redraw timing and a text file has none. Feeding a
    # WORKING fixture a redrawing snapshot would assert the label onto the input
    # and then read it back off the output, which tests nothing. Fixtures
    # captured since the header exists carry a measurement; older ones do not.
    if expected is AgentState.WORKING and redrawing != "yes":
        pytest.skip(
            "redraw state unmeasured at capture; a text-only fixture cannot "
            "test the temporal rule that decides WORKING"
        )

    if redrawing == "yes":
        # The contract worth pinning: a repainting pane is WORKING no matter
        # what its text says. This is what stops an 'esc to interrupt' style
        # text rule being reintroduced.
        result = classifier.classify(snap(body, since_redraw=0.1))
        assert result is AgentState.WORKING, (
            f"{os.path.basename(path)}: a repainting pane must be working, "
            f"got {result.value}"
        )
        return

    # Stopped, which is what a WAITING or ACTION label already asserts. The
    # discrimination under test is the text half: does this stop need a
    # decision, or just a new instruction?
    result = classifier.classify(snap(body))
    assert result in (expected, AgentState.UNKNOWN), (
        f"{os.path.basename(path)}: expected {expected.value} (or unknown), got {result.value}"
    )
