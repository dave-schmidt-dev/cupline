"""State classification.

The question this answers is deliberately narrow: **is this agent working on
something, or has it stopped?** Everything else is a refinement of "stopped".

The primary rule is temporal and needs no text at all. A terminal running an
agent that is doing anything repaints constantly — spinner frames, an elapsed
counter, streaming output. A terminal whose agent has stopped repaints nothing.
Measured over 150 s across 9 live sessions, working panes were never silent for
more than 0.7 s and stopped panes emitted zero events, so the two populations do
not overlap. See ``config.IDLE_AFTER_SECONDS``.

That rule is universal in the way this project requires: it reads redraw timing,
not language, so it cannot accidentally depend on which harness produced the
screen. Text is consulted *only after* the agent is known to have stopped, and
mostly only to say why — a visible input control means the stop needs a decision
from a human (ACTION) rather than just a new instruction (WAITING).

One text rule goes further and withdraws the report altogether: an agent whose
turn ended with a background shell still running is stopped but not *yours*,
because the shell exiting re-enters it without a human. See
``_BACKGROUND_RUNNING`` for why that one is allowed to outrank the redraw clock
when ``_STALE_WORKING_CLAIMS`` is not.

Nothing in this module may import ``iterm2``, and nothing may branch on which
harness produced the text. The remaining progression is a local small-model
fallback (Gemma 2B-4B) for the stopped-but-ambiguous case; the signature is
shaped to accept it unchanged.
"""

from __future__ import annotations

import re

from config import IDLE_AFTER_SECONDS
from models import AgentState, TerminalSnapshot

#: Manual overrides keyed by session id. Populated by ``--set`` and ``--demo``
#: so tab states can be exercised end-to-end without any classifier at all.
_OVERRIDES: dict[str, AgentState] = {}

#: Generic "a human must answer this" shapes. Harness-independent by design.
#:
#: These match an input *control* — a widget that only exists on screen while
#: something is actually blocked — rather than prose that happens to sound like
#: a question. That distinction is the whole rule, and it was learned the hard
#: way: see _PROSE_PATTERNS.
_ACTION_PATTERNS = (
    re.compile(r"\(y/n\)", re.IGNORECASE),
    re.compile(r"\[y/N\]|\[Y/n\]"),
    re.compile(r"\bpress\s+(enter|return|any key)\b", re.IGNORECASE),
    re.compile(r"^\s*[❯>]\s*\d+\.\s", re.MULTILINE),      # numbered selection list
    re.compile(r"\bwaiting for (your )?(approval|confirmation)\b", re.IGNORECASE),
    re.compile(r"\b(password|passphrase|api key|token)\s*:", re.IGNORECASE),
)

#: Question-shaped prose. **Not sufficient for ACTION on its own.**
#:
#: Caught live: a session was painted red for over a minute because an agent had
#: written "How do you want to finish this?" — a question at the *end of its
#: turn*, which is WAITING. An agent that is genuinely blocked renders a control
#: as well, so prose only counts when a control is on screen with it. Kept
#: because it may earn its place as a tie-breaker once the temporal signals are
#: real; useless as a standalone rule.
_PROSE_PATTERNS = (
    re.compile(r"\bdo you want to\b", re.IGNORECASE),
    re.compile(r"\ballow\b.*\?\s*$", re.IGNORECASE | re.MULTILINE),
)

#: Background work the harness resumes from on its own.
#:
#: A turn that ended with background shells still running has stopped in the
#: redraw sense and has *not* stopped in the sense this tool exists to report:
#: the shell exiting re-enters the agent with no human involved. Amber there is
#: a tab that was never yours, and a colour you learn to ignore.
#:
#: This is the one place text is allowed to overrule the stop verdict, and why
#: it is allowed here and nowhere else is the point. ``_STALE_WORKING_CLAIMS``
#: below is refused because redraw silence *contradicts* "I am working" — the
#: screen outranks the words. Redraw silence cannot contradict "a background
#: shell is still running", because a background shell paints nothing by design.
#: There is no competing evidence for the text to lose to; it is the only
#: evidence there is.
#:
#: The cost is not the ACTION/WAITING one either. Misreading that spends a
#: colour; misreading this spends the alert. Hence a narrow pattern, and the
#: anchoring in ``_background_work_running``.
_BACKGROUND_RUNNING = re.compile(r"\b\d+\s+shells?\s+still\s+running\b", re.IGNORECASE)

#: The one-line footer an agent writes when a turn ends, e.g.
#: ``✻ Cogitated for 1m 12s · done 3:40 PM · 2 shells still running``.
#: Matched on shape alone — a leading glyph, one word, a duration — because the
#: word itself is picked at random by the harness and the glyph varies. The
#: glyph class is non-word rather than "any character" on purpose: an ordinary
#: sentence ("I waited for 3 hours") would otherwise pass as a summary line, and
#: one sitting below the real footer would shadow it, since the scan reads
#: backwards and stops at the first match.
_TURN_SUMMARY = re.compile(r"^\s*[^\w\s]\s+\w+ for \d")

#: Text that *claims* the agent is mid-turn — "esc to interrupt" and friends.
#:
#: Deliberately **not** consulted. It was a WORKING rule until redraw timing
#: replaced it, and keeping it would actively defeat the requirement: a harness
#: that freezes mid-turn leaves this text on screen while repainting nothing at
#: all. Trusting the words over the redraws would report a hung agent as busy,
#: which is precisely the stop the user most needs to hear about. Kept as a named
#: constant so the decision is visible rather than an absence.
_STALE_WORKING_CLAIMS = (
    re.compile(r"\besc to interrupt\b", re.IGNORECASE),
    re.compile(r"\bctrl\+c to (stop|interrupt|cancel)\b", re.IGNORECASE),
)


def set_override(session_id: str, state: AgentState | None) -> None:
    """Force ``session_id`` to a state, or clear with ``None``."""
    if state is None:
        _OVERRIDES.pop(session_id, None)
    else:
        _OVERRIDES[session_id] = state


def clear_overrides() -> None:
    _OVERRIDES.clear()


def _background_work_running(tail: str) -> bool:
    """True when the agent's *most recent* turn ended with shells still running.

    Read off the last summary line rather than searched for anywhere in the
    tail, and that anchor is the whole safety argument. These footers stay in
    the transcript: a captured screen carries "1 shell still running" eleven
    lines up, three turns and a user reply ago, while the bottom of the same
    screen is genuinely waiting on a human. A bare substring search suppresses
    that alert on the strength of a shell that exited minutes earlier.

    Reading only the last summary line also makes the suppression self-clearing.
    When the shell exits and the agent is re-entered, the turn that follows
    writes a new summary line, and that is the one read next. A tail with no
    summary line on it at all suppresses nothing, which is the safe direction.
    """
    for line in reversed(tail.splitlines()):
        if _TURN_SUMMARY.search(line):
            return bool(_BACKGROUND_RUNNING.search(line))
    return False


def classify(snapshot: TerminalSnapshot) -> AgentState:
    """Decide what an agent terminal is currently doing.

    The stable interface. Replace the body; do not change the signature.
    """
    override = _OVERRIDES.get(snapshot.session_id)
    if override is not None:
        return override

    # The whole question, decided without reading a word. A pane that repainted
    # within the threshold has an agent doing something; there is no ambiguity
    # here to resolve and no reason to look at the text.
    #
    # This is not "inactivity implies waiting", which the spike rejected and
    # still rejects. Redraw activity is *positive evidence of work* — the agent
    # is animating a spinner or streaming output right now. Its absence is the
    # observation, not a failure to observe.
    if snapshot.seconds_since_redraw < IDLE_AFTER_SECONDS:
        return AgentState.WORKING

    # The stop verdict rests entirely on the redraw clock, so it is worth
    # exactly as much as the thing advancing it. A dead screen streamer freezes
    # that clock, and a frozen clock is indistinguishable from a genuinely quiet
    # terminal — which reported a working agent stopped, and kept reporting it,
    # because nothing was left to move the clock back. Abstain instead: UNKNOWN
    # holds the tab's existing colour rather than inventing a transition out of
    # a signal nobody is feeding.
    #
    # This deliberately trades a false alert for a missed one, which is the
    # worse direction by this project's own priority. It is only defensible
    # because the failure is now loud (WARNING) and self-correcting (the watcher
    # backs off and retries); silence here would not be.
    if not snapshot.redraw_signal_ok:
        return AgentState.UNKNOWN

    # Stopped. Everything below only decides how loudly to say so.
    tail = snapshot.tail
    if not tail.strip():
        # Nothing readable, so the reason is unknown — but the stop itself is
        # not in doubt. UNKNOWN holds whatever the tab already showed rather
        # than inventing a reason for a screen that cannot be read.
        return AgentState.UNKNOWN

    # Only a control counts. Question-shaped prose (_PROSE_PATTERNS) is
    # deliberately not consulted here: an agent that ends its turn by asking
    # something has finished, and colouring that red spends the alarm on a
    # session that needs nothing urgent.
    for pattern in _ACTION_PATTERNS:
        if pattern.search(tail):
            return AgentState.ACTION

    # Stopped, and nothing on screen needs a decision — but "stopped" is not
    # the same as "yours". An agent whose turn ended with a background shell
    # still running is re-entered when that shell exits, so there is nothing
    # here for the user to do and no reason to spend a colour saying otherwise.
    #
    # WORKING rather than a state of its own: "no paint, hand the title back to
    # iTerm2" is exactly what WORKING already means to tab_state, and a fifth
    # enum member with identical paint, priority and title behaviour would be a
    # synonym carried through every table that keys on state.
    if _background_work_running(tail):
        return AgentState.WORKING

    # Stopped, with no control on screen: it wants an instruction, not a
    # decision. Under the old text-only rules this branch returned UNKNOWN,
    # because text alone genuinely could not tell "finished" from "thinking".
    # Redraw timing now answers that, so the honest answer is WAITING.
    return AgentState.WAITING
