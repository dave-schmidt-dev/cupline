#!/usr/bin/env python3
"""cupline — universal terminal-attention monitor for AI coding agents.

Pipeline:

    iTerm sessions -> screen change events -> per-session debounce
                   -> classifier -> tab colour

Run with the shared venv:

    ~/.venvs/iterm2/bin/python cupline.py [--demo | --list | --reset | ...]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import os
import signal
import sys
import time
from typing import Optional

import iterm2

import classifier
import screen as screenlib
import sessions as sessionlib
from config import (
    DEBOUNCE_SECONDS,
    IDLE_AFTER_SECONDS,
    IDLE_RECHECK_SECONDS,
    LOG_DIR,
    LOG_FILE,
    MAX_CLASSIFY_INTERVAL_SECONDS,
    SHUTDOWN_TIMEOUT_SECONDS,
    SWEEP_INTERVAL_SECONDS,
    TAIL_LINES,
    WATCHDOG_INTERVAL_SECONDS,
    WATCHDOG_TIMEOUT_SECONDS,
    WATCHER_BACKOFF_BASE_SECONDS,
    WATCHER_BACKOFF_MAX_SECONDS,
)
from models import AgentState, PaneVerdict
from tab_state import TabPainter

log = logging.getLogger("cupline")


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

def setup_logging(debug: bool) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger("cupline")
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers.clear()

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, LOG_FILE), maxBytes=2_000_000, backupCount=3
    )
    file_handler.setLevel(logging.DEBUG if debug else logging.WARNING)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    root.addHandler(console)


# --------------------------------------------------------------------------
# the monitor
# --------------------------------------------------------------------------

class Cupline:
    """Owns the registry, the per-session watchers, and the sweeper."""

    def __init__(self, connection, app, *, show_tail: bool, debounce: float):
        self.connection = connection
        self.app = app
        self.registry = sessionlib.SessionRegistry(connection)
        self.painter = TabPainter()
        self.show_tail = show_tail
        self.debounce = debounce
        self.watchers: dict[str, asyncio.Task] = {}
        #: session_id -> consecutive watcher failures, and the monotonic time
        #: before which no respawn should be attempted.
        self._watcher_failures: dict[str, int] = {}
        self._watcher_retry_at: dict[str, float] = {}
        self._stop = asyncio.Event()

    # -- session watching --------------------------------------------------

    async def _watch_session(self, session_id: str) -> None:
        """Mark a session dirty whenever its screen changes.

        Deliberately uses ``want_contents=False``: this coroutine does no work
        beyond setting a flag. Fetching screen contents here would mean an RPC
        per spinner frame per session. The sweeper fetches instead, at most once
        per debounce window.
        """
        session = self.app.get_session_by_id(session_id)
        if session is None:
            # Not an exception, but the same outcome: nothing will feed this
            # session's redraw clock. Counted as a failure so it backs off
            # rather than being retried at every sweep in silence.
            self._note_watcher_failure(session_id, "no such session")
            return
        try:
            async with session.get_screen_streamer(want_contents=False) as streamer:
                self._note_watcher_started(session_id)
                while not self._stop.is_set():
                    await streamer.async_get()
                    state = self.registry.states.get(session_id)
                    if state is not None:
                        state.note_event(time.monotonic())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._note_watcher_failure(session_id, exc)

    def _note_watcher_started(self, session_id: str) -> None:
        """A streamer opened: this session's redraw clock is being fed again."""
        if self._watcher_failures.pop(session_id, 0):
            log.info("screen watcher for %s recovered", session_id)
        self._watcher_retry_at.pop(session_id, None)
        state = self.registry.states.get(session_id)
        if state is not None:
            state.streamer_ok = True

    def _note_watcher_failure(self, session_id: str, exc) -> None:
        """A watcher died. Say so, back off, and stop trusting its redraw clock.

        Logged at WARNING deliberately. This used to be a single ``log.debug``
        against a file handler set to WARNING, so a session whose streamer died
        left no record anywhere while its frozen clock reported it stopped for
        the rest of the process's life.
        """
        state = self.registry.states.get(session_id)
        if state is not None:
            state.streamer_ok = False
        failures = self._watcher_failures.get(session_id, 0) + 1
        self._watcher_failures[session_id] = failures
        delay = min(
            WATCHER_BACKOFF_BASE_SECONDS * (2 ** (failures - 1)),
            WATCHER_BACKOFF_MAX_SECONDS,
        )
        self._watcher_retry_at[session_id] = time.monotonic() + delay
        log.warning(
            "screen watcher for %s failed (%d in a row): %s; retrying in %.0fs",
            session_id, failures, exc, delay,
        )

    def _ensure_watcher(self, session_id: str) -> None:
        task = self.watchers.get(session_id)
        if task is not None and not task.done():
            return
        retry_at = self._watcher_retry_at.get(session_id)
        if retry_at is not None and time.monotonic() < retry_at:
            return  # still backing off; see _note_watcher_failure
        self.watchers[session_id] = asyncio.create_task(self._watch_session(session_id))

    def _drop_watcher(self, session_id: str) -> None:
        # Does not touch `streamer_ok`. Cancelling re-raises CancelledError out
        # of `_watch_session` without running either notifier, so a session that
        # *survived* in the registry would be left claiming a healthy clock with
        # no watcher feeding it. That cannot happen: `on_session_gone` pops the
        # state, and the periodic prune only drops watchers whose session is
        # already gone from the registry. Left as a note rather than defensive
        # code, so the reason it is safe is checkable if either caller changes.
        task = self.watchers.pop(session_id, None)
        self._watcher_failures.pop(session_id, None)
        self._watcher_retry_at.pop(session_id, None)
        if task is not None:
            task.cancel()

    # -- lifecycle events --------------------------------------------------

    async def on_new_session(self, session_id: str) -> None:
        await self.registry.discover(self.app)
        state = self.registry.states.get(session_id)
        if state is None:
            return
        log.info("new session %s agent=%s job=%s", state.label, state.agent, state.job_name)
        self._ensure_watcher(session_id)

    async def on_session_gone(self, session_id: str) -> None:
        self._drop_watcher(session_id)
        self.registry.states.pop(session_id, None)

    # -- the sweeper -------------------------------------------------------

    async def sweep_forever(self) -> None:
        """Single timer for all sessions. Debounce lives here and nowhere else."""
        tick = 0
        while not self._stop.is_set():
            try:
                await self._sweep(tick)
            except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the loop
                log.warning("sweep error: %s", exc, exc_info=True)
            tick += 1
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), SWEEP_INTERVAL_SECONDS)

    async def _sweep(self, tick: int) -> None:
        now = time.monotonic()

        # Agent identity is re-resolved periodically, not per tick: the
        # foreground job changes constantly and one `ps` covers every session.
        #
        # Rediscovery runs here too, so NewSessionMonitor is an optimisation
        # rather than a single point of failure. A monitor event that is missed,
        # or that arrives before app.windows reflects the new session, would
        # otherwise leave that session invisible for its entire lifetime:
        # refresh_agents only touches sessions already in the registry.
        if tick % 8 == 0:
            # One forced read for the whole tick. Both calls below refresh the
            # process table, and at a 2 s cache TTL against a 4 s period each
            # would otherwise fork its own `ps` over ~2500 processes, back to
            # back, for the same answer. Forcing once here keeps "this tick
            # sees a fresh table" while halving the forks.
            await sessionlib.refresh_process_table(force=True)
            await self.registry.discover(self.app)
            await self.registry.refresh_agents(self.app)
            # discover() also prunes sessions iTerm2 no longer reports, which
            # covers a missed termination event — but the watcher task for a
            # pruned session would otherwise stay parked on a dead streamer.
            for gone in set(self.watchers) - set(self.registry.states):
                self._drop_watcher(gone)

        tab_votes: dict[str, list[tuple[str, AgentState, Optional[str], bool]]] = {}
        tabs_by_id = {t.tab_id: t for w in self.app.windows for t in w.tabs}

        for state in self.registry.agent_sessions():
            self._ensure_watcher(state.session_id)

            if self._should_read(state, now):
                await self._read_and_classify(state, now)
            elif state.has_been_read():
                # No fetch needed to notice a stop: the redraw clock advances on
                # its own. This is what makes detection cost one comparison per
                # tick instead of one screen RPC per session per interval.
                self._conclude(state, now, changed=False)

            if state.has_been_read():
                tab_votes.setdefault(state.tab_id, []).append(
                    (state.session_id, state.previous_classification,
                     state.project, state.evidence_is_current())
                )

        for tab_id, votes in tab_votes.items():
            tab = tabs_by_id.get(tab_id)
            if tab is None:
                continue
            winner = self.painter.aggregate(vote for _, vote, _, _ in votes)
            # The tab title names the pane that won the aggregation, so a shared
            # tab says which project wants attention, not just that one does.
            #
            # With several panes in the same state the title can only name one,
            # so it also carries how many others are in it. Naming one and
            # dropping the rest silently under-reported the thing being asked
            # for: three panes per tab here, all on auto, so agents finishing
            # together is the normal case rather than an edge one.
            winners = [p for _, vote, p, _ in votes if vote is winner]
            project = next((p for p in winners if p), None)
            others = max(len(winners) - 1, 0)
            # Keep holding an UNKNOWN only while some pane can still point at the
            # screen its state came from. Panes with nothing to protect abstain
            # rather than voting to hold, or a tab full of never-classified panes
            # keeps a colour that the one pane which earned it has moved past.
            hold = any(
                current for _, vote, _, current in votes if vote is AgentState.UNKNOWN
            )
            # The aggregate is still what the tab bar needs, but the panes each
            # get their own verdict: painting all three amber because one agent
            # stopped says three of them want you, which is the opposite of the
            # question being answered.
            per_pane = {
                sid: PaneVerdict(vote, current) for sid, vote, _, current in votes
            }
            await self.painter.apply(tab, winner, project=project, hold=hold,
                                     others=others, per_pane=per_pane)

        # A tab only gets repainted while something in it still votes, and votes
        # come only from sessions that currently resolve as agents. Quit the
        # harness back to a shell and the tab stops voting mid-alert: nothing
        # ever visits it again, so the red stays until cupline exits. A stuck
        # alert is the worst failure this tool has — it is indistinguishable from
        # a real one and it never clears — so any tab we coloured that has gone
        # silent is released here.
        #
        # The cost of being wrong is a *cleared* tab, not a false alert: if the
        # agent is still really there, the next sweep that resolves it repaints.
        for tab_id in self.painter.colored - set(tab_votes):
            tab = tabs_by_id.get(tab_id)
            if tab is not None:
                log.info("tab %s no longer has any agent; releasing", tab_id)
                await self.painter.clear(tab)

    def _should_read(self, state, now: float) -> bool:
        """Debounce with both a floor and a ceiling.

        Floor: wait for ``debounce`` seconds of quiet, so a burst of redraws
        produces one reading rather than dozens. Keyed on raw redraws
        (``last_event_at``), because the point is to let the burst finish.

        Ceiling: a session animating a spinner never goes quiet, so once it has
        been dirty for ``MAX_CLASSIFY_INTERVAL_SECONDS`` read it regardless. The
        normalised hash then decides whether the content really moved — that is
        what keeps spinner frames from becoming classifications.

        Idle recheck: a session that has gone quiet emits no events at all, so
        neither branch above can fire again for it. It still needs revisiting,
        because *how long it has held still* is the signal — and that only
        becomes observable through a later reading.
        """
        if not state.has_been_read():
            return True  # baseline reading on first sight
        if not state.dirty:
            return (now - state.last_read_at) >= IDLE_RECHECK_SECONDS
        if (now - state.last_event_at) >= self.debounce:
            return True
        return (now - state.last_read_at) >= MAX_CLASSIFY_INTERVAL_SECONDS

    async def _read_and_classify(self, state, now: float) -> None:
        """Fetch the screen once, hash it, and classify what is there now."""
        session = self.app.get_session_by_id(state.session_id)
        if session is None:
            return

        # Clear `dirty` BEFORE the fetch, not after. A screen event arriving
        # during the await would otherwise be erased by a later clear, leaving
        # contents that predate the change and no flag to trigger a re-read —
        # and since a settled session emits no further events, the reading could
        # be stale indefinitely. The lost transition is exactly "agent printed
        # its final prompt and went quiet", which is the amber this tool exists
        # to produce.
        state.dirty = False
        try:
            contents = await session.async_get_screen_contents()
        except Exception as exc:  # noqa: BLE001
            log.debug("screen fetch failed for %s: %s", state.label, exc)
            state.dirty = True  # otherwise this session is never read again
            return

        lines = screenlib.lines_from_contents(contents)
        tail = screenlib.tail_text(lines, TAIL_LINES)
        digest = screenlib.screen_hash(tail)
        changed = state.note_change(digest, tail, now)
        state.last_read_at = now
        self._conclude(state, now, changed=changed, tail=tail)

    def _conclude(self, state, now: float, *, changed: bool, tail: str = "") -> None:
        """Classify from what is already known, without fetching anything.

        Split out of ``_read_and_classify`` so the sweeper can run it on every
        tick. The primary signal is ``seconds_since_redraw``, which is maintained
        by the streamer and keeps advancing whether or not the screen is fetched
        — so gating classification on a fetch would delay every "stopped"
        verdict by up to a whole idle-recheck interval for no benefit. The fetch
        is now only needed to keep the *tail* current, which only matters for
        deciding WAITING versus ACTION.

        Classification also runs on unchanged readings on purpose: those are the
        ones carrying elapsed time, and elapsed time is the entire signal.
        """
        snapshot = state.snapshot(now)
        result = classifier.classify(snapshot)
        previous = state.note_classification(result)

        if not changed and result is previous:
            return  # nothing moved and nothing concluded: not worth a line

        # A state change is the interesting event. Screen churn that lands on
        # the same state is DEBUG, or an active agent floods the console.
        log.log(
            logging.INFO if previous is not result else logging.DEBUG,
            "session=%s process=%s agent=%s changed=%s state=%s%s",
            state.label, state.job_name, state.agent, str(changed).lower(), result.value,
            "" if previous is result else f" (was {previous.value})",
        )
        if self.show_tail:
            print("--- terminal tail ---")
            print("\n".join(tail.splitlines()[-12:]))
            print("---------------------")

    # -- run ---------------------------------------------------------------

    async def run(self) -> None:
        found = await self.registry.discover(self.app)
        agents = [s for s in found if s.is_agent()]
        log.info(
            "discovered %d sessions across %d windows; %d look like agents",
            len(found), len(self.app.windows), len(agents),
        )
        for state in found:
            log.info(
                "  %s tab=%s job=%-16s agent=%s",
                state.label, state.tab_id, state.job_name, state.agent or "-",
            )
            if state.is_agent():
                self._ensure_watcher(state.session_id)

        tasks = [
            asyncio.create_task(self.sweep_forever()),
            asyncio.create_task(
                sessionlib.watch_new_sessions(self.connection, self.on_new_session)
            ),
            asyncio.create_task(
                sessionlib.watch_terminations(self.connection, self.on_session_gone)
            ),
            asyncio.create_task(self._watchdog_forever()),
        ]
        try:
            await self._stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            for session_id in list(self.watchers):
                self._drop_watcher(session_id)
            log.info("restoring tab appearance before exit")
            try:
                await asyncio.wait_for(
                    self.painter.restore_painted(self.app), timeout=SHUTDOWN_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                log.warning(
                    "could not restore tab appearance within %.0fs; connection is likely dead",
                    SHUTDOWN_TIMEOUT_SECONDS,
                )

    def stop(self) -> None:
        self._stop.set()

    # -- connection watchdog -------------------------------------------------

    async def _watchdog_forever(self) -> None:
        """Prove the primary connection is alive; do not just assume it.

        Nothing else in this process can tell the difference between "iTerm2
        is quiet" and "the connection died silently": a screen watcher that
        can't reopen its streamer just backs off forever (see
        `_note_watcher_failure`), and the per-tick RPCs in `_sweep` read
        locally-cached state that a dead connection does not invalidate. A
        cheap round-trip RPC on its own timer is what catches that — it does
        not matter which internal task died, only whether an RPC still
        completes. A failure here is fatal on purpose: exiting is what lets
        launchd's KeepAlive reconnect against a live iTerm2 instead of leaving
        a process that looks running but does nothing.
        """
        while not self._stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), WATCHDOG_INTERVAL_SECONDS)
            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(self.app.async_refresh(), timeout=WATCHDOG_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001 - any failure here means the connection is dead
                log.error(
                    "connection watchdog: %s; exiting so launchd can restart against a live connection",
                    exc,
                )
                self.stop()
                return


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


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="cupline terminal-attention monitor")
    parser.add_argument("--debug", action="store_true", help="DEBUG-level file logging")
    parser.add_argument("--tail", action="store_true",
                        help="print the terminal tail on every classified change")
    parser.add_argument("--debounce", type=float, default=DEBOUNCE_SECONDS,
                        help=f"seconds of quiet before classifying (default {DEBOUNCE_SECONDS})")
    parser.add_argument("--list", action="store_true", help="list sessions and exit")
    parser.add_argument("--reset", action="store_true",
                        help="clear tab colour on every session and exit")
    parser.add_argument("--set", metavar="ID=STATE",
                        help="paint one session's tab and exit, e.g. --set 5D23=action")
    parser.add_argument("--demo", action="store_true",
                        help="cycle a tab through WAITING/ACTION/WORKING and exit")
    parser.add_argument("--demo-session", metavar="ID",
                        help="session id prefix for --demo")
    parser.add_argument("--demo-pause", type=float, default=5.0,
                        help="seconds to hold each demo state (default 5)")
    parser.add_argument("--capture", metavar="ID", help="save a session's tail as a fixture")
    parser.add_argument("--label", default="unknown",
                        help="expected label for --capture (working/waiting/action/unknown)")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    setup_logging(args.debug)

    async def entry(connection):
        app = await iterm2.async_get_app(connection)

        if args.list:
            return await cmd_list(app)
        if args.reset:
            return await cmd_reset(app)
        if args.set:
            return await cmd_set(app, args.set)
        if args.demo:
            return await cmd_demo(app, args.demo_session, args.demo_pause)
        if args.capture:
            return await cmd_capture(app, args.capture, args.label)

        monitor = Cupline(connection, app, show_tail=args.tail, debounce=args.debounce)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, monitor.stop)
        log.info("watching. ctrl-c to stop; tab colour is restored on exit.")
        await monitor.run()

    iterm2.run_until_complete(entry, retry=False)


if __name__ == "__main__":
    main()
