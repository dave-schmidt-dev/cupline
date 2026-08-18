"""Tunables for cupline. Plain module constants — no config framework."""

from __future__ import annotations

from models import AgentState

#: How long a session's screen must hold still before it is classified.
#: Agents redraw spinners many times a second; this is what stops every one of
#: those frames from becoming a classification.
DEBOUNCE_SECONDS = 1.5

#: Ceiling on the debounce. A session showing a permanent spinner emits screen
#: events continuously (measured: ~26/second for Codex), so it never goes quiet
#: and a floor-only debounce would never classify it. Past this long without a
#: reading, classify anyway and let the normalised hash decide whether anything
#: actually moved.
MAX_CLASSIFY_INTERVAL_SECONDS = 4.0

#: How often the sweeper wakes to look for settled sessions.
SWEEP_INTERVAL_SECONDS = 0.5

#: How often a *quiet* session is re-read even though nothing has happened.
#:
#: A settled session emits no screen events, so without this the sweeper never
#: looks at it again — and since ``stable_since`` is only set by a second
#: reading that finds the screen unchanged, "this agent has been sitting still
#: for 40 seconds" could never be observed at all. That is the whole temporal
#: half of the classifier input, and it is the half that distinguishes "finished
#: its turn" from "thinking". The cost is one screen fetch per idle session per
#: interval, which is bounded and small; the alternative is a signal that cannot
#: exist.
IDLE_RECHECK_SECONDS = 3.0

#: Redraw silence, in seconds, after which an agent is treated as not working.
#:
#: **This is the primary signal.** An agent that is doing anything at all repaints
#: its terminal continuously — a spinner frame, an elapsed counter, streaming
#: output. One that has stopped repaints nothing whatsoever. Measured across 9
#: live sessions over 150 s, sampled through the same screen streamer the sweeper
#: uses:
#:
#:   * continuously working: longest silence **0.1 – 0.7 s** (Codex redraws ~27x/s)
#:   * stopped: **zero** redraw events across the entire 150 s
#:
#: There is no middle ground to tune against, so this threshold is not a
#: balance-point — it is a margin over the largest observed working gap. 5 s is
#: roughly 7x that, which absorbs a slower harness without making the signal
#: sluggish.
#:
#: Note this deliberately reports a *hung* agent as stopped: if a harness freezes
#: mid-turn its screen stops repainting, and "stopped working for whatever reason"
#: is the requirement. Text still saying "esc to interrupt" does not overrule it.
IDLE_AFTER_SECONDS = 5.0

#: Backoff for a screen watcher that keeps dying. `_ensure_watcher` runs every
#: sweep, so an unconditional respawn is a create-and-die loop at 2 Hz with no
#: ceiling — invisible, and pointless once the first few attempts have failed.
WATCHER_BACKOFF_BASE_SECONDS = 1.0
WATCHER_BACKOFF_MAX_SECONDS = 60.0

#: Ceiling on one process-table read. Standalone `ps` costs ~0.04 s here even
#: at load 137, so this is ~100x headroom — and it has still been exceeded 27
#: times in half an hour from inside the running service. PS_SLOW_SECONDS is
#: where a *successful* read starts being reported, so converting the read to
#: asyncio does not silently delete the only symptom of that.
PS_TIMEOUT_SECONDS = 5.0
PS_SLOW_SECONDS = 1.0

#: Lines of visible screen handed to the classifier. The screen is ~50-80 rows;
#: the interesting part of an agent turn is nearly always the bottom.
TAIL_LINES = 40

#: Commands treated as interactive AI agents. Matched against the session's
#: process ancestry, not just the foreground job — see sessions.resolve_agent.
#:
#: This is the ONLY harness-aware part of cupline, and it is deliberately
#: quarantined here. It gates *which sessions to watch*, never *how to classify
#: them*: the classifier is not told which agent produced the text.
AGENT_COMMANDS = (
    "claude",
    "codex",
    "opencode",
    "agy",
    "cursor-agent",
)

#: Shell names. A *login* shell (comm reported with a leading dash, e.g.
#: ``-zsh``) is the session's own shell and ends the ancestry walk. A plain
#: ``zsh``/``sh`` is a shell the agent itself spawned to run a command, and the
#: walk must continue past it — stopping there hid the agent for as long as it
#: was running any subprocess, which is most of the time it is working.
SHELL_NAMES = ("zsh", "bash", "sh", "fish", "dash", "ksh", "tcsh", "csh")

#: Hard boundaries: iTerm2's own plumbing. Matched by prefix because the server
#: reports a version suffix (``iTermServer-3.6.11``).
SESSION_ROOT_COMMANDS = ("login", "ShellLauncher", "iTermServer", "iTerm2")

#: Ancestry walk depth cap. Real chains run deeper than they look — an agent
#: running a tool through npm reaches 6 hops before the login shell.
MAX_ANCESTRY_DEPTH = 12

#: Tab colours per state. WORKING and UNKNOWN are absent on purpose: they mean
#: "no paint", which restores the tab's default appearance.
STATE_COLORS: dict[AgentState, tuple[int, int, int]] = {
    AgentState.WAITING: (230, 150, 30),   # amber
    AgentState.ACTION: (200, 40, 40),     # red
}

#: Words shown in the tab title next to the project name. States absent here
#: clear the title, which hands the tab back to iTerm2's automatic name — worth
#: keeping, since that name is usually the agent's current task.
STATE_WORDS: dict[AgentState, str] = {
    AgentState.WAITING: "your turn",
    AgentState.ACTION: "needs you",
}

#: Separator between project name and state word in the tab title.
TITLE_SEPARATOR = " · "

#: When True, an UNKNOWN reading holds whatever the tab already showed rather
#: than clearing it. Prevents a tab flickering back to default mid-turn.
HOLD_ON_UNKNOWN = True

LOG_DIR = ".logs"
LOG_FILE = "cupline.log"
