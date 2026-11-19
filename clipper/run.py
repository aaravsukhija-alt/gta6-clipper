"""One run: find new videos -> transcript -> pick GTA 6 moments -> cut + edit clips -> summary.

  python -m clipper.run                      # channels in config.yaml + links.txt
  python -m clipper.run --url <video link>   # also clip this link now
"""
import argparse
import json
import re
import shutil
import sys
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import editor, transcript, vod, ytdlp
from .config import OUTPUT, load_config, read_links
from .selector import find_moments
from .state import State
from .textmatch import find_terms

LIVE_STATES = {"is_live", "is_upcoming", "post_live"}
BLOCKED = ("confirm you", "not a bot", "HTTP Error 429")   # site is blocking this server, not a bad video


@dataclass
class Job:
    url: str
    keys: list[str]
    streamer: dict
    forced: bool = False      # link given by you: always treated as GTA 6 content


@dataclass
class Report:
    clips: list[dict] = field(default_factory=list)
    videos: list[dict] = field(default_factory=list)
    errors: int = 0
    per_streamer: dict = field(default_factory=dict)
    queued: list[dict] = field(default_factory=list)


def _before_min_date(info: dict, cfg: dict) -> bool:
    md = cfg["discovery"].get("min_date")
    ts = info.get("timestamp") or info.get("release_timestamp")
    if not md or not ts:
        return False
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat() < str(md)


def process_twitch_vod(job, info, cfg, state, keys, title, run_dir, report) -> str:
    st = job.streamer
    cap = cfg["moments"]["keep_per_streamer_per_run"]
    have = report.per_streamer.get(st["channel"], 0)
    if have >= cap:
        state.set(keys, "deferred", note="streamer already has enough clips this run", title=title)
        return "later (enough clips from this streamer this run)"
    work = Path(tempfile.mkdtemp(prefix="gta6_"))
    try:
        clips = vod.process_vod(job, info, cfg, cap - have, work, run_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for c in clips:
        report.queued.append(c)
        report.clips.append({"files": [Path(c["file"]).name], "credit": st["credit"], "video_title": title,
                             "source": c["source"], "start": c["start"], "end": c["end"], "length": c["length"],
                             "score": c["score"], "why": c["why"], "transcript": c["transcript"],
                             "transcript_source": "whisper", "hook": c["hook"], "face_ratio": c["face_ratio"],
                             "suggested_caption": ""})
    report.per_streamer[st["channel"]] = have + len(clips)
    state.set(keys, "done", clips=len(clips), title=title, note="twitch vod pipeline")
    return f"{len(clips)} clip(s)"


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def channel_tabs(s: dict) -> list[str]:
    ch = str(s["channel"]).strip()
    if s["platform"] == "twitch":
        login = ch.rstrip("/").split("/")[-1].lower()
        return [f"https://www.twitch.tv/{login}/videos?filter=archives&sort=time"]
    base = ch.rstrip("/") if ch.startswith("http") else "https://www.youtube.com/" + (ch if ch.startswith("@") else "@" + ch)
    base = re.sub(r"/(videos|streams|shorts|featured|live)$", "", base)
    return [base + "/videos", base + "/streams"]


def discover(cfg: dict, state: State, extra_urls: list[str], use_links: bool) -> list[Job]:
    jobs, seen_urls = [], set()
    links = list(extra_urls) + (read_links() if use_links else [])
    for url in links:
        key = f"link:{url}"
        if url in seen_urls or state.is_final(key):
            continue
        seen_urls.add(url)
        jobs.append(Job(url, [key], {"credit": "", "channel": "", "platform": "link"}, forced=True))

    limit = cfg["discovery"]["recent_videos_per_channel"]
    for s in cfg["streamers"]:
        if s.get("platform") not in ("youtube", "twitch") or not s.get("channel"):
            log(f"config: skipping bad streamer entry {s}")
            continue
        if not s.get("permission"):
            continue
        s.setdefault("credit", "@" + str(s["channel"]).lstrip("@").split("/")[-1])
        for tab in channel_tabs(s):
            try:
                entries = ytdlp.list_channel(tab, limit)
            except ytdlp.YtDlpError as e:
                if not tab.endswith("/streams"):       # many channels simply have no Live tab
                    log(f"could not list {tab}: {e}")
                continue
            for e in entries:
                key = f"{s['platform']}:{e['id']}"
                if state.is_final(key):
                    continue
                title = e.get("title")
                if title and not s.get("scan_all_videos") and not find_terms(title, cfg["keywords"]):
                    state.set([key], "skipped", note="title has no GTA 6 keyword", title=title)
                    continue
                url = e.get("url") or e.get("webpage_url")
                if not url or url in seen_urls:
                    continue
                if not url.startswith("http"):
                    url = f"https://www.youtube.com/watch?v={e['id']}" if s["platform"] == "youtube" else url
                seen_urls.add(url)
                jobs.append(Job(url, [key], s))
    return jobs


def _age_days(info: dict) -> float | None:
    ts = info.get("timestamp") or info.get("release_timestamp")
    if not ts and info.get("upload_date"):
        ts = datetime.strptime(info["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
    return (time.time() - ts) / 86400 if ts else None


def _link_at(info: dict, t: float) -> str:
    url = info.get("webpage_url") or ""
    t = int(t)
    if "twitch.tv" in url:
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        return f"{url}{'&' if '?' in url else '?'}t={h}h{m}m{s}s"
    if "youtube.com" in url or "youtu.be" in url:
        return f"{url}{'&' if '?' in url else '?'}t={t}s"
    return url


def credit_from_info(info: dict) -> str:
    """@handle of the creator. YouTube gives '@handle' as uploader_id; on Twitch clips the
    uploader is whoever made the clip, so the broadcaster ('channel') is used instead."""
    handle = str(info.get("uploader_id") or "")
    if not handle.startswith("@"):
        handle = str(info.get("channel") or info.get("uploader") or handle)
    handle = re.sub(r"\s+", "", handle).lstrip("@")
    return "@" + handle if handle else ""


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:40] or "clip"


def _mmss(t: float) -> str:
    t = int(t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m:02d}m{s:02d}s"


def process(job: Job, cfg: dict, state: State, run_dir: Path, report: Report) -> str:
    info = ytdlp.video_info(job.url)
    canon = f"{info.get('extractor_key', 'video').lower()}:{info.get('id')}"
    keys = job.keys + [canon]
    title = info.get("title") or ""
    if state.is_final(canon):
        state.set(job.keys, "done", note="already processed", title=title)
        return "already processed"

    if info.get("is_live") or info.get("live_status") in LIVE_STATES:
        state.set(keys, "deferred", note=f"stream not finished ({info.get('live_status')})", title=title)
        return "waiting for the stream to end"

    if not job.forced and _before_min_date(info, cfg):
        state.set(keys, "skipped", note=f"before {cfg['discovery']['min_date']}", title=title)
        return "before the start date"

    age = _age_days(info)
    if not job.forced and age is not None and age > cfg["discovery"]["max_age_days"]:
        state.set(keys, "skipped", note=f"older than {cfg['discovery']['max_age_days']} days", title=title)
        return "too old"

    meta_text = " ".join([title] + list(info.get("categories") or []) + list(info.get("tags") or []))
    title_relevant = job.forced or bool(find_terms(meta_text, cfg["keywords"]))
    if not title_relevant and not job.streamer.get("scan_all_videos"):
        state.set(keys, "skipped", note="not a GTA 6 video", title=title)
        return "not GTA 6"

    ek = str(info.get("extractor_key", ""))
    if (ek.startswith("TwitchVod") or ek == "Youtube") and job.streamer.get("channel"):
        return process_twitch_vod(job, info, cfg, state, keys, title, run_dir, report)

    tcfg = cfg["transcript"]
    work = Path(tempfile.mkdtemp(prefix="gta6_"))
    try:
        segs, source = [], "none"
        if info.get("subtitles") or info.get("automatic_captions"):
            segs = transcript.captions(info, tcfg["languages"], work)
            source = "captions" if segs else "none"
        if not segs:
            waits = state.get(canon).get("caption_waits", 0)
            duration_min = (info.get("duration") or 0) / 60
            is_youtube = "youtube" in canon
            if is_youtube and waits < tcfg["caption_wait_runs"] and (age is None or age < 2):
                state.set(keys, "deferred", caption_waits=waits + 1, title=title,
                          note="no captions yet (YouTube adds them a few hours after upload)")
                return "waiting for captions"
            if tcfg["whisper_fallback"] and 0 < duration_min <= tcfg["whisper_max_minutes"]:
                log(f"  transcribing {duration_min:.0f} min with Whisper ({tcfg['whisper_model']})...")
                segs = transcript.whisper(job.url, tcfg["whisper_model"], tcfg["whisper_language"], work / "audio")
                source = "whisper"
        log(f"  transcript: {len(segs)} lines from {source}")

        moments = find_moments(segs, cfg, info.get("heatmap"), title_relevant, info.get("duration"))
        if not moments:
            state.set(keys, "done", clips=0, title=title, note=f"no strong GTA 6 moments (transcript: {source})")
            return "no strong moments"

        credit = job.streamer.get("credit") or credit_from_info(info)
        edit = cfg["edit"]
        name_base = _slug(credit) + "_" + _slug(str(info.get("id")))
        made = 0
        for n, m in enumerate(moments, 1):
            stem = f"{name_base}_{n}_{_mmss(m.start)}"
            raw = ytdlp.download_section(job.url, m.start, m.end, work / f"{stem}.raw.mp4")
            files = []
            if edit["vertical"]:
                ass = None
                if edit["burn_captions"] and segs:
                    ass = work / f"{stem}.ass"
                    family = editor.pick_font(edit.get("font_file"))[1]
                    if not editor.write_ass(segs, m.start, m.end, ass, edit["width"], edit["height"], family):
                        ass = None
                out = run_dir / f"{stem}_vertical.mp4"
                editor.make_vertical(raw, out, edit["headline"], f"clip: {credit}" if credit.strip("@") else "", edit, ass)
                files.append(out.name)
            if edit["keep_original"] or not edit["vertical"]:
                out = run_dir / f"{stem}_original.mp4"
                editor.export_original(raw, out)
                files.append(out.name)
            raw.unlink(missing_ok=True)
            made += 1
            report.clips.append({
                "files": files, "credit": credit, "video_title": title,
                "source": _link_at(info, m.start), "start": m.start, "end": m.end,
                "length": round(m.end - m.start, 1), "score": m.score, "why": m.reasons,
                "transcript": m.excerpt, "transcript_source": source,
                "suggested_caption": f"{edit['headline']} | clip from {credit}\n#gta6 #gtavi #gaming #shorts",
            })
            log(f"  clip {n}: {_mmss(m.start)}-{_mmss(m.end)} score {m.score} -> {', '.join(files)}")
        state.set(keys, "done", clips=made, title=title, note=f"transcript: {source}")
        return f"{made} clip(s)"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def write_summary(report: Report, run_dir: Path, started: str):
    (run_dir / "clips.json").write_text(json.dumps(asdict(report), indent=1, ensure_ascii=False), encoding="utf-8")
    md = [f"# GTA 6 clips - run {started} UTC", ""]
    if report.clips:
        md.append(f"**{len(report.clips)} new clip(s).** Download them from the *Artifacts* section of this run.\n")
        for c in report.clips:
            md += [f"### {', '.join(c['files'])}",
                   f"- From: {c['credit']} - [{c['video_title']}]({c['source']})",
                   f"- Length: {c['length']}s, score {c['score']} ({', '.join(c['why'])})",
                   f"- Transcript ({c['transcript_source']}): \"{c['transcript']}\"", ""]
    else:
        md.append("No new clips this run.\n")
    if report.videos:
        md += ["## Videos checked", "", "| Video | Result |", "|---|---|"]
        md += [f"| {v['title'] or v['url']} | {v['result']} |".replace("\n", " ") for v in report.videos]
    text = "\n".join(md) + "\n"
    (run_dir / "summary.md").write_text(text, encoding="utf-8")
    (OUTPUT / "summary.md").write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", action="append", default=[], help="extra video link(s) to clip")
    ap.add_argument("--config")
    ap.add_argument("--no-links", action="store_true", help="ignore links.txt")
    ap.add_argument("--streamer", help="only this channel (test)")
    args = ap.parse_args(argv)
    return run_clips(args)


def run_clips(args, on_clips=None, between=None, should_stop=None) -> int:

    cfg = load_config(args.config)
    state = State()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    run_dir = OUTPUT / started
    run_dir.mkdir(parents=True, exist_ok=True)
    report = Report()

    extra = [u.strip() for u in args.url if u and u.strip()]
    try:
        if getattr(args, "streamer", None):
            cfg["streamers"] = [x for x in cfg["streamers"] if x.get("channel") == args.streamer]
        jobs = discover(cfg, state, extra, not args.no_links)
        state.save()
        cap = cfg["discovery"]["max_videos_per_run"]
        log(f"{len(jobs)} video(s) to check" + (f", doing {cap} now" if len(jobs) > cap else ""))
        for job in jobs[:cap]:
            if should_stop and should_stop():
                log("stopping early (a manual run is waiting)")
                break
            log(f"video: {job.url}")
            try:
                result = process(job, cfg, state, run_dir, report)
            except Exception as e:
                report.errors += 1
                if any(b in str(e) for b in BLOCKED):
                    result = "BLOCKED by the site (see README: YouTube blocking) - will retry next run"
                    state.set(job.keys, "deferred", note=str(e)[:300])
                else:
                    result = f"error: {e}"
                    state.error(job.keys, str(e))
                    traceback.print_exc()
            log(f"  -> {result}")
            report.videos.append({"url": job.url, "title": state.get(job.keys[0]).get("title", ""), "result": result})
            state.save()
            if on_clips and report.queued:
                on_clips(report.queued)
                report.queued = []
            if between:
                between()
    finally:
        state.save()
        write_summary(report, run_dir, started)
    log(f"finished: {len(report.clips)} clip(s), {report.errors} error(s)")
    # fail the run (GitHub emails you) only if every video we tried crashed - usually a blocked server
    return 1 if report.videos and report.errors == len(report.videos) else 0


if __name__ == "__main__":
    sys.exit(main())
