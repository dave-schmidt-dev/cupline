# cupline

A universal terminal-attention monitor that highlights iTerm2 tabs when an AI
coding agent stops working — whatever the reason.

**Status:** working. The stop/working signal is measured and verified live on
iTerm2 3.6.11 across Claude Code and Codex.

## Priorities (in order)

1. **Universality at the terminal layer.** One mechanism for Claude Code, Codex, OpenCode, AGY, and Cursor Agent. No per-harness hooks, no modifications to any agent.
2. **Signal fidelity.** A wrong amber is worse than no amber — but a *missed* one is worse still, since a silent agent is the whole reason this exists. Decide from measured redraw activity; abstain only when the screen genuinely cannot be read.
3. **A replaceable classifier.** `classify(snapshot) -> AgentState` is the only seam the future rules/LLM work may touch.
4. **Low overhead at 4–10 concurrent sessions.** Event-driven where iTerm2 offers it; one debounce sweeper, not N timers.

## Layout

| Path | Purpose |
|---|---|
| `README.md` | This file. Setup and run instructions. |
| `RESEARCH.md` | What was verified against iTerm2 3.6.11, and what failed. |
| `cupline.py` | Entry point. Wires monitors, sweeper, and paint loop. |
| `models.py` | `AgentState`, `TerminalSnapshot`, `SessionState`. No iTerm2 imports. |
| `sessions.py` | Session discovery, create/terminate monitors, agent identification. |
| `screen.py` | Screen streaming, text normalization, hashing, tail extraction. |
| `classifier.py` | `classify(snapshot) -> AgentState`. Redraw timing decides; text only refines. |
| `tab_state.py` | Per-tab aggregation and tab-color paint/restore. |
| `config.py` | Debounce interval, colors, agent command patterns. |
| `tests/` | pytest suite (no iTerm2 required). |
| `launchd/*.plist.template` | launchd plist for automatic startup. See "Run it automatically" below. |
| `requirements.txt` | Pinned dependencies. |
| `LICENSE` | MIT. |
| `fixtures/` | *(local only)* Captured terminal tails with expected labels. Real screens, real content; not committed. |
| `HISTORY.md` | *(local only)* Meaningful changes, bugs, remediation, regression notes. |
| `TASKS.md` | *(local only)* Per-project task tracking. |

## What you see

| State | Colour | Tab title | Meaning |
|---|---|---|---|
| `WORKING` | default | iTerm2's automatic name | The terminal is still repainting; the agent is doing something. |
| `WAITING` | amber | `<project> · your turn` | **Stopped.** Finished, errored, rate-limited, hung — the reason is not claimed. |
| `ACTION` | red | `<project> · needs you` | Stopped, *and* an input control is on screen, so it needs a decision. |
| `UNKNOWN` | holds previous, then releases | holds previous | Stopped, but the screen is unreadable. Rare. |

### Split panes

Colour is applied per pane, so in a tab of three agents only the ones that
actually stopped go amber. The **active** pane is the exception: iTerm2 draws a
tab's entry in the tab bar from its active session alone, so that pane carries
the tab's overall state to keep the alert visible whichever pane has focus. In
practice this means the pane you are looking at may show the tab's colour rather
than its own — every *other* pane is telling you about itself.

The tab title still names one project, with `+N` for how many others reached the
same state, since a tab has only one title.

### How it decides

One rule does the work, and it reads no text at all: **an agent that is doing
anything repaints its terminal constantly** — a spinner frame, an elapsed
counter, streaming output. One that has stopped repaints nothing. Measured
across 9 live sessions over 150 s through the same screen streamer the sweeper
uses, working panes were never silent longer than **0.7 s** and stopped panes
emitted **zero** events. There is no overlap between the two, so the threshold
(`IDLE_AFTER_SECONDS`, 5 s) is a margin rather than a tuning compromise.

The rule has one precondition, and cupline checks it rather than assuming it:
the redraw clock is only trustworthy while something is feeding it. If a pane's
screen streamer dies, that clock freezes, and a frozen clock crosses the
threshold on its own — reporting every agent behind it as stopped, forever.
cupline therefore abstains on a pane whose streamer is not known to be healthy
instead of reading silence as a stop, logs the failure at WARNING, and backs off
its retries. The cost is deliberate and in the worse direction: an agent that
genuinely stops behind a dead streamer holds its colour rather than going amber.
That is only defensible because the failure is now loud.

That is what makes this universal in the way the project requires: it reads
redraw timing, not language, so it cannot accidentally learn one harness's
phrasing. Codex and Claude Code were measured together and behave identically on
it.

Text is consulted *only after* an agent is known to have stopped, and only to
decide how loudly to say so — amber for "wants an instruction", red for "wants a
decision". Getting that refinement wrong costs you a colour, not the signal.

A consequence worth stating plainly: an agent frozen mid-turn still displays
"esc to interrupt", and cupline reports it as stopped anyway. The screen is the
evidence; leftover words claiming otherwise are not.

Each session gets one `IDLE_AFTER_SECONDS` grace period from the moment cupline
first sees it, because silence that was not watched for is not evidence of
silence. In practice a session that was already idle at startup is reported one
threshold later.

`<project>` is the basename of the session's working directory. Titles are only
set for states that need a human — clearing the title hands the tab back to
iTerm2's automatic name, which is normally the agent's current task and more
useful than a static label. A tab whose title you set by hand is left alone.

**Colour is preserved the same way.** A pane's existing tab colour is read once,
immediately before cupline first paints it, and put back when the pane is
cleared or on shutdown — the stored colour as well as the `use_tab_color` flags,
since painting overwrites both. A pane cupline has never painted is not written
to at all, so an unwatched split sharing a tab with an agent keeps whatever you
gave it.

Two exceptions, both deliberate. The **active** pane carries its tab's aggregate
state so the tab bar stays correct whichever pane has focus, so it is painted
even when it is not an agent. And `--reset` is unconditional: it runs in a fresh
process that painted nothing and so has nothing to put back.

An `UNKNOWN` reading holds the tab's colour rather than clearing it, so the tab
does not flicker every time an agent's screen becomes momentarily unreadable.
The hold is not indefinite: it lasts only while the screen that produced the
colour is still on screen. Once the content moves on, the colour goes, because
a red that has outlived its prompt is worse than no red.

When several agents share a tab, the tab shows the most urgent one
(`ACTION > WAITING > WORKING`) and the title names that pane's project plus a
count of the others in the same state — `atlas +1 · your turn`. One tab has
one colour and one title, so naming a single project would under-report: three
panes per tab, all on auto, means agents finishing together is the normal case.
The count is deliberately not a list, because tab titles are narrow and "there
is more than this one" is the part you cannot work out for yourself.

**Known false positive, red only:** an agent that *displays* terminal content —
a log excerpt, a transcript, docs describing a prompt — can render something
shaped like an input control and be called `ACTION` rather than `WAITING`. It
costs the colour, not the alert: the agent has genuinely stopped either way. See
`RESEARCH.md` finding 14.

**Every signal here is drawn inside the iTerm2 window.** On another Space,
another display, or behind another app, none of it is visible. Future work
may add out-of-window signalling via macOS notifications or similar.

## Setup

Requires macOS, iTerm2 3.6.11, and the iTerm2 Python API enabled
(**Preferences → General → Magic → Enable Python API**).

Create a venv and install dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Verify the install:

```bash
.venv/bin/python -c "import iterm2; print(iterm2.__version__)"   # expect 2.20
```

To run tests, pytest is included in `requirements.txt`.

## Workflows

### Run the monitor

```bash
.venv/bin/python cupline.py
```

Prints one line per state change to stdout. The rotating file log
(`.logs/cupline.log`) records WARNING and above by default; `--debug` drops it to
DEBUG, which is what you want when inspecting session targeting. `--tail` dumps
the captured terminal text for every change.

### Prove the tab states without a classifier

`--demo` cycles a chosen session through `WAITING` → `ACTION` → `WORKING`,
pausing between each so the tab chrome can be observed:

```bash
.venv/bin/python cupline.py --demo
```

Or drive one session directly:

```bash
.venv/bin/python cupline.py --list
.venv/bin/python cupline.py --set <session-id>=action
```

### Run it automatically, tied to iTerm2

A launchd agent keeps cupline running whenever iTerm2 is. It needs no polling of
its own: cupline exits when the iTerm2 API socket goes away, so `KeepAlive` plus
a 15 s `ThrottleInterval` means the process fails fast while iTerm2 is closed and
is back within seconds of it reopening.

launchd requires absolute paths, so the repo ships
`launchd/com.zerodelta.cupline.plist.template` and you generate the real plist
for your machine. The generated file is gitignored.

```bash
# The interpreter that has iterm2 installed. This is the repo venv if you
# followed Setup — override it if yours lives elsewhere, e.g. a shared
# ~/.venvs/iterm2. Nothing downstream assumes the venv is inside the repo.
PYTHON="$PWD/.venv/bin/python"

sed -e "s#__CUPLINE_DIR__#$PWD#g" \
    -e "s#__PYTHON__#$PYTHON#" \
    launchd/com.zerodelta.cupline.plist.template \
    > launchd/com.zerodelta.cupline.plist
```

Check the generated plist before handing it to launchd. A plist naming a
path that does not exist still bootstraps cleanly and then fails on every
launch, which surfaces as a service that is present but never running:

```bash
plutil -lint launchd/com.zerodelta.cupline.plist          # expect: OK
"$PYTHON" -c "import iterm2"                              # expect: no output
grep -c '__' launchd/com.zerodelta.cupline.plist          # expect: 0
```

Then install and start it:

```bash
ln -sfn "$PWD/launchd/com.zerodelta.cupline.plist" ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.zerodelta.cupline.plist
launchctl print gui/$(id -u)/com.zerodelta.cupline | grep -E "state|pid"
```

`state = running` with a pid is the only success signal. A plist naming an
interpreter that does not exist reports `state = spawn scheduled`, no pid, and
`last exit code = 78: EX_CONFIG` — verified by bootstrapping one deliberately.
For any other startup failure the process runs and then dies, so read
`.logs/cupline-agent.log`, which is where launchd sends stdout and stderr.

To stop it, and to stop it coming back:

```bash
launchctl bootout gui/$(id -u)/com.zerodelta.cupline
```

Output goes to `.logs/cupline-agent.log`. The plist lives in the project and is
symlinked into `~/Library/LaunchAgents`, so the project stays self-contained.

**Why not iTerm2's AutoLaunch folder?** It is the more native mechanism, but
`$HOME/Library/Application Support/iTerm2/Scripts/AutoLaunch` scripts run under
iTerm2's *managed* Python runtime (`iterm2env`), which is a separate install from
the `.venv` this project uses, and adopting it needs an iTerm2 restart
to take effect. launchd achieves the same lifetime tie with neither.

### Clear all paint

If cupline is killed while tabs are colored, the color persists — it lives in
iTerm2's profile state, not in the process. Reset every session iTerm2 knows
about:

```bash
.venv/bin/python cupline.py --reset
```

`--reset` is a recovery command and is deliberately unconditional: it also
returns every tab to its automatic title, including tabs you named by hand.

### Capture fixtures

```bash
.venv/bin/python cupline.py --capture <session-id> --label waiting
```

Writes `fixtures/<agent>-<label>-NN.txt`, passing the tail through a coarse
redaction of home paths, URLs, and key-shaped strings.

Capture watches the pane for `IDLE_AFTER_SECONDS` first and records a
`# redrawing:` header, because the classifier decides on redraw timing and a tail
saved without it is a fixture for half the input. Older fixtures are marked
`unmeasured` and the replay test skips the cases they cannot honestly exercise.

That redaction is a floor, not a guarantee: it cannot know that a project or
client name is sensitive. An agent's screen is the user's real work, and a
fixture outlives the session it came from — so the `fixtures/` directory is
gitignored and the corpus stays local. **Review by hand before sharing any fixture outside this machine.**

### Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

## Conventions

- All files self-contained under this directory.
- Work is tracked in `TASKS.md`, not GitHub Issues, despite the repo being public. This is a deliberate exception to the usual "Issues once a repo exists" rule — `TASKS.md` is gitignored here, so the queue stays local and open defects are not published alongside the code.
- Secrets in BWS. Never committed. Captured fixtures are reviewed by hand before they land.
- Update `HISTORY.md` alongside every meaningful change. Bug entries follow the format: `- [bug] <description> | files: path/a.py, path/b.ts`.
- Tests verify real behavior — no smoke-only "did it run" checks.
- `models.py` and `classifier.py` must not import `iterm2`. That boundary is what makes the classifier swappable and the tests runnable without a terminal.
- Classification never branches on which agent produced the text — redraw timing and text shape are harness-agnostic.
