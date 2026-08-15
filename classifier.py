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
only to say why — a visible input control means the stop needs a decision from a
human (ACTION) rather than just a new instruction (WAITING).

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

    # Stopped, with no control on screen: it wants an instruction, not a
    # decision. Under the old text-only rules this branch returned UNKNOWN,
    # because text alone genuinely could not tell "finished" from "thinking".
    # Redraw timing now answers that, so the honest answer is WAITING.
    return AgentState.WAITING
