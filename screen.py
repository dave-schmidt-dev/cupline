"""Screen text extraction, normalisation, and hashing.

Three separate representations come out of here and they must not be confused:

* the **tail** — real terminal text, handed to the classifier;
* the **normalised** form — spinner frames, elapsed counters *and plain numbers*
  flattened, used only to decide whether anything meaningful changed;
* the **acknowledgement** form — the same minus the number flattening, used only
  to decide whether a screen is the one the user already read.

Hashing the raw text would make every spinner frame a "change" and the debounce
would never settle. Hashing the fully normalised text for the second question
makes two different screens look identical, which loses an alert; see
:func:`normalize_for_ack`.
"""

from __future__ import annotations

import hashlib
import re

from config import TAIL_LINES

#: iTerm2 returns unset cells as NUL rather than space. Left in place, these
#: corrupt every string comparison and leak into fixtures.
_NUL = "\x00"

#: Braille block (U+2800-U+28FF) covers the dot-spinners every agent in scope
#: uses. The rest are the common ASCII/box spinner frames.
_SPINNER_RE = re.compile(r"[⠀-⣿◐-◓▖-▟|/\\\-]")

#: Elapsed counters ("12s", "1.4s", "3m 20s") and token/cost readouts change on
#: their own with no state change behind them.
_ELAPSED_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|m|h)\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b\d[\d,._]*\b")
_WS_RE = re.compile(r"[ \t]+")


def lines_from_contents(contents) -> list[str]:
    """Extract visible screen lines from an iTerm2 ``ScreenContents``.

    ``ScreenContents`` covers the visible screen only. Scrollback is reachable,
    but through a different call — see :func:`fetch_scrollback`.
    """
    out = []
    for i in range(contents.number_of_lines):
        out.append(contents.line(i).string.replace(_NUL, " ").rstrip())
    return out


def tail_text(lines: list[str], count: int = TAIL_LINES) -> str:
    """Bottom ``count`` non-trailing-blank lines, as one string."""
    trimmed = list(lines)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    return "\n".join(trimmed[-count:])


def normalize(text: str) -> str:
    """Flatten animation noise so a redraw is not mistaken for progress."""
    text = text.replace(_NUL, " ")
    text = _SPINNER_RE.sub("", text)
    text = _ELAPSED_RE.sub("<t>", text)
    text = _NUMBER_RE.sub("<n>", text)
    text = _WS_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def screen_hash(text: str) -> str:
    """Stable hash of the normalised text."""
    return hashlib.sha256(normalize(text).encode("utf-8", "replace")).hexdigest()[:16]


def normalize_for_ack(text: str) -> str:
    """Like :func:`normalize`, but keeps plain numbers.

    Two questions look alike and are not. "Has this screen *moved*?" wants every
    self-changing readout flattened, digits included — a token counter ticking
    up is not progress, and leaving it in meant a settled pane never settled.
    "Is this the same screen the user already read?" wants the opposite: a
    number is content, and ``Done. 3 tests failed.`` is not the screen that says
    ``Done. 5 tests failed.``

    Sharing one hash between the two collapsed that distinction, so a stop whose
    only difference from an acknowledged screen was a digit went unreported —
    the failure this project rates worst. Spinner frames and elapsed counters
    are still flattened here, and that half is required rather than inherited:
    the acknowledged screen is captured while the pane is still animating, and
    the stop it must match arrives after the animation has moved on.
    """
    text = text.replace(_NUL, " ")
    text = _SPINNER_RE.sub("", text)
    text = _ELAPSED_RE.sub("<t>", text)
    text = _WS_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def ack_hash(text: str) -> str:
    """Stable hash for "the user has already seen this screen". See above."""
    return hashlib.sha256(
        normalize_for_ack(text).encode("utf-8", "replace")
    ).hexdigest()[:16]


#: Coarse redaction for captured fixtures. An agent's terminal shows the user's
#: real work — absolute paths, repository URLs, client names — and a fixture is
#: a file that outlives the session it came from.
_HOME_RE = re.compile(r"/(?:Users|home)/[^\s/]+")
_URL_RE = re.compile(r"https?://\S+")
#: Long unbroken alphanumeric runs: the shape of a key, not of English.
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{28,}\b")


def redact(text: str) -> str:
    """Blunt the obvious identifiers in a captured tail.

    Deliberately coarse and deliberately not trusted: it cannot know that
    "atlas" is a client name, so captured fixtures still need human review
    before they leave the machine. It removes the mechanical leaks — home paths,
    URLs, key-shaped strings — so that review is about judgement, not grep.
    """
    text = _URL_RE.sub("<url>", text)
    text = _HOME_RE.sub("~", text)
    return _LONG_TOKEN_RE.sub("<redacted>", text)


async def fetch_scrollback(session, lines_above: int = 40) -> list[str]:
    """Read ``lines_above`` lines from above the visible screen.

    Not used by the main loop — the visible screen is what carries an agent's
    current state — but verified working and kept for the classifier, which may
    want context when a full-screen TUI leaves nothing useful on screen.

    Line numbers here are **absolute** and keep counting as content scrolls off,
    so the base is ``overflow + scrollback_buffer_height``, not 0. Measured on a
    live session: overflow 11051, scrollback 1000, visible 73.
    """
    info = await session.async_get_line_info()
    base = info.overflow + info.scrollback_buffer_height
    start = max(info.overflow, base - lines_above)
    contents = await session.async_get_contents(start, base - start)
    return [line.string.replace(_NUL, " ").rstrip() for line in contents]
