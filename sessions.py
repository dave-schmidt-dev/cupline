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

import logging
import subprocess
import time
from typing import Optional

import iterm2

from config import (
    AGENT_COMMANDS,
    MAX_ANCESTRY_DEPTH,
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


def _process_table(force: bool = False) -> dict[int, tuple[int, str]]:
    """Map pid -> (ppid, command). One fork for the whole process tree."""
    global _PS_CACHE, _PS_CACHE_AT
    now = time.monotonic()
    if not force and _PS_CACHE and (now - _PS_CACHE_AT) < _PS_CACHE_TTL:
        return _PS_CACHE

    table: dict[int, tuple[int, str]] = {}
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm="],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("ps failed, agent identification degraded: %s", exc)
        return _PS_CACHE

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


def resolve_agent(pid: Optional[int], force_refresh: bool = False) -> Optional[str]:
    """Walk the ancestry of ``pid`` and return the agent command, if any.

    Returns None for ordinary shells and non-agent programs, which is what gates
    a session out of cupline entirely.

    The walk passes *through* non-login shells. It previously stopped at any
    shell, which meant an agent running a command (``claude`` -> ``zsh`` -> the
    tool) resolved to None for the whole duration of that command — the session
    dropped off the watch list exactly while it was busiest.
    """
    if not pid:
        return None
    table = _process_table(force=force_refresh)
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
    """Build a SessionState from a live iTerm2 session."""
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
        seen = set()
        found = []
        for window in app.windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    seen.add(session.session_id)
                    if session.session_id in self.states:
                        found.append(self.states[session.session_id])
                        continue
                    state = await describe(session, tab, window)
                    self.states[session.session_id] = state
                    found.append(state)
        # Drop sessions that vanished without a termination event reaching us.
        for gone in set(self.states) - seen:
            self.states.pop(gone, None)
        return found

    async def refresh_agents(self, app) -> None:
        """Re-resolve agent identity. Cheap: one `ps` shared across sessions."""
        _process_table(force=True)
        for window in app.windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    state = self.states.get(session.session_id)
                    if state is None:
                        continue
                    state.tab_id = tab.tab_id          # panes can be moved
                    state.window_id = window.window_id
                    job_pid = await session.async_get_variable("jobPid")
                    try:
                        state.job_pid = int(job_pid) if job_pid else None
                    except (TypeError, ValueError):
                        state.job_pid = None
                    state.job_name = await session.async_get_variable("jobName")
                    state.agent = resolve_agent(state.job_pid)
                    # Agents change directory mid-session, so the project name
                    # is re-read rather than fixed at discovery.
                    state.project = project_name(
                        await session.async_get_variable("path")
                    )

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
