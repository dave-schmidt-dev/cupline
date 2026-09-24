"""One-shot CLI commands."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import screen as screenlib
import sessions as sessionlib
from config import IDLE_AFTER_SECONDS, TAIL_LINES
from models import AgentState
from tab_state import TabPainter

log = logging.getLogger("cupline")


# --------------------------------------------------------------------------
# one-shot commands
# --------------------------------------------------------------------------

async def cmd_list(app) -> None:
    registry = sessionlib.SessionRegistry(None)
    states = await registry.discover(app)
    print(f"{len(states)} sessions across {len(app.windows)} window(s)\n")
    print(f"{'session id':38} {'tab':>5}  {'job':16} {'agent':12} label")
    for state in states:
        print(
            f"{state.session_id:38} {state.tab_id:>5}  "
            f"{(state.job_name or '-'):16} {(state.agent or '-'):12} {state.label}"
        )


async def cmd_reset(app) -> None:
    painter = TabPainter()
    count = await painter.reset_all(app)
    print(f"cleared tab colour on {count} sessions")


async def cmd_set(app, assignment: str) -> None:
    """``--set <session-id>=action`` — paint one tab without any classifier."""
    session_id, _, name = assignment.partition("=")
    try:
        state = AgentState(name.strip().lower())
    except ValueError:
        print(f"unknown state {name!r}; expected one of "
              f"{', '.join(s.value for s in AgentState)}")
        return
    tab = _find_tab(app, session_id.strip())
    if tab is None:
        print(f"no session {session_id!r}; try --list")
        return
    registry = sessionlib.SessionRegistry(None)
    await registry.discover(app)
    project = next(
        (s.project for s in registry.states.values()
         if s.tab_id == tab.tab_id and s.project),
        None,
    )
    painter = TabPainter()
    await painter.apply(tab, state, force=True, project=project)
    title = painter.title_for(state, project)
    print(f"tab {tab.tab_id} -> {state.value}" + (f" ({title})" if title else ""))


def _find_tab(app, session_id: str):
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                if session.session_id == session_id or session.session_id.startswith(session_id):
                    return tab
    return None


async def cmd_demo(app, session_id: Optional[str], pause: float) -> None:
    """Cycle one tab through every state so the mapping can be seen.

    This is the no-LLM proof: it exercises exactly the paint path the real loop
    uses, with the classifier removed from the picture entirely.
    """
    registry = sessionlib.SessionRegistry(None)
    states = await registry.discover(app)
    if session_id:
        target = next((s for s in states if s.session_id.startswith(session_id)), None)
    else:
        target = next((s for s in states if s.is_agent()), None)
    if target is None:
        print("no agent session found; pass --demo-session <id>, or --list")
        return

    tab = _find_tab(app, target.session_id)
    painter = TabPainter()
    print(f"demo on tab {tab.tab_id} (session {target.label}, agent={target.agent}, "
          f"project={target.project})")
    try:
        for state in (AgentState.WAITING, AgentState.ACTION, AgentState.WORKING):
            await painter.apply(tab, state, force=True, project=target.project)
            title = painter.title_for(state, target.project)
            print(f"  {state.value:8} -> tab {tab.tab_id}"
                  f"{'  title=' + repr(title) if title else '  (default title)'}"
                  f"; watch the tab bar ({pause:.0f}s)")
            await asyncio.sleep(pause)
    finally:
        await painter.apply(tab, AgentState.WORKING, force=True)
        print("  restored default appearance")


async def cmd_capture(app, session_id: str, label: str) -> None:
    target_tab = _find_tab(app, session_id)
    if target_tab is None:
        print(f"no session {session_id!r}; try --list")
        return
    session = next(
        (s for s in target_tab.sessions if s.session_id.startswith(session_id)), None
    )
    if session is None:
        return
    registry = sessionlib.SessionRegistry(None)
    await registry.discover(app)
    state = registry.states.get(session.session_id)
    agent = (state.agent if state else None) or "unknown"

    contents = await session.async_get_screen_contents()
    tail = screenlib.tail_text(screenlib.lines_from_contents(contents), TAIL_LINES)

    # Record whether the pane was repainting, because that — not the text — is
    # what the classifier decides on. A tail saved without it is a fixture for
    # half the input, and replaying one cannot tell WORKING from stopped at all.
    # Watched for a shade over IDLE_AFTER_SECONDS so "no redraws" here means the
    # same thing it means to the sweeper.
    redraws = 0
    try:
        async with session.get_screen_streamer(want_contents=False) as streamer:
            async def _count():
                nonlocal redraws
                while True:
                    await streamer.async_get()
                    redraws += 1
            counter = asyncio.create_task(_count())
            await asyncio.sleep(IDLE_AFTER_SECONDS + 0.5)
            counter.cancel()
    except Exception as exc:  # noqa: BLE001
        log.debug("redraw sampling failed for %s: %s", session_id, exc)
        redraws = -1

    redrawing = {-1: "unmeasured", 0: "no"}.get(redraws, "yes")

    os.makedirs("fixtures", exist_ok=True)
    index = 1
    while os.path.exists(f"fixtures/{agent}-{label}-{index:02d}.txt"):
        index += 1
    path = f"fixtures/{agent}-{label}-{index:02d}.txt"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            f"# expected: {label.upper()}\n# agent: {agent}\n"
            f"# redrawing: {redrawing}\n"
            f"# redacted: paths, urls, long tokens\n---\n{screenlib.redact(tail)}\n"
        )
    print(f"wrote {path}  (redrawing: {redrawing}, {max(redraws, 0)} events "
          f"in {IDLE_AFTER_SECONDS + 0.5:.1f}s)")
    print("Redaction is a coarse first pass, not a guarantee.")
    print("REVIEW BEFORE COMMITTING — terminal tails can contain paths and credentials.")
