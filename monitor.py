"""The Cupline monitor."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Optional

import classifier
import screen as screenlib
import sessions as sessionlib
from config import (
    ACKNOWLEDGE_ON_FOCUS,
    IDLE_RECHECK_SECONDS,
    MAX_CLASSIFY_INTERVAL_SECONDS,
    SHUTDOWN_TIMEOUT_SECONDS,
    SWEEP_INTERVAL_SECONDS,
    TAIL_LINES,
    WATCHDOG_INTERVAL_SECONDS,
    WATCHDOG_TIMEOUT_SECONDS,
)
from models import AgentState, PaneVerdict
from tab_state import TabPainter
from watchers import SessionWatchers

log = logging.getLogger("cupline")


# --------------------------------------------------------------------------
# the monitor
# --------------------------------------------------------------------------

class Cupline(SessionWatchers):
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
        #: session_id -> the screen hash that was up while that pane had
        #: keyboard focus. A pane you have looked at is not news until its
        #: screen moves on; see ``_focused_session_id`` and ACKNOWLEDGE_ON_FOCUS.
        self.acked: dict[str, str] = {}
        self._stop = asyncio.Event()

    # -- the sweeper -------------------------------------------------------

    def _focused_session_id(self) -> Optional[str]:
        """The pane the user is actually looking at, or None.

        iTerm2 has to be the frontmost application for this to mean anything.
        The library keeps reporting a current window, tab and session whether or
        not iTerm2 is in front — that is the last pane to hold focus, not one
        anybody is reading — so without the ``app_active`` gate every pane the
        user last touched before switching to a browser would be treated as
        watched. ``app_active`` is None until a focus notification has been
        seen, and None is not True, so an unknown answer acknowledges nothing.

        The app object keeps all of this current from focus notifications, and
        the watchdog's ``async_refresh`` re-reads it every
        WATCHDOG_INTERVAL_SECONDS, so a dropped notification self-corrects
        rather than pinning the answer forever.
        """
        if not ACKNOWLEDGE_ON_FOCUS or self.app.app_active is not True:
            return None
        window = self.app.current_window
        session = getattr(getattr(window, "current_tab", None), "current_session", None)
        return getattr(session, "session_id", None)

    def _vote_for(self, state, focused: Optional[str]) -> AgentState:
        """That pane's classification, minus anything the user has already read.

        Recording happens here rather than on a focus event because the thing
        being acknowledged is a *screen*, not a moment: the sweep is where the
        current screen hash is known, and re-recording it every tick is what
        keeps a pane you are sitting on quiet as its content moves under you.

        The acknowledgement is void the instant the screen differs, and the
        entry is dropped rather than kept, so a screen that changes and later
        returns to identical text alerts again. That is the safe direction: the
        cost of dropping too eagerly is an amber you have seen before, and the
        cost of keeping it is an alert that never comes.

        Keyed on ``last_ack_hash`` and deliberately not on ``last_screen_hash``.
        The latter flattens plain numbers so a token counter cannot masquerade
        as progress, which is right for the debounce and wrong here: it makes
        ``Done. 3 tests failed.`` and ``Done. 5 tests failed.`` the same screen,
        so a glance at the first suppresses the second. Sharing one hash between
        the two questions cost an alert; see ``screen.normalize_for_ack``.
        """
        vote = state.previous_classification
        sid = state.session_id
        seen = state.last_ack_hash
        if sid == focused:
            self.acked[sid] = seen
        elif self.acked.get(sid) != seen:
            self.acked.pop(sid, None)
        if vote is AgentState.WAITING and self.acked.get(sid) == seen:
            return AgentState.WORKING
        return vote

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
        focused = self._focused_session_id()

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
                    (state.session_id, self._vote_for(state, focused),
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
        changed = state.note_change(digest, tail, now,
                                    ack_hash=screenlib.ack_hash(tail))
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
