"""All network access goes through yt-dlp (public pages only, no API keys)."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


class YtDlpError(RuntimeError):
    pass


def _base() -> list[str]:
    args = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress", "--retries", "5",
            "--extractor-retries", "3", "--sleep-requests", "0.5"]
    client = os.getenv("YTDLP_YOUTUBE_CLIENT")      # optional, e.g. "tv" or "web_safari"
    if client:
        args += ["--extractor-args", f"youtube:player_client={client}"]
    cookies = os.getenv("YTDLP_COOKIES_FILE")      # optional, only if YouTube blocks the server
    if cookies and Path(cookies).is_file():
        args += ["--cookies", cookies]
    return args


def _run(args: list[str], timeout: int) -> str:
    try:
        p = subprocess.run(_base() + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise YtDlpError(f"yt-dlp timed out after {timeout}s") from e
    if p.returncode != 0:
        err = (p.stderr or p.stdout).strip().splitlines()
        raise YtDlpError(err[-1] if err else f"yt-dlp exit code {p.returncode}")
    return p.stdout


def list_channel(url: str, limit: int) -> list[dict]:
    """Newest entries of a channel tab, without fetching each video."""
    out = _run(["-J", "--flat-playlist", "--playlist-end", str(limit), url], timeout=180)
    data = json.loads(out)
    return [e for e in (data.get("entries") or []) if e and e.get("id")]


def video_info(url: str) -> dict:
    out = _run(["-J", "--no-playlist", url], timeout=300)
    return json.loads(out)


def download_subtitle(info: dict, lang: str, auto: bool, workdir: Path) -> Path | None:
    """Download one caption track (json3 preferred) re-using already-fetched metadata."""
    workdir.mkdir(parents=True, exist_ok=True)
    info_file = workdir / "info.json"
    info_file.write_text(json.dumps(info), encoding="utf-8")
    for old in workdir.glob("sub.*"):
        old.unlink()
    flag = "--write-auto-subs" if auto else "--write-subs"
    _run(["--load-info-json", str(info_file), "--skip-download", flag, "--sub-langs", lang,
          "--sub-format", "json3/vtt", "-o", str(workdir / "sub.%(ext)s")], timeout=180)
    files = sorted(workdir.glob("sub.*.json3")) + sorted(workdir.glob("sub.*.vtt"))
    return files[0] if files else None


def download_audio(url: str, workdir: Path) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    _run(["--no-playlist", "-f", "ba/worst", "-x", "--audio-format", "m4a",
          "-o", str(workdir / "audio.%(ext)s"), url], timeout=7200)
    files = sorted(workdir.glob("audio.*"))
    if not files:
        raise YtDlpError("audio download produced no file")
    return files[0]


def download_section(url: str, start: float, end: float, dest: Path) -> Path:
    """Download only [start, end] seconds of the video as mp4."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(dir=dest.parent))
    _run(["--no-playlist",
          "-f", "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
          "--download-sections", f"*{start:.2f}-{end:.2f}", "--force-keyframes-at-cuts",
          "--merge-output-format", "mp4", "-o", str(tmpdir / "cut.%(ext)s"), url], timeout=3600)
    files = sorted(tmpdir.glob("cut.*"))
    if not files:
        raise YtDlpError("clip download produced no file")
    files[0].replace(dest)
    for f in tmpdir.iterdir():
        f.unlink()
    tmpdir.rmdir()
    return dest
