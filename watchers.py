"""Per-session screen watchers and lifecycle events."""

from __future__ import annotations

import asyncio
import logging
import time

from config import WATCHER_BACKOFF_BASE_SECONDS, WATCHER_BACKOFF_MAX_SECONDS

log = logging.getLogger("cupline")


class SessionWatchers:
    """Mixed into ``Cupline``; reads app, registry, watchers, _watcher_failures, _watcher_retry_at, acked, and _stop set by ``Cupline.__init__``."""

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
        self.acked.pop(session_id, None)
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
