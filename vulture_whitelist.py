# Vulture whitelist: each entry names the only caller that keeps it alive.
# New entries need a one-line justification.

set_override  # test-only seam into classifier._OVERRIDES: tests/test_classifier.py
clear_overrides  # test-only seam into classifier._OVERRIDES: tests/test_classifier.py
seconds_since_change  # Snapshot field, built in models.py:250, asserted in tests/test_models.py
seconds_stable  # Snapshot field, built in models.py:251, asserted in tests/test_models.py
previous_state  # Snapshot field, built in models.py:252, asserted in tests/test_models.py
