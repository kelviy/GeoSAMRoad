"""The review queue QGIS plugin's parts that run without QGIS: its client for
the review service, which starts a real service process here, and installing
the plugin into a QGIS profile."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sentinel2data.cleaning import review
from sentinel2data.cleaning.plugin_install import PLUGIN_SOURCE
from sentinel2data.cleaning.qgis_plugin.rosa_review_queue.client import (
    ReviewClient,
    ServiceError,
    service_python,
)
from sentinel2data.cli import app
from tests.sentinel2data.synthetic_zone import TRAIN_ZONE
from tests.sentinel2data.test_detect import gpkg, run_detect
from tests.sentinel2data.test_package_zones import _qgis_python
from tests.sentinel2data.test_review import read


@pytest.fixture
def zone(zones):
    run_detect(zones)
    return gpkg(zones)


@pytest.fixture
def client():
    with ReviewClient(sys.executable) as c:
        yield c


def test_the_client_decides_through_a_running_service(zone, client):
    first, second = review.build_queue(zone)[:2]
    assert client.ask("state", zone)["state"]["item"]["fid"] == first.fid
    answer = client.ask(
        "decide", zone, fid=first.fid, decision="real", annotator="Thandi"
    )
    assert answer["state"]["item"]["fid"] == second.fid
    assert read(zone, "flags").loc[first.fid, "decision"] == "real"


def test_a_refusal_comes_back_as_an_answer_and_the_service_keeps_running(zone, client):
    refused = client.ask("decide", zone, fid=-1, decision="real", annotator="Thandi")
    assert (refused["ok"], refused["error_type"]) == (False, "ValueError")
    assert client.ask("state", zone)["ok"]


def test_a_service_that_cannot_start_raises_with_its_error_output(zone, tmp_path):
    broken = tmp_path / "python"
    broken.write_text("#!/bin/sh\necho 'no review core here' >&2\nexit 3\n")
    broken.chmod(0o755)
    with (
        ReviewClient(str(broken)) as c,
        pytest.raises(ServiceError, match="no review core"),
    ):
        c.ask("state", zone)


def test_the_client_restarts_a_service_that_stopped(zone, client):
    client.ask("state", zone)
    client.close()
    assert client.ask("state", zone)["ok"]


def test_installing_copies_the_plugin_into_the_profile_with_this_python(tmp_path):
    profile = tmp_path / "profiles" / "default"
    (profile / "python" / "plugins" / "quick_map_services").mkdir(parents=True)

    result = CliRunner().invoke(
        app, ["install-qgis-plugin", "--profile-dir", str(profile)]
    )

    assert result.exit_code == 0, result.output
    plugins = profile / "python" / "plugins"
    installed = plugins / "rosa_review_queue"
    assert sorted(p.name for p in plugins.iterdir()) == [
        "quick_map_services",
        "rosa_review_queue",
    ]
    assert {"__init__.py", "metadata.txt", "client.py"} <= {
        p.name for p in installed.iterdir()
    }
    assert service_python(installed) == sys.executable
    assert str(installed) in result.output


def test_reinstalling_replaces_the_old_copy(tmp_path):
    profile = tmp_path / "default"
    args = ["install-qgis-plugin", "--profile-dir", str(profile)]
    CliRunner().invoke(app, args)
    stale = profile / "python" / "plugins" / "rosa_review_queue" / "stale.py"
    stale.write_text("")
    assert CliRunner().invoke(app, args).exit_code == 0
    assert not stale.exists()


def test_the_review_python_variable_overrides_the_installed_one(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSA_REVIEW_PYTHON", "/opt/other/python")
    assert service_python(tmp_path) == "/opt/other/python"


def test_a_plugin_with_no_service_python_says_how_to_install(tmp_path, monkeypatch):
    monkeypatch.delenv("ROSA_REVIEW_PYTHON", raising=False)
    with pytest.raises(ServiceError, match="install-qgis-plugin"):
        service_python(tmp_path)


@pytest.fixture
def qgis_check(packaged, tmp_path):
    """What the plugin's zone layer and client did in headless QGIS, on a
    copy of the train zone folder after ``detect``."""
    found = _qgis_python()
    if found is None:
        pytest.skip(
            "QGIS not found (set QGIS_PYTHON to a Python that can import qgis.core)"
        )
    qgis_python, env = found
    zones = tmp_path / "zones"
    shutil.copytree(packaged / TRAIN_ZONE, zones / TRAIN_ZONE)
    run_detect(zones)
    script = Path(__file__).with_name("qgis_review_plugin_check.py")
    result = subprocess.run(
        [
            qgis_python,
            str(script),
            str(PLUGIN_SOURCE.parent),
            str(zones / TRAIN_ZONE / f"{TRAIN_ZONE}.qgs"),
            sys.executable,
        ],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    lines = result.stdout.strip().splitlines()
    assert lines and lines[-1].startswith("{"), result.stderr
    return json.loads(lines[-1])


def test_accepting_in_qgis_shows_the_road_and_keeps_an_unsaved_hand_edit(qgis_check):
    assert qgis_check["first_kind"] == "proposal"
    assert qgis_check["buffer_modified"] and qgis_check["decided"]
    assert qgis_check["proposal_on_map"] == "added"
    assert qgis_check["hand_edit_on_map"] == "modified"
    assert qgis_check["still_editing"] and qgis_check["commit_after"]
    assert qgis_check["stored_proposal"] == ["added", "gap", "Thandi"]
    assert qgis_check["stored_hand_edit"] == ["modified", "misalignment"]
    assert qgis_check["stored_flag"] == ["real", "Thandi"]


def test_qgis_zooms_to_and_selects_the_next_item_with_sentinel_2_on(qgis_check):
    assert qgis_check["canvas_holds_next_item"]
    assert qgis_check["selected"] == [qgis_check["next_fid"]]
    assert qgis_check["s2_visible"] is True


def test_the_panel_keys_win_over_qgis_window_shortcuts_but_not_in_the_note(
    qgis_check,
):
    assert qgis_check["panel_keys"] == {
        "s2_toggled": True,
        "went_back": True,
        "window_actions_fired": [],
        "note": "SRTB",
    }
