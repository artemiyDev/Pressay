from __future__ import annotations

import json
import os

import pytest

from pressay.config import AppConfig, ConfigError, config_path, legacy_config_path


@pytest.fixture(autouse=True)
def _windows_config_paths(monkeypatch):
    monkeypatch.setattr("pressay.platform_support.sys.platform", "win32")


def test_default_path_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert config_path() == tmp_path / "Pressay" / "config.json"


def test_round_trip_utf8_settings_to_default_path(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    config = AppConfig(
        model="medium",
        language="ru",
        microphone="Микрофон USB",
        auto_insert=False,
        smart_spacing=False,
        remove_fillers=False,
        voice_press_enter=True,
        resource_mode="balanced",
        snippets={"моя подпись": "С уважением, Анна"},
        replacements={"вайт маркет": "White.Market"},
    )

    saved_to = config.save()

    assert saved_to == config_path()
    assert AppConfig.load() == config
    assert "Микрофон USB" in saved_to.read_text(encoding="utf-8")


def test_default_load_reads_previous_product_config_when_pressay_is_missing(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    previous = legacy_config_path()
    previous.parent.mkdir(parents=True)
    previous.write_text(
        json.dumps({"language": "ru", "auto_insert": False}),
        encoding="utf-8",
    )

    loaded = AppConfig.load()

    assert loaded.language == "ru"
    assert loaded.auto_insert is False
    assert not config_path().exists()


def test_missing_file_loads_independent_defaults(tmp_path):
    first = AppConfig.load(tmp_path / "missing.json")
    second = AppConfig.load(tmp_path / "missing.json")

    first.snippets["x"] = "y"

    assert second.snippets == {}


def test_legacy_numeric_microphone_index_remains_loadable(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"microphone": 7}), encoding="utf-8")

    config = AppConfig.load(target)

    assert config.microphone == 7
    config.save(target)
    assert AppConfig.load(target).microphone == 7


def test_config_without_smart_spacing_uses_enabled_default(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"model": "small"}), encoding="utf-8")

    config = AppConfig.load(target)

    assert config.smart_spacing is True


def test_invalid_json_and_known_setting_types_fail_closed(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("{oops", encoding="utf-8")
    with pytest.raises(ConfigError):
        AppConfig.load(target)

    target.write_text(json.dumps({"auto_insert": 1}), encoding="utf-8")
    with pytest.raises(ConfigError, match="auto_insert"):
        AppConfig.load(target)

    target.write_text(json.dumps({"smart_spacing": 1}), encoding="utf-8")
    with pytest.raises(ConfigError, match="smart_spacing"):
        AppConfig.load(target)

    target.write_text(json.dumps({"language": "de"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="auto, ru, en"):
        AppConfig.load(target)

    target.write_text(json.dumps({"microphone": True}), encoding="utf-8")
    with pytest.raises(ConfigError, match="microphone"):
        AppConfig.load(target)

    target.write_text(json.dumps({"resource_mode": "maximum"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="instant, balanced, eco"):
        AppConfig.load(target)


def test_replacements_with_colliding_keys_after_normalization_fail_closed():
    with pytest.raises(ConfigError, match="replacements"):
        AppConfig.from_dict({"replacements": {"Foo": "a", "foo": "b"}})

    # Collision is detected after collapsing internal whitespace, matching
    # apply_replacements' own normalization -- not just a raw casefold.
    with pytest.raises(ConfigError, match="replacements"):
        AppConfig.from_dict(
            {"replacements": {"фаст  апи": "FastAPI", "фаст апи": "x"}}
        )


def test_snippets_with_colliding_keys_after_trailing_punctuation_strip_fail_closed():
    # snippet_key trims trailing command punctuation, so "привет." and
    # "привет" collide even though they differ as raw JSON keys.
    with pytest.raises(ConfigError, match="snippets"):
        AppConfig.from_dict({"snippets": {"привет.": "a", "привет": "b"}})


def test_key_that_normalizes_to_empty_string_fails_closed():
    # A zero-width space is not blank by str.strip(), but normalize_text
    # removes it as an invisible artifact, leaving an unusable empty key.
    zero_width_space = chr(0x200B)
    with pytest.raises(ConfigError, match="replacements"):
        AppConfig.from_dict({"replacements": {zero_width_space: "a"}})


def test_non_colliding_replacements_and_snippets_load_unchanged():
    raw = {
        "replacements": {"вайт маркет": "White.Market", "фаст апи": "FastAPI"},
        "snippets": {"моя подпись": "С уважением, Анна"},
    }

    config = AppConfig.from_dict(raw)

    assert config.replacements == raw["replacements"]
    assert config.snippets == raw["snippets"]


def test_failed_replace_keeps_previous_file_and_removes_temp(monkeypatch, tmp_path):
    target = tmp_path / "config.json"
    target.write_text('{"model": "old"}', encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(ConfigError, match="simulated replace failure"):
        AppConfig(model="new").save(target)

    assert target.read_text(encoding="utf-8") == '{"model": "old"}'
    assert list(tmp_path.glob(".config.json.*.tmp")) == []
