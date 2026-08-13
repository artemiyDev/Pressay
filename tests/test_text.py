from __future__ import annotations

from pressay.text import (
    apply_replacements,
    expand_snippet,
    is_press_enter_command,
    normalize_text,
    process_transcript,
    remove_filler_words,
)


def test_normalize_text_handles_unicode_spaces_and_punctuation_safely():
    decomposed = "Cafe\u0301\u00a0 ,\tмир ! Версия 1.2"

    assert normalize_text(decomposed) == "Caf\u00e9, мир! Версия 1.2"


def test_filler_removal_is_conservative_for_real_ru_en_words():
    assert remove_filler_words("Эм, привет, um, world.") == "привет, world."
    assert remove_filler_words("Ну, это типа данных и I like it.") == (
        "Ну, это типа данных и I like it."
    )


def test_replacements_are_literal_longest_first_and_non_cascading():
    replacements = {
        "c++": "C plus plus",
        "вайт": "WRONG",
        "вайт маркет": "White.Market",
        "alpha": "beta",
        "beta": "gamma",
    }

    result = apply_replacements("C++ и вайт маркет; alpha", replacements)

    assert result == "C plus plus и White.Market; beta"


def test_replacement_does_not_change_part_of_a_larger_word():
    assert apply_replacements("cat concatenate CAT", {"cat": "dog"}) == (
        "dog concatenate dog"
    )


def test_snippet_requires_the_whole_phrase_and_preserves_expansion():
    snippets = {"моя подпись": "С уважением,\nАнна"}

    assert expand_snippet("Моя подпись.", snippets) == ("С уважением,\nАнна", True)
    assert expand_snippet("вставь моя подпись", snippets) == (
        "вставь моя подпись",
        False,
    )


def test_press_enter_is_opt_in_and_whole_phrase_only():
    assert not is_press_enter_command("Нажми энтер", enabled=False)
    assert is_press_enter_command("Нажми энтер!", enabled=True)
    assert not is_press_enter_command("пожалуйста, нажми энтер", enabled=True)


def test_pipeline_keeps_enter_action_out_of_inserted_text():
    command = process_transcript("press enter", voice_press_enter=True)
    text = process_transcript(
        "Эм, скажи вайт маркет",
        replacements={"вайт маркет": "White.Market"},
    )

    assert command.text == ""
    assert command.press_enter is True
    assert text.text == "скажи White.Market"
    assert text.press_enter is False
