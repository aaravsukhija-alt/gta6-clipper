"""Workflow entry point.

Schedules:  every 3 h -> find VODs + make clips (+ post if it's posting time)
            every 30 min in the posting window -> post only
Manual:     TEST_STREAMER=kaicenat -> clips + previews for one streamer, no posting
            POST_NOW=1 -> post the best queued clip now (1 per platform)
            RESET_ERRORS=1 -> re-enable platforms stopped after an error
"""
import os
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):  # Windows consoles choke on emoji in stream titles
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
from argparse import Namespace
from pathlib import Path

from . import publisher, run
from .config import ROOT, load_config

PREVIEWS = ROOT / "previews"


def preview(clip: dict) -> Path:
    """4 frames side by side, small JPEG committed to the repo so the clip can be checked anywhere."""
    PREVIEWS.mkdir(exist_ok=True)
    dest = PREVIEWS / f"{clip['id']}.jpg"
    dur = max(1.0, float(clip.get("length") or 20))
    frames = [str(round(dur * f, 2)) for f in (0.03, 0.3, 0.6, 0.9)]
    inputs = []
    for t in frames:
        inputs += ["-ss", t, "-i", clip["file"]]
    filt = "".join(f"[{i}:v]scale=270:-2[v{i}];" for i in range(4)) + "".join(f"[v{i}]" for i in range(4)) + "hstack=4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex", filt,
                    "-frames:v", "1", "-q:v", "4", str(dest)], check=True)
    return dest


def manual_run_waiting() -> bool:
    """True if someone started a manual (test) run that is waiting for this PC - then we hand over early."""
    tok, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not tok or not repo:
        return False
    import json
    import urllib.request
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/runs?status=queued&per_page=20",
                                     headers={"Authorization": f"Bearer {tok}", "User-Agent": "gta6-clipper",
                                              "Accept": "application/vnd.github+json"})
        runs = json.loads(urllib.request.urlopen(req, timeout=20).read()).get("workflow_runs", [])
        return any(r.get("event") == "workflow_dispatch" for r in runs)
    except Exception:  # noqa: BLE001
        return False


def service(cfg: dict, on_clips, minutes: float) -> int:
    """The scheduled run: live mode + posting clock in the background, VOD backup in front,
    for ~minutes, then save everything. The next scheduled run takes over right after."""
    import threading
    import time
    from . import live
    stop = threading.Event()
    deadline = time.time() + minutes * 60
    threads = []
    if cfg["live"]["enabled"]:
        mgr = live.LiveManager(cfg)
        threads.append(threading.Thread(target=mgr.run, args=(stop,), name="live", daemon=True))

    def poster():
        while not stop.is_set():
            if cfg["posting"]["auto"]:
                try:
                    publisher.run_posting(cfg)
                except Exception as e:  # noqa: BLE001 - keep the clock running
                    print(f"[post] {e}")
            stop.wait(60)
    threads.append(threading.Thread(target=poster, name="poster", daemon=True))
    for t in threads:
        t.start()
    code = run.run_clips(Namespace(url=[], config=None, no_links=False, streamer=None), on_clips=on_clips)
    print(f"VOD backup pass done; live mode + posting keep running until {time.strftime('%H:%M', time.localtime(deadline))}")
    while time.time() < deadline:
        if manual_run_waiting():
            print("a manual run is waiting - finishing early so it can start")
            break
        time.sleep(120)
    stop.set()
    for t in threads:
        t.join(timeout=900)
    return code


def main() -> int:
    cfg = load_config()
    publisher.configure(cfg)
    media = publisher.media_dir()
    test = os.getenv("TEST_STREAMER", "").strip().lower()
    auto = bool(cfg["posting"]["auto"]) and not test

    if os.getenv("RESET_ERRORS"):
        p = publisher.load_posted()
        print(f"re-enabling: {list(p['disabled']) or 'nothing was stopped'}")
        p["disabled"] = {}
        publisher.save_posted(p)

    if os.getenv("POST_NOW"):
        res = publisher.run_posting(cfg, force=True, limit=1)
        print(f"posted now: {[r.get('link') or r.get('error') for r in res] or 'nothing queued'}")
        publisher.write_report(cfg)
        publisher.mirror_to_repo()
        return 0 if res and all(r.get("ok") for r in res) else 1

    def on_clips(clips):
        for c in clips:
            with publisher.ledger_lock():
                publisher.enqueue(c)
            if test:
                print(f"preview: {preview(dict(c, file=str(media / Path(c['file']).name)))}")

    url = os.getenv("EXTRA_URL", "").strip()
    if test or url:
        code = run.run_clips(Namespace(url=[url] if url else [], config=None, no_links=bool(test),
                                       streamer=test or None), on_clips=on_clips)
    else:
        code = service(cfg, on_clips, float(os.getenv("CLIPPER_RUN_MINUTES", "165")))
    if auto:
        publisher.run_posting(cfg)
    publisher.write_report(cfg)
    publisher.mirror_to_repo()
    return code


if __name__ == "__main__":
    sys.exit(main())
