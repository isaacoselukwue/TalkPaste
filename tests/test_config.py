"""Tests for the settings model and config load/save."""

from __future__ import annotations

import json

from app.config import get_paths, load_settings, save_settings
from app.models import (
    ASRBackendKind,
    LanguageMode,
    ModelProfile,
    PasteMode,
    Settings,
)


def test_defaults():
    s = Settings()
    assert s.asr.profile is ModelProfile.BALANCED
    assert s.asr.resolved_model() == "base.en"
    assert s.asr.resolved_language() == "en"
    assert s.asr.compute_type == "int8"
    assert s.asr.device == "cpu"
    assert s.hotkeys.push_to_talk == "Ctrl+Alt+Space"
    assert s.hotkeys.cancel == "Esc"
    assert s.rewrite.enabled is False
    assert s.formatting.british_english is True
    assert s.paste.mode is PasteMode.PASTE


def test_profile_to_model_mapping():
    s = Settings()
    s.asr.profile = ModelProfile.FAST
    assert s.asr.resolved_model() == "tiny.en"
    s.asr.profile = ModelProfile.ACCURATE
    assert s.asr.resolved_model() == "small.en"


def test_multilingual_models():
    s = Settings()
    s.asr.language_mode = LanguageMode.MULTILINGUAL
    s.asr.profile = ModelProfile.BALANCED
    assert s.asr.resolved_model() == "base"
    assert s.asr.resolved_language() is None
    s.asr.language = "fr"
    assert s.asr.resolved_language() == "fr"


def test_custom_profile_uses_model_name():
    s = Settings()
    s.asr.profile = ModelProfile.CUSTOM
    s.asr.model_name = "distil-large-v3"
    assert s.asr.resolved_model() == "distil-large-v3"


def test_roundtrip_to_from_dict():
    s = Settings()
    s.asr.profile = ModelProfile.ACCURATE
    s.asr.backend = ASRBackendKind.WHISPER_CPP
    s.formatting.british_english = False
    s.paste.mode = PasteMode.COPY_ONLY
    restored = Settings.from_dict(json.loads(json.dumps(s.to_dict())))
    assert restored.asr.profile is ModelProfile.ACCURATE
    assert restored.asr.backend is ASRBackendKind.WHISPER_CPP
    assert restored.formatting.british_english is False
    assert restored.paste.mode is PasteMode.COPY_ONLY


def test_partial_dict_fills_defaults():
    s = Settings.from_dict({"asr": {"profile": "fast"}})
    assert s.asr.profile is ModelProfile.FAST
    # Untouched fields keep defaults.
    assert s.hotkeys.push_to_talk == "Ctrl+Alt+Space"
    assert s.asr.compute_type == "int8"


def test_unknown_keys_ignored():
    s = Settings.from_dict({"asr": {"profile": "fast", "bogus": 123}, "nonsense": True})
    assert s.asr.profile is ModelProfile.FAST


def test_vad_params_dict():
    s = Settings()
    params = s.asr.vad.to_faster_whisper_params()
    assert params["threshold"] == 0.5
    assert "min_silence_duration_ms" in params


def test_save_and_load(data_dir):
    paths = get_paths().ensure()
    s = Settings()
    s.asr.profile = ModelProfile.ACCURATE
    s.general.log_level = "DEBUG"
    save_settings(s, paths)
    assert paths.config_file.exists()

    loaded = load_settings(paths)
    assert loaded.asr.profile is ModelProfile.ACCURATE
    assert loaded.general.log_level == "DEBUG"


def test_load_missing_returns_defaults(data_dir):
    paths = get_paths()
    loaded = load_settings(paths)
    assert loaded.asr.profile is ModelProfile.BALANCED


def test_load_corrupt_returns_defaults(data_dir):
    paths = get_paths().ensure()
    paths.config_file.write_text("{ broken json", encoding="utf-8")
    loaded = load_settings(paths)
    assert loaded.asr.profile is ModelProfile.BALANCED


def test_env_override_data_dir(data_dir):
    # data_dir fixture set TALKPASTE_DATA_DIR -> get_paths must honour it.
    assert get_paths().data_dir == data_dir
