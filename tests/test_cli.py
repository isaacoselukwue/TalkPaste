"""Tests for the Typer CLI via CliRunner (no real model/mic needed)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import app.services.controller as controller_mod
from app.cli import app
from tests.conftest import FakeASRBackend

runner = CliRunner()


@pytest.fixture()
def fake_backend(monkeypatch):
    monkeypatch.setattr(
        controller_mod,
        "create_asr_backend",
        lambda s: FakeASRBackend(s, transcript="hello comma world period"),
    )


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "TalkPaste" in result.stdout


def test_config_path(data_dir):
    result = runner.invoke(app, ["config-path"])
    assert result.exit_code == 0
    assert str(data_dir) in result.stdout


def test_diagnose_platform(data_dir):
    result = runner.invoke(app, ["diagnose-platform"])
    assert result.exit_code == 0
    assert "platform" in result.stdout.lower()
    # One of the known kinds is reported.
    assert any(k in result.stdout for k in ("linux_x11", "linux_wayland", "windows", "macos", "unknown"))


def test_transcribe_file(data_dir, fake_backend, sample_wav):
    result = runner.invoke(app, ["transcribe", str(sample_wav)])
    assert result.exit_code == 0, result.stdout
    assert "Hello, world." in result.stdout


def test_transcribe_json(data_dir, fake_backend, sample_wav):
    result = runner.invoke(app, ["transcribe", str(sample_wav), "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["text"] == "Hello, world."
    assert payload["backend"] == "fake"


def test_transcribe_no_format(data_dir, fake_backend, sample_wav):
    result = runner.invoke(app, ["transcribe", str(sample_wav), "--no-format"])
    assert result.exit_code == 0, result.stdout
    # Without formatting the raw canned transcript survives.
    assert "hello comma world period" in result.stdout


def test_transcribe_missing_file(data_dir):
    result = runner.invoke(app, ["transcribe", "does-not-exist.wav"])
    assert result.exit_code != 0


def test_init_config(data_dir):
    result = runner.invoke(app, ["init-config"])
    assert result.exit_code == 0
    from app.config import get_paths

    paths = get_paths()
    assert paths.config_file.exists()
    assert paths.dictionary_file.exists()
    assert paths.snippets_file.exists()


def test_history_empty(data_dir):
    result = runner.invoke(app, ["history"])
    assert result.exit_code == 0
    assert "No transcripts" in result.stdout


def test_history_shows_entries(data_dir):
    from app.config import get_paths
    from app.services.history_store import HistoryEntry, HistoryStore

    store = HistoryStore(get_paths().ensure().history_file)
    store.add(HistoryEntry(text="first note", backend="fake", model="m"))
    store.add(HistoryEntry(text="second note", backend="fake", model="m"))

    result = runner.invoke(app, ["history", "--last", "5"])
    assert result.exit_code == 0
    assert "second note" in result.stdout and "first note" in result.stdout

    result_json = runner.invoke(app, ["history", "--json"])
    assert result_json.exit_code == 0
    payload = json.loads(result_json.stdout)
    assert payload[0]["text"] == "second note"  # newest first


def test_history_path(data_dir):
    from app.config import get_paths

    result = runner.invoke(app, ["history", "--path"])
    assert result.exit_code == 0
    assert str(get_paths().history_file) in result.stdout


def test_list_audio_devices_handles_missing_backend(data_dir):
    # sounddevice is not installed in the test env; the command must exit
    # gracefully with a non-zero code, not crash with a traceback.
    result = runner.invoke(app, ["list-audio-devices"])
    assert result.exit_code in (0, 2)
