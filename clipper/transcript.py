"""Get a timed transcript: YouTube captions first, local Whisper as fallback. No API keys."""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import ytdlp


@dataclass
class Seg:
    start: float
    end: float
    text: str


def parse_json3(data: dict) -> list[Seg]:
    segs = []
    for ev in data.get("events", []):
        if "segs" not in ev:
            continue
        text = "".join(s.get("utf8", "") for s in ev["segs"]).replace("\n", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        start = ev.get("tStartMs", 0) / 1000
        segs.append(Seg(start, start + ev.get("dDurationMs", 0) / 1000, text))
    return normalize(segs)


_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")


def _ts(s: str) -> float:
    m = _TS.search(s)
    h, mnt, sec, ms = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(sec) + int(ms) / 1000


def parse_vtt(text: str) -> list[Seg]:
    segs, last = [], None
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = block.strip().split("\n")
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        a, b = lines[idx].split("-->")
        body = " ".join(re.sub(r"<[^>]+>", "", l) for l in lines[idx + 1:])
        body = re.sub(r"\s+", " ", body).strip()
        if body and body != last:
            segs.append(Seg(_ts(a), _ts(b), body))
            last = body
    return normalize(segs)


def normalize(segs: list[Seg]) -> list[Seg]:
    """Sort, drop empties, and stop captions overlapping (YouTube auto-captions roll over each other)."""
    segs = sorted((s for s in segs if s.text.strip()), key=lambda s: s.start)
    for cur, nxt in zip(segs, segs[1:]):
        if cur.end > nxt.start:
            cur.end = nxt.start
    for s in segs:
        if s.end <= s.start:
            s.end = s.start + 0.5
    return segs


def pick_caption_track(info: dict, languages: list[str]) -> tuple[str, bool] | None:
    """Return (lang, is_auto). Manual captions > original-language auto captions > other auto."""
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    for lang in languages:
        for k in manual:
            if k == lang or k.startswith(lang + "-"):
                return k, False
    for lang in languages:
        for k in (f"{lang}-orig", lang):
            if k in auto:
                return k, True
    orig = [k for k in auto if k.endswith("-orig")]   # video's own spoken language
    if orig:
        return orig[0], True
    return None


def captions(info: dict, languages: list[str], workdir: Path) -> list[Seg]:
    track = pick_caption_track(info, languages)
    if not track:
        return []
    path = ytdlp.download_subtitle(info, track[0], track[1], workdir)
    if not path:
        return []
    raw = path.read_text(encoding="utf-8", errors="ignore")
    return parse_json3(json.loads(raw)) if path.suffix == ".json3" else parse_vtt(raw)


def transcribe_file(audio: Path, model_name: str, language: str | None) -> list[Seg]:
    from faster_whisper import WhisperModel   # imported lazily: heavy
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 2)
    it, _ = model.transcribe(str(audio), language=language or None, vad_filter=True,
                             beam_size=1, condition_on_previous_text=False)
    return normalize([Seg(s.start, s.end, s.text.strip()) for s in it])


def whisper(url: str, model_name: str, language: str | None, workdir: Path) -> list[Seg]:
    audio = ytdlp.download_audio(url, workdir)
    try:
        return transcribe_file(audio, model_name, language)
    finally:
        audio.unlink(missing_ok=True)
