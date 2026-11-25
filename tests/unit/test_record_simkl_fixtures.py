"""The fixture recorder's scrubbing and writing, which run before anything is committed."""

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "record_simkl_fixtures.py"
spec = importlib.util.spec_from_file_location("record_simkl_fixtures", SCRIPT)
assert spec and spec.loader
recorder = importlib.util.module_from_spec(spec)
sys.modules["record_simkl_fixtures"] = recorder
spec.loader.exec_module(recorder)

ACCESS = "simkl_at_REALACCESSTOKEN"
REFRESH = "simkl_rt_REALREFRESHTOKEN"


def test_users_settings_loses_everything_that_identifies_the_account():
    scrubbed = recorder.scrub(
        {
            "user": {
                "name": "realname",
                "joined_at": "2015-03-04T00:00:00Z",
                "gender": "Female",
                "avatar": "https://simkl.in/avatars/12/real_100.jpg",
                "email": "me@real.example",
                "bio": "my story",
                "loc": "Paris",
                "age": 33,
            },
            "account": {"id": 987654, "timezone": "Europe/Paris", "type": "pro"},
        },
        names_to_hide=("realname",),
    )
    assert scrubbed == {
        "user": {
            "name": "curio-fixture-user",
            "joined_at": "2020-01-01T00:00:00Z",
            "gender": "",
            "avatar": "https://example.invalid/avatar.jpg",
            "email": "user@example.invalid",
            "bio": "",
            "loc": "",
            "age": None,
        },
        "account": {"id": 424242, "timezone": "Europe/Paris", "type": "pro"},
    }
    assert "realname" not in json.dumps(scrubbed) and "real.example" not in json.dumps(scrubbed)


def test_titles_are_public_and_keep_their_names_even_when_they_match_the_username():
    body = {
        "shows": [
            {
                "user_rating": 8,
                "show": {"title": "alice", "ids": {"simkl": 1, "tmdb": "2"}},
            },
            {"show": {"title": "Alice in Wonderland", "ids": {"simkl": 3}}},
        ]
    }
    scrubbed = recorder.scrub(body, names_to_hide=("alice",))
    titles = [row["show"]["title"] for row in scrubbed["shows"]]
    # An exact match of the username is hidden, as a last resort. A longer title is untouched.
    assert titles == ["curio-fixture-user", "Alice in Wonderland"]
    assert scrubbed["shows"][0]["user_rating"] == 8
    assert scrubbed["shows"][0]["show"]["ids"] == {"simkl": 1, "tmdb": "2"}


def test_tokens_and_secrets_are_removed_wherever_they_appear():
    body = {"note": f"token {ACCESS} and {REFRESH}", "nested": [{"x": f"s3cret-{ACCESS}"}]}
    scrubbed = recorder.scrub(body, secrets_to_hide=(ACCESS, REFRESH, "s3cret"))
    dumped = json.dumps(scrubbed)
    assert ACCESS not in dumped and REFRESH not in dumped and "s3cret" not in dumped


def test_fixtures_are_written_as_pretty_sorted_json(tmp_path):
    path = recorder.write_fixture(tmp_path / "out", "x.json", {"b": 1, "a": {"d": 2, "c": [3]}})
    text = path.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert list(json.loads(text)) == ["a", "b"]
    assert text.index('"a"') < text.index('"b"') and '\n  "a"' in text


def test_the_recorder_writes_the_files_the_tests_load():
    # Every fixture the Simkl tests read is a file the recorder writes (and the README lists).
    source = SCRIPT.read_text(encoding="utf-8")
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "simkl"
    for path in sorted(fixtures.glob("*.json")):
        stem = path.stem
        written = (
            stem in {"activities", "users_settings", "oauth_token"}
            or (stem.startswith("all_items_") and "all_items_{simkl_type}" in source)
            or (stem.startswith("ratings_") and "ratings_{simkl_type}" in source)
            or (stem.startswith("ids_only_") and "ids_only_{simkl_type}" in source)
        )
        assert written, f"{path.name} is not written by the recorder"
        if stem in {"activities", "users_settings", "oauth_token"}:
            assert f'"{path.name}"' in source
    for simkl_type in recorder.TYPES:
        for prefix in ("all_items_", "ratings_", "ids_only_"):
            assert (fixtures / f"{prefix}{simkl_type}.json").is_file()
