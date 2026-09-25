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


def test_exactly_500_lines_passes_silently(tmp_path: Path) -> None:
    write_lines(tmp_path / "limit.py", 500)

    result = run_check(tmp_path, "limit.py")

    assert result.returncode == 0
    assert not result.stdout
    assert not result.stderr


def test_501_lines_warns_and_passes(tmp_path: Path) -> None:
    write_lines(tmp_path / "warning.py", 501)

    result = run_check(tmp_path, "warning.py")

    assert result.returncode == 0
    assert "warning.py has 501 lines (target 500)" in result.stdout
    assert not result.stderr


def test_800_lines_warns_and_passes(tmp_path: Path) -> None:
    write_lines(tmp_path / "limit.py", 800)

    result = run_check(tmp_path, "limit.py")

    assert result.returncode == 0
    assert "limit.py has 800 lines (target 500)" in result.stdout
    assert not result.stderr


def test_801_lines_fails_and_names_file(tmp_path: Path) -> None:
    write_lines(tmp_path / "too_large.py", 801)

    result = run_check(tmp_path, "too_large.py")

    assert result.returncode == 1
    assert "too_large.py has 801 lines" in result.stderr


def test_listed_801_line_file_passes(tmp_path: Path) -> None:
    write_lines(tmp_path / "generated.py", 801)
    (tmp_path / ".file-size-exceptions").write_text("generated.py generated code\n")

    result = run_check(tmp_path, "generated.py")

    assert result.returncode == 0
    assert not result.stderr


def test_listed_10_line_file_prints_removal_note(tmp_path: Path) -> None:
    write_lines(tmp_path / "smaller.py", 10)
    (tmp_path / ".file-size-exceptions").write_text("smaller.py generated code\n")

    result = run_check(tmp_path, "smaller.py")

    assert result.returncode == 0
    assert "exception entry can be removed" in result.stdout
    assert not result.stderr


def test_entry_with_no_reason_fails(tmp_path: Path) -> None:
    (tmp_path / ".file-size-exceptions").write_text("generated.py\n")

    result = run_check(tmp_path, "missing.py")

    assert result.returncode == 1
    assert "reason" in result.stderr


def test_line_cap_entry_fails(tmp_path: Path) -> None:
    (tmp_path / ".file-size-exceptions").write_text("a.py 600 reason\n")

    result = run_check(tmp_path, "missing.py")

    assert result.returncode == 1
    assert "line caps are no longer supported; remove the cap" in result.stderr


def test_duplicate_exception_path_fails(tmp_path: Path) -> None:
    (tmp_path / ".file-size-exceptions").write_text(
        "generated.py generated code\ngenerated.py another reason\n"
    )

    result = run_check(tmp_path, "missing.py")

    assert result.returncode == 1
    assert "path is listed twice: generated.py" in result.stderr


def test_comments_and_blank_lines_are_ignored(tmp_path: Path) -> None:
    write_lines(tmp_path / "generated.py", 801)
    (tmp_path / ".file-size-exceptions").write_text(
        "\n# retained generated file\n\ngenerated.py generated code\n"
    )

    result = run_check(tmp_path, "generated.py")

    assert result.returncode == 0
    assert not result.stderr


def test_nonexistent_file_argument_is_skipped(tmp_path: Path) -> None:
    result = run_check(tmp_path, "missing.py")

    assert result.returncode == 0
    assert not result.stdout
    assert not result.stderr


def test_thresholds_override_defaults(tmp_path: Path) -> None:
    write_lines(tmp_path / "small.py", 3)

    result = run_check(tmp_path, "--target", "2", "--max-lines", "3", "small.py")

    assert result.returncode == 0
    assert "small.py has 3 lines (target 2)" in result.stdout


def test_target_above_ceiling_fails(tmp_path: Path) -> None:
    write_lines(tmp_path / "small.py", 1)

    result = run_check(tmp_path, "--target", "2", "--max-lines", "1", "small.py")

    assert result.returncode == 1
    assert "--target must not exceed --max-lines" in result.stderr


def test_nonpositive_thresholds_fail(tmp_path: Path) -> None:
    write_lines(tmp_path / "small.py", 1)

    target_result = run_check(tmp_path, "--target", "0", "small.py")
    maximum_result = run_check(tmp_path, "--max-lines", "0", "small.py")

    assert target_result.returncode == 1
    assert "--target must be a positive integer" in target_result.stderr
    assert maximum_result.returncode == 1
    assert "--max-lines must be a positive integer" in maximum_result.stderr


def test_final_line_without_trailing_newline_is_counted(tmp_path: Path) -> None:
    (tmp_path / "no_final_newline.py").write_bytes(b"\n" * 500 + b"x")

    result = run_check(tmp_path, "no_final_newline.py")

    assert result.returncode == 0
    assert "501 lines" in result.stdout
