"""Twitch VOD pipeline: loudness spikes -> Whisper (words) on just those spots -> score -> cut ->
face check -> 9:16 edit with word captions + hook -> posting queue."""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import audio, editor, texts, transcript, vision, ytdlp
from .textmatch import REACTION_TAG, count_terms


def live_moments() -> list[dict]:
    """Moments the live recorder already clipped (shared file in the PC's media folder)."""
    from . import publisher
    f = publisher.media_dir() / "live_moments.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    except ValueError:
        return []


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def score_window(segs, z, a: float, b: float, cfg: dict) -> tuple[float, list[str], float]:
    w = cfg["moments"]["weights"]
    text = " ".join(s.text for s in segs if s.end > a and s.start < b)
    hype = count_terms(text, cfg["hype_phrases"])
    kw = count_terms(text, cfg["keywords"])
    tags = REACTION_TAG.findall(text)
    loud, _ = audio.peak_in(z, a, b)
    score = (w["loudness"] * max(0.0, loud) + w["hype"] * sum(hype.values())
             + w["keyword"] * sum(kw.values()) + w["reaction_tag"] * len(tags))
    why = [f"loud x{loud:.1f}"] + list(hype) + list(kw) + [f"[{t.lower()}]" for t in tags]
    return round(score, 2), why, loud


def bounds(segs, peak: float, cfg: dict, duration: float) -> tuple[float, float]:
    """~25 s of build-up before the spike and ~5 s after (15-30 s total including the 1.5 s hook),
    snapped to word edges so nobody is cut off mid-word."""
    m, teaser = cfg["moments"], cfg["edit"]["teaser_seconds"]
    max_len = m["max_seconds"] - teaser
    start = max(0.0, peak - m["seconds_before_peak"] + teaser)
    end = min(duration, peak + m["seconds_after_peak"])
    words = sorted(wd for s in segs for wd in (s.words or []))
    inside = [wd[0] for wd in words if start - 0.01 <= wd[0] <= start + 2.0]
    if inside:
        start = max(0.0, inside[0] - 0.15)            # start on the first word, not mid-word
    after = [wd[1] for wd in words if wd[1] >= end - 0.01]
    if after and after[0] - end < 2:
        end = min(duration, after[0] + 0.3)
    end = min(end, start + max_len)
    if end - start < m["min_seconds"] - teaser:
        start = max(0.0, end - (m["min_seconds"] - teaser))
    return round(start, 2), round(end, 2)


def find_candidates(audio_file: Path, info: dict, cfg: dict, work: Path) -> list[dict]:
    m = cfg["moments"]
    duration = float(info.get("duration") or 0)
    log("  loudness scan...")
    db = audio.loudness_profile(audio_file)
    z = audio.spike_scores(db)
    peaks = audio.top_spikes(z, m["loudness_windows"], min_gap=int(m["target_seconds"]), min_z=m["min_loudness_z"])
    log(f"  {len(peaks)} loud spots; transcribing them with Whisper ({cfg['transcript']['whisper_model']})")
    cands = []
    for n, p in enumerate(peaks):
        a = max(0.0, p - m["seconds_before_peak"] - 4)
        span = m["seconds_before_peak"] + m["seconds_after_peak"] + 8
        wav = audio.cut_audio(audio_file, a, span, work / f"w{n}.wav")
        segs, lang, prob = transcript.transcribe_words(wav, cfg["transcript"]["whisper_model"], offset=a)
        wav.unlink(missing_ok=True)
        if lang != "en" or prob < m["min_english_prob"]:
            continue                                         # English only
        start, end = bounds(segs, p, cfg, duration or a + span)
        inside = [s for s in segs if s.end > start and s.start < end]
        text = " ".join(s.text for s in inside)
        if not texts.is_clean(text) or not text.strip():
            continue
        score, why, loud = score_window(segs, z, start, end, cfg)
        if score < m["min_score"]:
            continue
        cands.append({"start": start, "end": end, "peak": p, "score": score, "why": why,
                      "text": text[:500], "segs": inside, "loud": loud, "lang": lang})
    return sorted(cands, key=lambda c: -c["score"])


def make_clip(job, info: dict, cand: dict, cfg: dict, work: Path, out_dir: Path) -> dict | None:
    st = job.streamer
    stem = f"{st['channel']}_{info['id']}_{int(cand['start'])}"
    raw = ytdlp.download_section(job.url, cand["start"], cand["end"], work / f"{stem}.raw.mp4")
    edit = cfg["edit"]
    lay = vision.layout_for(raw, edit["width"], edit["height"], st)
    if lay["face_ratio"] < cfg["moments"]["min_face_ratio"]:
        log(f"  skip {stem}: streamer not on screen (face in {lay['face_ratio']:.0%} of frames)")
        raw.unlink(missing_ok=True)
        return None
    # teaser = loudest 1.5 s, shown first as the hook (only if the peak isn't already at the start)
    pk = cand["peak"] - cand["start"]
    teaser = (max(0.0, pk - 0.5), max(0.0, pk - 0.5) + cfg["edit"]["teaser_seconds"]) if pk > 3 else None
    hook = texts.hook_text(cand["text"], st["short_name"], stem)
    ass = work / f"{stem}.ass"
    family = editor.pick_font(edit.get("font_file"))[1]
    n_caps = editor.write_word_ass(cand["segs"], cand["start"], cand["end"], ass, edit["width"], edit["height"],
                                   family, teaser)
    out = out_dir / f"{stem}_vertical.mp4"
    editor.make_vertical(raw, out, hook, f"clip: {st['credit']}", edit, ass if n_caps else None, teaser, lay)
    raw.unlink(missing_ok=True)
    length = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                   str(out)], capture_output=True, text=True).stdout.strip() or 0)
    return {"id": stem, "file": str(out), "streamer": st["channel"], "hook": hook, "score": cand["score"],
            "why": cand["why"], "transcript": cand["text"], "face_ratio": round(lay["face_ratio"], 2),
            "layout": lay.get("mode"), "facecam": lay.get("box"), "lang": cand.get("lang", "en"),
            "source": f"https://www.twitch.tv/videos/{str(info['id']).lstrip('v')}?t={int(cand['start'])}s",
            "vod_title": info.get("title", ""), "length": round(length, 1), "start": cand["start"],
            "end": cand["end"], "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def process_vod(job, info: dict, cfg: dict, keep: int, work: Path, out_dir: Path) -> list[dict]:
    """Returns up to `keep` finished clips for this VOD, best first."""
    audio_file = ytdlp.download_audio(job.url, work / "audio")
    try:
        cands = find_candidates(audio_file, info, cfg, work)
    finally:
        audio_file.unlink(missing_ok=True)
    vod_start = info.get("timestamp")
    live = [m for m in live_moments() if m["streamer"] == job.streamer["channel"]]
    if vod_start and live:
        before = len(cands)
        cands = [c for c in cands if all(abs(vod_start + c["peak"] - m["utc"]) > 60 for m in live)]
        if before != len(cands):
            log(f"  {before - len(cands)} moment(s) already clipped live - skipped")
    log(f"  {len(cands)} scored candidates")
    clips = []
    for cand in cands[: cfg["moments"]["candidates_per_vod"]]:
        if any(cand["start"] < c["end"] and c["start"] < cand["end"] for c in clips):
            continue
        clip = make_clip(job, info, cand, cfg, work, out_dir)
        if clip:
            clips.append(clip)
            log(f"  clip {clip['id']} score {clip['score']} face {clip['face_ratio']:.0%} -> {Path(clip['file']).name}")
        if len(clips) >= keep:
            break
    return clips
