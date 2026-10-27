# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A tab colour left behind by a cupline process that was killed before it could restore (for example `launchctl kickstart -k`) is now cleared by the next process, with a WARNING naming the pane. Only colours from cupline's own palette are touched; a hand-set colour is still left alone, and a pane whose profile cannot be read is retried rather than recorded as clean.
- A tab whose stopped panes have no project name now shows how many stopped together (`your turn +1`) instead of reading as one.
