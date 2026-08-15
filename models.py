"""Core data types for cupline.

This module deliberately does not import ``iterm2``. Everything here must be
constructible from a plain string of terminal text so the classifier and its
tests can run without a terminal attached.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import NamedTuple, Optional


class AgentState(Enum):
    """What an interactive agent terminal is currently doing."""

    WORKING = "working"
    WAITING = "waiting"
    ACTION = "action"
    UNKNOWN = "unknown"


#: Higher wins when several panes share one tab. UNKNOWN is lowest so it never
#: overrides a confident reading from a sibling pane.
STATE_PRIORITY = {
    AgentState.UNKNOWN: 0,
    AgentState.WORKING: 1,
    AgentState.WAITING: 2,
    AgentState.ACTION: 3,
}


def most_urgent(states) -> AgentState:
    """Pick the state a shared tab should display.

    A tab can hold several split panes, but iTerm2 gives one tab one colour, so
    the states have to collapse. ACTION beats WAITING beats WORKING: the tab
    should surface the pane that most needs a human.
    """
    chosen = AgentState.UNKNOWN
    for state in states:
        if STATE_PRIORITY[state] > STATE_PRIORITY[chosen]:
            chosen = state
    return chosen


class PaneVerdict(NamedTuple):
    """One pane's own reading, kept separate from its tab's aggregate.

    A tab collapses to one colour, but a *pane* does not have to. iTerm2 renders
    the tab bar from the active session alone (RESEARCH.md finding 1) while every
    pane's own title bar renders its own ``tab_color`` — verified by painting a
    single background pane and photographing the result. So the aggregate is
    still needed for the tab bar, and this is what the individual panes say.

    ``evidence_current`` mirrors ``SessionState.evidence_is_current``: whether
    this pane can still point at the screen its state came from. It gates the
    per-pane hold exactly as the tab-level ``hold`` gates the tab's, so a pane
    that goes UNKNOWN forever eventually releases its colour instead of keeping
    it on the strength of a reading it can no longer justify.
    """

    state: AgentState
    evidence_current: bool = False


@dataclass(frozen=True)
class TerminalSnapshot:
    """Everything the classifier is allowed to see.

    Kept intentionally small: the eventual local-model classifier is expected to
    take the tail plus the temporal fields, and nothing else.
    """

    session_id: str
    agent: Optional[str]
    tail: str
    #: Seconds since the normalised screen text last changed. Redraw noise
    #: (spinners, elapsed counters) does not reset this.
    seconds_since_change: float
    #: Seconds the normalised text has been confirmed unchanged by a re-read.
    #: 0.0 until a second reading confirms it.
    seconds_stable: float
    previous_state: AgentState = AgentState.UNKNOWN
    #: Seconds since the terminal last redrew *anything*, spinner frames
    #: included. Near zero means the pane is animating, which is a strong
    #: WORKING signal even when the meaningful text is frozen. Distinct from
    #: ``seconds_since_change`` on purpose: an agent thinking behind a spinner
    #: redraws constantly while its content sits still.
    seconds_since_redraw: float = 0.0

    @property
    def lines(self) -> list[str]:
        return self.tail.splitlines()


@dataclass
class SessionState:
    """Mutable per-session bookkeeping owned by the registry."""

    session_id: str
    tab_id: str
    window_id: str
    label: str = ""
    agent: Optional[str] = None
    job_name: Optional[str] = None
    job_pid: Optional[int] = None
    #: Basename of the session's working directory — what the user calls the
    #: project. Shown in the tab title alongside the state word.
    project: Optional[str] = None

    last_screen_hash: Optional[str] = None
    #: When the *normalised content* last moved. Written only by note_change.
    last_change_at: float = field(default_factory=time.monotonic)
    #: When the terminal last redrew anything at all, spinner frames included.
    #: Written only by note_event. Kept separate from last_change_at because
    #: conflating them meant redraw noise reset the content clock, and a session
    #: animating a spinner could never accumulate stable time.
    #: Dated from construction, not epoch zero, and that default is doing real
    #: work now that ``seconds_since_redraw`` is the primary signal: a session
    #: discovered while already stopped has emitted no events and never will, so
    #: an epoch-zero clock would report it stopped before it had been watched at
    #: all. Starting the clock at discovery means the first verdict on any
    #: session is WORKING, and only IDLE_AFTER_SECONDS of observed silence
    #: changes it. Silence that was not watched for is not evidence of silence.
    last_event_at: float = field(default_factory=time.monotonic)
    stable_since: Optional[float] = None
    previous_classification: AgentState = AgentState.UNKNOWN
    last_terminal_tail: str = ""

    #: Set by the screen streamer, cleared by the sweeper. The streamer never
    #: fetches contents itself — see screen.py for why.
    dirty: bool = False
    #: Monotonic time of the last screen read. Drives the debounce ceiling so a
    #: continuously-animating session still gets looked at.
    last_read_at: float = 0.0
    #: Hash of the screen that justified the last non-UNKNOWN classification.
    #: An UNKNOWN reading holds the previous state, so without this a state
    #: outlives its own evidence indefinitely — see evidence_is_current.
    confident_hash: Optional[str] = None

    def has_been_read(self) -> bool:
        return self.last_screen_hash is not None

    def is_agent(self) -> bool:
        return self.agent is not None

    def note_event(self, now: float) -> None:
        """Record a raw screen redraw. Called by the streamer, per frame.

        Deliberately does **not** touch ``stable_since`` or ``last_change_at``.
        A spinner emits ~26 of these per second; letting them reset the content
        clock meant an animating session reported zero stable seconds forever,
        which is exactly the session a temporal heuristic needs to reason about.
        """
        self.dirty = True
        self.last_event_at = now

    def note_change(self, screen_hash: str, tail: str, now: float) -> bool:
        """Record a screen reading. Returns True if the content actually moved."""
        changed = screen_hash != self.last_screen_hash
        self.last_screen_hash = screen_hash
        self.last_terminal_tail = tail
        if changed:
            self.last_change_at = now
            self.stable_since = None
        elif self.stable_since is None:
            # Dated from this confirming read, not from last_change_at: between
            # the two readings the content could have moved and moved back
            # unobserved. This under-claims stability, which is the safe
            # direction — a wrong amber costs more than a late one.
            self.stable_since = now
        return changed

    def note_classification(self, result: AgentState) -> AgentState:
        """Record a classification and what screen justified it.

        Only a confident (non-UNKNOWN) reading updates ``confident_hash``: that
        is the screen a held state is standing on. Returns the previous state so
        the caller can decide whether this reading is worth logging.
        """
        previous = self.previous_classification
        self.previous_classification = result
        if result is not AgentState.UNKNOWN:
            self.confident_hash = self.last_screen_hash
        return previous

    def evidence_is_current(self) -> bool:
        """True when this pane has a confident state still visible on screen.

        ``UNKNOWN`` means "this reading tells me nothing", so the tab keeps its
        previous colour rather than flickering. But "tells me nothing" is not
        the same as "the reason is still on screen": once the content has
        changed away from the screen that produced the last confident state,
        continuing to hold shows a state whose evidence is provably gone.

        Observed live: an ACTION prompt matched for 6.7 seconds, the user
        answered it, and the tab stayed red indefinitely because every
        subsequent reading was UNKNOWN. A stale red is worse than no red — it
        trains you to ignore the colour.

        A pane that has never been classified confidently returns False. It has
        no state of its own to protect, so it must not vote to keep a colour
        that some *other* pane in the same tab put there — which is exactly how
        a red survived on a three-pane tab after the pane that earned it had
        moved on.
        """
        return (
            self.confident_hash is not None
            and self.last_screen_hash == self.confident_hash
        )

    def snapshot(self, now: float) -> TerminalSnapshot:
        return TerminalSnapshot(
            session_id=self.session_id,
            agent=self.agent,
            tail=self.last_terminal_tail,
            seconds_since_change=now - self.last_change_at,
            seconds_stable=0.0 if self.stable_since is None else now - self.stable_since,
            previous_state=self.previous_classification,
            seconds_since_redraw=now - self.last_event_at,
        )
