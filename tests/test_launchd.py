import plistlib
import xml.etree.ElementTree as ET
from pathlib import Path


def test_launchd_template_parses_with_elementtree_and_no_forbidden_double_hyphen() -> None:
    template = Path("launchd/com.zerodelta.cupline.plist.template")
    ET.parse(template)


def test_launchd_template_keeps_the_service_lifecycle_contract() -> None:
    config = plistlib.loads(Path("launchd/com.zerodelta.cupline.plist.template").read_bytes())
    assert config["Label"] == "com.zerodelta.cupline"
    assert config["RunAtLoad"] is True
    assert config["KeepAlive"] is True
    assert config["ProcessType"] == "Interactive"
    assert config["ThrottleInterval"] == 15
    assert config["ProgramArguments"] == ["__PYTHON__", "__CUPLINE_DIR__/cupline.py"]
    assert config["WorkingDirectory"] == "__CUPLINE_DIR__"
    assert config["StandardOutPath"] == config["StandardErrorPath"]


def test_readme_reloads_launchagent_after_installing_it() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    install = readme.index("install -m 600")
    replace = readme.index("mv -f ~/Library/LaunchAgents/com.zerodelta.cupline.plist.new")
    bootout = readme.index("launchctl bootout gui/$(id -u)/com.zerodelta.cupline")
    bootstrap = readme.index("launchctl bootstrap gui/$(id -u)")
    assert install < replace < bootout < bootstrap
