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


NUM = r"(\d+|two|three|four|five|six|seven|eight|nine|ten|twenty)"
NUMS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "twenty": 20}
# (pattern, hook) - what a stranger is about to see. Checked against the transcript; later matches
# (closer to the spike) win ties. {n} = name, {k} = number from the match.
EVENTS = [
    (rf"\b{NUM} (cop|police) (cars?|cruisers?)\b", "{n} GETS CHASED BY {k} COP CARS"),
    (rf"\b{NUM} cops\b", "{n} VS {k} COPS IN GTA 6"),
    (rf"\b{NUM}[- ]stars?\b", "{n} HITS A {k} STAR WANTED LEVEL"),
    (r"\b(wanted level|stars on me)\b", "{n} GETS A WANTED LEVEL IN GTA 6"),
    (r"\b(cops?|police)\b.*\b(chas\w*|run|running|lose them|escape)\b|\b(chas\w*|run|running|escape)\b.*\b(cops?|police)\b",
     "{n} TRIES TO ESCAPE THE COPS"),
    (r"\b(alligator|gator|crocodile)s?\b", "{n} RUNS INTO A GATOR IN GTA 6"),
    (r"\b(heist|rob(bing|bed)?|bank job|vault)\b", "{n} PULLS OFF A HEIST IN GTA 6"),
    (r"\b(helicopter|heli|chopper)\b", "{n} TRIES TO FLY A HELICOPTER"),
    (r"\b(plane|jet)\b", "{n} TRIES TO FLY A PLANE"),
    (r"\b(boat|jet ?ski|speedboat)\b", "{n} TAKES A BOAT OUT IN VICE CITY"),
    (r"\b(blew up|blow (it|this|him|that) up|explod\w*|explosion|grenade|rpg)\b", "{n} BLOWS EVERYTHING UP"),
    (r"\b(crash(ed|ing)?|wreck(ed)?|flipped)\b", "{n} CRASHES AT FULL SPEED"),
    (r"\b(jump|ramp|flip|stunt)\b", "{n} ATTEMPTS A CRAZY JUMP"),
    (r"\b(beach|ocean|vice beach)\b", "{n} CAUSES CHAOS ON THE BEACH"),
    (r"\b(million|money|cash|bands)\b", "{n} MAKES BANK IN GTA 6"),
    (r"\b(lucia)\b", "{n} PLAYS AS LUCIA FOR THE FIRST TIME"),
    (r"\b(glitch(ed)?|bugged|bug)\b", "GTA 6 GLITCHED AND {n} LOST IT"),
    (r"\b(danc(e|ing))\b", "{n} STARTS DANCING ON STREAM"),
    (r"\b(donation|dono|gifted|thank you for the)\b", "THIS DONATION MADE {n} LOSE IT"),
    (r"\b(roast(ed|ing)?|violated)\b", "CHAT ROASTED {n} AND HE LOST IT"),
    (r"\bchat\b.*\b(look|what|why|bro)\b", "{n} CANNOT BELIEVE WHAT CHAT SAID"),
]


def event_hook(transcript_text: str, short_name: str) -> str | None:
    t = (transcript_text or "").lower()
    best = None
    for pat, hook in EVENTS:
        for m in re.finditer(pat, t):
            k = next((g for g in m.groups() if g and (g.isdigit() or g in NUMS)), None) if m.groups() else None
            k = str(NUMS.get(k, k)) if k else ""
            if "{k}" in hook and not k:
                continue
            if best is None or m.start() > best[0] or (m.start() == best[0] and len(hook) > len(best[1])):
                best = (m.start(), hook.format(n=short_name.upper(), k=k))
    return best[1] if best else None


def hook_text(excerpt: str, short_name: str, seed: str) -> str:
    ev = event_hook(excerpt, short_name)
    if ev:
        return ev
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
    if not is_positive(caption):
        problems.append("caption is negative / drama")
    return problems


NEGATIVE = re.compile(r"\b(fail(s|ed)?|cringe|worst|trash|exposed|caught lying|cancel(l)?ed|drama|beef|"
                      r"meltdown|disgusting|toxic|hates?|ruin(s|ed)?|scam)\b", re.I)


def is_positive(text: str) -> bool:
    """Hooks/captions must not be negative about the streamer or invent drama."""
    return not NEGATIVE.search(text or "")
