import json
import os
import stat

import pytest

from webapp import config, garmin_config


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    cfg_dir = tmp_path / ".GarminDb"
    cfg_file = cfg_dir / "GarminConnectConfig.json"
    monkeypatch.setattr(config, "GARMIN_CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "GARMIN_CONFIG_FILE", cfg_file)
    return cfg_file


def test_write_credentials_creates_file_with_0600(isolated_config_dir):
    garmin_config.write_credentials("runner@example.com", "not-a-real-password")
    st = isolated_config_dir.stat()
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert st.st_uid == os.getuid()


def test_write_credentials_preserves_non_credential_keys(isolated_config_dir):
    isolated_config_dir.parent.mkdir(parents=True)
    existing = {
        "db": {"type": "sqlite"},
        "credentials": {"user": "", "password": ""},
        "data": {"weight_start_date": "01/01/2020", "download_latest_activities": 99},
        "settings": {"metric": True, "default_display_activities": ["running"]},
    }
    isolated_config_dir.write_text(json.dumps(existing))
    os.chmod(isolated_config_dir, 0o600)

    garmin_config.write_credentials("runner@example.com", "not-a-real-password")

    written = json.loads(isolated_config_dir.read_text())
    assert written["data"]["weight_start_date"] == "01/01/2020"
    assert written["data"]["download_latest_activities"] == 99
    assert written["settings"]["metric"] is True
    assert written["credentials"]["user"] == "runner@example.com"
    assert written["credentials"]["password"] == "not-a-real-password"


def test_write_credentials_rejects_empty(isolated_config_dir):
    with pytest.raises(garmin_config.GarminConfigError):
        garmin_config.write_credentials("", "not-a-real-password")
    with pytest.raises(garmin_config.GarminConfigError):
        garmin_config.write_credentials("runner@example.com", "")


def test_status_never_includes_password(isolated_config_dir):
    garmin_config.write_credentials("runner@example.com", "not-a-real-password")
    st = garmin_config.status()
    assert st == {"configured": True, "username": "runner@example.com"}
    assert "password" not in st
    assert "not-a-real-password" not in json.dumps(st)


def test_temp_file_never_left_behind_on_success(isolated_config_dir):
    garmin_config.write_credentials("runner@example.com", "not-a-real-password")
    leftovers = list(isolated_config_dir.parent.glob(".GarminConnectConfig.json.tmp-*"))
    assert leftovers == []
