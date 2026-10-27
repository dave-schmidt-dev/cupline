"""Tab-colour profiles: what to push, what to put back, and what is ours.

The painting policy (which pane gets which colour, and why) lives in
``tab_state``; this module only knows how to turn a colour into an iTerm2
profile write, how to remember what a pane had before, and how to recognise
cupline's own palette when it meets it again after a restart.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

import iterm2

from config import STATE_COLORS


class PriorAppearance(NamedTuple):
    """A pane's tab-colour state before cupline first touched it.

    Both halves matter. Painting overwrites the stored colour *and* switches the
    ``use_`` flags on, so putting a hand-set colour back needs all six values,
    not just the flags.
    """

    use: bool
    use_dark: bool
    use_light: bool
    color: object = None
    color_dark: object = None
    color_light: object = None


#: What a pane whose profile could not be read is assumed to have had: no tab
#: colour. That is the old unconditional behaviour, kept as the fallback because
#: it is the common case — most panes have no manual colour — and because an
#: unreadable profile is no basis for claiming otherwise.
PRIOR_OFF = PriorAppearance(False, False, False)

#: Display P3 -> linear sRGB, both D65. iTerm2 hands colours back in P3 even
#: when they were pushed as sRGB (RESEARCH.md: an sRGB red read back as
#: ``(255,0,0,P3)``), so a colour of cupline's own is only recognisable after
#: converting it back.
_P3_TO_SRGB = (
    (1.2249401, -0.2249404, 0.0),
    (-0.0420569, 1.0420571, 0.0),
    (-0.0196376, -0.0786361, 1.0982735),
)

#: Per-channel slack when deciding a colour is one of ``STATE_COLORS``. The
#: round trip through iTerm2 lands within a couple of counts of the value
#: pushed (observed: amber ``(230,150,30)`` came back as P3 ``(219,154,61)``,
#: which converts to ``(230,150,28)`` with the matrix above). The palette's
#: entries are far further apart than this, and a hand-set colour inside the
#: band is indistinguishable from cupline's on screen anyway.
_COLOR_TOLERANCE = 6


def _decode(component: float) -> float:
    value = component / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _encode(linear: float) -> int:
    linear = min(1.0, max(0.0, linear))
    value = (
        12.92 * linear if linear <= 0.0031308 else 1.055 * linear ** (1 / 2.4) - 0.055
    )
    return round(value * 255)


def _to_srgb(color) -> Optional[tuple[int, int, int]]:
    """A profile colour's 8-bit sRGB components, or None if it has none."""
    try:
        rgb = (float(color.red), float(color.green), float(color.blue))
    except AttributeError, TypeError, ValueError:
        return None
    if getattr(color, "color_space", None) is iterm2.ColorSpace.P3:
        linear = tuple(_decode(c) for c in rgb)
        return tuple(
            _encode(sum(row[i] * linear[i] for i in range(3))) for row in _P3_TO_SRGB
        )
    return tuple(round(c) for c in rgb)


def carries_state_colour(prior: PriorAppearance) -> bool:
    """True if any *enabled* slot holds a colour from cupline's own palette.

    Each slot is judged on its own: the stranded pane found live had only the
    dark slot switched on, which ``profile_for`` never writes, so requiring
    all three would have missed the one real example there is.
    """
    slots = (
        (prior.use, prior.color),
        (prior.use_dark, prior.color_dark),
        (prior.use_light, prior.color_light),
    )
    for enabled, color in slots:
        if not enabled:
            continue
        rgb = _to_srgb(color)
        if rgb is None:
            continue
        for ours in STATE_COLORS.values():
            if all(abs(a - b) <= _COLOR_TOLERANCE for a, b in zip(rgb, ours)):
                return True
    return False


def restore_profile(prior: PriorAppearance) -> iterm2.LocalWriteOnlyProfile:
    """Put back exactly what a pane had before cupline first painted it."""
    profile = iterm2.LocalWriteOnlyProfile()
    if prior.color is not None:
        profile.set_tab_color(prior.color)
    if prior.color_dark is not None:
        profile.set_tab_color_dark(prior.color_dark)
    if prior.color_light is not None:
        profile.set_tab_color_light(prior.color_light)
    profile.set_use_tab_color(prior.use)
    profile.set_use_tab_color_dark(prior.use_dark)
    profile.set_use_tab_color_light(prior.use_light)
    return profile


def profile_for(color: Optional[tuple[int, int, int]]) -> iterm2.LocalWriteOnlyProfile:
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
