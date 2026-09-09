import logging
import plistlib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import cupline


def test_launchd_template_parses_with_elementtree_and_no_forbidden_double_hyphen() -> None:
    template = Path("launchd/com.zerodelta.cupline.plist.template")
    ET.parse(template)


def test_launchd_template_keeps_the_service_lifecycle_contract() -> None:
    config = plistlib.loads(Path("launchd/com.zerodelta.cupline.plist.template").read_bytes())
    assert config["Label"] == "com.zerodelta.cupline"
    assert config["RunAtLoad"] is True
    assert config["KeepAlive"] is True
    assert config["ThrottleInterval"] == 15
    assert config["ProcessType"] == "Interactive"
    assert config["ProgramArguments"] == ["__PYTHON__", "__CUPLINE_DIR__/cupline.py"]
    assert config["WorkingDirectory"] == "__CUPLINE_DIR__"
    assert config["StandardOutPath"] == config["StandardErrorPath"] == "/dev/null"


def test_main_leaves_initial_retry_to_launchd(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class FakeConnection:
        def run_until_complete(self, entry, retry) -> None:
            captured["retry"] = retry
            raise ConnectionRefusedError

    monkeypatch.setattr(cupline, "setup_logging", lambda debug: None)
    monkeypatch.setattr(cupline.iterm2, "Connection", FakeConnection)
    monkeypatch.setattr("sys.argv", ["cupline.py"])
    with pytest.raises(SystemExit) as raised:
        cupline.main()

    assert raised.value.code == cupline.os.EX_TEMPFAIL
    assert captured == {"retry": False}


def test_main_does_not_remap_unexpected_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeConnection:
        def run_until_complete(self, entry, retry) -> None:
            raise SystemExit(1)

    monkeypatch.setattr(cupline, "setup_logging", lambda debug: None)
    monkeypatch.setattr(cupline.iterm2, "Connection", FakeConnection)
    monkeypatch.setattr("sys.argv", ["cupline.py"])
    with pytest.raises(SystemExit) as raised:
        cupline.main()

    assert raised.value.code == 1


def test_setup_logging_retains_info_in_normal_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cupline, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(cupline, "LOG_FILE", "cupline.log")
    logger = logging.getLogger("cupline")
    previous_level = logger.level
    try:
        cupline.setup_logging(False)
        handlers = [
            handler
            for handler in logger.handlers
            if isinstance(handler, logging.handlers.RotatingFileHandler)
        ]
        assert len(handlers) == 1

        file_handler = handlers[0]
        assert file_handler.level == logging.INFO
        assert file_handler.maxBytes == 2_000_000
        assert file_handler.backupCount == 3
        logger.info("retained lifecycle record")
        file_handler.flush()
        assert "retained lifecycle record" in (tmp_path / "cupline.log").read_text()
    finally:
        logger.setLevel(previous_level)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()


def test_readme_reloads_launchagent_after_installing_it() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    install = readme.index("install -m 600")
    replace = readme.index("mv -f ~/Library/LaunchAgents/com.zerodelta.cupline.plist.new")
    bootout = readme.index("launchctl bootout gui/$(id -u)/com.zerodelta.cupline")
    bootstrap = readme.index("launchctl bootstrap gui/$(id -u)")
    assert install < replace < bootout < bootstrap
