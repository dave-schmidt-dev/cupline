"""Tests for the pre-commit file-size checker."""

from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_file_size.py"


def run_check(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the checker in *tmp_path* with *arguments*."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def write_lines(path: Path, line_count: int) -> None:
    """Write *line_count* newline-terminated lines to *path*."""
    path.write_bytes(b"\n" * line_count)


def test_exactly_500_lines_passes(tmp_path: Path) -> None:
    write_lines(tmp_path / "limit.py", 500)

    result = run_check(tmp_path, "limit.py")

    assert result.returncode == 0
    assert not result.stdout
    assert not result.stderr


def test_501_lines_fails_and_names_file(tmp_path: Path) -> None:
    write_lines(tmp_path / "too_large.py", 501)

    result = run_check(tmp_path, "too_large.py")

    assert result.returncode == 1
    assert "too_large.py" in result.stderr


def test_listed_file_within_its_cap_passes(tmp_path: Path) -> None:
    write_lines(tmp_path / "generated.py", 501)
    (tmp_path / ".file-size-exceptions").write_text("generated.py 600 generated code\n")

    result = run_check(tmp_path, "generated.py")

    assert result.returncode == 0
    assert not result.stderr


def test_listed_file_over_its_cap_fails(tmp_path: Path) -> None:
    write_lines(tmp_path / "generated.py", 601)
    (tmp_path / ".file-size-exceptions").write_text("generated.py 600 generated code\n")

    result = run_check(tmp_path, "generated.py")

    assert result.returncode == 1
    assert "generated.py" in result.stderr


def test_entry_with_no_reason_fails(tmp_path: Path) -> None:
    (tmp_path / ".file-size-exceptions").write_text("generated.py 600\n")

    result = run_check(tmp_path, "missing.py")

    assert result.returncode == 1
    assert "reason" in result.stderr


def test_non_integer_cap_fails(tmp_path: Path) -> None:
    (tmp_path / ".file-size-exceptions").write_text("generated.py many generated code\n")

    result = run_check(tmp_path, "missing.py")

    assert result.returncode == 1
    assert "positive integer" in result.stderr


def test_comments_and_blank_lines_are_ignored(tmp_path: Path) -> None:
    write_lines(tmp_path / "generated.py", 501)
    (tmp_path / ".file-size-exceptions").write_text(
        "\n# retained generated file\n\ngenerated.py 600 generated code\n"
    )

    result = run_check(tmp_path, "generated.py")

    assert result.returncode == 0


def test_nonexistent_file_argument_is_skipped(tmp_path: Path) -> None:
    result = run_check(tmp_path, "missing.py")

    assert result.returncode == 0
    assert not result.stdout
    assert not result.stderr


def test_max_lines_overrides_default(tmp_path: Path) -> None:
    write_lines(tmp_path / "small.py", 2)

    result = run_check(tmp_path, "--max-lines", "1", "small.py")

    assert result.returncode == 1
    assert "exceeds 1" in result.stderr


def test_final_line_without_trailing_newline_is_counted(tmp_path: Path) -> None:
    (tmp_path / "no_final_newline.py").write_bytes(b"\n" * 500 + b"x")

    result = run_check(tmp_path, "no_final_newline.py")

    assert result.returncode == 1
    assert "501 lines" in result.stderr


def test_removable_exception_note_is_non_failing(tmp_path: Path) -> None:
    write_lines(tmp_path / "smaller.py", 500)
    (tmp_path / ".file-size-exceptions").write_text("smaller.py 600 generated code\n")

    result = run_check(tmp_path, "smaller.py")

    assert result.returncode == 0
    assert "remove its exception" in result.stdout
    assert not result.stderr
