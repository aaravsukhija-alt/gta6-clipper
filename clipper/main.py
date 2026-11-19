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


def main() -> int:
    cfg = load_config()
    media = Path(os.path.expanduser(cfg["posting"]["media_dir"]))
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
        return 0 if all(r.get("ok") for r in res) else 1

    if os.getenv("POST_ONLY"):
        if auto:
            publisher.run_posting(cfg)
        else:
            print("auto-posting is off (posting.auto: false in config.yaml)")
        publisher.write_report(cfg)
        return 0

    def on_clips(clips):
        for c in clips:
            publisher.enqueue(c, media)
            if test:
                print(f"preview: {preview(dict(c, file=str(media / Path(c['file']).name)))}")

    def between():
        if auto:
            publisher.run_posting(cfg)

    url = os.getenv("EXTRA_URL", "").strip()
    code = run.run_clips(Namespace(url=[url] if url else [], config=None, no_links=bool(test), streamer=test or None),
                         on_clips=on_clips, between=between)
    if auto:
        publisher.run_posting(cfg)
    publisher.write_report(cfg)
    return code


if __name__ == "__main__":
    sys.exit(main())
