"""Unit tests for the deterministic formatting pipeline."""

from __future__ import annotations

from app.models import FormattingSettings
from app.services.formatter import Formatter


def make(**overrides) -> FormattingSettings:
    base = FormattingSettings()
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_filler_removal_default():
    f = Formatter(make())
    assert f.remove_fillers("um hello uh there") == "hello there"
    assert f.remove_fillers("er well hmm okay") == "well okay"


def test_filler_removal_preserves_real_words():
    f = Formatter(make())
    # "like" and "you know" are NOT default fillers (they carry meaning).
    assert f.remove_fillers("I like this you know") == "I like this you know"


def test_extra_fillers():
    f = Formatter(make(extra_fillers=["you know", "basically"]))
    assert f.remove_fillers("basically you know it works") == "it works"


def test_british_english_case_preserving():
    f = Formatter(make())
    assert f.to_british("color") == "colour"
    assert f.to_british("Color") == "Colour"
    assert f.to_british("COLOR") == "COLOUR"
    assert f.to_british("organize the center") == "organise the centre"
    # Ambiguous words are left alone.
    assert f.to_british("check the program") == "check the program"


def test_capitalization_and_i():
    f = Formatter(make())
    assert f.normalize("hello. world") == "Hello. World"
    assert f.normalize("i think i am right") == "I think I am right"
    assert f.normalize("i'm sure i'll go") == "I'm sure I'll go"


def test_whitespace_normalisation():
    f = Formatter(make())
    assert f.normalize("hello    world") == "Hello world"
    assert f.normalize("word  ,  next") == "Word, next"
    # 3+ blank lines collapse to one, and the first letter of each line is
    # capitalised.
    assert f.normalize("a\n\n\n\nb") == "A\n\nB"


def test_dictionary_replacement():
    f = Formatter(make(), dictionary={"github": "GitHub", "javascript": "JavaScript"})
    assert f.apply_dictionary("i love github and javascript") == "i love GitHub and JavaScript"
    # Whole-word only: "githubs" is not replaced.
    assert f.apply_dictionary("many githubs") == "many githubs"


def test_snippet_expansion_longest_first():
    f = Formatter(make(), snippets={"my email": "isaac@example.com", "my": "MINE"})
    assert f.apply_snippets("send my email now") == "send isaac@example.com now"


def test_full_pipeline():
    f = Formatter(
        make(),
        dictionary={"github": "GitHub"},
        snippets={"sign off": "Kind regards"},
    )
    out = f.format("um i pushed to github comma sign off period")
    assert out == "I pushed to GitHub, Kind regards."


def test_full_pipeline_developer_mode():
    f = Formatter(make(developer_mode=True))
    out = f.format("the function is called snake case get user name")
    assert "get_user_name" in out


def test_toggles_disable_steps():
    f = Formatter(make(remove_fillers=False, enable_commands=False,
                       british_english=False, auto_capitalize=False,
                       normalize_whitespace=False, trim_trailing_space=False))
    # Nothing should change except being returned as-is.
    assert f.format("um hello comma color") == "um hello comma color"


def test_none_input():
    f = Formatter(make())
    assert f.format(None) == ""
