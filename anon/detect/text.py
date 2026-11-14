"""Regex building blocks for German legal text (used on normalised text)."""

from __future__ import annotations

import re
from functools import lru_cache

UPPER = "A-ZÄÖÜÉÈÀÂÊÎÔÛÇÑŠŽČĆĐ"
LOWER = "a-zäöüßéèàâêîôûçñšžčćđëïáíóúýğış"
NAME_WORD = rf"[{UPPER}][{LOWER}']+(?:-[{UPPER}][{LOWER}']+)*"
CAPS_WORD = rf"[{UPPER}]{{2,}}(?:-[{UPPER}]{{2,}})*"
INITIAL = rf"[{UPPER}]\."
PARTICLES = ["von der", "van der", "van den", "de la", "von", "van", "de", "del", "della", "di", "da", "dos",
             "du", "zu", "zur", "le", "la", "el", "al", "bin", "ibn"]
PARTICLE = "(?:" + "|".join(re.escape(p).replace(r"\ ", r"\s") for p in PARTICLES) + ")"

# Hyphen counts as a word character for names: "Peter-Kaiser-Platz" never
# matches "Kaiser". Numbers use a looser boundary ("4716/4718").
NAME_LEFT = r"(?<![\w\-])"
NAME_RIGHT = r"(?![\w\-])"
NUM_LEFT = r"(?<![\w])(?<!\d[.,'])"
NUM_RIGHT = r"(?![\w])(?![.,']\d)"

_ARTICLES = {"der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "eines", "einer",
             "kein", "keine", "dieser", "diese", "dieses", "diesem", "diesen", "jeder", "jede", "jedes"}


def alternation(items: list[str], escape: bool = True) -> str:
    """Longest-first alternation; whitespace in literals matches any whitespace."""
    parts = sorted({i for i in items if i}, key=len, reverse=True)
    if escape:
        parts = [re.escape(p).replace(r"\ ", r"\s+") for p in parts]
    return "(?:" + "|".join(parts) + ")" if parts else "(?!x)x"


def case_variants(form: str) -> list[str]:
    """Exact form, ALL CAPS, and surname-in-caps ("Albert KAISER")."""
    out = [form, form.upper()]
    toks = form.split(" ")
    if len(toks) > 1:
        out.append(" ".join(toks[:-1] + [toks[-1].upper()]))
    return list(dict.fromkeys(out))


@lru_cache(maxsize=4096)
def form_regex(form: str, numeric: bool = False) -> re.Pattern:
    if numeric:
        return re.compile(NUM_LEFT + re.escape(form) + NUM_RIGHT)
    body = alternation(case_variants(form))
    return re.compile(NAME_LEFT + body + NAME_RIGHT)


def is_numeric_form(form: str) -> bool:
    return bool(re.fullmatch(r"[\d\s./-]+[a-z]?", form))


def preceded_by_article(norm: str, start: int) -> bool:
    m = re.search(r"(\w+)\s+$", norm[max(0, start - 20) : start])
    return bool(m and m.group(1).lower() in _ARTICLES)


def compile_list(patterns: list[str], flags: int = 0) -> list[re.Pattern]:
    return [re.compile(p, flags) for p in patterns]


def titles_regex(titles: list[str]) -> str:
    return "(?:" + "|".join(sorted(titles, key=len, reverse=True)) + ")"


def strip_leading(text: str, pattern: re.Pattern) -> tuple[str, int]:
    """Repeatedly strip a leading pattern (titles/honorifics); returns
    (rest, number of characters removed)."""
    removed = 0
    while True:
        m = pattern.match(text)
        if not m or m.end() == 0:
            return text, removed
        text = text[m.end() :]
        removed += m.end()


GENITIVE_ENDINGS = ("s", "ß", "x", "z")


def genitive_forms(form: str) -> list[str]:
    """German genitive of a name: Kaiser -> Kaisers; Burger -> Burgers;
    names ending in s/x/z take an apostrophe (typographic quotes are
    normalised to ' before matching)."""
    if form.endswith(GENITIVE_ENDINGS):
        return [form + "'"]
    return [form + "s"]
