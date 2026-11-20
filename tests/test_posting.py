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
    monkeypatch.setattr(publisher, "MEDIA", tmp_path / "media")
    monkeypatch.setattr(publisher, "QUEUE_DIR", tmp_path / "media" / "queue")
    monkeypatch.setattr(publisher, "POSTED", tmp_path / "media" / "posted.json")
    monkeypatch.setattr(publisher, "REPORT", tmp_path / "daily_report.md")
    monkeypatch.setattr(publisher.notify, "send", lambda t: True)
    return tmp_path


def add_clip(tmp, cid, streamer, score):
    f = tmp / f"{cid}.mp4"
    f.write_bytes(b"x")
    f.write_bytes(cid.encode())                              # different content per clip
    publisher.enqueue({"id": cid, "file": str(f), "streamer": streamer, "hook": "WAIT FOR IT", "score": score,
                       "source": f"https://www.twitch.tv/videos/1?t={len(cid)}{score}s", "face_ratio": 0.9,
                       "length": 25.0, "lang": "en", "transcript": "no way bro",
                       "created": "2026-11-20T00:00:00+00:00"}, tmp / "media")


IN_WINDOW = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)       # 05:30 IST


def test_window_caps_priority_and_no_double_posting(pub, cfg, monkeypatch):
    cfg["posting"].update(window_ist=["05:00", "10:00"], auto_streamers=["kaicenat"])
    calls = []
    monkeypatch.setitem(publisher.POSTERS, "youtube", lambda it, st: calls.append(("yt", it["id"])) or f"yt/{it['id']}")
    monkeypatch.setitem(publisher.POSTERS, "instagram", lambda it, st: calls.append(("ig", it["id"])) or f"ig/{it['id']}")
    for i in range(16):
        add_clip(pub, f"kai{i + 1}", "kaicenat", 50 - i)
    add_clip(pub, "lacy0", "lacy", 99)                # other streamers are never auto-posted
    add_clip(pub, "kai0", "kaicenat", 60)             # best score goes first
    assert publisher.run_posting(cfg, now=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)) == []  # outside window
    publisher.run_posting(cfg, now=IN_WINDOW)
    assert calls[0] == ("yt", "kai0") and ("ig", "kai0") not in calls   # one platform per clip
    # spread out: first slot posts only a share of the day's cap
    assert 0 < sum(c[0] == "ig" for c in calls) < 8
    for minute in range(30, 300, 30):                 # rest of the window
        publisher.run_posting(cfg, now=IN_WINDOW.replace(hour=(minute // 60), minute=minute % 60))
    yt = [c for c in calls if c[0] == "yt"]
    ig = [c for c in calls if c[0] == "ig"]
    assert len(yt) == 6 and len(ig) == 8              # week-1 caps
    assert len({c[1] for c in calls}) == len(calls)   # every post is a different clip
    assert not any(c[1] == "lacy0" for c in calls)
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
    p = json.loads((pub / "media" / "posted.json").read_text())
    assert "youtube" in p["disabled"] and "instagram" not in p["disabled"]
    assert sum(1 for r in p["posts"] if r["platform"] == "youtube") == 1     # stopped after the first error
    assert sum(1 for r in p["posts"] if r["platform"] == "instagram" and r["ok"]) == 3
    assert publisher.run_posting(cfg, now=IN_WINDOW, force=True, only=["youtube"]) == []


def test_rules_check_rejects_bad_clips(pub, cfg, monkeypatch):
    calls = []
    monkeypatch.setitem(publisher.POSTERS, "youtube", lambda it, st: calls.append(it["id"]) or "yt")
    monkeypatch.setitem(publisher.POSTERS, "instagram", lambda it, st: calls.append(it["id"]) or "ig")
    add_clip(pub, "good", "kaicenat", 10)
    for cid, change in [("noface", {"face_ratio": 0.1}), ("spanish", {"lang": "es"}), ("long", {"length": 44}),
                        ("slur", {"transcript": "nigga what"}), ("neg", {"hook": "KAI EXPOSED FOR CHEATING"})]:
        add_clip(pub, cid, "kaicenat", 50)
        f = publisher.QUEUE_DIR / f"{cid}.json"
        f.write_text(json.dumps(dict(json.loads(f.read_text()), **change)))
    publisher.run_posting(cfg, now=IN_WINDOW, force=True)
    assert set(calls) == {"good"}
    q = {x["id"]: x for x in publisher.load_queue()}
    assert all(q[c].get("rejected") for c in ("noface", "spanish", "long", "slur", "neg"))
    assert "rejected by rules: 5" in (pub / "daily_report.md").read_text()


def test_same_video_twice_is_rejected(pub, cfg, monkeypatch):
    monkeypatch.setitem(publisher.POSTERS, "youtube", lambda it, st: "yt")
    monkeypatch.setitem(publisher.POSTERS, "instagram", lambda it, st: "ig")
    add_clip(pub, "a1", "kaicenat", 10)
    publisher.run_posting(cfg, now=IN_WINDOW, force=True)
    add_clip(pub, "a2", "kaicenat", 10)
    (pub / "a2.mp4").write_bytes((pub / "media" / "a1.mp4").read_bytes())       # identical file, new id
    (publisher.MEDIA / "a2.mp4").write_bytes((pub / "media" / "a1.mp4").read_bytes())
    res = publisher.run_posting(cfg, now=IN_WINDOW, force=True)
    assert res == [] and publisher.load_queue()[1]["rejected"]


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
    monkeypatch.setattr(vod.vision, "layout_for", lambda v, w, h, o=None: {"mode": "split", "face_ratio": 0.9,
                                                                         "box": [0.7, 0.0, 0.3, 0.35]})
    monkeypatch.setattr(vod, "live_moments", lambda: [{"streamer": "kaicenat", "utc": 1_800_000_000 + 600}])
    job = run.Job("https://www.twitch.tv/videos/123", ["twitch:v123"], kai)
    info = {"id": "v123", "duration": 900, "title": "GTA 6 RP", "timestamp": 1_800_000_000}
    out = tmp_path / "out"
    out.mkdir()
    clips = vod.process_vod(job, info, cfg, 2, tmp_path / "work", out)
    assert len(clips) == 1                       # the spike at 600 s was already clipped live
    c = clips[0]
    assert 15 <= c["length"] <= 30.5, c["length"]                      # final video incl. 1.5 s hook
    assert 195 - 25 <= c["start"] <= 200 - 20 and 200 + 3 <= c["end"] <= 200 + 8, (c["start"], c["end"])
    assert Path(c["file"]).exists() and "KAI" in c["hook"]
    assert c["source"].startswith("https://www.twitch.tv/videos/123?t=") and c["layout"] == "split"
    # streamer not on screen -> no clip
    monkeypatch.setattr(vod.vision, "layout_for", lambda v, w, h, o=None: {"mode": "blur", "face_ratio": 0.1})
    assert vod.process_vod(job, info, cfg, 2, tmp_path / "work2", out) == []



# ---------- upgrades: hooks, layout, live mode ----------
def test_event_hooks_tell_what_happens():
    assert texts.hook_text("yo there's like six cop cars behind us", "Kai", "s") == "KAI GETS CHASED BY 6 COP CARS"
    assert texts.hook_text("we got five stars now", "Lacy", "s") == "LACY HITS A 5 STAR WANTED LEVEL"
    assert texts.hook_text("bro is that a gator", "Kai", "s") == "KAI RUNS INTO A GATOR IN GTA 6"
    assert texts.hook_text("let's rob the bank", "Clix", "s") == "CLIX PULLS OFF A HEIST IN GTA 6"
    assert texts.hook_text("hello chat", "Kai", "s")                  # falls back to a generic hook
    assert editor.wrap_hook("KAI GETS CHASED BY 6 COP CARS").count("\n") == 1


def test_layout_falls_back_to_blur_without_faces_and_respects_override(tmp_path):
    v = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
                    "-t", "2", str(v)], check=True)
    assert vision.layout_for(v, 1080, 1920)["mode"] == "blur"
    lay = vision.layout_for(v, 1080, 1920, {"facecam": [0.7, 0.02, 0.28, 0.3]})
    assert lay == {"mode": "split", "box": [0.7, 0.02, 0.28, 0.3], "face_ratio": 0.0, "auto": False}
    assert vision.layout_for(v, 1080, 1920, {"layout": "blur", "facecam": [0, 0, 1, 1]})["mode"] == "blur"


def test_split_layout_renders_1080x1920(tmp_path):
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3", str(src)], check=True)
    out = editor.make_vertical(src, tmp_path / "o.mp4", "KAI GETS CHASED BY 6 COP CARS", "clip: @KaiCenat",
                               {"width": 1080, "height": 1920, "font_file": None}, None, None,
                               {"mode": "split", "box": [0.72, 0.03, 0.26, 0.33]})
    pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
    assert pr == "1080,1920"


def test_chat_spike_detection():
    from clipper import live
    cfg = load_config(ROOT / "config.yaml")
    fired = []
    w = live.ChatWatcher("kaicenat", cfg["live"], lambda now, st: fired.append(st))
    t = 1000.0
    for sec in range(600):                                   # 10 min of normal chat: 0.5 msg/s
        if sec % 2 == 0:
            w.msgs.append((t + sec, False))
        if sec % 2 == 0:
            w.evaluate(t + sec)
    assert fired == []
    now = t + 600
    for i in range(80):                                      # 80 msgs in 20 s, mostly hype
        w.msgs.append((now + i * 0.25, i % 3 != 0))
    w.evaluate(now + 20)
    assert len(fired) == 1 and fired[0]["ratio"] >= 3
    for i in range(80):
        w.msgs.append((now + 21 + i * 0.25, True))
    w.evaluate(now + 41)
    assert len(fired) == 1                                   # cooldown
    assert w.is_hype("KEKW KEKW") and w.is_hype("bro no way") and not w.is_hype("hello there")


def test_recorder_cut_from_rolling_segments(tmp_path):
    import os
    import time as _t
    from clipper import live
    cfg = load_config(ROOT / "config.yaml")
    rec = live.Recorder("kaicenat", cfg["live"], tmp_path / "rec")
    rec.dir.mkdir(parents=True)
    now = _t.time()
    for i in range(8):                                       # 8 x 6 s segments ending now
        f = rec.dir / f"seg{i:06d}.ts"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6", "-c:v", "libx264", "-c:a", "aac",
                        "-f", "mpegts", str(f)], check=True)
        end = now - (7 - i) * 6
        os.utime(f, (end, end))
    out = rec.cut(now - 40, now - 10, tmp_path / "cut.mp4")
    d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(out)], capture_output=True, text=True).stdout)
    assert abs(d - 30) < 1.0
    rec.cfg = dict(rec.cfg, buffer_seconds=20)
    rec.prune()
    assert len(list(rec.dir.glob("seg*.ts"))) < 8


def test_live_clip_and_instant_post(tmp_path, monkeypatch, pub, cfg):
    from clipper import live, transcript
    src = tmp_path / "raw.mp4"                                  # the live cut: 23.5 s before + 5 s after
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "28.5", str(src)], check=True)
    words = [(20 + i * 0.4, 20.35 + i * 0.4, w) for i, w in enumerate("bro six cop cars no way oh my god".split())]
    monkeypatch.setattr(transcript, "transcribe_words",
                        lambda wav, model, offset=0.0: ([Seg(20, 24, "bro six cop cars no way oh my god", words)],
                                                        "en", 0.97))
    monkeypatch.setattr(live.vision, "layout_for", lambda v, w, h, o=None: {"mode": "blur", "face_ratio": 0.8})
    st = next(s for s in cfg["streamers"] if s["channel"] == "kaicenat")
    clip = live.make_live_clip(st, src, {"ratio": 4.0, "messages": 90, "hype": 50}, cfg, tmp_path)
    assert clip and clip["hook"] == "KAI GETS CHASED BY 6 COP CARS" and clip["score"] >= cfg["live"]["post_min_score"]
    assert 15 <= clip["length"] <= 30.2
    out_len = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    clip["file"]], capture_output=True, text=True).stdout)
    assert 15 <= out_len <= 30.2, out_len                        # final video with the 1.5 s hook
    calls = []
    monkeypatch.setitem(publisher.POSTERS, "youtube", lambda it, s: calls.append(it["id"]) or "yt/x")
    monkeypatch.setitem(publisher.POSTERS, "instagram", lambda it, s: calls.append(it["id"]) or "ig/x")
    publisher.enqueue(clip)
    add_clip(pub, "older", "kaicenat", 99)                      # a higher score in the queue is NOT posted
    res = publisher.run_posting(cfg, now=datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc), force=True,
                                ids=[clip["id"]])
    assert [r["platform"] for r in res] == ["youtube"] and set(calls) == {clip["id"]}   # one platform per clip



def test_sharing_sends_others_and_kai_overflow(pub, cfg, monkeypatch):
    from clipper import ghrelease
    cfg["posting"].update(auto=True, window_ist=["05:00", "10:00"], auto_streamers=["kaicenat"])
    sent, uploads = [], []
    monkeypatch.setattr(publisher.notify, "send", lambda t: sent.append(t) or True)
    monkeypatch.setattr(ghrelease, "available", lambda: True)
    monkeypatch.setattr(ghrelease, "upload", lambda day, f, name: uploads.append(name) or f"https://dl/{name}")
    for i in range(14):
        add_clip(pub, f"lacy{i:02d}", "lacy", 30 + i)
        (pub / "media" / f"lacy{i:02d}_c.mp4").write_bytes(b"clean")
        q = publisher.QUEUE_DIR / f"lacy{i:02d}.json"
        q.write_text(json.dumps(dict(json.loads(q.read_text()), clean_file=str(pub / "media" / f"lacy{i:02d}_c.mp4"))))
    add_clip(pub, "kaiX", "kaicenat", 80)
    publisher.run_sharing(cfg, now=IN_WINDOW)
    assert len(sent) == 10                                    # max 10 per day
    assert "lacy13" in sent[0]                                # best first
    assert all("kaiX" not in t for t in sent)                 # Kai is still going to be auto-posted
    t = sent[0]
    assert "EDITED" in t and "CLEAN" in t and "#LACY #GTA6 #GTAVI" in t and "within 30 minutes" in t
    assert "contentrewards.com/c/campaigns/a5c63fad" in t
    assert "lacy13_EDITED.mp4" in uploads and "lacy13_CLEAN.mp4" in uploads
    sent.clear()
    publisher.run_sharing(cfg, now=IN_WINDOW)
    assert sent == []                                         # limit reached, nothing sent twice
    # next day after the window: Kai clip that never got posted is sent to you
    publisher.run_sharing(cfg, now=datetime(2026, 11, 21, 6, 0, tzinfo=timezone.utc))
    assert any("kaiX" in t and "1d2bcb00" in t for t in sent)
