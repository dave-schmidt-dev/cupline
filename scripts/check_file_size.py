"""Enforce the rule that files may not exceed 500 lines without justification."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

DEFAULT_MAX_LINES = 500
DEFAULT_EXCEPTIONS_PATH = ".file-size-exceptions"


def count_lines(path: Path) -> int:
    """Return the line count of *path* without decoding its contents."""
    contents = path.read_bytes()
    if not contents:
        return 0
    return contents.count(b"\n") + (not contents.endswith(b"\n"))


def load_exceptions(path: Path) -> tuple[dict[str, int], list[str]]:
    """Load exception caps and validation errors from *path*."""
    if not path.exists():
        return {}, []

    exceptions: dict[str, int] = {}
    errors: list[str] = []
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(maxsplit=2)
        if len(fields) < 3 or not fields[2].strip():
            errors.append(f"{path}:{line_number}: exception entry needs a positive cap and reason")
            continue
        entry_path, cap_text, _reason = fields
        try:
            cap = int(cap_text)
        except ValueError:
            errors.append(f"{path}:{line_number}: cap must be a positive integer")
            continue
        if cap <= 0:
            errors.append(f"{path}:{line_number}: cap must be a positive integer")
            continue
        exceptions[entry_path] = cap
    return exceptions, errors


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    """Parse command-line *arguments* for the file-size checker."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    parser.add_argument("--exceptions", default=DEFAULT_EXCEPTIONS_PATH)
    parser.add_argument("files", metavar="FILE", nargs="+")
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    """Check files named by *arguments* and return a process exit status."""
    options = parse_arguments(sys.argv[1:] if arguments is None else arguments)
    exceptions_path = Path(options.exceptions)
    exceptions, errors = load_exceptions(exceptions_path)

    if options.max_lines <= 0:
        errors.append("--max-lines must be a positive integer")

    for file_name in options.files:
        path = Path(file_name)
        if not path.exists():
            continue
        line_count = count_lines(path)
        exception_cap = exceptions.get(file_name)
        if line_count <= options.max_lines:
            if exception_cap is not None:
                print(
                    f"{file_name}: {line_count} lines is at or under "
                    f"{options.max_lines}; remove its exception from {options.exceptions}"
                )
        elif exception_cap is None or line_count > exception_cap:
            errors.append(
                f"{file_name}: {line_count} lines exceeds {options.max_lines}; split it or "
                f"add a justified entry to {options.exceptions}"
            )

    for error in errors:
        print(error, file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
