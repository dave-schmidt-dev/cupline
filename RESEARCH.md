# iTerm2 3.6.11 API research

Everything below was verified by running against the live iTerm2 on this machine
on 2026-08-14, not read from documentation. Where a probe contradicted an
assumption, the probe wins and the assumption is recorded as wrong.

**Environment**

| | |
|---|---|
| iTerm2 | 3.6.11 (stable) |
| Python client | `iterm2==2.20`, `protobuf==7.35.1`, `websockets==16.0` |
| Interpreter | Python 3.14.7 in `~/.venvs/iterm2` |
| Live load during probes | 10 sessions / 3 tabs / 1 window; 9 resolved as agents (8 × Claude Code, 1 × Codex) |

The API server was already enabled (`defaults read com.googlecode.iterm2
EnableAPIServer` → 1). A script run from outside `~/Library/Application
Support/iTerm2/Scripts` connected with **no authentication prompt and no
`ITERM2_COOKIE`**, so cupline does not need to live in iTerm2's Scripts folder.

## Spike goals — status

| # | Goal | Result | Mechanism |
|---|---|---|---|
| 1 | Discover all existing sessions | **verified** | `app.windows → window.tabs → tab.sessions` |
| 2 | Detect sessions created after startup | **verified** | `iterm2.NewSessionMonitor` (+ `SessionTerminationMonitor`) — exercised live, see below |
| 3 | Associate session ↔ tab/window ↔ process | **verified** | `session.session_id`, `tab.tab_id`, `window.window_id`, `jobName`/`jobPid` variables |
| 4 | Observe screen changes for many sessions concurrently | **verified** | one `get_screen_streamer()` task per session; 2 concurrent streamers measured at 520 and 161 events / 20 s |
| 5 | Retrieve recent terminal text | **verified** | `async_get_screen_contents()` for the visible screen; `async_get_contents()` + `async_get_line_info()` for scrollback |
| 6 | Debounce noisy updates | **verified** | normalise → hash → compare; floor + ceiling debounce (see "Debounce" below) |
| 7 | Highlight the correct tab | **verified** | `async_set_profile_properties()` with tab-colour keys |
| 8 | Restore default appearance | **verified** | same call with `use_tab_color = False` |

## Verified mechanisms

### Tab highlighting — `async_set_profile_properties`

The working mechanism. Build an `iterm2.LocalWriteOnlyProfile`, set the tab
colour keys, push it to a **session**:

```python
profile = iterm2.LocalWriteOnlyProfile()
profile.set_use_tab_color(True)
profile.set_tab_color(iterm2.Color(230, 150, 30))
await session.async_set_profile_properties(profile)
```

Restore by pushing `set_use_tab_color(False)`. The stored colour value survives;
the `use_` flag is what controls display.

Visually confirmed by screenshot at each step: amber outline + tint for
`WAITING`, red for `ACTION`, default chrome after restore.

### Tab title — `tab.async_set_title`

A tab-level call, unlike colour which is per-session. Setting `""` restores
iTerm2's automatic name; verified by screenshot that the agent's task name
("Review codebase consistency before TestFlight release (node)") comes back
after a reset.

Used to add words to the colour: `beacon · your turn` on amber,
`beacon · needs you` on red. The project name is the basename of the session's
`path` variable, re-read each sweep because agents change directory mid-session.

Only states that need a human get a title. Two consequences worth keeping:
clearing returns the tab to iTerm2's automatic name, which is usually more
informative than a static label; and a title this process never set is left
alone, so a manually-named tab is not wiped on the first sweep.

### Session lifecycle — `NewSessionMonitor` / `SessionTerminationMonitor`

Exercised live rather than introspected: with the monitor running, a second
process created a tab and closed it. Both events fired.

```
16:40:18.775  session created: A017146F-…
16:40:18.788  new session A017146F/ttys012 agent=None job=login   ← correctly not painted
16:40:35.771  session terminated: A017146F-…
```

Two observations. The creation event arrived within ~13 ms, but the termination
event lagged the `async_close()` call by roughly 5 s (single measurement, not a
characterised latency). And a plain shell tab resolved to `agent=None`, so it was
never painted — the gating works on a session that genuinely was not an agent.

Because that lag exists, and because a dropped event is silent, the monitors are
treated as an **optimisation rather than the source of truth**: the sweeper
re-runs `discover()` periodically, which both picks up sessions whose creation
event was missed or arrived before `app.windows` reflected them, and prunes ones
whose termination event never came.

> **Creating a tab from the API steals focus.** The probe above captured
> keystrokes the human was typing at the time, and discarded them when it closed
> the tab. Do not create sessions on a machine someone is using without saying so
> first.

### Screen streaming — `get_screen_streamer(want_contents=False)`

`want_contents=False` yields change notifications with no payload. The main loop
uses this and fetches contents separately, so screen reads are bounded by the
debounce interval instead of by how fast agents redraw. Measured cost of the
naive alternative: the Codex session alone emitted **520 screen events in 20
seconds** (~26/s), each of which would otherwise have been a full screen fetch.

### Scrollback — `async_get_line_info` + `async_get_contents`

Scrollback **is** available, contrary to the initial assumption that only the
visible screen could be read. Line numbers are **absolute** and keep increasing
as content scrolls off, so the base is `overflow + scrollback_buffer_height`,
not 0. Live reading: `overflow=11051, scrollback_buffer_height=1000,
mutable_area_height=73`. Implemented as `screen.fetch_scrollback()` and verified,
though the main loop does not need it.

### OSC 6 injection — works, but not needed

`session.async_inject(b"\033]6;1;bg;red;brightness;255\a")` succeeds and does
change the tab colour. Confirmed by reading the profile back afterwards:
`tab_color_dark` became `(255,0,0,P3)`.

Rejected as the primary mechanism because `async_set_profile_properties` is
direct, has an unambiguous restore, and does not write into the terminal's byte
stream at all.

> **Do not substitute `async_send_text` for `async_inject`.** `async_send_text`
> writes to the foreground program's *input*, which would type stray bytes into
> a live agent session. `async_inject` writes to the terminal's parser. Opposite
> effects, adjacent names.

## Findings that contradicted the starting assumptions

### 1. Tab colour follows the tab's *active* session

The first colour probe reported success and read back `use_tab_color = 1`, yet
the tab was visibly unchanged. The target was a background split pane. A tab
renders the colour of whichever session is active in it.

**Reading a property back is not evidence that anything is visible.** Every
colour claim here was checked by screenshot.

cupline's first response was to paint *every* session in the tab with the same
colour, so the tab is right regardless of which pane is focused, and no focus
tracking is needed. **Superseded by finding 17** — that made every pane in a
three-pane tab claim to want attention when one did. The finding itself stands;
only the response changed.

### 2. Sessions are not one-per-tab

The spec's model is one agent per tab. The actual live layout was 10 sessions in
3 tabs — 4, 3 and 3 split panes. One tab has one colour, so per-pane states must
collapse. cupline aggregates `ACTION > WAITING > WORKING > UNKNOWN`: the tab
surfaces the pane that most needs a human.

This is the sharpest limitation of tab colour as an output channel. With four
agents sharing a tab, the colour says "something in here needs you", not which.

### 3. `jobName` cannot identify the agent

Two independent failures:

* **False negatives.** Claude Code, OpenCode and Cursor Agent all report
  `jobName == "node"`.
* **Instability.** The Codex session reported `codex` on one poll and
  `SkyComputerUseCl` on another seconds later — `jobName` is the *foreground*
  job, which changes as the agent runs subprocesses.

**Working approach:** walk the process tree upward from `jobPid` until reaching
the session's shell, and match a known agent command anywhere in that chain.
Real chains captured live:

```
node (playwright-mcp) → npm exec → claude → -zsh → login    ⇒ claude
SkyComputerUseClient  → codex → -zsh                        ⇒ codex
ridge/.venv/bin/python3 → -zsh → login                     ⇒ (not an agent)
```

Stopping at the shell matters: without it, a nested shell would let one session
claim an agent belonging to another. One `ps -Ao pid=,ppid=,comm=` covers every
session, cached for 2 s.

### 4. Screen text uses NUL, not space, for unset cells

`LineContents.string` returns `\x00` where spaces are expected:
`'Opus\x005\x00(1M\x00context)'`. Left unhandled this corrupts every string
comparison and leaks into fixtures. Normalised in `screen.lines_from_contents`.

### 5. Profiles here use separate light/dark colours

`use_separate_colors_for_light_and_dark_mode` is `1` on this machine's Default
profile, so `set_tab_color()` alone is not reliably sufficient. cupline sets
the plain, `_dark` and `_light` variants together. (Consistent with OSC 6, which
was observed writing only the `_dark` variant while the system was in dark mode.)

### 6. Debounce needs a ceiling, not just a floor

A quiet-for-N-seconds debounce never fires for an agent showing a permanent
spinner — the session never goes quiet, so it is never classified. cupline
classifies when the screen has been quiet for `DEBOUNCE_SECONDS` **or** when it
has gone `MAX_CLASSIFY_INTERVAL_SECONDS` without a reading. The normalised hash
then decides whether anything actually changed.

This is what makes the spinner normalisation matter: braille frames
(U+2800–U+28FF), elapsed counters (`3s` → `41s`) and token counts are flattened
before hashing, so redraw churn is not mistaken for progress.

The same confusion had a second, quieter form: **one clock was doing two jobs.**
The streamer wrote redraw times into the field the content comparison also used,
so a spinning session reset its own stability counter ~26 times a second and
reported zero stable seconds forever — starving the exact temporal heuristic
that would have called it WAITING. The clocks are now separate:

| Field | Written by | Answers |
|---|---|---|
| `last_event_at` | the streamer, per frame | has the terminal redrawn? (debounce floor) |
| `last_change_at` | the content hash | has the *meaning* moved? |
| `stable_since` | the content hash | how long has it demonstrably held still? |

The snapshot exposes both `seconds_since_redraw` and `seconds_since_change`, and
their divergence is itself the signal: a pane redrawing constantly while its
normalised text sits still is an agent thinking behind a spinner.

### 7. The ancestry walk must pass *through* shells, not stop at them

The original walk stopped at the first shell it met, reasoning that the
session's shell is the boundary. Found during the soak: the Codex session
suddenly reported `agent=-` while running a command.

Reproduced directly, walking up from a shell this very session spawned:

```
zsh -> claude -> -zsh -> login -> iTermServer-3.6.11 -> iTerm2
       ^^^^^^ the agent, two hops up
resolve_agent() -> None
```

Coding agents shell out constantly, so this was not an edge case: **every agent
dropped off the watch list for the whole duration of every command it ran** —
precisely when it is working, and shortly before it needs a human.

The real boundary is the session's *login* shell. Unix passes argv[0] with a
leading dash for a login shell and `ps comm` preserves it, so the session's own
shell is `-zsh` while one the agent spawned is plain `zsh`. That one character
separates "this session's root" from "a subprocess". The walk now stops only at
a login shell or at iTerm2's own plumbing.

### 8. `ps comm` reports the invoked path, and honours `exec -a`

Three of the five configured agents were not running during the spike, so their
launch shapes were established from how `ps` behaves instead. Two probes,
both run live:

* A binary invoked through a symlink reports the **symlink path**, not the
  resolved target.
* `exec -a somename /bin/sleep` reports **`somename`** — argv[0] renaming is
  honoured.

That settles all three:

| Agent | Ships as | Reports as | Verified |
|---|---|---|---|
| `claude` | Mach-O binary | `claude` | live |
| `codex` | Mach-O binary | `codex` | live |
| `agy` | Mach-O binary | `agy` | by binary type |
| `cursor-agent` | bash script doing `exec -a "$0" node index.js` | `cursor-agent`, not `node` | by `exec -a` probe |
| `opencode` | symlink chain ending at `opencode.exe` | `opencode` via the symlink | by symlink probe |

Only `opencode` needed a code change: if its real binary is ever exec'd
directly the basename is `opencode.exe`, so a trailing `.exe` is stripped before
matching.

### 9. Per-pane signalling: badge works, pane name does not, neither is adopted

With ten panes in three tabs, one colour per tab cannot say *which* pane wants
attention. Both per-pane channels were probed.

**Badge** (`set_badge_text`) renders per-pane and is accepted. But every badge
*geometry* key is rejected on 3.6.11:

```
OK      badge_text
REJECT  badge_max_height / badge_max_width        REQUEST_MALFORMED
REJECT  badge_top_margin / badge_right_margin     REQUEST_MALFORMED
REJECT  badge_font                                REQUEST_MALFORMED
```

So size is whatever the user's profile says, and the default is enormous — the
probe painted "NEEDS YOU" in letters spanning half the pane, over the agent's
output.

**Pane name** (`async_set_name`) is overwritten by the agent within ~2 s:

```
t+0.2s  autoName='NEEDS-YOU-PROBE'
t+2.0s  autoName='⠐ Scaffold cupline iTerm2 terminal attention monitor'
```

Coding agents set their own terminal titles continuously. Any per-session name
cupline writes is transient by construction — unusable for exactly the programs
it targets.

**Decision: neither is adopted.** Tab colour plus tab title stays the channel,
and the tab title names the project of the pane that won aggregation, so a
shared tab says *which project* wants you even though it cannot say which pane.
The badge was rejected on a second ground that outweighs the sizing problem: it
draws *inside* the terminal, so it is invisible whenever the window is not on
screen — which is the situation the tool exists for.

That last point generalises to tab colour itself, and is recorded as an open
question: **no in-window signal works when the window is not visible.** A
different channel (system notification, Dock badge) is needed for that case, and
it is out of scope for this spike.

### 10. Classifying only on content change made every temporal input zero

The sweeper called `classify()` only when the normalised screen hash had moved.
That same reading pinned `last_change_at = now` and cleared `stable_since`, so
**every snapshot the classifier ever received reported
`seconds_since_change == 0.0` and `seconds_stable == 0.0`.** Two of the three
temporal fields were structurally unreachable — not wrong, uncomputable.

The second half was worse. `stable_since` is only set by a *second* reading that
finds the screen unchanged, and a settled session emits no screen events at all,
so `_should_read` never fired for it again. A session that finished its turn was
read exactly once and then never revisited, meaning stability could not begin to
accumulate during precisely the period WAITING is defined by.

Measured through the sweeper, before and after:

| Reading | Before | After |
|---|---|---|
| change lands | `0.0 / 0.0` | `0.0 / 0.0` |
| +3 s quiet | *never read* | `3.0 / 0.0` |
| +6 s quiet | *never read* | `6.0 / 3.0` |
| +9 s quiet | *never read* | `9.0 / 6.0` |

(`seconds_since_change / seconds_stable`.)

The fix is two lines of policy: re-read a quiet session every
`IDLE_RECHECK_SECONDS`, and classify on *every* reading rather than only on
changed ones. A reading where only the clock moved is new information, because
elapsed time is itself a classifier input.

Worth recording as a process point: the unit tests were green throughout,
because they called `snapshot()` directly at an arbitrary later time. That
proves the arithmetic and not that anything ever calls it at that time. The
tests added for this go through the sweeper for that reason.

### 11. A held state outlived its own evidence

Observed live, by the user, during the soak: a tab was red and labelled
`cinder · needs you` while the agent was not blocked on anything.

The log showed why. ACTION matched at 17:07:47 and the next reading at 17:07:54
was UNKNOWN — a 6.7-second window, almost certainly a real prompt that was
answered. But `UNKNOWN` holds the previous state to stop the tab flickering
mid-turn, and every subsequent reading was also UNKNOWN, so the red became
permanent. The tool was reporting a condition that had provably ended a minute
earlier.

Holding on UNKNOWN is still right; the flaw was holding *unconditionally*.
"This reading tells me nothing" is not the same claim as "the reason is still on
screen". `SessionState` now records `confident_hash` — the screen that justified
the last non-UNKNOWN state — and the hold is released once the content has
changed away from it. A session that has never been classified confidently has
nothing to go stale and still holds, which is the flicker case the hold was
built for.

This is the failure mode the priority list ranks worst, and neither the test
suite nor the spike's own review found it. It took someone looking at a tab.

### 12. Match the input control, not the question-shaped prose

Chasing finding 11 turned up its actual cause, which is a classifier rule rather
than a paint rule. The tail that painted the tab red contained:

> How do you want to finish this? …

`\bdo you want to\b` matched. But that is an agent **ending its turn with a
question** — WAITING under this project's definition — not one blocked at an
input control. The same rule would fire on an agent merely *narrating* a
question inside a paragraph.

The distinction that holds up: **an agent that is genuinely blocked renders a
control.** Prose is something it can emit at any point in a turn; a numbered
option list, a `(y/n)`, or a password prompt only exists on screen while
something is actually waiting for a keystroke. So the ACTION rules now match
controls only, and the two prose patterns are kept in a separate
`_PROSE_PATTERNS` tuple that `classify()` does not consult.

Verified against the live sessions immediately afterwards. Two tabs classified
ACTION, both on the numbered-selection control, and both correct — real option
lists the agents were blocked on:

```
❯ 1. Ship, then test post (Recommended)
❯ 1. Throwaway Xcode project in the guest
```

The second of those became `fixtures/claude-action-01.txt`, the corpus's first
real ACTION and the one state that previously had zero evidence behind it. The
other was left uncaptured on purpose: that session's screen was client work, and
`redact()` cannot recognise client material as sensitive.

**What this change is not backed by: the corpus.** Checked directly rather than
inferred — no fixture in `fixtures/` matches either prose pattern, so every
fixture's label is identical before and after the change and
`test_fixture_expectations` would have passed either way. The evidence is one
observed live false positive plus a hand-written regression test confirmed to
fail against the old rules. That justifies removing a rule seen to be wrong; it
does **not** establish that the corpus can tell prose from controls, because the
corpus has never contained a prose case at all. Capturing one belongs in Task 1
alongside the ACTION examples — a rule with no counter-example in the fixtures is
a rule no fixture run can defend.

This is the third defect in a row (10, 11, 12) whose common shape is *a signal
asserted more confidently than the evidence supports*. Worth carrying into the
classifier work as the governing bias, not just a bug pattern — and the paragraph
above is that same shape appearing in the write-up of the fix rather than in the
code.

### 13. Bystander panes were voting to hold a colour they had no claim to

Finding 11's release did not fire on the tab where it was first observed. The
log showed a three-pane tab painted red at 17:16:50 and still red minutes later,
long after the pane that earned it had returned to UNKNOWN with changed content.

The staleness predicate was per-pane but the aggregation was not. Only one pane
had ever been classified confidently; the other two had `confident_hash = None`,
which the first version read as "nothing has gone stale" and therefore "hold".
**Two panes with no state of their own outvoted the one pane that had moved on.**

The rule is now positive rather than negative: a pane votes to hold only if it
has a confident state *and* that state's screen is still what it is showing.
A pane that has never been classified confidently abstains. `evidence_is_stale`
became `evidence_is_current` to make the abstention the default reading rather
than a double negative.

This matters more than a three-pane edge case suggests, because on this machine
every tab holds three panes — the common configuration, not the exotic one. It
is also the second bug in the same feature within an hour, both from the same
root: reasoning about one session while the thing being painted is a tab.

### 14. The tool's own output about a control is indistinguishable from a control

While probing the above, cupline classified *its own terminal* as ACTION. The
session was running a diagnostic that printed the matched lines from other
sessions, including:

```
      | ❯ 1. Ship, then test post (Recommended)
```

That is text *about* a control, echoed into a log, and the numbered-selection
pattern cannot tell it from the real thing. The general case is not the
diagnostic: any agent that displays terminal content — a diff of a transcript,
a log excerpt, documentation describing a prompt — can render a control shape it
is not blocked on.

Not fixed, because the obvious fix is a guess. A real control is plausibly the
last interactive thing on screen, so a positional constraint (only match within
the final few non-blank lines) would likely help, but "likely" is not a
measurement and the corpus is currently one ACTION deep. Recorded as a known
false-positive class and left for the classifier work, where it can be tested
against fixtures rather than asserted. This is the same discipline finding 12
imposed: do not add a rule that cannot be shown to work.

### 15. A false ACTION keeps animating; a real one freezes the screen

Tracing which run produced the red tab the user asked about turned up a
discriminator that no rule currently uses. The same session (`3497061D`,
cinder) was painted red twice, once on each side of the finding 12 fix, and
the two events have opposite signatures in the log:

| | pre-fix, 17:02:02 | post-fix, 17:20:46 |
|---|---|---|
| Duration held ACTION | 102 s | 196 s |
| Reads where content had changed | **18** | **1** (the entry read itself) |
| Read cadence while ACTION | every ~4.1 s — the ceiling | none; nothing to report |

A session pinned at the debounce *ceiling* is one whose screen is changing on
every single read. That is an agent working — spinner turning, output streaming —
which is exactly what an agent blocked at a prompt is not doing. The post-fix
ACTION sat on a frozen normalised hash for over three minutes.

So **"is the screen still moving?" is direct evidence against ACTION**, and it is
already computed: `seconds_since_change` carries a real value as of finding 10,
and the sweeper already distinguishes a ceiling-triggered read from a quiet one.
Nothing consults it. This is the first concrete rule the temporal inputs make
possible, and unlike the positional heuristic in finding 14 it is not a guess —
it is a measured contrast between a known false positive and a known true one, on
the same session and the same agent. Two events is not a corpus, so it belongs in
the classifier work behind fixture evidence (Task 2), not bolted on here.

### 16. Redraw activity separates working from stopped with no overlap

Finding 15 said a screen still moving is evidence against ACTION. Measuring that
properly turned it into the whole design.

The requirement was restated by the user and is narrower than the spike assumed:
*"I want to know when they've stopped working for whatever reason."* Every one of
their sessions runs with auto-approve on, so approval prompts — the state the
spike had invested most in — barely occur. WORKING versus stopped is the entire
product, and ACTION is a refinement of "stopped", not a peer state.

Two measurements, both through the same screen streamer the sweeper uses:

**Sampled screen text, 1 s apart, 30 samples:**

| | raw changes | after `normalize()` |
|---|---|---|
| 6 working sessions | 22 – 29 / 29 | varies |
| 3 stopped sessions | **0 / 29** | 0 |

Codex is the instructive row: `29/29` raw, `5/29` normalised. `normalize()`
strips spinners and elapsed counters, which was right for "has the content
moved" (finding 6) and destroys "is it moving at all". **cupline was normalising
away the exact signal the requirement needs.**

**Streamer events over 150 s, 9 sessions:**

| session | events | longest silence |
|---|---|---|
| codex / beacon | 4128 | 0.1 s |
| 5 other working panes | 1271 – 1453 | 0.3 – 0.7 s |
| one stopped pane | **0** | 150 s (the whole run) |

A continuously working agent is never silent for more than **0.7 s**; a stopped
one is silent indefinitely. Two sessions showed single large gaps (48.8 s, 63.1 s)
which are state *transitions* — idle, then started working — not pauses within
work.

The populations do not overlap, so `IDLE_AFTER_SECONDS = 5.0` is a margin (~7x
the largest working gap), not a tuning compromise between competing errors.

Three consequences worth recording:

1. **The signal was already being collected and thrown away.**
   `get_screen_streamer(want_contents=False)` fires on every repaint, and
   `note_event` has always stamped `last_event_at`; `TerminalSnapshot` has
   carried `seconds_since_redraw` with a docstring describing exactly this use.
   No rule read it. The fix deleted more logic than it added.
2. **It needs no screen fetch.** The redraw clock advances on its own, so
   classification moved to every sweep tick, and detection latency is now the
   sweep interval rather than the idle-recheck interval.
3. **It cannot depend on a harness.** It reads timing, not language. Codex and
   Claude Code were measured side by side and are indistinguishable on it, which
   is the first time the universality claim has been *measured* rather than
   argued from the absence of harness-specific code.

Verified live afterwards on 9 sessions: 7 busy panes stayed unpainted, the two
genuinely stopped ones went amber 5.0 s and 5.6 s after startup — the grace
period elapsing — with correct project names and zero errors.

The text rule this replaced (`esc to interrupt` ⇒ WORKING) was not just
redundant but harmful: a harness frozen mid-turn leaves that text on screen while
repainting nothing. Trusting the words would report a hung agent as busy, which
is the stop most worth hearing about. It is kept as `_STALE_WORKING_CLAIMS`,
unconsulted, so the decision is visible rather than an absence.

### 17. Panes render their own colour; only the tab *bar* needs the aggregate

Reported from use: "if the tab group is yellow then all three subtabs are yellow
too, not just the one or ones waiting for me." Confirmed against the API before
anything was changed — tab 50's three sessions all carried `use_tab_color = 1`
amber while only one was stopped, the other two mid-turn at 20 and 14 minutes
elapsed. Painting all of them was finding 1's deliberate response, and on a
three-pane tab it turned one alert into three claims.

The fix needed one fact nothing in the spike had established: does a *pane's own
title bar* render that session's `tab_color`, or is the property purely tab-level?
Probed by painting a single background pane amber, clearing the active one, and
photographing the result:

```
tab bar (⌘3)             grey      <- finding 1 confirmed: active session only
background pane title    AMBER     <- panes DO render their own colour
```

And inverted, because the active-pane rule stands entirely on the converse and
finding 1's original probe only established it as a negative — paint the active
pane amber, clear every background pane:

```
tab bar (⌘1)             AMBER     <- one active session is sufficient
background pane title    grey      <- and no background pane is needed
```

So both scopes are real and they disagree, which is what makes precision
possible at all:

| chrome | reads colour from |
|---|---|
| tab bar entry | the tab's **active** session, and nothing else |
| split pane title bar | **that pane's own** session |

The rule adopted: **every pane shows its own verdict, except the active pane,
which carries the tab's aggregate.** The tab bar is then correct whichever pane
has focus, every background pane tells the truth, and the one remaining
overstatement is on the pane already in front of you.

Three consequences that are not obvious:

* The active-pane rule cannot be conditional on that pane being a watched agent.
  A plain shell in the focused split next to a waiting agent still has to carry
  the aggregate, or the tab bar goes dark and the alert disappears.
* Dedupe had to move from tab-level to per-session. The aggregate sits still
  while an individual pane changes state, and it sits still every time focus
  moves — both must repaint. Keeping the tab-level check would have silently
  dropped the tab bar to grey on any focus change within an amber tab.
* Where the two scopes conflict, the fallback is *over*-painting. If the active
  pane cannot be determined, the whole tab is painted the aggregate. A spurious
  colour costs a glance; a missing one costs the alert.
* The "which tabs need undoing on exit" set had to become **derived** from what
  landed on each pane, not tracked beside it. With per-pane pushes, one pane's
  profile call can fail while its siblings succeed; a separately-maintained
  tab-level flag would already read "no longer coloured", and the shutdown
  restore would walk past that tab and leave amber behind with no process left
  to explain it.

No `FocusMonitor` is needed: `app.py:404-410` assigns `tab.active_session_id`
straight from the FocusChanged notification, and `async_get_app` subscribes to it
(`app.py:700`), so `tab.current_session` stays live on a long-lived `App` with no
extra RPC. Verified live at 22:31 — tab 2 painted `waiting, working` across two
panes, screenshot showing an amber tab bar, an amber stopped pane and a grey
working pane beside it.

## Limitations

- **One colour per tab bar entry.** The bar shows one state, so a tab with two
  differently-stopped panes surfaces only the most urgent (the title carries a
  `+N` count for the rest). Individual panes are no longer aggregated — see
  finding 17 — but the tab bar still is, and the active pane cannot show its own
  state when it differs from the tab's.
- **Alt-screen TUIs.** Visible-screen capture works for the agents in scope
  (Codex and Claude Code tails were both captured cleanly). Scrollback for a
  program in the alternate buffer was **not** tested and should not be assumed.
- **Crash leaves colour behind.** Tab colour lives in iTerm2's profile state, not
  in this process. `--reset` clears everything; the signal handler restores on
  clean exit and was verified against SIGTERM.
- **Text-only signal.** Nothing in the terminal distinguishes "reasoning" from
  "waiting for you" when neither prints anything. Inactivity is not evidence.
- **Tab colour is subtle in this theme.** Visible as an outline plus a light
  tint, not a filled block. Legible, but not loud across a large monitor.
- **No signal reaches a window you cannot see.** Tab colour, tab title and badge
  are all drawn inside the iTerm2 window. On another Space, another display, or
  behind another app, none of them exist. Covering that needs a channel outside
  the terminal and is not in this spike.
- **The dependency chain rests on a deprecated websockets API.** `iterm2` 2.20
  declares `websockets` with no upper bound, and `iterm2/connection.py` prefers
  `websockets.legacy.client` — importing it is what makes the test suite report
  one `DeprecationWarning`. Verified on the installed **websockets 16.0**, which
  still ships `legacy` *and* a working `websockets.client.unix_connect` fallback,
  so the connection has two viable paths today and neither is broken. The risk is
  reproducibility, not current function: a fresh `pip install iterm2` resolves
  `websockets` to whatever is newest, and the day `legacy` is removed the
  preferred path goes with it. Record the verified version rather than assuming
  the next install matches this one.

## Performance

Measured on the shipped code — after findings 10–13 — with 9 agent sessions
across 3 tabs in 1 window, `DEBOUNCE_SECONDS = 1.5`, `IDLE_RECHECK_SECONDS = 3.0`.
40 samples at 20 s intervals across a 20-minute run:

| Metric | Value | Before findings 10–13 |
|---|---|---|
| CPU, median | **1.55 %** | 0.9 – 1.0 % |
| CPU, mean / range | 1.59 % / 0.9 – 2.7 % | — |
| CPU during startup | 3.1 % | 3.1 % |
| RSS, start → end | 43.0 → 43.9 MB | 43.5 MB |
| Errors / warnings in 20 min | **0 / 0** | — |
| Read cadence, animating session | every ~4.1 s (the ceiling) | same |
| Read cadence, idle session | every 3.0 s (new) | never re-read |
| `ps` invocations | 1 per 2 s, shared across all sessions | same |

**CPU roughly doubled, and that is the price of finding 10.** A settled session
used to be read once and abandoned; it is now re-read every 3 s so stability can
accumulate. That is 3 fetches/s at 9 idle sessions, and it buys the entire
temporal half of the classifier's input — which previously could not be computed
at all. Measured *after* the change, not extrapolated from before it, so the cost
is not open for re-litigation: 1.55 % median is still an order of magnitude
inside budget for 4–10 sessions.

**RSS grew 0.88 MB over the first 13 minutes, then held flat at 43.9 MB for the
final 3 minutes** (9 consecutive identical samples). Growth-then-plateau is the
shape of allocator arenas filling, not of an unbounded leak — but 20 minutes
cannot distinguish a leak with a long period from no leak at all. Claim: no leak
observable at this horizon. Not: no leak.

### What the soak did and did not cover

Eleven tab transitions, all accounted for. Two were the real result: a tab held
red for 3 m 10 s and 3 m 16 s across two genuinely-blocked panes, retitled from
`atlas · needs you` to `cinder · needs you` when the first prompt was
answered, then released. Four were finding 14 firing on cupline's own tab, which
is a known false positive and not paint churn from the debounce. Clean shutdown
on SIGTERM: `restoring tab appearance before exit` logged, and all nine screen
watchers closed with websocket status 1000.

**Not covered: session create/destroy.** No session was opened or closed during
this run. That behaviour was verified live in an earlier run against *older*
code, before the idle-recheck and vote-path changes — both of which touch
`_should_read` and the aggregation. It is carried over, not re-established, and
should be re-run before anyone relies on it.

## Open questions and risks

1. **The working/stopped rule is measured; the WAITING/ACTION split is not.**
   Finding 16 settles the primary question with non-overlapping populations
   across two harnesses. What remains unproven is the refinement on top: whether
   a stopped agent needs a decision (red) or an instruction (amber). That still
   rests on six regexes and one ACTION fixture. Getting it wrong costs a colour,
   not the alert.

   Two open risks specific to the new rule. A harness that pauses its animation
   while genuinely working would read as stopped — not observed across Claude
   Code or Codex over 150 s, not ruled out for OpenCode, AGY or Cursor Agent.
   And the 0.7 s figure comes from one machine under one load; a saturated
   system could stretch a working pane's silence, though 5 s leaves room.
2. **The ACTION corpus is one fixture deep, from one agent.** A real numbered
   choice control was finally captured (finding 12), which is a start, but one
   example from Claude Code is not enough to generalise a rule that carries the
   highest cost of being wrong. Codex, OpenCode, AGY and Cursor Agent all render
   their own prompt shapes and none has been seen blocked.
3. **The WAITING/ACTION boundary is genuinely fuzzy.** Two captures were first
   labelled ACTION and corrected to WAITING on review — a human judgment call
   about turn-ended versus blocked, made while labelling, and **not** something
   the prose rules caused or would have caught: neither fixture matches a prose
   pattern, which is exactly why finding 12's change has no corpus coverage. An
   agent that ends its turn asking "should I do X?" has finished, and is not
   blocked at a prompt.
   The distinction that survived is *blocked at an input control* versus *turn
   ended*. If that line is hard for a human labeller it will be hard for a
   classifier, and mislabelled fixtures are worse than missing ones. Finding 12
   is the same boundary showing up a third time, in the rules rather than the
   labels — which is fair warning that it will keep recurring.
4. **Colour choice is untested for accessibility** and against iTerm2's other
   themes; only the current Minimal-style dark theme was checked.
5. **Multi-window is untested.** Only one window existed. Nothing is
   window-specific in the code, but that is an argument, not a measurement.
6. **No signal reaches an invisible window.** See Limitations. This is the
   largest functional gap the spike leaves open, and it was raised by the user
   looking at the badge probe: an in-window marker is useless from another tab
   group.
7. **Alt-screen TUIs remain untested** for scrollback specifically.
8. **A false positive is worse than a miss.** Agent gating happens before any
   painting, so a non-agent session cannot be coloured — verified against a
   long-running Python process that correctly resolved to no agent.

9. **A held state can still be stale in a way this fix does not cover.** Finding
   11 releases the hold once the screen has moved on from the evidence. It does
   not cover the opposite case: an agent genuinely blocked at a prompt that
   *also* animates something, so the content keeps changing while the prompt
   stays. There the release would fire while the prompt is still up.

   The soak gave this its first real evidence, and it points the right way: two
   Claude Code sessions held ACTION for **3 m 10 s** and **3 m 16 s** — both
   already blocked when the monitor started, so those are lower bounds on how
   long the prompts had been up — with the normalised hash never moving once
   across either. The prompt shape that matters most today does not animate, so
   on it the release has nothing to fire on early. That is one agent's prompt
   widget, not a general result: a spinner *above* a live prompt, or a countdown
   inside one, would still trip it, and neither has been seen. Downgraded from
   unquantified risk to a known-narrow one.

   What is **not** measured here is detection latency. The 76 ms between the last
   blocked pane releasing and the tab clearing is paint latency, not detection —
   the gap between the screen actually changing and cupline noticing is bounded
   by the read cadence (≤ 4 s, finding 6) and was never observed directly,
   because nothing timestamps the change itself.

Resolved during the spike, kept here because the reasoning matters: stability is
now measured on normalised content (finding 6); all five agents' launch shapes
are settled (finding 8); per-pane signalling was evaluated and rejected with
reasons (finding 9); the temporal classifier inputs actually carry values
(finding 10); and a held state now expires with its evidence (finding 11).

## Recommendation

**Proceed to classifier work.** The architecture is viable.

Every mechanism the design depends on exists and works on 3.6.11 stable, with no
3.7 beta required, no agent modification, and no per-harness integration. The
observation → targeting → highlight → restore loop runs end to end at ~1% CPU,
and the surprises found (active-session targeting, split panes, `jobName`
instability) all had clean solutions inside the supported API.

The classifier boundary is real, not aspirational: `models.py` and
`classifier.py` import nothing from `iterm2`, and the whole suite runs with no
terminal attached. Replacing `classify()` with rules, temporal heuristics, or a
local model touches no iTerm2 code.

Two things should be settled early in that work, because they are design
questions rather than implementation details: how deep the ACTION corpus needs
to be before the WAITING/ACTION split can be called measured (open question 2),
and where the boundary between those two actually sits (open question 3).
