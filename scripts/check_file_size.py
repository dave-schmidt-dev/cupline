"""Warn about files over a target size and reject files over a ceiling."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

DEFAULT_TARGET = 500
DEFAULT_MAX_LINES = 800
DEFAULT_EXCEPTIONS_PATH = ".file-size-exceptions"


def count_lines(path: Path) -> int:
    """Return the line count of *path* without decoding its contents."""
    contents = path.read_bytes()
    if not contents:
        return 0
    return contents.count(b"\n") + (not contents.endswith(b"\n"))


def load_exceptions(path: Path) -> tuple[set[str], list[str]]:
    """Load exception paths and validation errors from *path*."""
    if not path.exists():
        return set(), []

    exceptions: set[str] = set()
    errors: list[str] = []
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        entry_path = fields[0]
        if entry_path in exceptions:
            errors.append(f"{path}:{line_number}: path is listed twice: {entry_path}")
        else:
            exceptions.add(entry_path)
        if len(fields) < 2:
            errors.append(f"{path}:{line_number}: exception entry needs a reason")
        elif fields[1].isdigit():
            errors.append(
                f"{path}:{line_number}: line caps are no longer supported; remove the cap"
            )
    return exceptions, errors


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    """Parse command-line *arguments* for the file-size checker."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET)
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    parser.add_argument("--exceptions", default=DEFAULT_EXCEPTIONS_PATH)
    parser.add_argument("files", metavar="FILE", nargs="+")
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    """Check files named by *arguments* and return a process exit status."""
    options = parse_arguments(sys.argv[1:] if arguments is None else arguments)
    exceptions_path = Path(options.exceptions)
    exceptions, errors = load_exceptions(exceptions_path)

    if options.target <= 0:
        errors.append("--target must be a positive integer")
    if options.max_lines <= 0:
        errors.append("--max-lines must be a positive integer")
    if options.target > options.max_lines:
        errors.append("--target must not exceed --max-lines")

    for file_name in options.files:
        path = Path(file_name)
        if not path.exists():
            continue
        line_count = count_lines(path)
        is_listed = file_name in exceptions
        if line_count <= options.target:
            if is_listed:
                print(
                    f"file-size: {file_name} has {line_count} lines (at or under ceiling "
                    f"{options.max_lines}); its exception entry can be removed"
                )
        elif line_count <= options.max_lines:
            print(
                f"file-size: {file_name} has {line_count} lines (target {options.target}); "
                "split it when a clean seam exists"
            )
            if is_listed:
                print(
                    f"file-size: {file_name} has {line_count} lines (at or under ceiling "
                    f"{options.max_lines}); its exception entry can be removed"
                )
        elif not is_listed:
            errors.append(
                f"file-size: {file_name} has {line_count} lines (ceiling {options.max_lines}); "
                f"split it or add a written reason to {options.exceptions}"
            )

    for error in errors:
        print(error, file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
