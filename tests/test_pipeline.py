import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clipper import config, editor, run, transcript, ytdlp
from clipper.config import load_config
from clipper.selector import find_moments
from clipper.state import State
from clipper.textmatch import count_terms, find_terms
from clipper.transcript import Seg

ROOT = Path(__file__).resolve().parent.parent
HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture
def cfg():
    return load_config(ROOT / "config.yaml")


def json3(lines):
    return {"events": [{"tStartMs": int(a * 1000), "dDurationMs": int((b - a) * 1000),
                        "segs": [{"utf8": t}]} for a, b, t in lines] + [{"tStartMs": 0, "aAppend": 1}]}


# ---------- text matching ----------
def test_whole_word_matching():
    assert find_terms("GTA 6 is here", ["gta 6"]) == ["gta 6"]
    assert find_terms("gta 60 fps", ["gta 6"]) == []
    assert find_terms("GTA-6's map is huge", ["gta 6"]) == ["gta 6"]
    assert find_terms("this is broken", ["bro"]) == []
    assert find_terms("BRO what", ["bro"]) == ["bro"]
    assert find_terms("Let's go!!", ["let's go"]) == ["let's go"]
    assert count_terms("no way no way NO WAY", ["no way"]) == {"no way": 3}
    assert find_terms("Grand Theft Auto VI trailer", ["grand theft auto vi"])


# ---------- transcripts ----------
def test_parse_json3_and_overlap_fix():
    segs = transcript.parse_json3(json3([(0, 5, "hello"), (2, 6, "world\n"), (7, 8, "  ")]))
    assert [s.text for s in segs] == ["hello", "world"]
    assert segs[0].end == 2.0                 # trimmed so captions don't overlap


def test_parse_vtt_dedupes_rolling_lines():
    vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:02.500\n<c>gta 6</c> is crazy\n\n"
           "00:00:02.500 --> 00:00:04.000\ngta 6 is crazy\n\n01:00:00.000 --> 01:00:01.000\nlate line\n")
    segs = transcript.parse_vtt(vtt)
    assert [s.text for s in segs] == ["gta 6 is crazy", "late line"]
    assert segs[1].start == 3600.0


def test_pick_caption_track_priority():
    info = {"subtitles": {"live_chat": [{}], "en-US": [{}]}, "automatic_captions": {"en-orig": [{}], "en": [{}]}}
    assert transcript.pick_caption_track(info, ["en"]) == ("en-US", False)
    info = {"subtitles": {}, "automatic_captions": {"de": [{}], "en-orig": [{}], "en": [{}]}}
    assert transcript.pick_caption_track(info, ["en"]) == ("en-orig", True)
    info = {"automatic_captions": {"hi-orig": [{}], "fr": [{}]}}
    assert transcript.pick_caption_track(info, ["en"]) == ("hi-orig", True)
    assert transcript.pick_caption_track({}, ["en"]) is None


# ---------- moment selection ----------
def long_stream():
    segs = [Seg(i * 4, i * 4 + 4, "just driving around the map okay") for i in range(900)]  # 1 hour
    segs[100] = Seg(400, 404, "BRO GTA 6 police are insane no way")
    segs[101] = Seg(404, 408, "oh my god [Laughter] they got a five star wanted level")
    segs[500] = Seg(2000, 2004, "gta 6 heist time let's go")
    segs[700] = Seg(2800, 2804, "no way bro oh my god insane")   # hype but no GTA 6 word
    segs[800] = Seg(3200, 3204, "no way bro")       # too weak on its own
    return segs


def test_find_moments_picks_keyword_moments(cfg):
    ms = find_moments(long_stream(), cfg, title_relevant=False, duration=3600)
    assert len(ms) == 2
    assert ms[0].start <= 400 and ms[0].end >= 408
    assert ms[1].start <= 2000 and ms[1].end >= 2004
    sel = cfg["selection"]
    for m in ms:
        assert sel["min_seconds"] <= m.end - m.start <= sel["max_seconds"]
        assert 0 <= m.start < m.end <= 3600
    assert "gta 6" in ms[0].reasons and "[laughter]" in ms[0].reasons
    assert not any(a.start < b.end and b.start < a.end for a, b in zip(ms, ms[1:]))


def test_title_relevant_allows_hype_only_and_limits_count(cfg):
    ms = find_moments(long_stream(), cfg, title_relevant=True, duration=3600)
    assert len(ms) == cfg["selection"]["clips_per_video"]
    assert any(m.start <= 2800 <= m.end for m in ms)
    assert not any(m.start <= 3200 <= m.end for m in ms)


def test_heatmap_and_bounds(cfg):
    heat = [{"start_time": i * 10, "end_time": i * 10 + 10, "value": 1.0 if i == 30 else 0.05} for i in range(36)]
    ms = find_moments([], cfg, heatmap=heat, title_relevant=True, duration=360)
    assert len(ms) == 1 and ms[0].start <= 305 <= ms[0].end
    assert find_moments([], cfg, heatmap=heat, title_relevant=False, duration=360) == []
    # very short video: clip never goes past the end
    ms = find_moments([Seg(1, 3, "gta 6 no way")], cfg, duration=10)
    assert ms and ms[0].end <= 10 and ms[0].start >= 0
    assert find_moments([], cfg, duration=0) == []


def test_nothing_relevant(cfg):
    segs = [Seg(i, i + 1, "hello chat welcome") for i in range(200)]
    assert find_moments(segs, cfg, title_relevant=True, duration=200) == []


# ---------- state ----------
def test_state_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    s = State(p)
    s.set(["a", "b"], "done", clips=2)
    for _ in range(3):
        s.error(["c"], "boom")
    s.save()
    s2 = State(p)
    assert s2.is_final("b") and s2.get("a")["clips"] == 2
    assert s2.get("c")["status"] == "gave_up" and s2.is_final("c")


# ---------- urls ----------
def test_channel_tabs_and_links():
    assert run.channel_tabs({"platform": "youtube", "channel": "SomeOne"}) == [
        "https://www.youtube.com/@SomeOne/videos", "https://www.youtube.com/@SomeOne/streams"]
    assert run.channel_tabs({"platform": "youtube", "channel": "https://www.youtube.com/@x/videos"})[0] == \
        "https://www.youtube.com/@x/videos"
    assert run.channel_tabs({"platform": "twitch", "channel": "https://twitch.tv/Foo"}) == [
        "https://www.twitch.tv/foo/videos?filter=archives&sort=time"]
    assert run._link_at({"webpage_url": "https://www.youtube.com/watch?v=abc"}, 125.7) == \
        "https://www.youtube.com/watch?v=abc&t=125s"
    assert run._link_at({"webpage_url": "https://www.twitch.tv/videos/123"}, 3725) == \
        "https://www.twitch.tv/videos/123?t=1h2m5s"


def test_config_example_is_valid(cfg):
    assert "gta 6" in cfg["keywords"]
    assert cfg["discovery"]["max_videos_per_run"] >= 1


# ---------- editing + full run (offline, fake network) ----------
def make_video(path: Path, seconds: float, size="1280x720"):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds),
                    "-c:v", "libvpx-vp9", "-b:v", "300k", "-c:a", "libopus", str(path)], check=True)


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_full_run_offline(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(config, "OUTPUT", tmp_path / "out")
    monkeypatch.setattr(run, "OUTPUT", tmp_path / "out")
    monkeypatch.setattr(config, "LINKS_FILE", tmp_path / "links.txt")
    (tmp_path / "links.txt").write_text("# comment\nhttps://www.youtube.com/watch?v=FAKE1\n")
    import clipper.state as st
    monkeypatch.setattr(st, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(run, "State", lambda: State(tmp_path / "state.json"))
    monkeypatch.setattr(run, "read_links", lambda: config.read_links(tmp_path / "links.txt"))

    cap_lines = [(i * 3, i * 3 + 3, "walking around") for i in range(400)]
    cap_lines[50] = (150, 153, "GTA 6 cops are insane bro")
    cap_lines[51] = (153, 156, "no way [Laughter] five star wanted level")
    sections = []

    def fake_info(url):
        return {"id": "FAKE1", "extractor_key": "Youtube", "title": "Playing GTA 6 for the first time",
                "webpage_url": "https://www.youtube.com/watch?v=FAKE1", "uploader_id": "@StreamerX",
                "duration": 1200, "live_status": "was_live", "automatic_captions": {"en-orig": [{}]},
                "heatmap": None}

    def fake_sub(info, lang, auto, workdir):
        workdir.mkdir(parents=True, exist_ok=True)
        p = workdir / f"sub.{lang}.json3"
        p.write_text(json.dumps(json3(cap_lines)))
        assert (lang, auto) == ("en-orig", True)
        return p

    def fake_section(url, start, end, dest):
        sections.append((start, end))
        make_video(dest, end - start)
        return dest

    monkeypatch.setattr(ytdlp, "video_info", fake_info)
    monkeypatch.setattr(ytdlp, "download_subtitle", fake_sub)
    monkeypatch.setattr(ytdlp, "download_section", fake_section)

    assert run.main([]) == 0
    runs = [d for d in (tmp_path / "out").iterdir() if d.is_dir()]
    assert len(runs) == 1
    files = sorted(p.name for p in runs[0].glob("*.mp4"))
    assert len(sections) == 1 and sections[0][0] <= 150 and sections[0][1] >= 156
    assert len(files) == 2, files
    v = probe(runs[0] / next(f for f in files if f.endswith("_vertical.mp4")))
    vs = next(s for s in v["streams"] if s["codec_type"] == "video")
    assert (vs["width"], vs["height"], vs["codec_name"]) == (1080, 1920, "h264")
    assert any(s["codec_type"] == "audio" and s["codec_name"] == "aac" for s in v["streams"])
    length = sections[0][1] - sections[0][0]
    assert abs(float(v["format"]["duration"]) - length) < 0.5
    o = probe(runs[0] / next(f for f in files if f.endswith("_original.mp4")))
    assert next(s for s in o["streams"] if s["codec_type"] == "video")["codec_name"] == "h264"
    summary = (runs[0] / "summary.md").read_text()
    assert "@StreamerX" in summary and "t=" in summary
    clips = json.loads((runs[0] / "clips.json").read_text())["clips"]
    assert clips[0]["transcript_source"] == "captions"

    # second run: link is remembered, nothing is done twice
    sections.clear()
    assert run.main([]) == 0
    assert sections == []
    s = State(tmp_path / "state.json")
    assert s.get("youtube:FAKE1")["status"] == "done" and s.get("youtube:FAKE1")["clips"] == 1


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_defer_live_and_missing_captions(tmp_path, monkeypatch, cfg):
    s = State(tmp_path / "s.json")
    job = run.Job("https://www.youtube.com/watch?v=L", ["youtube:L"], {"credit": "@a", "platform": "youtube"})
    info = {"id": "L", "extractor_key": "Youtube", "title": "GTA 6 live", "live_status": "is_live"}
    monkeypatch.setattr(ytdlp, "video_info", lambda u: info)
    assert "stream" in run.process(job, cfg, s, tmp_path, run.Report())
    assert s.get("youtube:L")["status"] == "deferred"

    info.update(live_status="was_live", duration=600, timestamp=__import__("time").time())
    called = []
    monkeypatch.setattr(transcript, "whisper", lambda *a: called.append(1) or [Seg(10, 13, "gta 6 no way")])
    monkeypatch.setattr(ytdlp, "download_section", lambda u, a, b, d: (make_video(d, b - a), d)[1])
    for _ in range(cfg["transcript"]["caption_wait_runs"]):
        assert run.process(job, cfg, s, tmp_path, run.Report()) == "waiting for captions"
    assert called == []
    rep = run.Report()
    assert run.process(job, cfg, s, tmp_path, rep) == "1 clip(s)"
    assert called == [1] and rep.clips[0]["transcript_source"] == "whisper"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_whisper_audio_decoder_works(tmp_path):
    """Real faster-whisper audio decoding (no model needed) - catches PyAV incompatibilities."""
    from faster_whisper.audio import decode_audio
    make_video(tmp_path / "a.webm", 2)
    audio = decode_audio(str(tmp_path / "a.webm"))
    assert 30000 < len(audio) < 34000          # 2 s at 16 kHz


def test_blocked_site_is_retried_not_given_up(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "OUTPUT", tmp_path / "out")
    monkeypatch.setattr(run, "State", lambda: State(tmp_path / "s.json"))
    monkeypatch.setattr(run, "read_links", lambda: ["https://www.youtube.com/watch?v=B"])

    def blocked(url):
        raise ytdlp.YtDlpError("ERROR: [youtube] B: Sign in to confirm you\u2019re not a bot.")
    monkeypatch.setattr(ytdlp, "video_info", blocked)
    for _ in range(4):
        assert run.main([]) == 1                # run fails -> GitHub emails you
    s = State(tmp_path / "s.json")
    assert s.get("link:https://www.youtube.com/watch?v=B")["status"] == "deferred"


def test_credit_from_info():
    assert run.credit_from_info({"uploader_id": "@StreamerX", "uploader": "Streamer X"}) == "@StreamerX"
    # Twitch clip: uploader is the person who clipped it, channel is the streamer
    assert run.credit_from_info({"uploader_id": "124477138", "uploader": "randomclipper", "channel": "shroud"}) == "@shroud"
    assert run.credit_from_info({"uploader_id": "xqc", "uploader": "xQc"}) == "@xQc"
    assert run.credit_from_info({}) == ""
