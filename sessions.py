"""Session discovery, lifecycle tracking, and agent identification.

Agent identification is the part worth reading. iTerm2's ``jobName`` variable
reports the *foreground* job, which for these agents is whatever subprocess they
happen to be running right now — observed live: a Codex session reported
``codex`` on one poll and ``SkyComputerUseCl`` seconds later, and every
Node-based agent (Claude Code, OpenCode, Cursor Agent) reports a bare ``node``.
Matching on it directly is unreliable in both directions.

So instead we walk the process tree upward from the foreground job's pid until
we hit the session's shell, and look for a known agent command anywhere in that
chain. A Claude Code session running an MCP server resolves as:

    node (playwright-mcp) -> npm exec -> claude -> -zsh   =>  claude
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Optional

import iterm2

from config import (
    AGENT_COMMANDS,
    MAX_ANCESTRY_DEPTH,
    PS_SLOW_SECONDS,
    PS_TIMEOUT_SECONDS,
    SESSION_ROOT_COMMANDS,
    SHELL_NAMES,
)
from models import SessionState

log = logging.getLogger("cupline.sessions")

#: Process table cache. Rebuilding costs one `ps` fork, so it is shared across
#: all sessions in a sweep instead of forking once per session.
_PS_CACHE: dict[int, tuple[int, str]] = {}
_PS_CACHE_AT: float = 0.0
_PS_CACHE_TTL = 2.0


#: Kept as a module constant so a test can point the reader at a command whose
#: timing it controls, without a live process table in the picture.
_PS_COMMAND = ("ps", "-Ao", "pid=,ppid=,comm=")


async def _run_ps() -> str:
    """Read the process table without blocking the event loop."""
    proc = await asyncio.create_subprocess_exec(
        *_PS_COMMAND,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=PS_TIMEOUT_SECONDS
        )
    except (TimeoutError, asyncio.CancelledError):
        # `wait_for` cancels the *await*, not the process. Without this the
        # timed-out `ps` keeps running and is never reaped, so the 27 timeouts
        # observed in half an hour would have been 27 stranded children.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    if proc.returncode != 0:
        raise OSError(f"ps exited {proc.returncode}")
    return stdout.decode(errors="replace")


def _process_table() -> dict[int, tuple[int, str]]:
    """The cached pid -> (ppid, command) map. Never forks.

    Refreshing is an ``await`` now, so it lives in ``refresh_process_table``
    and this only reads what that last stored.
    """
    return _PS_CACHE


async def refresh_process_table(force: bool = False) -> dict[int, tuple[int, str]]:
    """Rebuild the process table, off the event loop.

    This used to be a synchronous ``subprocess.run`` reached from an awaited
    call, so for up to the full timeout the entire loop was frozen: no screen
    streamer callbacks, no sweeps, no signal handling, nothing on any output
    surface. That is the project's no-silent-waits rule, not a style point.

    The timeout is still *reported* rather than smoothed away. Making the read
    async removes the freeze, and the freeze was the only symptom pointing at
    whatever makes an 0.04 s command exceed five seconds inside this process —
    so a slow-but-successful read is logged too, or the evidence trail for that
    open question disappears with the stall.
    """
    global _PS_CACHE, _PS_CACHE_AT
    now = time.monotonic()
    if not force and _PS_CACHE and (now - _PS_CACHE_AT) < _PS_CACHE_TTL:
        return _PS_CACHE

    started = time.monotonic()
    try:
        out = await _run_ps()
    except Exception as exc:  # noqa: BLE001
        if _PS_CACHE:
            log.warning(
                "ps failed after %.1fs, agent identification degraded to a "
                "%.1fs-old table: %s",
                time.monotonic() - started, now - _PS_CACHE_AT, exc,
            )
        else:
            # Cold start. There is no stale table to fall back on, so *no*
            # session resolves as an agent — on screen that is indistinguishable
            # from "no agents are running", which is why it gets its own line.
            log.warning(
                "ps failed after %.1fs with no cached table (%s); no session "
                "can be identified as an agent until this succeeds",
                time.monotonic() - started, exc,
            )
        return _PS_CACHE

    elapsed = time.monotonic() - started
    table: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        table[pid] = (ppid, parts[2].strip())
    _PS_CACHE, _PS_CACHE_AT = table, now
    if elapsed >= PS_SLOW_SECONDS:
        log.warning(
            "%.2fs to resume after reading the process table (%d processes). "
            "ps itself costs ~0.1s here, so this is scheduling delay, not a "
            "slow read: alerts can lag by about this much",
            elapsed, len(table),
        )
    return table


def _basename(command: str) -> str:
    return command.rsplit("/", 1)[-1].lstrip("-")


def _is_login_shell(command: str) -> bool:
    """A shell started as a login shell — the session's own, not a nested one.

    Unix convention is to pass argv[0] with a leading dash for a login shell,
    and `ps comm` preserves it: the session's shell is ``-zsh`` while a shell an
    agent spawned to run a command is plain ``zsh``. That one character is the
    whole boundary between "this session's root" and "a subprocess".
    """
    tail = command.rsplit("/", 1)[-1]
    return tail.startswith("-") and tail.lstrip("-") in SHELL_NAMES


def _is_session_root(name: str) -> bool:
    return any(name.startswith(root) for root in SESSION_ROOT_COMMANDS)


def resolve_agent(pid: Optional[int]) -> Optional[str]:
    """Walk the ancestry of ``pid`` and return the agent command, if any.

    Returns None for ordinary shells and non-agent programs, which is what gates
    a session out of cupline entirely.

    Reads the cached process table and never refreshes it — refreshing is an
    ``await`` now and this is synchronous. Callers must have awaited
    ``refresh_process_table`` first; ``describe`` and ``refresh_agents`` both
    do. The old ``force_refresh`` parameter is gone rather than kept as a
    silent no-op.

    The walk passes *through* non-login shells. It previously stopped at any
    shell, which meant an agent running a command (``claude`` -> ``zsh`` -> the
    tool) resolved to None for the whole duration of that command — the session
    dropped off the watch list exactly while it was busiest.
    """
    if not pid:
        return None
    table = _process_table()
    current, depth = pid, 0
    while current and current != 1 and depth < MAX_ANCESTRY_DEPTH:
        entry = table.get(current)
        if entry is None:
            return None
        ppid, command = entry
        name = _basename(command)
        # opencode ships as `opencode.exe` behind a symlink. Invoked through the
        # symlink the kernel records the symlink path, so the plain name matches
        # — but exec'd directly it would not. Cheap guard against that.
        if name.endswith(".exe"):
            name = name[: -len(".exe")]
        if name in AGENT_COMMANDS:
            return name
        # Boundary: the session's own login shell, or iTerm2's plumbing above
        # it. Anything past here belongs to the terminal, not to this session.
        if _is_login_shell(command) or _is_session_root(name):
            return None
        current, depth = ppid, depth + 1
    return None


async def describe(session, tab, window) -> SessionState:
    """Build a SessionState from a live iTerm2 session.

    Assumes the caller has refreshed the process table; ``discover`` does.
    """
    job_name = await session.async_get_variable("jobName")
    job_pid = await session.async_get_variable("jobPid")
    try:
        job_pid = int(job_pid) if job_pid else None
    except (TypeError, ValueError):
        job_pid = None

    state = SessionState(
        session_id=session.session_id,
        tab_id=tab.tab_id,
        window_id=window.window_id,
        job_name=job_name,
        job_pid=job_pid,
        agent=resolve_agent(job_pid),
        project=project_name(await session.async_get_variable("path")),
    )
    state.label = await _label(session, state)
    return state


def project_name(path: Optional[str]) -> Optional[str]:
    """Basename of the session's working directory, or None at ``/`` or home."""
    if not path:
        return None
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return name or None


async def _label(session, state: SessionState) -> str:
    """Short human-readable id for logs, e.g. ``w1t2p0`` style plus agent."""
    tty = await session.async_get_variable("tty") or "?"
    return f"{state.session_id[:8]}/{tty.rsplit('/', 1)[-1]}"


class SessionRegistry:
    """Tracks every session iTerm2 knows about, agent or not."""

    def __init__(self, connection):
        self.connection = connection
        self.states: dict[str, SessionState] = {}

    async def discover(self, app) -> list[SessionState]:
        """Enumerate all current sessions. Idempotent — safe to re-run."""
        # Once for the whole pass, and deliberately *outside* the per-session
        # guard below. Refreshing inside `describe` put it inside that guard,
        # where a process-table failure would have been logged as a pane that
        # vanished and skipped the session — a misattribution that would read
        # as a closing pane forever.
        await refresh_process_table()
        seen = set()
        found = []
        for window in app.windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    seen.add(session.session_id)
                    if session.session_id in self.states:
                        found.append(self.states[session.session_id])
                        continue
                    try:
                        state = await describe(session, tab, window)
                    except Exception as exc:  # noqa: BLE001
                        # Same race as refresh_agents below: a pane can close
                        # between this enumeration and the RPCs inside
                        # describe(). It was never in the registry, so there is
                        # nothing to drop — just do not let it end the walk.
                        log.info("session %s vanished during discovery (%s)",
                                 session.session_id, exc)
                        continue
                    self.states[session.session_id] = state
                    found.append(state)
        # Drop sessions that vanished without a termination event reaching us.
        for gone in set(self.states) - seen:
            self.states.pop(gone, None)
        return found

    @staticmethod
    async def _refresh_one(session, state: SessionState) -> None:
        """Re-read the per-session variables that change during its life.

        Every ``await`` here is an RPC against a session that may have closed
        since the caller enumerated it, so the whole body is one unit that the
        caller guards — see ``refresh_agents``.
        """
        job_pid = await session.async_get_variable("jobPid")
        try:
            state.job_pid = int(job_pid) if job_pid else None
        except (TypeError, ValueError):
            state.job_pid = None
        state.job_name = await session.async_get_variable("jobName")
        state.agent = resolve_agent(state.job_pid)
        # Agents change directory mid-session, so the project name is re-read
        # rather than fixed at discovery.
        state.project = project_name(await session.async_get_variable("path"))

    async def refresh_agents(self, app) -> None:
        """Re-resolve agent identity. Cheap: one `ps` shared across sessions.

        That sentence was false for as long as this forced its own refresh.
        The only caller runs ``discover`` immediately before, which refreshes
        too, so the forced read re-forked over a table that was ~45 ms old --
        confirmed in production, where 15 of 15 closely-spaced slow reads had
        the second starting within 5-51 ms of the first finishing. The caller
        now takes one forced read for the whole tick and both passes share it.

        Each session is guarded individually. iTerm2 answers
        ``SESSION_NOT_FOUND`` for a pane that closed between the enumeration
        above and the RPC below, and that used to propagate out of here and out
        of the sweep — so **one** closed pane left **every** pane unclassified
        and unpainted for that tick. Caught in production at 22:21:33.
        """
        await refresh_process_table()
        for window in app.windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    state = self.states.get(session.session_id)
                    if state is None:
                        continue
                    state.tab_id = tab.tab_id          # panes can be moved
                    state.window_id = window.window_id
                    try:
                        await self._refresh_one(session, state)
                    except Exception as exc:  # noqa: BLE001
                        # Dropped rather than kept stale: the overwhelmingly
                        # likely cause is that the pane is gone. If it is not,
                        # the next discover() re-adds it, so the cost of being
                        # wrong is one tick of identity — the same self-heal
                        # the old behaviour had, minus taking its peers down.
                        log.info("session %s vanished during refresh (%s); dropping",
                                 session.session_id, exc)
                        self.states.pop(session.session_id, None)

    def agent_sessions(self) -> list[SessionState]:
        return [s for s in self.states.values() if s.is_agent()]


async def watch_new_sessions(connection, on_new) -> None:
    """Long-lived: call ``on_new(session_id)`` for each session created."""
    async with iterm2.NewSessionMonitor(connection) as monitor:
        while True:
            session_id = await monitor.async_get()
            log.info("session created: %s", session_id)
            await on_new(session_id)


async def watch_terminations(connection, on_gone) -> None:
    """Long-lived: call ``on_gone(session_id)`` for each session destroyed."""
    async with iterm2.SessionTerminationMonitor(connection) as monitor:
        while True:
            session_id = await monitor.async_get()
            log.info("session terminated: %s", session_id)
            await on_gone(session_id)
