"""Twitch VOD pipeline: loudness spikes -> Whisper (words) on just those spots -> score -> cut ->
face check -> 9:16 edit with word captions + hook -> posting queue."""
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import audio, editor, texts, transcript, vision, ytdlp
from .textmatch import REACTION_TAG, count_terms


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
    """20-45 s around the peak: a few seconds of build-up, then the reaction; snapped to word edges."""
    m = cfg["moments"]
    start = max(0.0, peak - m["seconds_before_peak"])
    end = min(duration, start + m["target_seconds"])
    words = sorted(wd for s in segs for wd in (s.words or []))
    before = [wd[0] for wd in words if wd[0] <= start + 0.01]
    if before and start - before[-1] < 2.5:
        start = max(0.0, before[-1] - 0.15)
    after = [wd[1] for wd in words if wd[1] >= end - 0.01]
    if after and after[0] - end < 3:
        end = min(duration, after[0] + 0.3)
    end = min(end, start + m["max_seconds"])
    if end - start < m["min_seconds"]:
        end = min(duration, start + m["min_seconds"])
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
        a = max(0.0, p - m["seconds_before_peak"] - 6)
        span = m["max_seconds"] + 12
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
                      "text": text[:500], "segs": inside, "loud": loud})
    return sorted(cands, key=lambda c: -c["score"])


def make_clip(job, info: dict, cand: dict, cfg: dict, work: Path, out_dir: Path) -> dict | None:
    st = job.streamer
    stem = f"{st['channel']}_{info['id']}_{int(cand['start'])}"
    raw = ytdlp.download_section(job.url, cand["start"], cand["end"], work / f"{stem}.raw.mp4")
    focus = vision.face_focus(raw, min_face_height=cfg["moments"]["min_face_height"])
    if focus["ratio"] < cfg["moments"]["min_face_ratio"]:
        log(f"  skip {stem}: streamer not the focus (face in {focus['ratio']:.0%} of frames)")
        raw.unlink(missing_ok=True)
        return None
    # teaser = loudest 1.5 s, shown first as the hook (only if the peak isn't already at the start)
    pk = cand["peak"] - cand["start"]
    teaser = (max(0.0, pk - 0.5), max(0.0, pk - 0.5) + cfg["edit"]["teaser_seconds"]) if pk > 3 else None
    hook = texts.hook_text(cand["text"], st["short_name"], stem)
    edit = cfg["edit"]
    ass = work / f"{stem}.ass"
    family = editor.pick_font(edit.get("font_file"))[1]
    n_caps = editor.write_word_ass(cand["segs"], cand["start"], cand["end"], ass, edit["width"], edit["height"],
                                   family, teaser)
    out = out_dir / f"{stem}_vertical.mp4"
    editor.make_vertical(raw, out, hook, f"clip: {st['credit']}", edit, ass if n_caps else None, teaser)
    raw.unlink(missing_ok=True)
    length = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                   str(out)], capture_output=True, text=True).stdout.strip() or 0)
    return {"id": stem, "file": str(out), "streamer": st["channel"], "hook": hook, "score": cand["score"],
            "why": cand["why"], "transcript": cand["text"], "face_ratio": round(focus["ratio"], 2),
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
