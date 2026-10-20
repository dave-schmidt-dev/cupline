# Invariants — cupline

Terminal activity classification remains a replaceable seam; source tests are distinct from a live iTerm2 acceptance run.

Charter adoption records required behavior; it does not certify runtime compliance.

### INV-1 — Attention state derives from measured terminal activity
area: ["classifier.py", "models.py", "screen.py"]
gate_test: tests/test_classifier.py
threshold: 3
rationale: The classifier must preserve the documented working/attention decision and avoid inventing a stop from an unreadable screen.

### INV-2 — Tab state changes preserve ownership and restoration across panes
area: ["tab_state.py", "watchers.py", "sessions.py"]
gate_test: tests/test_tab_state_restore.py
threshold: 3
rationale: A session-attention change must not clobber unrelated pane or tab state and must restore owned changes correctly.

### INV-3 — Monitoring remains observable through blocking connection and sweep work
area: ["monitor.py", "cupline.py"]
gate_test: manual: inspect connection, watchdog and sweep paths for visible progress or actionable logs and terminal failure; complete automated progress coverage is not implemented
threshold: 3
rationale: A tool intended to reveal stalled agents must not itself wait silently or hide a dead monitoring connection.
