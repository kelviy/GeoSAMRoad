"""Installs the review queue QGIS plugin into a QGIS 3 user profile."""

import json
import os
import shutil
import sys
from pathlib import Path

PLUGIN_NAME = "rosa_review_queue"
PLUGIN_SOURCE = Path(__file__).with_name("qgis_plugin") / PLUGIN_NAME


def default_profile_dir() -> Path:
    """The ``default`` QGIS 3 profile's folder on this machine."""
    if sys.platform == "win32":
        base = Path(os.environ["APPDATA"])
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".local" / "share"
    return base / "QGIS" / "QGIS3" / "profiles" / "default"


def install(profile_dir: Path, python: str) -> Path:
    """Copy the plugin into ``profile_dir``'s plugins folder, replacing any
    older copy, and record ``python`` as the Python that runs the review
    service. Returns the installed plugin folder."""
    target = Path(profile_dir) / "python" / "plugins" / PLUGIN_NAME
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(PLUGIN_SOURCE, target, ignore=shutil.ignore_patterns("__pycache__"))
    (target / "service.json").write_text(
        json.dumps({"python": python}, indent=2) + "\n"
    )
    return target
