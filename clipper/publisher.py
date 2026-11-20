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
    item = dict(item)
    for key in ("file", "clean_file"):
        if not item.get(key):
            continue
        dest = media / Path(item[key]).name
        if Path(item[key]).resolve() != dest.resolve():
            dest.write_bytes(Path(item[key]).read_bytes())
        item[key] = str(dest)
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
    """Campaign (paid) streamers first, then priority, then best score."""
    st = {s["channel"]: s for s in cfg["streamers"]}
    return sorted(queue, key=lambda q: (0 if st.get(q["streamer"], {}).get("paid") else 1,
                                        st.get(q["streamer"], {}).get("priority", 9), -q["score"], q["created"]))


def in_hours(st: dict, now: datetime) -> bool:
    """Per-streamer posting hours (India time), e.g. Reach campaigns 09:00-23:00 so you can submit."""
    h = st.get("post_hours_ist")
    if not h:
        return True
    t = now.astimezone(IST).strftime("%H:%M")
    return h[0] <= t < h[1]


def growth_today(posted: dict, cfg: dict, now: datetime) -> int:
    paid = {s["channel"] for s in cfg["streamers"] if s.get("paid")}
    day = ist_day(now)
    return sum(1 for p in posted["posts"] if p.get("ok") and p["day"] == day and p["streamer"] not in paid)


# ---------------- platforms (Composio) ----------------
_CLIENT = None
_ACCOUNTS: dict = {}


def _timed(fn, seconds: float, what: str):
    """Run a network call with a hard time limit so a stalled connection can't freeze the run."""
    import concurrent.futures as cf
    ex = cf.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn)
    try:
        return fut.result(timeout=seconds)
    except cf.TimeoutError as e:
        raise PostError(f"{what} timed out after {int(seconds)} s") from e
    finally:
        ex.shutdown(wait=False)


def clean_key(raw: str) -> str:
    """Accept the key even if it was pasted as 'COMPOSIO_API_KEY=ak_...', with quotes or spaces."""
    k = (raw or "").strip().strip('"').strip("'").strip()
    if "=" in k and k.split("=", 1)[0].strip().upper().startswith("COMPOSIO"):
        k = k.split("=", 1)[1].strip().strip('"').strip("'").strip()
    return k


def key_shape(k: str) -> str:
    """Describe the key without revealing it (for error messages)."""
    pre = next((p for p in ("ak_", "uak_", "oak_") if k.startswith(p)), None)
    return f"{len(k)} characters, " + (f"starts with '{pre}'" if pre else "does NOT start with ak_ / uak_")


def _composio():
    global _CLIENT
    if _CLIENT is None:
        key = clean_key(os.getenv("COMPOSIO_API_KEY", ""))
        if not key:
            raise PostError("COMPOSIO_API_KEY secret is not set")
        from composio import Composio
        if key.startswith("uak_"):
            _CLIENT = Composio(user_api_key=key, disable_api_key=True,
                               dangerously_allow_auto_upload_download_files=True)
        else:
            _CLIENT = Composio(api_key=key, dangerously_allow_auto_upload_download_files=True)
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
    try:
        resp = _timed(lambda: _composio().connected_accounts.list(toolkit_slugs=["youtube", "instagram"]), 90,
                      "Composio account lookup")
    except Exception as e:
        if "401" in str(e) or "Invalid API key" in str(e):
            raise PostError(f"Composio rejected the API key ({key_shape(clean_key(os.getenv('COMPOSIO_API_KEY', '')))}). "
                            "Copy a project API key from dashboard.composio.dev -> Settings -> API Keys and "
                            "paste ONLY the key (ak_...) as the COMPOSIO_API_KEY secret.") from e
        raise
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
        req = _timed(lambda: _composio().toolkits.authorize(user_id=uid, toolkit=toolkit), 90, "Composio connect link")
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
    limit = 1800 if "UPLOAD" in slug or "MEDIA" in slug else 180
    r = _timed(lambda: _composio().tools.execute(slug, args, user_id=acc["user_id"], connected_account_id=acc["id"],
                                                 dangerously_skip_version_check=True), limit, slug)
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
    return f"https://youtube.com/shorts/{vid}", vid


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
    return link, mid


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
    if not item.get("hook") or item.get("n_captions", 1) <= 0:
        problems.append("not transformative enough (needs hook + word captions)")
    cap = texts.post_caption(st, item["id"], platform)
    if f"Credit: {st['credit']}" not in cap:
        problems.append("caption has no credit line")
    h = item.get("hash")
    if h and any(p.get("hash") == h and p.get("ok") for p in posted["posts"]):
        problems.append("this exact video was already posted")
    near = [p for p in posted["posts"] if p.get("ok") and p.get("source")
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
    # every clip goes to ONE platform only, so YouTube and Instagram each get different clips
    done = {(pl, p["clip"]) for p in posted["posts"] if p.get("ok") for pl in PLATFORMS}
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
        auto_ok = set(pc.get("auto_streamers") or [s["channel"] for s in cfg["streamers"]])
        shared = {x["clip"] for x in posted.get("shared", [])}
        blocked = posted.setdefault("blocked_streamers", {})
        todo = [q for q in queue if (platform, q["id"]) not in done and Path(q["file"]).exists() and not q.get("rejected")
                and q["streamer"] in auto_ok and q["id"] not in shared and q["streamer"] not in blocked
                and q["streamer"] in streamers and in_hours(streamers[q["streamer"]], now)
                and (ids is None or q["id"] in ids)]
        posted_now = 0
        for item in todo:
            if posted_now >= max(0, n):
                break
            st = streamers.get(item["streamer"])
            if not st:
                continue
            if not st.get("paid") and growth_today(posted, cfg, now) >= int(pc.get("growth_max_per_day", 3)):
                continue                                # non-campaign posts are capped; campaign clips go first
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
                res = POSTERS[platform](item, st)
                rec["link"], rec["media_id"] = res if isinstance(res, tuple) else (res, None)
                rec["ok"] = True
                posted_now += 1
                done.update((pl, item["id"]) for pl in PLATFORMS)   # never also on the other platform
                posted["first_post_day"] = posted["first_post_day"] or ist_day(now)
                if st.get("paid"):
                    notify.send(f"✅ **{st['name']}** clip posted on {platform.title()}: {rec['link']}\n"
                                f"➡️ Submit within 30 min to **{st['campaign']}**: {st.get('campaign_url', '')}\n"
                                f"Hook: {item.get('hook', '')} · score {item.get('score')}")
                else:
                    notify.send(f"📈 Growth post ({st['name']}, no campaign - nothing to submit) on "
                                f"{platform.title()}: {rec['link']}")
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


def _find_items(data) -> list[dict]:
    """YouTube video resources anywhere in a Composio response."""
    if isinstance(data, dict):
        if data.get("kind") == "youtube#video" or ("id" in data and "status" in data):
            return [data]
        out = []
        for v in data.values():
            out += _find_items(v)
        return out
    if isinstance(data, list):
        return [x for v in data for x in _find_items(v)]
    return []


def youtube_problem(video: dict | None) -> str | None:
    if video is None:
        return "video was removed from YouTube"
    st, cd = video.get("status") or {}, video.get("contentDetails") or {}
    if st.get("uploadStatus") in ("rejected", "deleted", "failed") or st.get("rejectionReason"):
        return f"YouTube rejected it ({st.get('rejectionReason') or st.get('uploadStatus')})"
    blocked = ((cd.get("regionRestriction") or {}).get("blocked")) or []
    if blocked:
        return f"blocked in {len(blocked)} countries (copyright claim)"
    if st.get("privacyStatus") and st["privacyStatus"] != "public":
        return f"YouTube made it {st['privacyStatus']}"
    return None


def check_takedowns(cfg: dict, now: datetime | None = None, days: int = 14) -> list[dict]:
    """Copyright safety: if a posted clip got a claim, strike, rejection or takedown, stop that streamer
    for good (until you say otherwise) and comment on the issue."""
    now = now or datetime.now(timezone.utc)
    with ledger_lock():
        posted = load_posted()
        blocked = posted.setdefault("blocked_streamers", {})
        recent = [p for p in posted["posts"] if p.get("ok") and p.get("media_id") and p["streamer"] not in blocked
                  and (now - datetime.fromisoformat(p["at"])).days <= days]
        found = []
        yt = [p for p in recent if p["platform"] == "youtube"]
        if yt:
            data = _exec("YOUTUBE_GET_VIDEO_DETAILS_BATCH", {"id": [p["media_id"] for p in yt],
                                                             "parts": ["status", "contentDetails"]})
            videos = {v.get("id"): v for v in _find_items(data)}
            for p in yt:
                why = youtube_problem(videos.get(p["media_id"]))
                if why:
                    found.append((p, why))
        for p in [p for p in recent if p["platform"] == "instagram"]:
            try:
                _exec("INSTAGRAM_GET_IG_MEDIA", {"ig_media_id": p["media_id"], "fields": "id"})
                p["ig_missing"] = 0
            except PostError as e:
                msg = str(e).lower()
                if "timed out" in msg or "network" in msg:
                    continue
                p["ig_missing"] = p.get("ig_missing", 0) + 1
                if p["ig_missing"] >= 2:                    # gone twice in a row = removed by Instagram
                    found.append((p, "Instagram removed the post"))
        out = []
        for p, why in found:
            if p["streamer"] in blocked:
                continue
            blocked[p["streamer"]] = {"reason": why, "link": p.get("link"), "at": now.isoformat(timespec="seconds")}
            p["takedown"] = why
            notify.send(f"🚫 **{p['streamer']}**: {why} - {p.get('link')}\n"
                        f"Auto-posting for **{p['streamer']}** is STOPPED and will stay stopped until you tell me "
                        f"to turn it back on.")
            out.append({"streamer": p["streamer"], "reason": why, "link": p.get("link")})
        save_posted(posted)
    return out


def _window_over(cfg: dict, created: str, now: datetime) -> bool:
    """True once the posting window of the clip's day (or a later day's window) has passed."""
    b = cfg["posting"]["window_ist"][1]
    t = now.astimezone(IST)
    end_today = t.replace(hour=int(b[:2]), minute=int(b[3:]), second=0, microsecond=0)
    made = datetime.fromisoformat(created).astimezone(IST)
    return t >= end_today and made < end_today


def run_sharing(cfg: dict, now: datetime | None = None) -> list[dict]:
    """Clips that are NOT auto-posted (other streamers, and auto streamers' extras beyond the daily
    limits) are sent to you on the issue: EDITED + CLEAN download links, caption, campaign link."""
    sc = cfg["posting"].get("share") or {}
    if not sc.get("enabled"):
        return []
    from . import ghrelease
    if not ghrelease.available():
        return []
    with ledger_lock():
        return _run_sharing(cfg, now or datetime.now(timezone.utc), sc, ghrelease)


def _run_sharing(cfg, now, sc, ghrelease) -> list[dict]:
    posted = load_posted()
    posted.setdefault("shared", [])
    day = ist_day(now)
    left = int(sc.get("max_per_day", 10)) - sum(1 for x in posted["shared"] if x["day"] == day)
    if left <= 0:
        return []
    streamers = {s["channel"]: s for s in cfg["streamers"]}
    auto_ok = set(cfg["posting"].get("auto_streamers") or [])
    done = {p["clip"] for p in posted["posts"] if p.get("ok")}
    shared = {x["clip"] for x in posted["shared"]}
    caps_full = all(posts_today(posted, p, now) >= daily_cap(cfg, posted, p, now)
                    for p in PLATFORMS if cfg["posting"]["platforms"].get(p))
    out = []
    for item in order_queue(load_queue(), cfg):
        if left <= 0:
            break
        if item["id"] in done or item["id"] in shared or item.get("rejected") or not Path(item["file"]).exists():
            continue
        st = streamers.get(item["streamer"])
        if not st:
            continue
        auto = cfg["posting"]["auto"] and item["streamer"] in auto_ok
        if auto and not (caps_full or _window_over(cfg, item["created"], now)):
            continue                                   # will still be auto-posted
        item.setdefault("hash", file_hash(item["file"]))
        problems = rules_check(item, st, cfg, "instagram", posted)
        if problems:
            reject(item, problems)
            continue
        edited = ghrelease.upload(day, Path(item["file"]), f"{item['id']}_EDITED.mp4")
        clean = ghrelease.upload(day, Path(item["clean_file"]), f"{item['id']}_CLEAN.mp4") \
            if item.get("clean_file") and Path(item["clean_file"]).exists() else None
        caption = texts.post_caption(st, item["id"], "instagram")
        why = "beyond today's auto-post limit" if auto else "for you to post"
        notify.send(
            f"📥 **{st['name']}** clip {why} (score {item.get('score')})\n\n"
            f"- EDITED (ready to post): {edited}\n"
            + (f"- CLEAN (no text, original audio - add your own song in the app): {clean}\n" if clean else "")
            + f"\n**Caption** (copy-paste):\n```\n{caption}\n```\n"
            f"**Campaign:** {st['campaign']} - {st.get('campaign_url', '')}\n"
            f"⏱️ Post it, then submit the post link within 30 minutes.\n"
            f"Hook: {item.get('hook', '')} · source: {item.get('source', '')}")
        rec = {"clip": item["id"], "streamer": item["streamer"], "day": day, "at": now.isoformat(timespec="seconds"),
               "edited": edited, "clean": clean, "campaign": st["campaign"]}
        posted["shared"].append(rec)
        save_posted(posted)
        out.append(rec)
        left -= 1
    return out


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
    if posted.get("blocked_streamers"):
        lines += ["## 🚫 Streamers stopped after a copyright problem", ""] + [
            f"- **{k}**: {v['reason']} ({v.get('link')}, {v['at']})" for k, v in posted["blocked_streamers"].items()] + [""]
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
        sent = [x for x in posted.get("shared", []) if x["day"] == day]
        if sent:
            lines += [f"- Sent to you to post: **{len(sent)}**", ""]
            lines += [f"  - {x['streamer']}: [edited]({x['edited']})" + (f" · [clean]({x['clean']})" if x.get("clean")
                      else "") + f" → {x['campaign']}" for x in sent]
            lines.append("")
        if posts:
            lines += ["| Time (UTC) | Platform | Streamer | Link | Campaign |", "|---|---|---|---|---|"]
            lines += [f"| {p['at'][11:16]} | {p['platform']} | {p['streamer']} | "
                      f"{p.get('link') or 'ERROR: ' + p.get('error', '')[:80]} | {p['campaign']} |" for p in posts]
            lines.append("")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
