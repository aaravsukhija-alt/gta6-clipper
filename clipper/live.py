"""LIVE MODE - runs on your laptop all the time (start_live.cmd). No API keys, no AI credits.

Every couple of minutes it checks whether your campaign streamers are live playing GTA 6.
While one is: it records the stream (streamlink, ads filtered) into short rolling segments and
reads chat anonymously. When chat suddenly moves ~3x faster than normal with lots of hype words,
it cuts the last ~30 s, transcribes it locally (Whisper on your CPU), edits the 9:16 clip and
posts the best ones right away (daily limits still apply). VOD mode stays as the backup.

    python -m clipper.live            (or double-click start_live.cmd)
"""
import json
import os
import queue
import random
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from . import audio, editor, publisher, texts, transcript, vision
from .config import load_config
from .textmatch import count_terms, find_terms

# Twitch's own public web-player client id (the same one yt-dlp and streamlink use). Not a secret.
TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
SECRETS_FILE = Path.home() / ".gta6clipper" / "secrets.env"


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(publisher.media_dir() / "live.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_local_secrets():
    """Posting keys for live mode live in your user folder, never in the repo."""
    if SECRETS_FILE.exists():
        for ln in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# ---------------- is the streamer live in GTA 6? ----------------
def stream_status(login: str) -> dict | None:
    q = 'query{user(login:"%s"){stream{id title createdAt game{name}}}}' % login
    req = urllib.request.Request("https://gql.twitch.tv/gql", data=json.dumps({"query": q}).encode(),
                                 headers={"Client-ID": TWITCH_WEB_CLIENT_ID, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    user = (data.get("data") or {}).get("user") or {}
    return user.get("stream")


def is_gta(stream: dict, cfg: dict) -> bool:
    game = ((stream.get("game") or {}).get("name") or "").lower()
    names = [g.lower() for g in cfg["live"]["twitch_game_names"]]
    return game in names or bool(find_terms(stream.get("title") or "", cfg["keywords"]))


# ---------------- chat ----------------
class ChatWatcher(threading.Thread):
    """Anonymous read-only chat. Calls on_spike(wallclock, stats) when chat explodes."""

    def __init__(self, channel: str, lcfg: dict, on_spike):
        super().__init__(daemon=True, name=f"chat-{channel}")
        self.channel, self.cfg, self.on_spike = channel, lcfg, on_spike
        self.msgs: deque = deque()
        self.baseline: float | None = None
        self.last_fire = 0.0
        self.stop = threading.Event()
        self.hype = [h.lower() for h in lcfg["hype_words"]]

    def is_hype(self, text: str) -> bool:
        words = set(text.lower().split())
        return any((h in words) if h.isalnum() else (h in text.lower()) for h in self.hype)

    def run(self):
        while not self.stop.is_set():
            try:
                self._loop()
            except Exception as e:  # noqa: BLE001 - reconnect on any network error
                if not self.stop.is_set():
                    log(f"[chat {self.channel}] reconnecting: {e}")
                    time.sleep(10)

    def _loop(self):
        s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=60)
        s.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{self.channel}\r\n".encode())
        buf, last_eval = "", time.time()
        try:
            while not self.stop.is_set():
                try:
                    data = s.recv(4096).decode("utf-8", "ignore")
                    if not data:
                        raise ConnectionError("chat closed")
                    buf += data
                    *lines, buf = buf.split("\r\n")
                    for line in lines:
                        if line.startswith("PING"):
                            s.sendall(b"PONG :tmi.twitch.tv\r\n")
                        elif " PRIVMSG " in line:
                            self.msgs.append((time.time(), self.is_hype(line.split(" :", 1)[-1])))
                except socket.timeout:
                    pass
                if time.time() - last_eval >= 2:
                    last_eval = time.time()
                    self.evaluate(time.time())
        finally:
            s.close()

    def evaluate(self, now: float):
        c = self.cfg
        while self.msgs and self.msgs[0][0] < now - c["window_seconds"]:
            self.msgs.popleft()
        n = len(self.msgs)
        hype = sum(1 for _, h in self.msgs if h)
        self.baseline = n if self.baseline is None else self.baseline * 0.985 + n * 0.015
        if now - self.last_fire < c["cooldown_seconds"]:
            return
        ratio = n / max(self.baseline, 1.0)
        if n >= c["min_messages"] and ratio >= c["spike_multiplier"] and hype >= n * c["hype_share"]:
            self.last_fire = now
            log(f"[chat {self.channel}] SPIKE {n} msgs/{c['window_seconds']}s = {ratio:.1f}x normal, {hype} hype")
            self.on_spike(now, {"messages": n, "ratio": round(ratio, 2), "hype": hype})


# ---------------- recorder ----------------
class Recorder:
    """streamlink (ads filtered) -> ffmpeg -> rolling 6-second .ts segments, last few minutes kept."""

    def __init__(self, channel: str, lcfg: dict, folder: Path):
        self.channel, self.cfg, self.dir = channel, lcfg, folder
        self.sl = self.ff = None
        self.keep_until = 0.0

    def running(self) -> bool:
        return bool(self.ff and self.ff.poll() is None)

    def start(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        for f in self.dir.glob("seg*.ts"):
            f.unlink(missing_ok=True)
        self.sl = subprocess.Popen([sys.executable, "-m", "streamlink", "--twitch-disable-ads", "--stdout",
                                    "--loglevel", "error", f"https://twitch.tv/{self.channel}", self.cfg["quality"]],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
                                    "-f", "segment", "-segment_time", str(self.cfg["segment_seconds"]),
                                    "-reset_timestamps", "1", str(self.dir / "seg%06d.ts")],
                                   stdin=self.sl.stdout, stderr=subprocess.DEVNULL)
        log(f"[rec {self.channel}] recording started")

    def stop(self):
        for p in (self.ff, self.sl):
            if p and p.poll() is None:
                p.terminate()
        self.ff = self.sl = None

    def segments(self) -> list[tuple[float, float, Path]]:
        """(start, end, file) in wall-clock seconds. A segment's end = when ffmpeg closed it (mtime)."""
        files = sorted(self.dir.glob("seg*.ts"))
        out = []
        for f in files[:-1]:                      # the newest one is still being written
            end = f.stat().st_mtime
            out.append((end - self.cfg["segment_seconds"], end, f))
        return out

    def prune(self):
        cutoff = time.time() - self.cfg["buffer_seconds"]
        if time.time() < self.keep_until:
            return
        for a, b, f in self.segments():
            if b < cutoff:
                f.unlink(missing_ok=True)

    def cut(self, a: float, b: float, dest: Path) -> Path | None:
        segs = [s for s in self.segments() if s[1] > a and s[0] < b]
        if not segs:
            return None
        lst = dest.with_suffix(".txt")
        lst.write_text("".join(f"file '{s[2].resolve().as_posix()}'\n" for s in segs), encoding="utf-8")
        offset = max(0.0, a - segs[0][0])
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(lst), "-ss", f"{offset:.2f}", "-t", f"{b - a:.2f}", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "160k", str(dest)], check=True)
        lst.unlink(missing_ok=True)
        return dest


# ---------------- turning a spike into a clip ----------------
def record_live_moment(streamer: str, utc: float):
    f = publisher.media_dir() / "live_moments.json"
    items = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    items.append({"streamer": streamer, "utc": utc})
    f.write_text(json.dumps(items[-2000:]), encoding="utf-8")


def make_live_clip(st: dict, raw: Path, spike: dict, cfg: dict, work: Path) -> dict | None:
    lc, m, edit = cfg["live"], cfg["moments"], cfg["edit"]
    wav = audio.cut_audio(raw, 0, 120, work / "a.wav")
    segs, lang, prob = transcript.transcribe_words(wav, cfg["transcript"]["whisper_model"])
    text = " ".join(s.text for s in segs)
    if lang != "en" or prob < m["min_english_prob"] or not texts.is_clean(text) or not text.strip():
        log(f"  skip: not usable English speech ({lang} {prob:.2f})")
        return None
    z = audio.spike_scores(audio.loudness_profile(wav), baseline_s=20)
    loud, _ = audio.peak_in(z, 0, len(z))
    hype = count_terms(text, cfg["hype_phrases"])
    w = lc["weights"]
    score = round(w["chat"] * spike["ratio"] + w["hype"] * sum(hype.values()) + w["loudness"] * max(0.0, loud), 2)
    lay = vision.layout_for(raw, edit["width"], edit["height"], st)
    if lay["face_ratio"] < m["min_face_ratio"]:
        log(f"  skip: streamer not on screen (face in {lay['face_ratio']:.0%} of frames)")
        return None
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(raw)], capture_output=True, text=True).stdout.strip() or 0)
    pk = min(dur, lc["seconds_before"] - edit["teaser_seconds"])   # where the spike sits in the cut
    teaser = (max(0.0, pk - 0.5), max(0.0, pk - 0.5) + edit["teaser_seconds"])
    stem = f"live_{st['channel']}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    hook = texts.hook_text(text, st["short_name"], stem)
    ass = work / "c.ass"
    family = editor.pick_font(edit.get("font_file"))[1]
    n = editor.write_word_ass(segs, 0, dur, ass, edit["width"], edit["height"], family, teaser)
    out_dir = publisher.media_dir() / "live"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{stem}_vertical.mp4"
    editor.make_vertical(raw, out, hook, f"clip: {st['credit']}", edit, ass if n else None, teaser, lay)
    final = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                  str(out)], capture_output=True, text=True).stdout.strip() or 0)
    return {"id": stem, "file": str(out), "streamer": st["channel"], "hook": hook, "score": score,
            "why": [f"chat {spike['ratio']}x", f"loud x{loud:.1f}"] + list(hype), "transcript": text[:500],
            "face_ratio": round(lay["face_ratio"], 2), "layout": lay.get("mode"), "facecam": lay.get("box"), "lang": lang,
            "source": f"https://www.twitch.tv/{st['channel']} (live)", "vod_title": "", "length": round(final, 1),
            "start": 0, "end": dur, "live": True, "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}


class LiveManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lc = cfg["live"]
        self.streamers = {s["channel"]: s for s in cfg["streamers"]
                          if s.get("platform") == "twitch" and s.get("permission") and s.get("live", True)}
        self.rec: dict[str, Recorder] = {}
        self.chat: dict[str, ChatWatcher] = {}
        self.clips_this_stream: dict[str, int] = {}
        self.jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self.worker, daemon=True, name="clip-worker").start()

    def spike(self, channel: str):
        def handler(now: float, stats: dict):
            if self.clips_this_stream.get(channel, 0) >= self.lc["max_clips_per_stream"]:
                return
            self.jobs.put((channel, now, stats))
        return handler

    def worker(self):
        while True:
            channel, now, stats = self.jobs.get()
            try:
                self.handle_spike(channel, now, stats)
            except Exception as e:  # noqa: BLE001 - one bad clip must not stop live mode
                log(f"[clip {channel}] failed: {e}")

    def handle_spike(self, channel: str, now: float, stats: dict):
        lc, st, rec = self.lc, self.streamers[channel], self.rec.get(channel)
        if not rec:
            return
        center = now - lc["reaction_delay"]                 # chat reacts a few seconds after the moment
        lead = lc["seconds_before"] - self.cfg["edit"]["teaser_seconds"]   # keep the final clip <= 30 s
        a, b = center - lead, center + lc["seconds_after"]
        rec.keep_until = b + 60
        wait = b + lc["segment_seconds"] * 2 - time.time()
        if wait > 0:
            time.sleep(wait)
        work = publisher.media_dir() / "work" / channel
        work.mkdir(parents=True, exist_ok=True)
        raw = rec.cut(a, b, work / "raw.mp4")
        if not raw:
            log(f"[clip {channel}] nothing recorded for that moment yet")
            return
        log(f"[clip {channel}] cutting {b - a:.0f}s around the spike")
        clip = make_live_clip(st, raw, stats, self.cfg, work)
        raw.unlink(missing_ok=True)
        if not clip:
            return
        with publisher.ledger_lock():
            publisher.enqueue(clip)
            record_live_moment(channel, center)
        self.clips_this_stream[channel] = self.clips_this_stream.get(channel, 0) + 1
        log(f"[clip {channel}] ready: {Path(clip['file']).name} score {clip['score']} hook '{clip['hook']}'")
        if self.cfg["posting"]["auto"] and clip["score"] >= lc["post_min_score"]:
            res = publisher.run_posting(self.cfg, force=True, ids=[clip["id"]])
            log(f"[clip {channel}] posted: {[r.get('link') or r.get('error') for r in res] or 'daily limit reached'}")
        elif self.cfg["posting"]["auto"]:
            log(f"[clip {channel}] score below {lc['post_min_score']}: queued for the normal posting window")

    def check(self):
        for ch in self.streamers:
            try:
                stream = stream_status(ch)
            except Exception as e:  # noqa: BLE001
                log(f"[status {ch}] {e}")
                continue
            live_gta = bool(stream) and (is_gta(stream, self.cfg) or not self.lc["require_gta"])
            if live_gta and ch not in self.rec:
                log(f"[{ch}] LIVE in GTA 6: {stream.get('title')!r} - recording + watching chat")
                self.rec[ch] = Recorder(ch, self.lc, publisher.media_dir() / "rec" / ch)
                self.rec[ch].start()
                self.chat[ch] = ChatWatcher(ch, self.lc, self.spike(ch))
                self.chat[ch].start()
                self.clips_this_stream[ch] = 0
            elif not live_gta and ch in self.rec:
                log(f"[{ch}] stream ended or left GTA 6 - stopping")
                self.rec.pop(ch).stop()
                self.chat.pop(ch).stop.set()
            elif live_gta and not self.rec[ch].running():
                log(f"[rec {ch}] recorder stopped, restarting")
                self.rec[ch].start()

    def run(self, stop: threading.Event | None = None):
        stop = stop or threading.Event()
        log(f"live mode watching: {', '.join(self.streamers)} (auto-post {'ON' if self.cfg['posting']['auto'] else 'OFF'})")
        while not stop.is_set():
            self.check()
            for _ in range(int(self.lc["poll_seconds"] / 5)):
                for r in list(self.rec.values()):
                    r.prune()
                if stop.wait(5):
                    break
        self.shutdown()

    def shutdown(self, wait_for_clips: float = 600):
        """Stop recording; let clips that are already being made finish (up to 10 min)."""
        for ch in list(self.rec):
            self.chat.pop(ch).stop.set()
        t0 = time.time()
        while not self.jobs.empty() and time.time() - t0 < wait_for_clips:
            time.sleep(5)
        for ch in list(self.rec):
            self.rec.pop(ch).stop()


def main():
    load_local_secrets()
    cfg = load_config()
    publisher.configure(cfg)
    publisher.media_dir().mkdir(parents=True, exist_ok=True)
    if not cfg["live"]["enabled"]:
        log("live mode is off (live.enabled: false in config.yaml)")
        return
    LiveManager(cfg).run()


if __name__ == "__main__":
    main()
