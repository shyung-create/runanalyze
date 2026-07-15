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
  activities_weeks_back: 0  # if > 0, overrides activities_since with a rolling window
  llm_provider: "deepseek"  # "deepseek" or "claude"
  # plan_id: ""         # optional -- force a specific program instead of
  #                     # auto-selection.
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


# ------------------------------------------------------------ blocked_dates

def test_read_blocked_dates_empty(isolated_race_config):
    assert race_config.read_blocked_dates() == []


def test_write_blocked_dates_updates_value(isolated_race_config):
    race_config.write_blocked_dates(["2026-07-22", "2026-08-01"])
    assert race_config.read_blocked_dates() == ["2026-07-22", "2026-08-01"]


def test_write_blocked_dates_returns_strings_not_date_objects(isolated_race_config):
    # PyYAML auto-parses unquoted YYYY-MM-DD as datetime.date on load --
    # confirmed plan_generator.py already handles this via str(x), but our
    # own read path should hand back plain strings too, not leak that detail.
    race_config.write_blocked_dates(["2026-07-22"])
    result = race_config.read_blocked_dates()
    assert result == ["2026-07-22"]
    assert all(isinstance(d, str) for d in result)


def test_write_blocked_dates_preserves_every_comment_and_other_field(isolated_race_config):
    original = isolated_race_config.read_text()
    race_config.write_blocked_dates(["2026-07-22"])
    new = isolated_race_config.read_text()
    for line in original.splitlines():
        if "#" in line and "blocked_dates:" not in line:
            assert line in new, f"comment line lost: {line!r}"
    assert 'name: "SF Marathon"' in new
    assert "rest_days: []" in new


def test_write_blocked_dates_rejects_invalid_date(isolated_race_config):
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_blocked_dates(["not-a-date"])
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_blocked_dates(["2026-13-45"])  # not a real calendar date
    assert race_config.read_blocked_dates() == []


def test_write_blocked_dates_dedupes_and_sorts(isolated_race_config):
    race_config.write_blocked_dates(["2026-08-01", "2026-07-22", "2026-07-22"])
    assert race_config.read_blocked_dates() == ["2026-07-22", "2026-08-01"]


def test_write_blocked_dates_independent_of_rest_days(isolated_race_config):
    race_config.write_rest_days(["monday"])
    race_config.write_blocked_dates(["2026-07-22"])
    assert race_config.read_rest_days() == ["monday"]
    assert race_config.read_blocked_dates() == ["2026-07-22"]


def test_write_blocked_dates_no_stray_temp_files(isolated_race_config):
    race_config.write_blocked_dates(["2026-07-22"])
    leftovers = list(isolated_race_config.parent.glob(".race_config.yaml.tmp-*"))
    assert leftovers == []


# ------------------------------------------------------------ race details

VALID_DETAILS = dict(
    name="Chicago Marathon", distance_type="full", race_date="2026-10-11",
    target_time="03:45:00", long_run_day="saturday", plan_id="",
    activities_weeks_back=0, llm_provider="deepseek",
)


def test_read_race_details_matches_sample(isolated_race_config):
    details = race_config.read_race_details()
    assert details == {
        "name": "SF Marathon", "distance_type": "full", "race_date": "2026-07-26",
        "target_time": "04:00:00", "long_run_day": "sunday", "plan_id": "",
        "activities_weeks_back": 0, "llm_provider": "deepseek",
    }


def test_write_race_details_updates_all_fields(isolated_race_config):
    race_config.write_race_details(**VALID_DETAILS)
    assert race_config.read_race_details() == VALID_DETAILS


def test_write_race_details_preserves_comments_and_other_fields(isolated_race_config):
    original = isolated_race_config.read_text()
    race_config.write_race_details(**VALID_DETAILS)
    new = isolated_race_config.read_text()
    for line in original.splitlines():
        if "#" in line and not any(k in line for k in
                                    ("name:", "distance_type:", "race_date:", "target_time:",
                                     "long_run_day:", "activities_weeks_back:", "llm_provider:")):
            assert line in new, f"comment line lost: {line!r}"
    assert "rest_days: []" in new
    assert "max_run_days_per_week: 5" in new


def test_write_race_details_rejects_bad_distance_type(isolated_race_config):
    bad = dict(VALID_DETAILS, distance_type="marathon")
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_race_details(**bad)
    assert race_config.read_race_details()["distance_type"] == "full"


def test_write_race_details_rejects_bad_race_date(isolated_race_config):
    bad = dict(VALID_DETAILS, race_date="not-a-date")
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_race_details(**bad)


def test_write_race_details_rejects_bad_target_time(isolated_race_config):
    bad = dict(VALID_DETAILS, target_time="4:00pm")
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_race_details(**bad)


def test_write_race_details_rejects_bad_long_run_day(isolated_race_config):
    bad = dict(VALID_DETAILS, long_run_day="funday")
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_race_details(**bad)


def test_write_race_details_rejects_name_with_double_quote(isolated_race_config):
    bad = dict(VALID_DETAILS, name='Race "The Big One"')
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_race_details(**bad)


def test_write_race_details_rejects_empty_name(isolated_race_config):
    bad = dict(VALID_DETAILS, name="   ")
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_race_details(**bad)


def test_write_race_details_rejects_plan_id_not_in_catalog(isolated_race_config):
    bad = dict(VALID_DETAILS, plan_id="not_a_real_plan")
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_race_details(**bad)


def test_write_race_details_rejects_plan_id_wrong_distance_type(isolated_race_config):
    # half_hansons_beginner is a "half" plan; distance_type here is "full".
    bad = dict(VALID_DETAILS, plan_id="half_hansons_beginner")
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_race_details(**bad)


def test_write_race_details_accepts_valid_plan_id_for_distance_type(isolated_race_config):
    good = dict(VALID_DETAILS, plan_id="marathon_higdon_novice_1")
    race_config.write_race_details(**good)
    assert race_config.read_race_details()["plan_id"] == "marathon_higdon_novice_1"


def test_write_race_details_can_clear_plan_id_back_to_auto(isolated_race_config):
    race_config.write_race_details(**dict(VALID_DETAILS, plan_id="marathon_higdon_novice_1"))
    race_config.write_race_details(**VALID_DETAILS)  # plan_id=""
    assert race_config.read_race_details()["plan_id"] == ""
    # Re-commented, not left as an empty active line.
    assert '# plan_id: ""' in isolated_race_config.read_text()


def test_write_race_details_rejects_invalid_write_without_touching_file(isolated_race_config):
    original = isolated_race_config.read_text()
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_race_details(**dict(VALID_DETAILS, distance_type="marathon"))
    assert isolated_race_config.read_text() == original


def test_write_race_details_no_stray_temp_files(isolated_race_config):
    race_config.write_race_details(**VALID_DETAILS)
    leftovers = list(isolated_race_config.parent.glob(".race_config.yaml.tmp-*"))
    assert leftovers == []


def test_plan_catalog_by_distance_type_grouped_and_sorted():
    catalog = race_config.plan_catalog_by_distance_type()
    assert set(catalog.keys()) == {"half", "full"}
    assert catalog["half"] == sorted(catalog["half"])
    assert "marathon_higdon_novice_1" in catalog["full"]
    assert "half_hansons_beginner" in catalog["half"]


# ------------------------------------------------ activities_weeks_back / llm_provider

def test_write_race_details_updates_activities_weeks_back(isolated_race_config):
    race_config.write_race_details(**dict(VALID_DETAILS, activities_weeks_back=8))
    assert race_config.read_race_details()["activities_weeks_back"] == 8


def test_write_race_details_rejects_negative_weeks_back(isolated_race_config):
    bad = dict(VALID_DETAILS, activities_weeks_back=-1)
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_race_details(**bad)
    assert race_config.read_race_details()["activities_weeks_back"] == 0


def test_write_race_details_updates_llm_provider(isolated_race_config):
    race_config.write_race_details(**dict(VALID_DETAILS, llm_provider="claude"))
    assert race_config.read_race_details()["llm_provider"] == "claude"


def test_write_race_details_rejects_invalid_llm_provider(isolated_race_config):
    bad = dict(VALID_DETAILS, llm_provider="chatgpt")
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_race_details(**bad)
    assert race_config.read_race_details()["llm_provider"] == "deepseek"


def test_write_race_details_rejects_invalid_weeks_back_without_touching_file(isolated_race_config):
    original = isolated_race_config.read_text()
    with pytest.raises(race_config.RaceConfigError):
        race_config.write_race_details(**dict(VALID_DETAILS, activities_weeks_back=-5))
    assert isolated_race_config.read_text() == original
