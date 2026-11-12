"""Four-level HID keyboard layouts (es_ES, en_US) with dead-key support.

The firmware's ``kb_text`` op goes through the Arduino ``Keyboard.write()``
ASCII map, which cannot express dead keys, the ISO 102nd key or AltGr
characters on the Spanish layout. This module models both layouts across the
four USB-HID shift levels (plain ``0x00``, Shift ``0x02``, AltGr ``0x40``,
Shift+AltGr ``0x42``), the ISO 102nd key (usage ``0x64``) and the Spanish
dead keys, and turns a whole string into raw ``"MM:UU"`` HID steps for the
firmware's ``kb_raw`` op after validating every character up front.
"""
from __future__ import annotations

# A HID step is a (modifier byte, usage byte) pair.
Step = tuple[int, int]

MOD_NONE = 0x00
MOD_SHIFT = 0x02
MOD_ALTGR = 0x40
MOD_SHIFT_ALTGR = 0x42

USAGE_SPACE = 0x2C
USAGE_ENTER = 0x28
USAGE_TAB = 0x2B
USAGE_BACKSPACE = 0x2A
# ISO 102nd key (the extra key next to left Shift on ISO 105 keyboards).
USAGE_ISO_EXTRA = 0x64

# Spanish ISO 105 dead keys: grave/circumflex share the key right of P
# (0x2f), acute/diaeresis the key right of Ñ (0x34); Shift picks the second
# accent of each key.
DEAD_GRAVE: Step = (MOD_NONE, 0x2F)
DEAD_CIRCUMFLEX: Step = (MOD_SHIFT, 0x2F)
DEAD_ACUTE: Step = (MOD_NONE, 0x34)
DEAD_DIAERESIS: Step = (MOD_SHIFT, 0x34)
# AltGr+4 is the dead tilde of the Spanish layout.
DEAD_TILDE: Step = (MOD_ALTGR, 0x21)

_CONTROL_STEPS: dict[str, tuple[Step, ...]] = {
    "\n": ((MOD_NONE, USAGE_ENTER),),
    "\t": ((MOD_NONE, USAGE_TAB),),
    "\b": ((MOD_NONE, USAGE_BACKSPACE),),
}

_VOWELS = {"a": 0x04, "e": 0x08, "i": 0x0C, "o": 0x12, "u": 0x18}
_ACCENTED_LOWER = {
    "`": "àèìòù",
    "^": "âêîôû",
    "´": "áéíóú",
    "¨": "äëïöü",
}
_ACCENTED_UPPER = {
    "`": "ÀÈÌÒÙ",
    "^": "ÂÊÎÔÛ",
    "´": "ÁÉÍÓÚ",
    "¨": "ÄËÏÖÜ",
}


def _steps(*pairs: Step) -> tuple[Step, ...]:
    return tuple(pairs)


def _build_es_es() -> dict[str, tuple[Step, ...]]:
    """Spanish ISO 105 layout: printable ASCII, AltGr level and dead keys.

    The ASCII rows mirror ``KeyboardLayout_es_ES.cpp`` so ``kb_text`` and
    ``kb_raw`` send the same chords for a character both paths can express.
    ``<`` and ``>`` sit on the ISO 102nd key (usage 0x64), which the Arduino
    table maps onto other usages; the raw steps use the physical key.
    """
    table: dict[str, tuple[Step, ...]] = {}

    def put(char: str, mod: int, usage: int) -> None:
        table[char] = ((mod, usage),)

    put(" ", MOD_NONE, USAGE_SPACE)
    for char, (mod, usage) in {
        "!": (MOD_SHIFT, 0x1E), '"': (MOD_SHIFT, 0x1F), "#": (MOD_ALTGR, 0x20),
        "$": (MOD_SHIFT, 0x21), "%": (MOD_SHIFT, 0x22), "&": (MOD_SHIFT, 0x23),
        "'": (MOD_NONE, 0x2D), "(": (MOD_SHIFT, 0x25), ")": (MOD_SHIFT, 0x26),
        "*": (MOD_SHIFT, 0x30), "+": (MOD_NONE, 0x30), ",": (MOD_NONE, 0x36),
        "-": (MOD_NONE, 0x38), ".": (MOD_NONE, 0x37), "/": (MOD_SHIFT, 0x24),
        ":": (MOD_SHIFT, 0x37), ";": (MOD_SHIFT, 0x36),
        "=": (MOD_SHIFT, 0x27), "?": (MOD_SHIFT, 0x2D), "@": (MOD_ALTGR, 0x1F),
        "[": (MOD_ALTGR, 0x2F), "]": (MOD_ALTGR, 0x30),
        "_": (MOD_SHIFT, 0x38), "{": (MOD_ALTGR, 0x34), "|": (MOD_ALTGR, 0x1E),
        "}": (MOD_ALTGR, 0x31),
        "<": (MOD_NONE, USAGE_ISO_EXTRA), ">": (MOD_SHIFT, USAGE_ISO_EXTRA),
        # The backslash is AltGr+º on Spanish Windows and macOS and AltGr+LSGT
        # on Linux; AltGr+º is the chord the firmware's own ASCII map sends,
        # so raw typing stays identical to what kb_text types for '\'.
        "\\": (MOD_ALTGR, 0x35),
    }.items():
        put(char, mod, usage)

    for digit, usage in enumerate(
        (0x27, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26)
    ):
        put(str(digit), MOD_NONE, usage)
    for lower, usage in zip("abcdefghijklmnopqrstuvwxyz", range(0x04, 0x1E)):
        put(lower, MOD_NONE, usage)
        put(lower.upper(), MOD_SHIFT, usage)

    # Characters the Spanish layout carries outside the ASCII table. The
    # inverted marks sit on the key right of the apostrophe (0x2e), the
    # ordinals on the key left of 1 (0x35), ç/Ç on the key next to Enter
    # (0x31) and the euro sign on AltGr+E, as printed on Spanish ISO 105.
    extras = {
        "¡": (MOD_NONE, 0x2E),
        "¿": (MOD_SHIFT, 0x2E),
        "º": (MOD_NONE, 0x35),
        "ª": (MOD_SHIFT, 0x35),
        "·": (MOD_SHIFT, 0x20),
        "ñ": (MOD_NONE, 0x33),
        "Ñ": (MOD_SHIFT, 0x33),
        "ç": (MOD_NONE, 0x31),
        "Ç": (MOD_SHIFT, 0x31),
        "€": (MOD_ALTGR, 0x08),
    }
    for char, (mod, usage) in extras.items():
        put(char, mod, usage)

    space: Step = (MOD_NONE, USAGE_SPACE)
    # Standalone dead accents: the dead key followed by a space.
    for mark, dead in (
        ("`", DEAD_GRAVE),
        ("^", DEAD_CIRCUMFLEX),
        ("´", DEAD_ACUTE),
        ("¨", DEAD_DIAERESIS),
    ):
        table[mark] = _steps(dead, space)
    # Composed vowels: dead key followed by the vowel (Shift for uppercase).
    for mark, dead in (
        ("`", DEAD_GRAVE),
        ("^", DEAD_CIRCUMFLEX),
        ("´", DEAD_ACUTE),
        ("¨", DEAD_DIAERESIS),
    ):
        for index, vowel in enumerate("aeiou"):
            table[_ACCENTED_LOWER[mark][index]] = _steps(dead, (MOD_NONE, _VOWELS[vowel]))
            table[_ACCENTED_UPPER[mark][index]] = _steps(dead, (MOD_SHIFT, _VOWELS[vowel]))
    # Tilde: dead tilde plus space or the vowel.
    table["~"] = _steps(DEAD_TILDE, space)
    for lower, upper, vowel in (("ã", "Ã", "a"), ("õ", "Õ", "o")):
        table[lower] = _steps(DEAD_TILDE, (MOD_NONE, _VOWELS[vowel]))
        table[upper] = _steps(DEAD_TILDE, (MOD_SHIFT, _VOWELS[vowel]))
    return table


def _build_en_us() -> dict[str, tuple[Step, ...]]:
    """US ANSI 104 layout: printable ASCII at the plain and Shift levels."""
    table: dict[str, tuple[Step, ...]] = {}

    def put(char: str, mod: int, usage: int) -> None:
        table[char] = ((mod, usage),)

    put(" ", MOD_NONE, USAGE_SPACE)
    for char, (mod, usage) in {
        "!": (MOD_SHIFT, 0x1E), "@": (MOD_SHIFT, 0x1F), "#": (MOD_SHIFT, 0x20),
        "$": (MOD_SHIFT, 0x21), "%": (MOD_SHIFT, 0x22), "^": (MOD_SHIFT, 0x23),
        "&": (MOD_SHIFT, 0x24), "*": (MOD_SHIFT, 0x25), "(": (MOD_SHIFT, 0x26),
        ")": (MOD_SHIFT, 0x27), "-": (MOD_NONE, 0x2D), "_": (MOD_SHIFT, 0x2D),
        "=": (MOD_NONE, 0x2E), "+": (MOD_SHIFT, 0x2E),
        "[": (MOD_NONE, 0x2F), "{": (MOD_SHIFT, 0x2F),
        "]": (MOD_NONE, 0x30), "}": (MOD_SHIFT, 0x30),
        "\\": (MOD_NONE, 0x31), "|": (MOD_SHIFT, 0x31),
        ";": (MOD_NONE, 0x33), ":": (MOD_SHIFT, 0x33),
        "'": (MOD_NONE, 0x34), '"': (MOD_SHIFT, 0x34),
        "`": (MOD_NONE, 0x35), "~": (MOD_SHIFT, 0x35),
        ",": (MOD_NONE, 0x36), "<": (MOD_SHIFT, 0x36),
        ".": (MOD_NONE, 0x37), ">": (MOD_SHIFT, 0x37),
        "/": (MOD_NONE, 0x38), "?": (MOD_SHIFT, 0x38),
    }.items():
        put(char, mod, usage)
    for digit, usage in enumerate(
        (0x27, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26)
    ):
        put(str(digit), MOD_NONE, usage)
    for lower, usage in zip("abcdefghijklmnopqrstuvwxyz", range(0x04, 0x1E)):
        put(lower, MOD_NONE, usage)
        put(lower.upper(), MOD_SHIFT, usage)
    return table


_PRINTABLE_ASCII = " " + "".join(chr(code) for code in range(0x21, 0x7F))

# Characters the firmware's ASCII map (Keyboard.write) can type per layout.
# The Spanish map lacks the three dead-key characters and nothing else.
FIRMWARE_ASCII: dict[str, frozenset[str]] = {
    "es_ES": frozenset(set(_PRINTABLE_ASCII) - {"`", "^", "~"}) | {"\n", "\t", "\b"},
    "en_US": frozenset(_PRINTABLE_ASCII) | {"\n", "\t", "\b"},
}

LAYOUTS: dict[str, dict[str, tuple[Step, ...]]] = {
    "es_ES": _build_es_es(),
    "en_US": _build_en_us(),
}
for _name, _table in LAYOUTS.items():
    _table.update(_CONTROL_STEPS)

SUPPORTED_LAYOUTS: tuple[str, ...] = tuple(LAYOUTS)


def normalize_text(text: str) -> str:
    r"""Normalizes \r\n and a lone \r to \n so Enter is one consistent step."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def unreachable_characters(text: str, layout: str) -> list[str]:
    """Characters the active layout cannot produce, in first-occurrence order."""
    table = LAYOUTS.get(layout)
    if table is None:
        return list(dict.fromkeys(normalize_text(text)))
    unreachable: list[str] = []
    for char in normalize_text(text):
        if char not in table and char not in unreachable:
            unreachable.append(char)
    return unreachable


def encode_steps(text: str, layout: str) -> list[str]:
    """Encodes a validated string into ``"MM:UU"`` raw HID steps.

    A character may expand to several steps (dead-key sequences). The caller
    must run :func:`unreachable_characters` first; an unknown layout or an
    untypeable character raises ``KeyError``.
    """
    table = LAYOUTS[layout]
    steps: list[str] = []
    for char in normalize_text(text):
        for mod, usage in table[char]:
            steps.append(f"{mod:02x}:{usage:02x}")
    return steps


def firmware_direct_text(text: str, layout: str) -> bool:
    """True when the string can travel through ``kb_text`` unchanged.

    That requires pure 7-bit ASCII with no control character beyond
    ``\\n``/``\\t``/``\\b`` and no character the firmware's ASCII map lacks
    for the layout (es_ES: `` ` `` ^ ~). An empty string is never worth a
    ``kb_text`` frame when ``enter`` is false, so the caller keeps the
    decision; this helper reports ``True`` for it only when there is input.
    """
    allowed = FIRMWARE_ASCII.get(layout, frozenset())
    normalized = normalize_text(text)
    return bool(normalized) and all(char in allowed for char in normalized)
