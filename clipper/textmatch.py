"""Whole-word / whole-phrase matching, so 'bro' doesn't match 'broken' and 'gta 6' doesn't match 'gta 60'."""
import re
from functools import lru_cache


def normalize(text: str) -> str:
    text = (text or "").lower().replace("’", "'")
    text = re.sub(r"'s\b", "", text)          # "gta 6's map" -> "gta 6 map"
    text = re.sub(r"[^\w\s'\[\]]", " ", text)   # keep letters, digits, apostrophes, [tags]
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=4096)
def _pattern(term: str) -> re.Pattern:
    words = normalize(term).split()
    return re.compile(r"(?<![\w'])" + r"\s+".join(re.escape(w) for w in words) + r"(?![\w'])")


def count_terms(text: str, terms: list[str]) -> dict[str, int]:
    t = normalize(text)
    out = {}
    for term in terms:
        if not normalize(term):
            continue
        n = len(_pattern(term).findall(t))
        if n:
            out[term] = n
    return out


def find_terms(text: str, terms: list[str]) -> list[str]:
    return list(count_terms(text, terms))


REACTION_TAG = re.compile(r"\[(laughter|laughs|laughing|applause|cheering|screaming|screams|gasps)\]", re.I)
