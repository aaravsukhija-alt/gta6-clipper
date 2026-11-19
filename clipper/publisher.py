"""Posting queue -> YouTube Shorts + Instagram Reels through Composio, with ramp-up limits,
US-evening posting window, per-platform error stop, notifications and a daily report."""
import json
import math
import os
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import notify, texts
from .config import ROOT

# The ledger (queue + posting log) lives in the PC's media folder so the live recorder and the
# scheduled runs share one set of daily limits. The repo gets a copy for the report.
STATE = ROOT / "state"
MEDIA = Path(os.path.expanduser("~/gta6clipper-media"))
QUEUE_DIR = MEDIA / "queue"
POSTED = MEDIA / "posted.json"
REPORT = ROOT / "daily_report.md"


def configure(cfg: dict):
    global MEDIA, QUEUE_DIR, POSTED
    MEDIA = Path(os.path.expanduser(cfg["posting"]["media_dir"]))
    QUEUE_DIR, POSTED = MEDIA / "queue", MEDIA / "posted.json"


def media_dir() -> Path:
    return MEDIA


@contextmanager
def ledger_lock(timeout: float = 900):
    """One writer at a time (live recorder vs scheduled run). Stale locks (>30 min) are broken."""
    MEDIA.mkdir(parents=True, exist_ok=True)
    lock = MEDIA / ".lock"
    t0 = time.time()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            if time.time() - lock.stat().st_mtime > 1800:
                lock.unlink(missing_ok=True)
                continue
            if time.time() - t0 > timeout:
                raise TimeoutError("posting ledger is locked")
            time.sleep(2)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def mirror_to_repo():
    """Copy the ledger into the repo (state/) so the run can commit it with the daily report."""
    STATE.mkdir(exist_ok=True)
    if POSTED.exists():
        shutil.copy(POSTED, STATE / "posted.json")
    rq = STATE / "queue"
    rq.mkdir(exist_ok=True)
    for f in QUEUE_DIR.glob("*.json") if QUEUE_DIR.exists() else []:
        shutil.copy(f, rq / f.name)
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


def enqueue(item: dict, media: Path | None = None):
    """Copy the finished clip to the PC's media folder and add it to the queue (one file per clip)."""
    media = media or MEDIA
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    media.mkdir(parents=True, exist_ok=True)
    dest = media / Path(item["file"]).name
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
_ACCOUNTS: dict = {}


def _composio():
    global _CLIENT
    if _CLIENT is None:
        if not os.getenv("COMPOSIO_API_KEY"):
            raise PostError("COMPOSIO_API_KEY secret is not set")
        from composio import Composio
        _CLIENT = Composio(api_key=os.environ["COMPOSIO_API_KEY"], dangerously_allow_auto_upload_download_files=True)
    return _CLIENT


def _val(obj, *names):
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            return v
    return None


def find_accounts() -> dict:
    """Find the active YouTube / Instagram connections this API key can use, and their user id.
    No user id has to be typed in anywhere."""
    if _ACCOUNTS:
        return _ACCOUNTS
    resp = _composio().connected_accounts.list(toolkit_slugs=["youtube", "instagram"])
    items = _val(resp, "items", "data") or []
    for it in items:
        status = str(_val(it, "status") or "").upper()
        tk = _val(it, "toolkit")
        slug = str(_val(tk, "slug") if tk is not None else _val(it, "toolkit_slug") or "").lower()
        if status == "ACTIVE" and slug in ("youtube", "instagram") and slug not in _ACCOUNTS:
            _ACCOUNTS[slug] = {"id": _val(it, "id"), "user_id": _val(it, "user_id")}
    if os.getenv("COMPOSIO_USER_ID"):
        for v in _ACCOUNTS.values():
            v["user_id"] = v["user_id"] or os.environ["COMPOSIO_USER_ID"]
    return _ACCOUNTS


def connect_link(toolkit: str) -> str | None:
    """A Composio connect link for a platform that this API key's project can't see yet."""
    acc = find_accounts()
    uid = next((a["user_id"] for a in acc.values() if a.get("user_id")), None) or "gta6-clipper"
    try:
        req = _composio().toolkits.authorize(user_id=uid, toolkit=toolkit)
        return getattr(req, "redirect_url", None)
    except Exception as e:  # noqa: BLE001
        print(f"[composio] could not create a {toolkit} connect link: {e}")
        return None


def _exec(slug: str, args: dict) -> dict:
    toolkit = slug.split("_", 1)[0].lower()
    acc = find_accounts().get(toolkit)
    if not acc:
        link = connect_link(toolkit)
        raise PostError(f"{toolkit} is not connected to the Composio project of this API key"
                        + (f" - connect it here: {link}" if link else ""))
    r = _composio().tools.execute(slug, args, user_id=acc["user_id"], connected_account_id=acc["id"],
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


def file_hash(path: str) -> str:
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rules_check(item: dict, st: dict, cfg: dict, platform: str, posted: dict) -> list[str]:
    """Campaign rules, checked automatically before every post. Any problem = the clip is rejected."""
    problems = texts.check_caption(st, texts.post_caption(st, item["id"], platform))
    if item.get("face_ratio", 0) < cfg["moments"]["min_face_ratio"]:
        problems.append(f"streamer not on screen enough ({item.get('face_ratio', 0):.0%})")
    if item.get("lang", "en") != "en":
        problems.append("not English")
    if not texts.is_clean(item.get("transcript", "")) or not texts.is_clean(item.get("hook", "")):
        problems.append("blocked words in transcript/hook")
    if not (14.5 <= float(item.get("length") or 0) <= 30.5):
        problems.append(f"length {item.get('length')}s is outside 15-30 s")
    if item.get("hook") and not texts.is_positive(item["hook"]):
        problems.append("hook is negative")
    h = item.get("hash")
    if h and any(p.get("hash") == h and p.get("ok") and p["platform"] == platform for p in posted["posts"]):
        problems.append("this exact video was already posted")
    near = [p for p in posted["posts"] if p.get("ok") and p["platform"] == platform and p.get("source")
            and p.get("source") == item.get("source") and p["clip"] != item["id"]]
    if near:
        problems.append("this moment was already posted")
    return problems


# ---------------- main loop ----------------
def run_posting(cfg: dict, now: datetime | None = None, force: bool = False, only: list[str] | None = None,
                limit: int | None = None, ids: list[str] | None = None) -> list[dict]:
    """Post what is due right now. force=True ignores the time window (live clips, first test);
    ids = only these clips. Daily limits always apply."""
    with ledger_lock():
        return _run_posting(cfg, now, force, only, limit, ids)


def _run_posting(cfg, now, force, only, limit, ids) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    pc = cfg["posting"]
    posted = load_posted()
    streamers = {s["channel"]: s for s in cfg["streamers"]}
    done = {(p["platform"], p["clip"]) for p in posted["posts"] if p.get("ok")}
    slots = slots_left(cfg, now)
    if slots is None and not force:
        print(f"[post] outside the posting window {pc['window_ist']} IST; nothing posted")
        return []
    slot_id = now.astimezone(IST).strftime("%Y-%m-%d %H:") + ("00" if now.astimezone(IST).minute < 30 else "30")
    if not force:
        if posted.get("last_slot") == slot_id:
            return []                                  # this 30-minute slot was already used
        posted["last_slot"] = slot_id
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
        todo = [q for q in queue if (platform, q["id"]) not in done and Path(q["file"]).exists() and not q.get("rejected")
                and (ids is None or q["id"] in ids)][:max(0, n)]
        for item in todo:
            st = streamers.get(item["streamer"])
            if not st:
                continue
            item.setdefault("hash", file_hash(item["file"]))
            problems = rules_check(item, st, cfg, platform, posted)
            if problems:
                print(f"[post] {item['id']} REJECTED by campaign rules: {problems}")
                reject(item, problems)
                continue
            rec = {"platform": platform, "clip": item["id"], "streamer": item["streamer"], "day": ist_day(now),
                   "at": now.isoformat(timespec="seconds"), "campaign": st["campaign"],
                   "campaign_url": st.get("campaign_url", ""), "hash": item["hash"], "source": item.get("source")}
            try:
                rec["link"] = POSTERS[platform](item, st)
                rec["ok"] = True
                posted["first_post_day"] = posted["first_post_day"] or ist_day(now)
                notify.send(f"✅ **{st['name']}** clip posted on {platform.title()}: {rec['link']}\n"
                            f"➡️ Submit within 30 min to **{st['campaign']}**: {st.get('campaign_url', '')}\n"
                            f"Hook: {item.get('hook', '')} · score {item.get('score')}")
                print(f"[post] {platform}: {rec['link']}")
            except Exception as e:  # noqa: BLE001 - stop this platform, keep the others going
                rec.update(ok=False, error=str(e)[:500])
                posted["disabled"][platform] = {"reason": str(e)[:300], "at": rec["at"]}
                notify.send(f"⛔ {platform.title()} posting PAUSED after an error (the other platform keeps "
                            f"running):\n{str(e)[:700]}\nAfter fixing it, run the workflow with reset_errors ticked.")
                print(f"[post] {platform} ERROR: {e}")
            posted["posts"].append(rec)
            results.append(rec)
            save_posted(posted)
            if not rec["ok"]:
                break
    write_report(cfg)
    return results


def reject(item: dict, problems: list[str]):
    f = QUEUE_DIR / f"{item['id']}.json"
    item = dict(item, rejected=problems)
    if f.exists():
        f.write_text(json.dumps(item, indent=1, ensure_ascii=False), encoding="utf-8")


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
        rej = [q for q in made if q.get("rejected")]
        posts = [p for p in posted["posts"] if p["day"] == day]
        ok = [p for p in posts if p.get("ok")]
        lines += [f"## {day}", "",
                  f"- Clips made: **{len(made)}**, posts: **{len(ok)}** "
                  f"(YouTube {sum(p['platform'] == 'youtube' for p in ok)}, "
                  f"Instagram {sum(p['platform'] == 'instagram' for p in ok)}), errors: {len(posts) - len(ok)}, "
                  f"rejected by rules: {len(rej)}", ""]
        lines += [f"- rejected `{q['id']}`: {', '.join(q['rejected'])}" for q in rej]
        if rej:
            lines.append("")
        if posts:
            lines += ["| Time (UTC) | Platform | Streamer | Link | Campaign |", "|---|---|---|---|---|"]
            lines += [f"| {p['at'][11:16]} | {p['platform']} | {p['streamer']} | "
                      f"{p.get('link') or 'ERROR: ' + p.get('error', '')[:80]} | {p['campaign']} |" for p in posts]
            lines.append("")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
