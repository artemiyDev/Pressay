"""Deterministic, dependency-free transcript post-processing."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Mapping


# Only unambiguous hesitation sounds are removed by default.  Words such as
# Russian "ну"/"типа" and English "like" can carry meaning and are deliberately
# left intact.
DEFAULT_FILLERS: tuple[str, ...] = (
    "mm-hmm",
    "мм-м",
    "ммм",
    "эээ",
    "erm",
    "hmm",
    "uh",
    "um",
    "er",
    "эм",
    "мм",
    "ээ",
    "э",
)

PRESS_ENTER_PHRASES: frozenset[str] = frozenset(
    {
        "press enter",
        "hit enter",
        "нажми enter",
        "нажми энтер",
    }
)

_WHITESPACE_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION_RE = re.compile(r"\s+([,.;:!?%\]\)}»”’])")
_SPACE_AFTER_OPENING_RE = re.compile(r"([\[({«“‘])\s+")
_TRAILING_COMMAND_PUNCTUATION_RE = re.compile(r"[\s.,;:!?…]+$")
_VOICE_FORMATTING_COMMAND_RE = re.compile(
    r"(?<!\w)(?P<new_line>с новой строки)(?!\w)[.,;:!?…]*"
    r"|(?<!\w)(?P<paragraph>абзац)(?!\w)[.,;:!?…]*",
    flags=re.IGNORECASE,
)
_INVISIBLE_ARTIFACTS = str.maketrans("", "", "\ufeff\u200b\u2060")


@dataclass(frozen=True, slots=True)
class ProcessedText:
    """Result of transcript processing.

    ``press_enter`` is separate from text so an adapter can synthesize the key
    only after checking focus.  A command never leaks into the inserted text.
    """

    text: str
    press_enter: bool = False
    snippet_expanded: bool = False


def normalize_text(text: str) -> str:
    """Normalize Unicode and spacing without rewriting the user's words.

    NFC is used rather than compatibility normalization, so meaningful symbols
    are not folded into different characters.  Newlines and Unicode spaces are
    collapsed because a dictation insertion is a single text fragment.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")

    normalized = unicodedata.normalize("NFC", text).translate(_INVISIBLE_ARTIFACTS)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    normalized = _SPACE_BEFORE_PUNCTUATION_RE.sub(r"\1", normalized)
    normalized = _SPACE_AFTER_OPENING_RE.sub(r"\1", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def _literal_pattern(phrases: tuple[str, ...]) -> re.Pattern[str] | None:
    normalized_phrases = {
        normalize_text(phrase) for phrase in phrases if isinstance(phrase, str) and phrase.strip()
    }
    if not normalized_phrases:
        return None

    alternatives: list[str] = []
    for phrase in sorted(normalized_phrases, key=lambda item: (-len(item), item.casefold(), item)):
        escaped = re.escape(phrase)
        left = r"(?<!\w)" if phrase[0].isalnum() or phrase[0] == "_" else ""
        right = r"(?!\w)" if phrase[-1].isalnum() or phrase[-1] == "_" else ""
        alternatives.append(f"{left}(?:{escaped}){right}")
    return re.compile("|".join(alternatives), flags=re.IGNORECASE)


def remove_filler_words(
    text: str,
    fillers: tuple[str, ...] = DEFAULT_FILLERS,
) -> str:
    """Remove conservative RU/EN hesitation sounds as whole tokens."""

    normalized = normalize_text(text)
    pattern = _literal_pattern(fillers)
    if pattern is None:
        return normalized

    without_fillers, count = pattern.subn("", normalized)
    if count == 0:
        return normalized

    # Repair separators made orphaned by a removed filler.  This cleanup runs
    # only after an actual removal, so intentional punctuation elsewhere is not
    # broadly rewritten.
    without_fillers = re.sub(r"^[\s,;:]+", "", without_fillers)
    without_fillers = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", without_fillers)
    without_fillers = re.sub(r"[,;:]\s*([.!?])", r"\1", without_fillers)
    without_fillers = re.sub(r"\s+([,;:.!?])", r"\1", without_fillers)
    return normalize_text(without_fillers)


def replacement_key(text: str) -> str:
    """Return the exact key :func:`apply_replacements` matches text against."""

    return normalize_text(text).casefold()


def apply_replacements(text: str, replacements: Mapping[str, str]) -> str:
    """Apply literal, case-insensitive whole-token replacements in one pass.

    Keys are escaped and therefore cannot inject regular expressions.  Longest
    keys win, matching is independent of dictionary insertion order, and the
    one-pass implementation prevents replacement values from cascading into
    another rule.
    """

    normalized = normalize_text(text)
    rules: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key, value in replacements.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("replacement keys must be non-empty strings")
        if not isinstance(value, str):
            raise TypeError("replacement values must be strings")
        normalized_key = normalize_text(key)
        folded = normalized_key.casefold()
        if folded in seen:
            raise ValueError(f"duplicate replacement key after normalization: {key!r}")
        seen.add(folded)
        rules.append((normalized_key, value))

    if not rules:
        return normalized

    rules.sort(key=lambda item: (-len(item[0]), item[0].casefold(), item[0]))
    alternatives: list[str] = []
    values: dict[str, str] = {}
    for index, (key, value) in enumerate(rules):
        escaped = re.escape(key)
        left = r"(?<!\w)" if key[0].isalnum() or key[0] == "_" else ""
        right = r"(?!\w)" if key[-1].isalnum() or key[-1] == "_" else ""
        group = f"r{index}"
        alternatives.append(f"(?P<{group}>{left}(?:{escaped}){right})")
        values[group] = value

    pattern = re.compile("|".join(alternatives), flags=re.IGNORECASE)

    def replace(match: re.Match[str]) -> str:
        assert match.lastgroup is not None
        return values[match.lastgroup]

    return pattern.sub(replace, normalized)


def snippet_key(text: str) -> str:
    """Return the exact key :func:`expand_snippet` matches an utterance against.

    Trailing command punctuation (a spoken period, comma, ellipsis, ...) is
    stripped in addition to the :func:`replacement_key` normalization, since a
    snippet trigger is compared against a whole dictated utterance rather than
    a token inside one.
    """

    normalized = normalize_text(text)
    normalized = _TRAILING_COMMAND_PUNCTUATION_RE.sub("", normalized)
    return normalized.casefold()


def expand_snippet(text: str, snippets: Mapping[str, str]) -> tuple[str, bool]:
    """Expand a snippet only when its trigger is the entire utterance."""

    normalized = normalize_text(text)
    utterance_key = snippet_key(normalized)
    matches: dict[str, str] = {}

    for trigger, expansion in snippets.items():
        if not isinstance(trigger, str) or not trigger.strip():
            raise ValueError("snippet triggers must be non-empty strings")
        if not isinstance(expansion, str):
            raise TypeError("snippet expansions must be strings")
        key = snippet_key(trigger)
        if key in matches:
            raise ValueError(f"duplicate snippet trigger after normalization: {trigger!r}")
        matches[key] = expansion

    if utterance_key in matches:
        return matches[utterance_key], True
    return normalized, False


def is_press_enter_command(text: str, *, enabled: bool = False) -> bool:
    """Return true for an explicit whole-phrase Enter command when opted in."""

    if not enabled:
        return False
    return snippet_key(text) in PRESS_ENTER_PHRASES


def apply_voice_formatting(text: str) -> str:
    """Replace opted-in voice formatting commands inside one utterance.

    Commands are matched as complete words after the same Unicode and spacing
    normalization used by replacements.  Attached terminal punctuation is part
    of the command rather than dictated output.
    """

    normalized = normalize_text(text)

    def replace(match: re.Match[str]) -> str:
        return "\n" if match.lastgroup == "new_line" else "\n\n"

    formatted = _VOICE_FORMATTING_COMMAND_RE.sub(replace, normalized)
    return re.sub(r" *\n *", "\n", formatted)


def process_transcript(
    text: str,
    *,
    remove_fillers: bool = True,
    replacements: Mapping[str, str] | None = None,
    snippets: Mapping[str, str] | None = None,
    voice_press_enter: bool = False,
    voice_formatting: bool = False,
) -> ProcessedText:
    """Run the deterministic post-processing pipeline for one transcript."""

    normalized = normalize_text(text)
    if is_press_enter_command(normalized, enabled=voice_press_enter):
        return ProcessedText(text="", press_enter=True)

    if remove_fillers:
        normalized = remove_filler_words(normalized)

    expanded, did_expand = expand_snippet(normalized, snippets or {})
    if did_expand:
        return ProcessedText(text=expanded, snippet_expanded=True)

    if not voice_formatting:
        replaced = apply_replacements(expanded, replacements or {})
        return ProcessedText(text=replaced)

    formatted = apply_voice_formatting(expanded)
    # Formatting runs before replacements, so a user-defined replacement value
    # remains literal even when it contains a command word.
    replaced = "\n".join(
        apply_replacements(line, replacements or {}) for line in formatted.split("\n")
    )
    return ProcessedText(text=replaced)
