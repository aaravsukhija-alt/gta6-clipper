"""Posting queue -> YouTube Shorts + Instagram Reels through Composio, with ramp-up limits,
US-evening posting window, per-platform error stop, notifications and a daily report."""
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import notify, texts
from .config import ROOT

STATE = ROOT / "state"
QUEUE_DIR = STATE / "queue"
POSTED = STATE / "posted.json"
REPORT = ROOT / "daily_report.md"
IST = timezone(timedelta(hours=5, minutes=30))
PLATFORMS = ("youtube", "instagram")


class PostError(RuntimeError):
    pass


# ---------------- state ----------------
def load_posted() -> dict:
    if POSTED.exists():
        return json.loads(POSTED.read_text(encoding="utf-8"))
    return {"first_post_day": None, "posts": [], "disabled": {}}


def save_posted(p: dict):
    STATE.mkdir(exist_ok=True)
    POSTED.write_text(json.dumps(p, indent=1, ensure_ascii=False), encoding="utf-8")


def load_queue() -> list[dict]:
    if not QUEUE_DIR.exists():
        return []
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(QUEUE_DIR.glob("*.json"))]


def enqueue(item: dict, media_dir: Path):
    """Copy the finished clip to the PC's media folder and add it to the queue (one file per clip)."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    dest = media_dir / Path(item["file"]).name
    if Path(item["file"]).resolve() != dest.resolve():
        dest.write_bytes(Path(item["file"]).read_bytes())
    item = dict(item, file=str(dest))
    (QUEUE_DIR / f"{item['id']}.json").write_text(json.dumps(item, indent=1, ensure_ascii=False), encoding="utf-8")


# ---------------- limits ----------------
def ist_day(now: datetime) -> str:
    return now.astimezone(IST).date().isoformat()


def daily_cap(cfg: dict, posted: dict, platform: str, now: datetime) -> int:
    ramp = cfg["posting"]["ramp"]
    first = posted.get("first_post_day")
    week = 1 if not first else (datetime.fromisoformat(ist_day(now)) - datetime.fromisoformat(first)).days // 7 + 1
    row = ramp["week1"] if week <= 1 else ramp["week2_plus"]
    return int(row.get(platform, 0))


def posts_today(posted: dict, platform: str, now: datetime) -> int:
    day = ist_day(now)
    return sum(1 for p in posted["posts"] if p["platform"] == platform and p["day"] == day and p.get("ok"))


def slots_left(cfg: dict, now: datetime) -> int | None:
    """Remaining 30-minute posting slots in today's window (IST), or None if outside the window."""
    a, b = cfg["posting"]["window_ist"]
    t = now.astimezone(IST)
    start = t.replace(hour=int(a[:2]), minute=int(a[3:]), second=0, microsecond=0)
    end = t.replace(hour=int(b[:2]), minute=int(b[3:]), second=0, microsecond=0)
    if not (start <= t < end):
        return None
    return max(1, math.ceil((end - t).total_seconds() / 1800))


def order_queue(queue: list[dict], cfg: dict) -> list[dict]:
    prio = {s["channel"]: s.get("priority", 9) for s in cfg["streamers"]}
    return sorted(queue, key=lambda q: (prio.get(q["streamer"], 9), -q["score"], q["created"]))


# ---------------- platforms (Composio) ----------------
_CLIENT = None


def _composio():
    global _CLIENT
    if _CLIENT is None:
        if not os.getenv("COMPOSIO_API_KEY") or not os.getenv("COMPOSIO_USER_ID"):
            raise PostError("COMPOSIO_API_KEY / COMPOSIO_USER_ID secrets are not set")
        from composio import Composio
        _CLIENT = Composio(api_key=os.environ["COMPOSIO_API_KEY"], dangerously_allow_auto_upload_download_files=True)
    return _CLIENT


def _exec(slug: str, args: dict) -> dict:
    r = _composio().tools.execute(slug, args, user_id=os.environ["COMPOSIO_USER_ID"],
                                  dangerously_skip_version_check=True)
    if not r.get("successful"):
        raise PostError(f"{slug}: {r.get('error') or r}")
    return r.get("data") or {}


def post_youtube(item: dict, streamer: dict) -> str:
    seed = item["id"]
    data = _exec("YOUTUBE_UPLOAD_VIDEO", {
        "title": texts.youtube_title(streamer, item["hook"]),
        "description": texts.post_caption(streamer, seed, "youtube") + f"\n\nOriginal stream: {item['source']}",
        "tags": ["GTA6", "GTA VI", streamer["name"], "shorts", "gaming"],
        "categoryId": "20", "privacyStatus": "public", "videoFilePath": item["file"]})
    video = data.get("response_data") or data
    vid = video.get("id")
    if not vid:
        raise PostError(f"YouTube returned no video id: {str(data)[:300]}")
    privacy = (video.get("status") or {}).get("privacyStatus")
    if privacy and privacy != "public":
        raise PostError(f"YouTube made the upload '{privacy}' instead of public (https://youtu.be/{vid})")
    return f"https://youtube.com/shorts/{vid}"


def post_instagram(item: dict, streamer: dict) -> str:
    cap = texts.post_caption(streamer, item["id"], "instagram")
    me = _exec("INSTAGRAM_GET_USER_INFO", {"ig_user_id": "me", "fields": "id,username"})
    ig = me.get("id")
    if not ig:
        raise PostError(f"could not read the Instagram account id: {me}")
    cont = _exec("INSTAGRAM_POST_IG_USER_MEDIA", {"ig_user_id": ig, "media_type": "REELS", "caption": cap,
                                                  "share_to_feed": True, "video_file": item["file"]})
    cid = cont.get("id")
    if not cid:
        raise PostError(f"Instagram returned no container id: {cont}")
    pub = _exec("INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH", {"ig_user_id": ig, "creation_id": cid,
                                                         "max_wait_seconds": 300, "poll_interval_seconds": 10})
    mid = pub.get("id")
    link = f"https://www.instagram.com/{me.get('username', '')}/"
    try:
        link = _exec("INSTAGRAM_GET_IG_MEDIA", {"ig_media_id": mid, "fields": "permalink"}).get("permalink") or link
    except PostError:
        pass
    return link


POSTERS = {"youtube": post_youtube, "instagram": post_instagram}


# ---------------- main loop ----------------
def run_posting(cfg: dict, now: datetime | None = None, force: bool = False, only: list[str] | None = None,
                limit: int | None = None) -> list[dict]:
    """Post what is due right now. force=True ignores the time window (used for the first test)."""
    now = now or datetime.now(timezone.utc)
    pc = cfg["posting"]
    posted = load_posted()
    streamers = {s["channel"]: s for s in cfg["streamers"]}
    done = {(p["platform"], p["clip"]) for p in posted["posts"] if p.get("ok")}
    slots = slots_left(cfg, now)
    if slots is None and not force:
        print(f"[post] outside the posting window {pc['window_ist']} IST; nothing posted")
        return []
    results = []
    queue = order_queue(load_queue(), cfg)
    for platform in only or PLATFORMS:
        if not pc["platforms"].get(platform):
            continue
        if platform in posted["disabled"]:
            print(f"[post] {platform} is stopped after an error: {posted['disabled'][platform]['reason']}")
            continue
        left = daily_cap(cfg, posted, platform, now) - posts_today(posted, platform, now)
        n = left if force else min(left, math.ceil(left / (slots or 1)))
        if limit is not None:
            n = min(n, limit)
        todo = [q for q in queue if (platform, q["id"]) not in done and Path(q["file"]).exists()][:max(0, n)]
        for item in todo:
            st = streamers.get(item["streamer"])
            if not st:
                continue
            caption = texts.post_caption(st, item["id"], platform)
            problems = texts.check_caption(st, caption)
            if problems:
                print(f"[post] {item['id']} not posted: {problems}")
                continue
            rec = {"platform": platform, "clip": item["id"], "streamer": item["streamer"], "day": ist_day(now),
                   "at": now.isoformat(timespec="seconds"), "campaign": st["campaign"]}
            try:
                rec["link"] = POSTERS[platform](item, st)
                rec["ok"] = True
                posted["first_post_day"] = posted["first_post_day"] or ist_day(now)
                notify.send(f"✅ Posted on {platform.title()}: {rec['link']}\n"
                            f"➡️ Submit to campaign: **{st['campaign']}** within 30 min"
                            + (f"\n{st['campaign_url']}" if st.get("campaign_url") else ""))
                print(f"[post] {platform}: {rec['link']}")
            except Exception as e:  # noqa: BLE001 - stop this platform, keep the others going
                rec.update(ok=False, error=str(e)[:500])
                posted["disabled"][platform] = {"reason": str(e)[:300], "at": rec["at"]}
                notify.send(f"⛔ {platform.title()} posting STOPPED after an error:\n{str(e)[:500]}\n"
                            f"Fix it, then run the workflow with reset_errors ticked.")
                print(f"[post] {platform} ERROR: {e}")
            posted["posts"].append(rec)
            results.append(rec)
            save_posted(posted)
            if not rec["ok"]:
                break
    write_report(cfg)
    return results


def write_report(cfg: dict):
    posted = load_posted()
    queue = load_queue()
    days = sorted({ist_day(datetime.fromisoformat(q["created"])) for q in queue}
                  | {p["day"] for p in posted["posts"]}, reverse=True)
    lines = ["# GTA 6 clipper - daily report", "", "Days are India time (IST).", ""]
    if posted["disabled"]:
        lines += ["## ⛔ Stopped platforms", ""] + [f"- **{k}**: {v['reason']} ({v['at']})"
                                                    for k, v in posted["disabled"].items()] + [""]
    for day in days[:30]:
        made = [q for q in queue if ist_day(datetime.fromisoformat(q["created"])) == day]
        posts = [p for p in posted["posts"] if p["day"] == day]
        ok = [p for p in posts if p.get("ok")]
        lines += [f"## {day}", "",
                  f"- Clips made: **{len(made)}**, posts: **{len(ok)}** "
                  f"(YouTube {sum(p['platform'] == 'youtube' for p in ok)}, "
                  f"Instagram {sum(p['platform'] == 'instagram' for p in ok)}), errors: {len(posts) - len(ok)}", ""]
        if posts:
            lines += ["| Time (UTC) | Platform | Streamer | Link | Campaign |", "|---|---|---|---|---|"]
            lines += [f"| {p['at'][11:16]} | {p['platform']} | {p['streamer']} | "
                      f"{p.get('link') or 'ERROR: ' + p.get('error', '')[:80]} | {p['campaign']} |" for p in posts]
            lines.append("")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
