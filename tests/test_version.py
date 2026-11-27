"""The version is written in two places: pyproject.toml and saverun.__version__ (shown by --version)."""
import tomllib

import pytest

from saverun import __version__
from saverun.cli import main


def test_pyproject_and_package_versions_agree():
    with open("pyproject.toml", "rb") as f:
        assert tomllib.load(f)["project"]["version"] == __version__


def test_cli_prints_the_package_version(capsys):
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"saverun {__version__}"
