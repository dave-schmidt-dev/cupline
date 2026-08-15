"""Painting iTerm2 tab chrome to reflect agent state.

Verified behaviour on iTerm2 3.6.11 (see RESEARCH.md for the probe):

* Tab colour is a **profile property of a session**, applied per-session via
  ``async_set_profile_properties``.
* A tab's entry in the tab bar renders the colour of its **active** session
  alone. Setting the property on a background split pane changes the stored
  profile — ``use_tab_color`` reads back as 1 — while the tab bar stays
  unchanged. Reading the property back is therefore not evidence of visibility.
* Each split pane's **own title bar** does render its own ``tab_color``. Probed
  directly: painting one background pane amber and leaving the active pane clear
  produced a grey tab bar and a visibly amber title bar on that one pane.
* Profiles here have ``use_separate_colors_for_light_and_dark_mode`` enabled, so
  the plain ``set_tab_color`` setter alone is not sufficient; the ``_dark`` and
  ``_light`` variants have to be set too.

Those first two points pull in opposite directions, and the resolution is the
whole design here. Painting every pane the aggregate colour makes the tab bar
correct whichever pane has focus — but it also tells you three agents want you
when one does, which is worse than useless on a tab full of panes that are all
still working. Painting only the pane that wants you makes the tab bar go dark
whenever a *different* pane is focused, losing the signal entirely.

So: **every pane shows its own verdict, except the active pane, which carries
the tab's aggregate.** The tab bar is then always right, each background pane is
always right, and the single overstatement left is on the pane already on
screen in front of you — where you can see for yourself what it is doing.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Iterable, Optional

import iterm2

from config import HOLD_ON_UNKNOWN, STATE_COLORS, STATE_WORDS, TITLE_SEPARATOR
from models import AgentState, PaneVerdict, most_urgent

log = logging.getLogger("cupline.tab")


def _profile_for(color: Optional[tuple[int, int, int]]) -> iterm2.LocalWriteOnlyProfile:
    """Build the write-only profile that sets, or clears, a tab colour."""
    profile = iterm2.LocalWriteOnlyProfile()
    if color is None:
        profile.set_use_tab_color(False)
        profile.set_use_tab_color_dark(False)
        profile.set_use_tab_color_light(False)
        return profile
    swatch = iterm2.Color(*color)
    profile.set_use_tab_color(True)
    profile.set_tab_color(swatch)
    profile.set_use_tab_color_dark(True)
    profile.set_tab_color_dark(swatch)
    profile.set_use_tab_color_light(True)
    profile.set_tab_color_light(swatch)
    return profile


class TabPainter:
    """Applies aggregated state to tabs and remembers what to undo."""

    def __init__(self):
        #: tab_id -> the last state actually pushed, including the "no colour"
        #: states. This is what suppresses redundant RPCs; tracking only the
        #: coloured tabs means every clear re-pushes on every sweep.
        self.applied: dict[str, AgentState] = {}
        #: session_id -> the last state actually pushed to *that pane*. Panes no
        #: longer agree with each other, so tab-level bookkeeping can no longer
        #: answer "does this pane already carry the right colour?" — and without
        #: a per-pane answer every sweep re-pushes a profile to every session,
        #: which is the exact cost the tab-level cache was added to avoid.
        self.pane_applied: dict[str, AgentState] = {}
        #: session_id -> the tab it was painted in, so ``colored`` can be derived
        #: rather than tracked in parallel.
        self.pane_tab: dict[str, str] = {}
        #: session_id -> that pane's OWN last confident reading, which is not
        #: the same as what was painted on it: the active pane is painted with
        #: the tab aggregate. Only this may be held across an UNKNOWN.
        self.pane_own: dict[str, AgentState] = {}
        #: tab_id -> last title pushed (None means "iTerm2's automatic name").
        self.titled: dict[str, Optional[str]] = {}

    @property
    def colored(self) -> set[str]:
        """Tabs carrying a colour — i.e. what needs undoing on exit.

        Derived from what actually landed on each pane rather than tracked
        alongside it. A parallel tab-level set goes wrong on partial failure:
        if one pane's profile push raises while its siblings succeed, that pane
        keeps its colour but a tab-level "no longer coloured" flag would already
        have been cleared, and ``restore_painted`` would walk straight past it
        on shutdown — leaving amber behind with no process left to explain it.
        """
        return {
            self.pane_tab[sid]
            for sid, state in self.pane_applied.items()
            if STATE_COLORS.get(state) is not None and sid in self.pane_tab
        }

    @property
    def painted(self) -> set[str]:
        """Tabs this process has coloured and has not yet cleared."""
        return self.colored

    @staticmethod
    def aggregate(states: Iterable[AgentState]) -> AgentState:
        """One tab, one colour: the pane that most needs a human wins."""
        return most_urgent(states)

    @staticmethod
    def title_for(state: AgentState, project, others: int = 0) -> Optional[str]:
        """Tab title for a state, or None to hand the tab back to iTerm2.

        Only the states that need a human get a title. Clearing it for WORKING
        restores iTerm2's automatic name, which is normally the agent's current
        task — more useful than a static label while nothing is wanted.

        ``others`` is how many *additional* panes reached the same state. A tab
        has one title, so naming one project and silently dropping the rest was
        under-reporting: on a three-pane tab two agents finishing together
        produced a title mentioning one of them, and the second was invisible
        until you opened the tab. ``+N`` is deliberately a count rather than a
        list — tab titles are narrow, and "there is more than this one" is the
        part you cannot work out for yourself.
        """
        word = STATE_WORDS.get(state)
        if word is None:
            return None
        if not project:
            return word
        name = f"{project} +{others}" if others > 0 else project
        return f"{name}{TITLE_SEPARATOR}{word}"

    def _wanted(
        self,
        tab,
        state: AgentState,
        per_pane: Optional[dict[str, PaneVerdict]],
    ) -> dict[str, AgentState]:
        """Decide the colour-state for every pane in ``tab``.

        With ``per_pane`` absent this is the old behaviour — one state, every
        pane — which is what ``clear``/``reset_all`` and the demo path want:
        they are making the whole tab agree, not reporting on it.
        """
        active = getattr(getattr(tab, "current_session", None), "session_id", None)
        # No per-pane detail, or no idea which pane is active: fall back to
        # making the whole tab agree. Losing pane-level precision is a cosmetic
        # regression; leaving no pane holding the aggregate would drop the tab
        # bar's colour altogether, which is the signal itself.
        if per_pane is None or active is None:
            return {session.session_id: state for session in tab.sessions}

        wanted: dict[str, AgentState] = {}
        for session in tab.sessions:
            sid = session.session_id
            if sid == active:
                # Unconditional, and deliberately not gated on this pane being a
                # watched agent: a plain shell split can hold focus next to a
                # waiting agent, and if it does not carry the aggregate the tab
                # bar goes dark and the alert disappears.
                wanted[sid] = state
                continue
            verdict = per_pane.get(sid)
            if verdict is None:
                # Unwatched or never-read: no claim, so no colour.
                wanted[sid] = AgentState.WORKING
                continue
            if (verdict.state is AgentState.UNKNOWN and HOLD_ON_UNKNOWN
                    and verdict.evidence_current):
                # Hold this pane's OWN last reading, not what was last painted on
                # it. Those differ for the active pane, which carries the tab
                # aggregate: holding `pane_applied` there resurrected a colour the
                # pane had never earned, so a background pane could sit red on the
                # strength of a sibling's alert from while it had focus.
                wanted[sid] = self.pane_own.get(sid, AgentState.UNKNOWN)
                continue
            self.pane_own[sid] = verdict.state
            wanted[sid] = verdict.state

        # The active pane's own verdict still has to be recorded even though it
        # is painted with the aggregate, or it has nothing to fall back to when
        # it loses focus and goes UNKNOWN.
        own = per_pane.get(active)
        if own is not None and own.state is not AgentState.UNKNOWN:
            self.pane_own[active] = own.state
        return wanted

    async def apply(
        self,
        tab,
        state: AgentState,
        force: bool = False,
        project: Optional[str] = None,
        hold: bool = True,
        others: int = 0,
        per_pane: Optional[dict[str, PaneVerdict]] = None,
    ) -> bool:
        """Paint ``tab`` for ``state``. Returns True if a change was pushed.

        ``hold`` is the caller's answer to "is the held state's evidence still on
        screen?". False releases the tab even on an UNKNOWN reading — see
        ``SessionState.evidence_is_current``.
        """
        # An UNKNOWN reading is not evidence that the previous state ended, so
        # it holds whatever colour the tab already carries. It must NOT skip the
        # repaint, though: focus can move while a tab is held, and the newly
        # active pane is not carrying the tab's colour. Returning early here
        # dropped the tab bar to grey while an agent was still waiting — the
        # alert disappearing at the exact moment the user looked at the window.
        if (state is AgentState.UNKNOWN and HOLD_ON_UNKNOWN and hold
                and tab.tab_id in self.colored):
            state = self.applied.get(tab.tab_id, state)
        title = self.title_for(state, project, others)
        wanted = self._wanted(tab, state, per_pane)
        # Forget panes this tab used to hold but no longer does. Without it a
        # closed pane's entry survives forever, so `colored` keeps naming a tab
        # that has nothing left to clear and `restore_painted` can never
        # converge. Only entries claiming *this* tab are dropped, so a pane that
        # moved to another tab is left for that tab's own sweep to re-record.
        live = {session.session_id for session in tab.sessions}
        stale = [s for s, t in self.pane_tab.items() if t == tab.tab_id and s not in live]
        for sid in stale:
            self.pane_applied.pop(sid, None)
            self.pane_tab.pop(sid, None)
            self.pane_own.pop(sid, None)
        # Dedupe on the whole per-pane picture, not the tab aggregate. The
        # aggregate can sit still while an individual pane changes state, and it
        # sits still every time focus moves between panes — both of which have to
        # repaint, or the tab bar keeps a colour the newly-active pane never
        # earned.
        if not force and self.titled.get(tab.tab_id) == title and all(
                self.pane_applied.get(sid) is want for sid, want in wanted.items()):
            return False

        pushed = 0
        for session in tab.sessions:
            sid = session.session_id
            want = wanted[sid]
            if not force and self.pane_applied.get(sid) is want:
                # Re-record the tab even when nothing is pushed. A pane dragged
                # into a new tab while holding the same colour hits this branch,
                # and leaving `pane_tab` pointing at the tab it left made
                # `restore_painted` clear the old tab and never reach the pane —
                # stranding amber on a live session after a "clean" shutdown.
                self.pane_tab[sid] = tab.tab_id
                pushed += 1  # already correct on screen; still counts as landed
                continue
            try:
                await session.async_set_profile_properties(
                    _profile_for(STATE_COLORS.get(want)))
                self.pane_applied[sid] = want
                self.pane_tab[sid] = tab.tab_id
                pushed += 1
            except Exception as exc:  # noqa: BLE001 - one bad pane must not stop the rest
                log.warning("paint failed for session %s: %s", sid, exc)

        if pushed == 0:
            return False

        # Title is a tab-level call, unlike colour which is per-session. An
        # empty string restores iTerm2's automatic name — so only clear a title
        # this process actually set, or a manually-named tab would be wiped on
        # the first sweep.
        if title is not None or tab.tab_id in self.titled:
            try:
                await tab.async_set_title(title or "")
                if title is None:
                    self.titled.pop(tab.tab_id, None)
                else:
                    self.titled[tab.tab_id] = title
            except Exception as exc:  # noqa: BLE001 - colour landed; title is a bonus
                log.warning("title failed for tab %s: %s", tab.tab_id, exc)

        self.applied[tab.tab_id] = state
        # `colored` needs no update: it reads from what actually landed, so a
        # pane whose push raised keeps counting as coloured until a later sweep
        # or the shutdown restore genuinely clears it.
        log.info(
            "tab %s -> %s (%d panes: %s)%s",
            tab.tab_id, state.value, pushed,
            ", ".join(sorted(want.value for want in wanted.values())),
            f" title={title!r}" if title else "",
        )
        return True

    async def clear(self, tab) -> None:
        """Return a tab to default colour and iTerm2's automatic title.

        Passes no ``per_pane``, so every pane is cleared rather than just the
        active one. Leaving that out would strand a background pane's colour on
        shutdown, where it would outlive the process that can explain it.
        """
        await self.apply(tab, AgentState.WORKING, force=True)

    async def reset_all(self, app) -> int:
        """Clear tab colour everywhere. The crash-recovery path.

        If cupline dies while tabs are amber, the colour persists — it lives
        in iTerm2's profile state, not in this process.
        """
        cleared = 0
        profile = _profile_for(None)
        for window in app.windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    try:
                        await session.async_set_profile_properties(profile)
                        cleared += 1
                    except Exception as exc:  # noqa: BLE001
                        log.warning("reset failed for %s: %s", session.session_id, exc)
        for window in app.windows:
            for tab in window.tabs:
                with contextlib.suppress(Exception):
                    await tab.async_set_title("")
        self.applied.clear()
        self.pane_applied.clear()
        self.pane_tab.clear()
        self.pane_own.clear()
        self.titled.clear()
        log.info("reset tab colour and title on %d sessions", cleared)
        return cleared

    async def restore_painted(self, app) -> None:
        """Undo only what this process coloured. Used on clean shutdown."""
        if not self.colored:
            return
        by_id = {tab.tab_id: tab for window in app.windows for tab in window.tabs}
        for tab_id in list(self.colored):
            tab = by_id.get(tab_id)
            if tab is not None:
                await self.clear(tab)
        # No explicit reset: `clear` records WORKING on every pane it reaches, so
        # `colored` empties itself — and a tab that could *not* be reached stays
        # listed, which is the honest answer rather than a tidy one.
