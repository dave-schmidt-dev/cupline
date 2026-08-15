"""Screen normalisation and hashing.

These are the tests that actually protect the debounce: if normalisation stops
flattening spinner frames, every redraw becomes a classification and the tab
flickers. No iTerm2 required.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen import (  # noqa: E402
    lines_from_contents,
    normalize,
    redact,
    screen_hash,
    tail_text,
)


class FakeLine:
    def __init__(self, string):
        self.string = string


class FakeContents:
    """Minimal stand-in for iterm2.screen.ScreenContents."""

    def __init__(self, lines):
        self._lines = [FakeLine(text) for text in lines]

    @property
    def number_of_lines(self):
        return len(self._lines)

    def line(self, index):
        return self._lines[index]


def test_nul_padding_becomes_spaces():
    """iTerm2 returns unset cells as NUL, not space. Verified on 3.6.11."""
    contents = FakeContents(["a\x00b\x00c", "plain"])
    assert lines_from_contents(contents) == ["a b c", "plain"]


def test_trailing_whitespace_stripped_per_line():
    contents = FakeContents(["text   ", "  indented  "])
    assert lines_from_contents(contents) == ["text", "  indented"]


def test_spinner_frames_hash_identically():
    """The core debounce guarantee."""
    frames = [
        "⠋ Working on it",
        "⠙ Working on it",
        "⠹ Working on it",
        "⣿ Working on it",
    ]
    hashes = {screen_hash(frame) for frame in frames}
    assert len(hashes) == 1


def test_elapsed_counter_does_not_count_as_change():
    assert screen_hash("Thinking (3s)") == screen_hash("Thinking (41s)")
    assert screen_hash("Thinking (1.4s)") == screen_hash("Thinking (2m)")


def test_token_counts_do_not_count_as_change():
    assert screen_hash("· 1,204 tokens") == screen_hash("· 98,551 tokens")


def test_real_content_change_is_detected():
    """Normalisation must not be so aggressive that it hides real progress."""
    assert screen_hash("Do you want to proceed?") != screen_hash("Applying edit to foo.py")


def test_prompt_appearing_changes_hash():
    working = "⠋ Editing file\n  running tests"
    waiting = "⠋ Editing file\n  running tests\n› "
    assert screen_hash(working) != screen_hash(waiting)


def test_tail_drops_trailing_blank_lines():
    lines = ["one", "two", "", "   ", ""]
    assert tail_text(lines) == "one\ntwo"


def test_tail_respects_count():
    lines = [str(n) for n in range(100)]
    assert tail_text(lines, 5) == "95\n96\n97\n98\n99"


def test_tail_on_empty_screen():
    assert tail_text(["", "  ", ""]) == ""


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_normalize_blank_is_empty(text):
    assert normalize(text) == ""


# -- redaction ------------------------------------------------------------
# Fixtures outlive the session they came from, so what lands in one is a
# security question, not a formatting one.

def test_redact_removes_home_paths():
    out = redact("cd /Users/dave/Documents/Projects/atlas && make")
    assert "/Users/dave" not in out
    assert out == "cd ~/Documents/Projects/atlas && make"


def test_redact_removes_urls():
    out = redact("repo https://github.com/acme-llc/private-thing | 1 unstaged")
    assert "github.com" not in out
    assert "<url>" in out


def test_redact_removes_key_shaped_strings():
    """The sample deliberately avoids any real vendor's key prefix.

    An earlier version used a synthetic ``sk_live_…`` string. It was fake, but
    GitHub's push protection cannot know that and blocked the push as a leaked
    Stripe key — correctly, since a scanner that trusted "it's only a test" would
    be useless. What is under test is the *shape* rule, so the fixture only needs
    to be long and high-entropy, not to impersonate a provider.
    """
    fake = "TOKEN=" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    out = redact(f"export {fake}")
    assert "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6" not in out
    assert "<redacted>" in out


def test_redact_keeps_ordinary_prose_intact():
    """Over-redaction would destroy the signal the corpus exists to capture."""
    text = "Do you want to proceed? (y/n)\n> 1. Yes  2. No"
    assert redact(text) == text


def test_redact_does_not_eat_short_identifiers():
    assert redact("run make test-all") == "run make test-all"
