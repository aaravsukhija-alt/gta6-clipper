import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from clipper import audio, editor, publisher, texts, vision
from clipper.config import load_config
from clipper.transcript import Seg

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def kai(cfg):
    return next(s for s in cfg["streamers"] if s["channel"] == "kaicenat")


# ---------- loudness ----------
def test_spikes_found_where_it_gets_loud(tmp_path):
    rate = 8000
    t = np.arange(rate * 600) / rate
    x = 0.02 * np.sin(2 * np.pi * 220 * t)
    for s in (100, 400):
        x[s * rate:(s + 3) * rate] = 0.6 * np.sin(2 * np.pi * 300 * t[s * rate:(s + 3) * rate])
    wav = tmp_path / "a.wav"
    pcm = (x * 32767).astype(np.int16).tobytes()
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1",
                    "-i", "-", str(wav)], input=pcm, check=True)
    db = audio.loudness_profile(wav)
    assert 598 <= len(db) <= 602
    z = audio.spike_scores(db)
    peaks = audio.top_spikes(z, 5, min_gap=30)
    assert len(peaks) == 2 and abs(peaks[0] - 101) <= 2 and abs(peaks[1] - 401) <= 2
    zp, i = audio.peak_in(z, 90, 120)
    assert zp > 3 and 99 <= i <= 104


# ---------- face check ----------
def test_face_focus_no_face(tmp_path):
    v = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
                    "-t", "2", str(v)], check=True)
    r = vision.face_focus(v, samples=4)
    assert r["frames"] == 4 and r["ratio"] == 0.0


# ---------- texts / campaign rules ----------
def test_captions_follow_campaign_rules(cfg):
    for s in cfg["streamers"]:
        for platform in ("youtube", "instagram"):
            for seed in ("a", "b", "c", "d"):
                cap = texts.post_caption(s, seed, platform)
                assert texts.check_caption(s, cap) == [], (s["channel"], cap)
        assert len(texts.youtube_title(s, "WAIT FOR KAI'S REACTION")) <= 100
    assert texts.hook_text("bro what no way", "Kai", "x") in ("KAI COULD NOT BELIEVE THIS", "WAIT FOR KAI'S REACTION")
    assert texts.hook_text("hello chat", "Kai", "x").startswith(("KAI", "WAIT"))
    assert not texts.is_clean("some slur nigga here") and texts.is_clean("no way bro")


def test_check_caption_catches_missing_tags(kai):
    assert "caption missing #KaiCenat" in texts.check_caption(kai, "Kai Cenat #GTA6 #GTAVI")
    assert "caption does not name the streamer" in texts.check_caption(kai, "#KaiCenat #GTA6 #GTAVI")


# ---------- word captions + hook ----------
def test_word_ass_and_teaser_video(tmp_path):
    words = [(10 + i * 0.4, 10.35 + i * 0.4, w) for i, w in enumerate("bro what no way this is insane".split())]
    segs = [Seg(10, 13, "bro what no way this is insane", words)]
    ass = tmp_path / "c.ass"
    n = editor.write_word_ass(segs, 9, 14, ass, 1080, 1920, teaser=(2.0, 3.5))
    body = ass.read_text()
    assert n > 7 and "{\\c&H00FFFF&}BRO{\\c&HFFFFFF&}" in body
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "5", str(src)], check=True)
    out = editor.make_vertical(src, tmp_path / "o.mp4", "WAIT FOR KAI'S REACTION", "clip: @KaiCenat",
                               {"width": 1080, "height": 1920, "font_file": None}, ass, teaser=(2.0, 3.5))
    d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(out)], capture_output=True, text=True).stdout)
    assert abs(d - 6.5) < 0.3          # 1.5 s hook + 5 s clip


# ---------- posting queue / limits / error stop ----------
@pytest.fixture
def pub(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "STATE", tmp_path / "state")
    monkeypatch.setattr(publisher, "QUEUE_DIR", tmp_path / "state" / "queue")
    monkeypatch.setattr(publisher, "POSTED", tmp_path / "state" / "posted.json")
    monkeypatch.setattr(publisher, "REPORT", tmp_path / "daily_report.md")
    monkeypatch.setattr(publisher.notify, "send", lambda t: True)
    return tmp_path


def add_clip(tmp, cid, streamer, score):
    f = tmp / f"{cid}.mp4"
    f.write_bytes(b"x")
    publisher.enqueue({"id": cid, "file": str(f), "streamer": streamer, "hook": "WAIT FOR IT", "score": score,
                       "source": "https://www.twitch.tv/videos/1?t=5s", "created": "2026-11-20T00:00:00+00:00"},
                      tmp / "media")


IN_WINDOW = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)       # 05:30 IST


def test_window_caps_priority_and_no_double_posting(pub, cfg, monkeypatch):
    calls = []
    monkeypatch.setitem(publisher.POSTERS, "youtube", lambda it, st: calls.append(("yt", it["id"])) or f"yt/{it['id']}")
    monkeypatch.setitem(publisher.POSTERS, "instagram", lambda it, st: calls.append(("ig", it["id"])) or f"ig/{it['id']}")
    for i in range(12):
        add_clip(pub, f"lacy{i}", "lacy", 50 - i)
    add_clip(pub, "kai0", "kaicenat", 5)             # low score but Kai goes first
    assert publisher.run_posting(cfg, now=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)) == []  # outside window
    publisher.run_posting(cfg, now=IN_WINDOW)
    assert calls[0] == ("yt", "kai0") and ("ig", "kai0") in calls
    # spread out: first slot posts only a share of the day's cap
    assert 0 < sum(c[0] == "ig" for c in calls) < 8
    for minute in range(30, 300, 30):                 # rest of the window
        publisher.run_posting(cfg, now=IN_WINDOW.replace(hour=(minute // 60), minute=minute % 60))
    yt = [c for c in calls if c[0] == "yt"]
    ig = [c for c in calls if c[0] == "ig"]
    assert len(yt) == 6 and len(ig) == 8              # week-1 caps
    assert len(set(calls)) == len(calls)              # never the same clip twice per platform
    report = (pub / "daily_report.md").read_text()
    assert "2026-11-20" in report and "Kai Cenat - Content Rewards" in report


def test_error_stops_only_that_platform(pub, cfg, monkeypatch):
    def boom(it, st):
        raise publisher.PostError("quota exceeded")
    monkeypatch.setitem(publisher.POSTERS, "youtube", boom)
    monkeypatch.setitem(publisher.POSTERS, "instagram", lambda it, st: f"ig/{it['id']}")
    for i in range(3):
        add_clip(pub, f"k{i}", "kaicenat", 10 - i)
    publisher.run_posting(cfg, now=IN_WINDOW, force=True)
    p = json.loads((pub / "state" / "posted.json").read_text())
    assert "youtube" in p["disabled"] and "instagram" not in p["disabled"]
    assert sum(1 for r in p["posts"] if r["platform"] == "youtube") == 1     # stopped after the first error
    assert sum(1 for r in p["posts"] if r["platform"] == "instagram" and r["ok"]) == 3
    assert publisher.run_posting(cfg, now=IN_WINDOW, force=True, only=["youtube"]) == []


def test_week2_ramp(pub, cfg):
    p = publisher.load_posted()
    p["first_post_day"] = "2026-11-10"
    now = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)
    assert publisher.daily_cap(cfg, p, "instagram", now) == 15
    assert publisher.daily_cap(cfg, p, "youtube", now) == 6


def test_vod_pipeline_end_to_end_offline(tmp_path, monkeypatch, cfg, kai):
    from clipper import run, transcript, vod, ytdlp
    rate = 8000
    t = np.arange(rate * 900) / rate
    x = 0.02 * np.sin(2 * np.pi * 220 * t)
    for s in (200, 600):
        x[s * rate:(s + 4) * rate] = 0.7 * np.sin(2 * np.pi * 330 * t[s * rate:(s + 4) * rate])

    def fake_audio(url, workdir):
        workdir.mkdir(parents=True, exist_ok=True)
        f = workdir / "audio.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "-",
                        str(f)], input=(x * 32767).astype(np.int16).tobytes(), check=True)
        return f

    def fake_words(wav, model, offset=0.0):
        lines = ["we are driving in vice city", "bro what no way", "oh my god this is insane", "let's go"]
        segs, tt = [], offset + 2
        for ln in lines * 6:
            ws = [(tt + i * 0.4, tt + i * 0.4 + 0.35, w) for i, w in enumerate(ln.split())]
            segs.append(Seg(tt, ws[-1][1], ln, ws))
            tt = ws[-1][1] + 0.3
        return segs, "en", 0.95

    def fake_section(url, a, b, dest):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-t", f"{b - a:.2f}", str(dest)], check=True)
        return dest

    monkeypatch.setattr(ytdlp, "download_audio", fake_audio)
    monkeypatch.setattr(transcript, "transcribe_words", fake_words)
    monkeypatch.setattr(ytdlp, "download_section", fake_section)
    monkeypatch.setattr(vod.vision, "face_focus", lambda v, **k: {"ratio": 0.9, "size": 0.3, "frames": 12})
    job = run.Job("https://www.twitch.tv/videos/123", ["twitch:v123"], kai)
    info = {"id": "v123", "duration": 900, "title": "GTA 6 RP"}
    out = tmp_path / "out"
    out.mkdir()
    clips = vod.process_vod(job, info, cfg, 2, tmp_path / "work", out)
    assert len(clips) == 2
    for c in clips:
        assert 20 <= c["end"] - c["start"] <= 45
        assert Path(c["file"]).exists() and c["hook"].isupper() and "KAI" in c["hook"] or "GTA 6" in c["hook"]
        assert abs(c["start"] + 8 - 200) < 12 or abs(c["start"] + 8 - 600) < 12
        assert c["source"].startswith("https://www.twitch.tv/videos/123?t=")
    # streamer not on screen -> no clip
    monkeypatch.setattr(vod.vision, "face_focus", lambda v, **k: {"ratio": 0.1, "size": 0.0, "frames": 12})
    assert vod.process_vod(job, info, cfg, 2, tmp_path / "work2", out) == []
