"""Shared fakes for tab-state painting tests."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeProfile:
    """Just the tab-colour half of an iTerm2 profile."""

    def __init__(self, use=False, color=None):
        self.use_tab_color = use
        self.use_tab_color_dark = use
        self.use_tab_color_light = use
        self.tab_color = color
        self.tab_color_dark = color
        self.tab_color_light = color


class FakeSession:
    def __init__(self, session_id, profile=None):
        self.session_id = session_id
        self.pushes = []
        #: What the pane looked like before cupline saw it. The default is the
        #: ordinary case: no manual tab colour.
        self.profile = profile or FakeProfile()

    async def async_get_profile(self):
        return self.profile

    async def async_set_profile_properties(self, profile):
        self.pushes.append(profile)


class FakeTab:
    def __init__(self, tab_id, pane_count=1, active=None):
        self.tab_id = tab_id
        self.sessions = [FakeSession(f"{tab_id}-p{i}") for i in range(pane_count)]
        self.titles = []
        #: Which pane iTerm2 considers active. None models a tab whose active
        #: pane could not be determined, which must fail safe rather than leave
        #: the tab bar uncoloured.
        self.active = active

    @property
    def current_session(self):
        if self.active is None:
            return None
        return next(s for s in self.sessions if s.session_id == self.active)

    async def async_set_title(self, title):
        self.titles.append(title)

    @property
    def total_pushes(self):
        return sum(len(s.pushes) for s in self.sessions)


def colors_of(tab, painter):
    """What each pane is currently painted, as the painter believes it."""
    return [painter.pane_applied.get(s.session_id) for s in tab.sessions]


class FakeWindow:
    def __init__(self, tabs):
        self.tabs = tabs


class FakeApp:
    def __init__(self, tabs):
        self.windows = [FakeWindow(tabs)]



