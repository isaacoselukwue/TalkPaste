"""Tests for dictionary, snippet and history persistence."""

from __future__ import annotations

from app.services.dictionary_store import DictionaryStore
from app.services.history_store import HistoryEntry, HistoryStore
from app.services.snippets_store import SnippetStore


def test_dictionary_add_get_remove(tmp_path):
    store = DictionaryStore(tmp_path / "dictionary.json")
    store.add("github", "GitHub")
    assert store.get("github") == "GitHub"
    # Case-insensitive key lookup.
    assert store.get("GitHub") == "GitHub"
    assert "GITHUB" in store
    assert store.remove("github") is True
    assert store.get("github") is None
    assert store.remove("nope") is False


def test_dictionary_persists_across_instances(tmp_path):
    path = tmp_path / "dictionary.json"
    first = DictionaryStore(path)
    first.add("javascript", "JavaScript")
    first.add("nodejs", "Node.js")
    second = DictionaryStore(path)
    assert second.entries == {"javascript": "JavaScript", "nodejs": "Node.js"}


def test_dictionary_case_insensitive_update_dedupes(tmp_path):
    store = DictionaryStore(tmp_path / "d.json")
    store.add("Github", "Github")
    store.add("github", "GitHub")  # should replace, not duplicate
    assert len(store) == 1
    assert store.get("github") == "GitHub"


def test_dictionary_ignores_corrupt_file(tmp_path):
    path = tmp_path / "dictionary.json"
    path.write_text("not json{", encoding="utf-8")
    store = DictionaryStore(path)
    assert store.entries == {}


def test_snippet_store_is_mapping(tmp_path):
    store = SnippetStore(tmp_path / "snippets.json")
    store.add("sign off", "Kind regards,\nIsaac")
    assert store.as_mapping()["sign off"].startswith("Kind regards")
    assert store.label == "snippets"


def test_history_add_and_recent(tmp_path):
    store = HistoryStore(tmp_path / "history.jsonl", max_entries=100)
    for i in range(3):
        store.add(HistoryEntry(text=f"entry {i}", backend="fake", model="m"))
    recent = store.recent(2)
    assert [e.text for e in recent] == ["entry 2", "entry 1"]
    assert store.count() == 3


def test_history_word_count():
    e = HistoryEntry(text="one two three")
    assert e.word_count == 3


def test_history_prunes_to_max(tmp_path):
    store = HistoryStore(tmp_path / "history.jsonl", max_entries=5)
    for i in range(20):
        store.add(HistoryEntry(text=f"e{i}"))
    entries = store.all()
    assert len(entries) == 5
    assert entries[0].text == "e15"
    assert entries[-1].text == "e19"


def test_history_skips_corrupt_lines(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text(
        '{"text": "good one"}\n'
        "this is not json\n"
        '{"text": "good two"}\n',
        encoding="utf-8",
    )
    store = HistoryStore(path, max_entries=0)
    texts = [e.text for e in store.all()]
    assert texts == ["good one", "good two"]


def test_history_clear(tmp_path):
    path = tmp_path / "history.jsonl"
    store = HistoryStore(path)
    store.add(HistoryEntry(text="x"))
    store.clear()
    assert store.count() == 0
