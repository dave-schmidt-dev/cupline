# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The fixture replay is strict: a WAITING or ACTION fixture must classify as labelled. UNKNOWN is accepted only for a fixture carrying `# abstain: allowed`, marked fixtures must stay a minority, and a test proves an always-abstaining classifier fails the replay.

### Fixed

- A control the agent merely displays (a quoted menu in a Bash output block, a `(y/n)` in a log excerpt) no longer reads as ACTION. A control counts only when it sits within `LIVE_CONTROL_LINES` (24) non-blank lines of the bottom with no turn footer below it; the live control on file still classifies ACTION.
- A tab colour left behind by a cupline process that was killed before it could restore (for example `launchctl kickstart -k`) is now cleared by the next process, with a WARNING naming the pane. Only colours from cupline's own palette are touched; a hand-set colour is still left alone, and a pane whose profile cannot be read is retried rather than recorded as clean.
- A tab whose stopped panes have no project name now shows how many stopped together (`your turn +1`) instead of reading as one.
