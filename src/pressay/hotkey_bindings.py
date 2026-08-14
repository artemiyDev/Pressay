"""User-configurable hotkey chords.

The module is deliberately free of Win32, Qt and platform imports: the same
binding model is validated when configuration loads (on any platform, including
the macOS build and CI) and consumed by the Windows low-level keyboard hook.

Cancel is intentionally not configurable.  It is bound to the recording
lifecycle and has to stay predictable, so ``Esc`` is fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


# Virtual-key values are constants of the Win32 ABI and safe to define on every
# platform.  Letters and digits reuse their ASCII uppercase codes.
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5

CTRL_KEYS = frozenset((VK_CONTROL, VK_LCONTROL, VK_RCONTROL))
WIN_KEYS = frozenset((VK_LWIN, VK_RWIN))
SHIFT_KEYS = frozenset((VK_SHIFT, VK_LSHIFT, VK_RSHIFT))
ALT_KEYS = frozenset((VK_MENU, VK_LMENU, VK_RMENU))

MODIFIER_KEYS: dict[str, frozenset[int]] = {
    "ctrl": CTRL_KEYS,
    "win": WIN_KEYS,
    "shift": SHIFT_KEYS,
    "alt": ALT_KEYS,
}
# Rendering order for a chord. Chosen so the shipped defaults keep reading the
# way users already see them: "Ctrl+Win" and "Shift+Alt+Z".
MODIFIER_ORDER = ("ctrl", "win", "shift", "alt")

# Only Win and Alt mean something pressed alone (Start menu, menu bar), so only
# they are worth withholding from the foreground app until the gesture
# resolves. Ctrl and Shift must never be withheld: they are constantly used
# with the mouse, whose events this hook does not observe, so a withheld Ctrl
# could not be replayed in time and Ctrl+click would break everywhere.
DEFERRED_MODIFIERS = ("win", "alt")

# Every two-modifier pair, in rendering order. Arbitrary chords are refused on
# purpose: a hold gesture is held down for seconds at a time, so it has to be
# one of a handful of combinations whose conflicts are known and documented.
HOLD_MODIFIER_PAIRS: tuple[tuple[str, str], ...] = (
    ("ctrl", "win"),
    ("ctrl", "shift"),
    ("ctrl", "alt"),
    ("win", "shift"),
    ("win", "alt"),
    ("shift", "alt"),
)

DISABLED = "none"


def _key_name_table() -> dict[str, int]:
    table: dict[str, int] = {"space": VK_SPACE}
    for offset in range(26):
        table[chr(ord("a") + offset)] = 0x41 + offset
    for digit in range(10):
        table[str(digit)] = 0x30 + digit
    for number in range(1, 13):
        table[f"f{number}"] = 0x70 + number - 1
    return table


KEY_NAMES: dict[str, int] = _key_name_table()


class HotkeyBindingError(ValueError):
    """Raised for a chord or a binding set that cannot be used safely."""


def _normalize(text: object, setting: str) -> str:
    if not isinstance(text, str):
        raise HotkeyBindingError(f"{setting} must be a string")
    return text.strip().casefold()


def _label(part: str) -> str:
    return part.capitalize()


def parse_key(text: object, setting: str) -> str:
    """Validate a single non-modifier key name such as ``z`` or ``space``."""

    name = _normalize(text, setting)
    if name in MODIFIER_KEYS:
        raise HotkeyBindingError(f"{setting} must be a regular key, not a modifier")
    if name not in KEY_NAMES:
        raise HotkeyBindingError(f"{setting} is not a supported key: {name!r}")
    return name


@dataclass(frozen=True, slots=True)
class Chord:
    """One or more modifier families plus exactly one regular key."""

    modifiers: frozenset[str]
    key_name: str

    @property
    def vk_code(self) -> int:
        return KEY_NAMES[self.key_name]

    @property
    def ordered_modifiers(self) -> tuple[str, ...]:
        return tuple(name for name in MODIFIER_ORDER if name in self.modifiers)

    @property
    def modifier_key_sets(self) -> tuple[frozenset[int], ...]:
        return tuple(MODIFIER_KEYS[name] for name in self.ordered_modifiers)

    def text(self) -> str:
        """Canonical form written to config.json and shown in the UI field."""

        return "+".join((*self.ordered_modifiers, self.key_name))

    def label(self) -> str:
        """Human-readable form for status messages and notifications."""

        return "+".join(_label(part) for part in (*self.ordered_modifiers, self.key_name))


def parse_chord(text: object, setting: str) -> Chord | None:
    """Parse ``"shift+alt+z"``; ``"none"`` and ``""`` disable the action."""

    raw = _normalize(text, setting)
    if raw in {"", DISABLED}:
        return None
    parts = [part.strip() for part in raw.split("+")]
    if any(not part for part in parts):
        raise HotkeyBindingError(f"{setting} has an empty part: {raw!r}")

    modifiers: set[str] = set()
    keys: list[str] = []
    for part in parts:
        if part in MODIFIER_KEYS:
            if part in modifiers:
                raise HotkeyBindingError(f"{setting} repeats the modifier {part!r}")
            modifiers.add(part)
        else:
            keys.append(parse_key(part, setting))
    if not modifiers:
        raise HotkeyBindingError(f"{setting} needs at least one modifier")
    if len(keys) != 1:
        raise HotkeyBindingError(f"{setting} needs exactly one regular key")
    return Chord(frozenset(modifiers), keys[0])


def parse_hold_modifiers(text: object, setting: str = "hold_modifiers") -> tuple[str, str]:
    """Parse the push-to-talk pair, restricted to the six known combinations."""

    raw = _normalize(text, setting)
    parts = tuple(part.strip() for part in raw.split("+") if part.strip())
    names = frozenset(parts)
    if len(parts) != 2 or len(names) != 2 or not names <= set(MODIFIER_KEYS):
        raise HotkeyBindingError(
            f"{setting} must be two different modifiers out of ctrl, win, shift, alt"
        )
    for pair in HOLD_MODIFIER_PAIRS:
        if frozenset(pair) == names:
            return pair
    raise HotkeyBindingError(f"{setting} is not a supported combination: {raw!r}")


def _default_paste() -> Chord:
    return Chord(frozenset({"shift", "alt"}), "z")


def _default_copy() -> Chord:
    return Chord(frozenset({"shift", "alt"}), "x")


@dataclass(frozen=True, slots=True)
class HotkeyBindings:
    """The complete, validated set of gestures. Defaults are today's chords."""

    hold_modifiers: tuple[str, str] = ("ctrl", "win")
    toggle_key: str | None = "space"
    paste_last: Chord | None = field(default_factory=_default_paste)
    copy_last: Chord | None = field(default_factory=_default_copy)
    push_to_talk: bool = True

    @property
    def hold_key_sets(self) -> tuple[frozenset[int], frozenset[int]]:
        first, second = self.hold_modifiers
        return MODIFIER_KEYS[first], MODIFIER_KEYS[second]

    @property
    def deferred_modifier(self) -> str | None:
        """Which held modifier must be withheld until the gesture resolves.

        ``None`` means neither key in the pair does anything on its own, so
        nothing is withheld and both reach the foreground app immediately.
        """

        for name in DEFERRED_MODIFIERS:
            if name in self.hold_modifiers:
                return name
        return None

    @property
    def toggle_vk(self) -> int | None:
        return None if self.toggle_key is None else KEY_NAMES[self.toggle_key]

    def hold_label(self) -> str:
        return "+".join(_label(name) for name in self.hold_modifiers)

    def toggle_label(self) -> str | None:
        if self.toggle_key is None:
            return None
        return f"{self.hold_label()}+{_label(self.toggle_key)}"

    def to_mapping(self) -> dict[str, Any]:
        """Canonical JSON representation; round-trips through :func:`from_mapping`."""

        return {
            "hold_modifiers": "+".join(self.hold_modifiers),
            "toggle_key": DISABLED if self.toggle_key is None else self.toggle_key,
            "paste_last": DISABLED if self.paste_last is None else self.paste_last.text(),
            "copy_last": DISABLED if self.copy_last is None else self.copy_last.text(),
            "push_to_talk": self.push_to_talk,
        }


def _parse_optional_key(value: object, setting: str) -> str | None:
    raw = _normalize(value, setting)
    if raw in {"", DISABLED}:
        return None
    return parse_key(raw, setting)


def validate(bindings: HotkeyBindings) -> HotkeyBindings:
    """Reject binding sets that collide with each other or with cancel."""

    parse_hold_modifiers("+".join(bindings.hold_modifiers))
    chords = {"paste_last": bindings.paste_last, "copy_last": bindings.copy_last}
    seen: dict[tuple[frozenset[str], str], str] = {}
    hold_pair = frozenset(bindings.hold_modifiers)
    for setting, chord in chords.items():
        if chord is None:
            continue
        identity = (chord.modifiers, chord.key_name)
        if identity in seen:
            raise HotkeyBindingError(
                f"{setting} duplicates {seen[identity]}: {chord.text()}"
            )
        seen[identity] = setting
        if (
            bindings.toggle_key is not None
            and chord.modifiers == hold_pair
            and chord.key_name == bindings.toggle_key
        ):
            raise HotkeyBindingError(
                f"{setting} duplicates the toggle gesture: {chord.text()}"
            )
    return bindings


def from_mapping(raw: object) -> HotkeyBindings:
    """Build bindings from the ``hotkeys`` object; missing keys use defaults."""

    if raw is None:
        return HotkeyBindings()
    if not isinstance(raw, Mapping):
        raise HotkeyBindingError("hotkeys must be a JSON object")

    defaults = HotkeyBindings()
    push_to_talk = raw.get("push_to_talk", defaults.push_to_talk)
    # Checked exactly, like every other boolean setting: JSON 0/1 must not
    # silently switch a gesture on or off.
    if type(push_to_talk) is not bool:
        raise HotkeyBindingError("push_to_talk must be a boolean")

    return validate(
        HotkeyBindings(
            hold_modifiers=parse_hold_modifiers(
                raw.get("hold_modifiers", "+".join(defaults.hold_modifiers))
            ),
            toggle_key=_parse_optional_key(
                raw.get("toggle_key", defaults.toggle_key or DISABLED), "toggle_key"
            ),
            paste_last=parse_chord(
                raw.get("paste_last", defaults.paste_last.text()), "paste_last"
            ),
            copy_last=parse_chord(
                raw.get("copy_last", defaults.copy_last.text()), "copy_last"
            ),
            push_to_talk=push_to_talk,
        )
    )


__all__ = [
    "ALT_KEYS",
    "CTRL_KEYS",
    "Chord",
    "DEFERRED_MODIFIERS",
    "DISABLED",
    "HOLD_MODIFIER_PAIRS",
    "HotkeyBindingError",
    "HotkeyBindings",
    "KEY_NAMES",
    "MODIFIER_KEYS",
    "MODIFIER_ORDER",
    "SHIFT_KEYS",
    "WIN_KEYS",
    "from_mapping",
    "parse_chord",
    "parse_hold_modifiers",
    "parse_key",
    "validate",
]
