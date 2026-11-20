"""Which parts of a VOD are actually GTA 6? Twitch records every game switch inside a stream
(chapter markers). Only those time ranges may be clipped - never another game in the same stream."""
import json
import urllib.request

GTA6_NAMES = ("grand theft auto vi", "grand theft auto 6", "gta vi", "gta 6", "gta6")
TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"   # Twitch's public web-player id (not a secret)
_CACHE: dict = {}


def is_gta6(game: str) -> bool:
    g = (game or "").lower()
    return any(n in g for n in GTA6_NAMES)


def _gql(query: str) -> dict:
    req = urllib.request.Request("https://gql.twitch.tv/gql", data=json.dumps({"query": query}).encode(),
                                 headers={"Client-ID": TWITCH_WEB_CLIENT_ID, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def twitch_segments(video_id: str, duration: float | None = None, chapters: list | None = None) -> list | None:
    """[(start, end, game)] for a Twitch VOD, or None if Twitch didn't say."""
    vid = str(video_id).lstrip("v")
    if vid in _CACHE:
        return _CACHE[vid]
    segs = None
    if chapters:                                   # from yt-dlp (built from Twitch's game-change markers)
        segs = [(float(c.get("start_time", 0)), float(c.get("end_time") or duration or 1e9), c.get("title", ""))
                for c in chapters]
    if not segs:
        q = ('query{video(id:"%s"){lengthSeconds game{name} moments(first:100,momentRequestType:'
             'VIDEO_CHAPTER_MARKERS){edges{node{positionMilliseconds durationMilliseconds '
             'details{...on GameChangeMomentDetails{game{name displayName}}}}}}}}' % vid)
        try:
            v = (_gql(q).get("data") or {}).get("video") or {}
        except Exception:  # noqa: BLE001
            v = {}
        length = float(v.get("lengthSeconds") or duration or 1e9)
        marks = []
        for e in ((v.get("moments") or {}).get("edges") or []):
            n = e.get("node") or {}
            g = ((n.get("details") or {}).get("game") or {})
            start = (n.get("positionMilliseconds") or 0) / 1000
            dur = (n.get("durationMilliseconds") or 0) / 1000
            marks.append((start, start + dur if dur else None, g.get("name") or g.get("displayName") or ""))
        if marks:
            marks.sort()
            segs = [(a, b if b else (marks[i + 1][0] if i + 1 < len(marks) else length), g)
                    for i, (a, b, g) in enumerate(marks)]
        elif (v.get("game") or {}).get("name"):
            segs = [(0.0, length, v["game"]["name"])]   # single-game stream
    _CACHE[vid] = segs
    return segs


def gta6_ranges(info: dict) -> list | None:
    """Time ranges of a VOD that are GTA 6. [] = none. None = not a Twitch VOD (no game data)."""
    if not str(info.get("extractor_key", "")).startswith("TwitchVod"):
        return None
    segs = twitch_segments(info.get("id"), info.get("duration"), info.get("chapters"))
    if segs is None:
        return []                                   # can't prove it's GTA 6 -> don't clip it
    return [(a, b) for a, b, g in segs if is_gta6(g)]


def inside(ranges: list | None, start: float, end: float) -> bool:
    if ranges is None:
        return True
    return any(a - 1 <= start and end <= b + 1 for a, b in ranges)
