"""Agent identification via process ancestry.

The chains below are real, captured from a live run — see RESEARCH.md. They are
the reason identification does not use iTerm2's ``jobName`` directly.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessions  # noqa: E402


def install_table(monkeypatch, table):
    """Seed the process-table cache so no real `ps` runs."""
    monkeypatch.setattr(sessions, "_PS_CACHE", table, raising=False)
    monkeypatch.setattr(sessions, "_PS_CACHE_AT", time.monotonic(), raising=False)


# Real chain: node(playwright-mcp) -> npm exec -> claude -> -zsh -> login
CLAUDE_CHAIN = {
    93140: (92980, "node"),
    92980: (92858, "npm exec @playwright/mcp@latest"),
    92858: (61355, "claude"),
    61355: (61354, "-zsh"),
    61354: (83829, "/usr/bin/login"),
}

# Real chain: the Codex foreground job is a helper, not `codex` itself.
CODEX_CHAIN = {
    77201: (92678, "SkyComputerUseClient"),
    92678: (92677, "codex"),
    92677: (83829, "-zsh"),
}

# Real chain: an ordinary long-running program that is not an agent.
NON_AGENT_CHAIN = {
    83662: (15667, "/Users/dave/Documents/Projects/ridge/.venv/bin/python3"),
    15667: (15666, "-zsh"),
    15666: (83829, "/usr/bin/login"),
}

# Real chain, captured live: a shell the agent spawned to run a command. The
# nested `zsh` has no leading dash; the session's own shell does.
NESTED_SHELL_CHAIN = {
    41001: (41000, "zsh"),
    41000: (40999, "claude"),
    40999: (40998, "-zsh"),
    40998: (40997, "/usr/bin/login"),
    40997: (40996, "iTermServer-3.6.11"),
    40996: (1, "iTerm2"),
}


def test_node_foreground_job_resolves_to_claude(monkeypatch):
    """Claude Code, OpenCode and Cursor Agent all report a bare `node`."""
    install_table(monkeypatch, CLAUDE_CHAIN)
    assert sessions.resolve_agent(93140) == "claude"


def test_helper_subprocess_resolves_to_codex(monkeypatch):
    """jobName was observed flipping between `codex` and `SkyComputerUseCl`."""
    install_table(monkeypatch, CODEX_CHAIN)
    assert sessions.resolve_agent(77201) == "codex"


def test_agent_named_directly_is_found(monkeypatch):
    install_table(monkeypatch, CODEX_CHAIN)
    assert sessions.resolve_agent(92678) == "codex"


def test_non_agent_program_is_not_claimed(monkeypatch):
    """A false positive here paints a tab the user never asked to watch."""
    install_table(monkeypatch, NON_AGENT_CHAIN)
    assert sessions.resolve_agent(83662) is None


def test_walk_stops_at_the_login_shell(monkeypatch):
    """An agent above the session's login shell belongs to a different session."""
    table = {
        500: (400, "python3"),
        400: (300, "-zsh"),
        300: (200, "claude"),   # must not be reached
    }
    install_table(monkeypatch, table)
    assert sessions.resolve_agent(500) is None


def test_walk_passes_through_a_shell_the_agent_spawned(monkeypatch):
    """The bug: stopping at *any* shell hid an agent while it ran commands.

    Coding agents shell out constantly, so this was not an edge case — a session
    dropped off the watch list for the whole duration of every command it ran.
    Captured live as `zsh -> claude -> -zsh -> login -> iTermServer -> iTerm2`.
    """
    install_table(monkeypatch, NESTED_SHELL_CHAIN)
    assert sessions.resolve_agent(41001) == "claude"


def test_iterm_plumbing_ends_the_walk(monkeypatch):
    """iTermServer carries a version suffix, so it is matched by prefix."""
    install_table(monkeypatch, NESTED_SHELL_CHAIN)
    assert sessions.resolve_agent(40997) is None


def test_a_nested_shell_alone_is_not_an_agent(monkeypatch):
    """Passing through shells must not turn a plain nested shell into a hit."""
    table = {
        900: (899, "zsh"),      # user ran `zsh` inside their own shell
        899: (898, "-zsh"),
        898: (1, "/usr/bin/login"),
    }
    install_table(monkeypatch, table)
    assert sessions.resolve_agent(900) is None


def test_login_shell_detection():
    assert sessions._is_login_shell("-zsh") is True
    assert sessions._is_login_shell("-bash") is True
    assert sessions._is_login_shell("zsh") is False
    assert sessions._is_login_shell("/bin/zsh") is False
    assert sessions._is_login_shell("claude") is False


def test_absolute_paths_are_matched_by_basename(monkeypatch):
    install_table(monkeypatch, {700: (600, "/opt/homebrew/bin/opencode"), 600: (1, "-zsh")})
    assert sessions.resolve_agent(700) == "opencode"


# The three agents below were not running during the spike. Their launch shapes
# were established empirically instead (see RESEARCH.md): `ps comm` records the
# path passed to execve, honours `exec -a` renaming, and does not resolve
# symlinks. These tables encode what each launcher therefore reports.

def test_agy_resolves_from_its_binary(monkeypatch):
    """A plain Mach-O binary on PATH — reported under its own name."""
    install_table(monkeypatch, {
        800: (799, "/Users/dave/.local/bin/agy"),
        799: (1, "-zsh"),
    })
    assert sessions.resolve_agent(800) == "agy"


def test_cursor_agent_resolves_despite_being_a_node_script(monkeypatch):
    """cursor-agent is a bash launcher doing `exec -a "$0" node index.js`.

    `exec -a` rewrites argv[0], and macOS `ps comm` honours it — verified with a
    live `exec -a` probe — so this reports as cursor-agent, not node.
    """
    install_table(monkeypatch, {
        810: (809, "/Users/dave/.local/bin/cursor-agent"),
        809: (1, "-zsh"),
    })
    assert sessions.resolve_agent(810) == "cursor-agent"


def test_opencode_exe_suffix_is_stripped(monkeypatch):
    """opencode's real binary is `opencode.exe` behind a symlink chain."""
    install_table(monkeypatch, {
        820: (819, "/opt/homebrew/Cellar/opencode/1.18.15/libexec/bin/opencode.exe"),
        819: (1, "-zsh"),
    })
    assert sessions.resolve_agent(820) == "opencode"


def test_missing_pid_is_none(monkeypatch):
    install_table(monkeypatch, CLAUDE_CHAIN)
    assert sessions.resolve_agent(None) is None
    assert sessions.resolve_agent(0) is None
    assert sessions.resolve_agent(999999) is None


def test_depth_cap_terminates_on_a_cycle(monkeypatch):
    """A malformed table must not hang the sweeper."""
    install_table(monkeypatch, {10: (11, "a"), 11: (10, "b")})
    assert sessions.resolve_agent(10) is None


def test_basename_strips_leading_dash():
    assert sessions._basename("-zsh") == "zsh"
    assert sessions._basename("/usr/bin/login") == "login"
