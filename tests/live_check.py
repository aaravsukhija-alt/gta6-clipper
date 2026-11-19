"""Live self-test against the real sites (run on the server/PC, not needed day to day).

python tests/live_check.py

Checks: YouTube (listing, metadata, captions, clip download + edit, Whisper),
Twitch (VOD listing + clip download), and one complete pipeline run on a short Twitch clip.
Set CLIPPER_YOUTUBE_OPTIONAL=1 to report YouTube blocks without failing (GitHub's cloud
servers are blocked by YouTube; on your own PC YouTube must pass).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yaml  # noqa: E402

from clipper import editor, run, transcript, ytdlp  # noqa: E402
from clipper.state import State  # noqa: E402

YT_OPTIONAL = True   # sources are Twitch-only now; YouTube downloads are informational
TWITCH_CHANNELS = ("xqc", "shroud", "summit1g", "kaicenat", "zackrawrr")
results = {"pass": [], "fail": [], "blocked": []}
tmp = Path(tempfile.mkdtemp())
state = {}


def check(name, fn, youtube=False):
    try:
        print(f"PASS {name}: {fn()}", flush=True)
        results["pass"].append(name)
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        if youtube and YT_OPTIONAL:
            print(f"INFO {name}: YouTube download not available here ({msg[:160]}) - not needed for Twitch", flush=True)
            results["blocked"].append(name)
        else:
            print(f"FAIL {name}: {msg}", flush=True)
            results["fail"].append(name)


# ---------------- YouTube ----------------
def yt_pick():
    entries = ytdlp.list_channel("ytsearch8:gta 6 gameplay reaction", 8)
    for e in entries:
        if e.get("duration") and 120 < e["duration"] < 1500:
            return f"https://www.youtube.com/watch?v={e['id']}"
    return f"https://www.youtube.com/watch?v={entries[0]['id']}"


check("youtube search/listing", lambda: state.setdefault("url", yt_pick()))
if "url" in state:
    def yt_info():
        state["info"] = i = ytdlp.video_info(state["url"])
        return f"{i['title']!r} {i.get('duration')}s heatmap={bool(i.get('heatmap'))}"
    check("youtube metadata", yt_info, youtube=True)

if "info" in state:
    def yt_caps():
        segs = transcript.captions(state["info"], ["en"], tmp / "subs")
        assert segs, "no caption lines"
        return f"{len(segs)} lines, e.g. {segs[len(segs) // 2].text!r}"
    check("youtube captions", yt_caps, youtube=True)

    def yt_cut():
        p = ytdlp.download_section(state["url"], 30, 40, tmp / "yt.mp4")
        v = editor.make_vertical(p, tmp / "yt_v.mp4", "GTA 6 MOMENT", "clip: @test",
                                 {"width": 1080, "height": 1920, "font_file": None})
        return f"{p.stat().st_size // 1024} KB cut, {v.stat().st_size // 1024} KB vertical"
    check("youtube clip download + edit", yt_cut, youtube=True)

    def yt_whisper():
        segs = transcript.whisper(state["url"], "tiny", None, tmp / "audio")
        assert segs, "whisper produced nothing"
        return f"{len(segs)} lines, e.g. {segs[0].text!r}"
    check("youtube audio + whisper", yt_whisper, youtube=True)


# ---------------- Twitch ----------------
def twitch_vod():
    for ch in TWITCH_CHANNELS:
        try:
            entries = ytdlp.list_channel(run.channel_tabs({"platform": "twitch", "channel": ch})[0], 3)
        except ytdlp.YtDlpError:
            continue
        if entries:
            e = entries[0]
            url = e.get("url") or f"https://www.twitch.tv/videos/{str(e['id']).lstrip('v')}"
            info = ytdlp.video_info(url)
            p = ytdlp.download_section(url, 60, 70, tmp / "tw.mp4")
            return f"{ch}: {info.get('title')!r} -> {p.stat().st_size // 1024} KB cut"
    raise RuntimeError("no Twitch channel returned VODs")


check("twitch VOD listing + clip download", twitch_vod)


def pipeline():
    """Full run (find -> Whisper -> pick moment -> cut -> vertical edit) on one short Twitch clip.
    Uses a test config that accepts everyday words, so a clip is always produced."""
    clip_url = None
    for ch in TWITCH_CHANNELS:
        try:
            entries = ytdlp.list_channel(f"https://www.twitch.tv/{ch}/clips?filter=clips&range=7d", 5)
        except ytdlp.YtDlpError:
            continue
        if entries:
            e = entries[0]
            clip_url = e.get("url") or e.get("webpage_url")
            break
    assert clip_url, "no Twitch clips found"
    cfg = yaml.safe_load((Path(run.__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))
    cfg["streamers"] = []
    cfg["hype_phrases"] = ["the", "you", "i", "it", "a", "and", "what", "no", "yeah", "oh"]
    cfg["selection"].update(min_score=1, min_seconds=5, target_seconds=20, max_seconds=30, clips_per_video=1)
    cfg["transcript"].update(whisper_model="tiny")
    cfg_file = tmp / "test_config.yaml"
    cfg_file.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    run.OUTPUT = tmp / "out"
    run.State = lambda: State(tmp / "state.json")
    code = run.main(["--config", str(cfg_file), "--no-links", "--url", clip_url])
    vids = sorted((tmp / "out").glob("*/*_vertical.mp4"))
    assert code == 0, f"run exited {code}"
    assert vids, "no vertical clip produced: " + (tmp / "out" / "summary.md").read_text(encoding="utf-8")[:300]
    return f"{clip_url} -> {vids[0].name} ({vids[0].stat().st_size // 1024} KB)"


check("full pipeline on a Twitch clip", pipeline)


def composio_accounts():
    if not os.getenv("COMPOSIO_API_KEY"):
        return "skipped (COMPOSIO_API_KEY secret not set yet)"
    from clipper import publisher
    ig = publisher._exec("INSTAGRAM_GET_USER_INFO", {"ig_user_id": "me", "fields": "id,username,account_type"})
    return f"Instagram @{ig.get('username')} ({ig.get('account_type')}) connected"


def notify_check():
    from clipper import notify
    if not (os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("TELEGRAM_BOT_TOKEN")):
        return "skipped (no Discord/Telegram secret yet)"
    assert notify.send("🔧 gta6-clipper self-test: notifications work"), "notification failed"
    return "test message sent"


check("composio (instagram) connection", composio_accounts)
check("phone notification", notify_check)

print(json.dumps(results))
sys.exit(1 if results["fail"] else 0)
