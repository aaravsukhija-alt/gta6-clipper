"""Hook line (top of video), post captions and titles. Always positive, always names the streamer
and carries the campaign hashtags; anything the transcript says is only used in burned-in captions."""
import hashlib
import re

from .textmatch import find_terms

# trigger phrase -> hook. {n} = short streamer name in capitals
HOOKS = [
    (["no way", "bro what", "what the"], ["{n} COULD NOT BELIEVE THIS", "WAIT FOR {n}'S REACTION"]),
    (["oh my god"], ["{n}'S REACTION IS INSANE", "{n} WAS NOT READY"]),
    (["let's go"], ["{n} ACTUALLY PULLED IT OFF", "{n} CLUTCHED IT"]),
    (["this is insane", "insane", "crazy"], ["THIS GTA 6 MOMENT IS INSANE", "{n} IN GTA 6 IS UNREAL"]),
]
DEFAULT_HOOKS = ["{n} IN GTA 6 IS UNREAL", "WAIT FOR {n}'S REACTION", "{n}'S GTA 6 MOMENT"]

CAPTIONS = [
    "{name}'s reaction to this GTA 6 moment is unreal 😂",
    "{name} was not ready for this in GTA 6 😭",
    "Wait for {name}'s reaction 🔥",
    "{name} in GTA 6 never disappoints 😂",
]

# moments containing these are skipped entirely (campaign rules: nothing offensive)
BLOCKED = [re.compile(p, re.I) for p in (r"n[i1]gg", r"f[a@]gg?(ot|y)?\b", r"\bretard", r"\bkys\b")]


def _pick(options: list[str], seed: str) -> str:
    return options[int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(options)]


def hook_text(excerpt: str, short_name: str, seed: str) -> str:
    for triggers, options in HOOKS:
        if find_terms(excerpt, triggers):
            return _pick(options, seed).format(n=short_name.upper())
    return _pick(DEFAULT_HOOKS, seed).format(n=short_name.upper())


def is_clean(text: str) -> bool:
    return not any(p.search(text or "") for p in BLOCKED)


def hashtags(streamer: dict) -> str:
    tags = [streamer["hashtag"], "#GTA6", "#GTAVI"]
    return " ".join(dict.fromkeys(tags))


def post_caption(streamer: dict, seed: str, platform: str) -> str:
    line = _pick(CAPTIONS, seed).format(name=streamer["name"])
    extra = " #shorts" if platform == "youtube" else " #reels"
    return f"{line}\nClip from {streamer['name']} ({streamer['credit']}).\n{hashtags(streamer)}{extra} #gaming"


def youtube_title(streamer: dict, hook: str) -> str:
    title = f"{hook.title()} | {streamer['name']} {streamer['hashtag']} #GTA6 #Shorts"
    return title[:100]


def check_caption(streamer: dict, caption: str) -> list[str]:
    """Campaign rule check; returns a list of problems (empty = OK)."""
    problems = []
    if streamer["name"].lower() not in caption.lower():
        problems.append("caption does not name the streamer")
    for tag in (streamer["hashtag"], "#GTA6", "#GTAVI"):
        if tag.lower() not in caption.lower():
            problems.append(f"caption missing {tag}")
    if not caption.isascii() and not all(ord(c) < 0x2E80 or ord(c) >= 0x1F000 for c in caption):
        problems.append("caption has non-English characters")
    if not is_clean(caption):
        problems.append("caption has blocked words")
    return problems
