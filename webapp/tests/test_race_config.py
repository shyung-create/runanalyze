import pytest

from webapp import config, race_config

SAMPLE_YAML = """\
race:
  name: "SF Marathon"
  distance_type: "full"
  race_date: "2026-07-26"
  target_time: "04:00:00"

preferences:
  units: "km"           # "miles" or "km" — must match your GarminDB measurement setting
  long_run_day: "sunday"   # weekly long run lands here (swapped within the week)
  rest_days: []         # weekdays that are ALWAYS rest, e.g. [friday, monday] —
  #                     # the displaced workout swaps to a rest day in the same week
  blocked_dates: []     # one-off no-run dates, e.g. [2026-07-22]
  max_run_days_per_week: 5
"""


@pytest.fixture
def isolated_race_config(tmp_path, monkeypatch):
    path = tmp_path / "race_config.yaml"
    path.write_text(SAMPLE_YAML)
    monkeypatch.setattr(config, "RACE_CONFIG_FILE", path)
    return path


def test_read_rest_days_empty(isolated_race_config):
    assert race_config.read_rest_days() == []


def test_write_rest_days_updates_value(isolated_race_config):
    race_config.write_rest_days(["friday", "monday"])
    assert race_config.read_rest_days() == ["friday", "monday"]


def test_write_rest_days_preserves_every_comment_and_other_field(isolated_race_config):
    original = isolated_race_config.read_text()
    race_config.write_rest_days(["friday"])
    new = isolated_race_config.read_text()

    # Every comment line from the original survives untouched.
    for line in original.splitlines():
        if "#" in line and "rest_days:" not in line:
            assert line in new, f"comment line lost: {line!r}"

    # Other fields (race name, units, blocked_dates, etc.) are byte-identical.
    assert 'name: "SF Marathon"' in new
    assert 'units: "km"' in new
    assert "blocked_dates: []" in new
    assert "max_run_days_per_week: 5" in new


def test_write_rest_days_rejects_invalid_day_name(isolated_race_config):
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_rest_days(["funday"])
    # Rejected write must not touch the file at all.
    assert race_config.read_rest_days() == []


def test_write_rest_days_lowercases_and_dedupes_via_yaml_semantics(isolated_race_config):
    race_config.write_rest_days(["MONDAY", "Friday"])
    assert race_config.read_rest_days() == ["monday", "friday"]


def test_write_rest_days_no_stray_temp_files(isolated_race_config):
    race_config.write_rest_days(["sunday"])
    leftovers = list(isolated_race_config.parent.glob(".race_config.yaml.tmp-*"))
    assert leftovers == []


def test_write_rest_days_clearing_back_to_empty(isolated_race_config):
    race_config.write_rest_days(["saturday"])
    race_config.write_rest_days([])
    assert race_config.read_rest_days() == []
