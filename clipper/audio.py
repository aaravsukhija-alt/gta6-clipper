"""Audio loudness: find screaming / hype spikes in a long VOD cheaply (no transcription needed)."""
import subprocess
from pathlib import Path

import numpy as np

RATE = 8000  # samples per second for the loudness scan (enough for loudness, small and fast)


def loudness_profile(path: Path) -> np.ndarray:
    """Loudness in dBFS for every second of the file, streamed through ffmpeg (low memory)."""
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
                          "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
                         stdout=subprocess.PIPE)
    out, chunk = [], RATE * 2
    while True:
        buf = p.stdout.read(chunk)
        if not buf:
            break
        x = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        out.append(20 * np.log10(max(rms, 1e-5)))
    p.wait()
    if p.returncode != 0:
        raise RuntimeError("ffmpeg could not read the audio for the loudness scan")
    return np.array(out, dtype=np.float32)


def spike_scores(db: np.ndarray, baseline_s: int = 300) -> np.ndarray:
    """How much louder each second is than the stream's normal level around it (robust z-score)."""
    n = len(db)
    if n == 0:
        return db
    half = baseline_s // 2
    padded = np.pad(db, half, mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(padded, baseline_s + 1)[:n]
    med = np.median(win, axis=1)
    mad = np.median(np.abs(win - med[:, None]), axis=1) + 0.5
    z = (db - med) / (1.4826 * mad)
    k = np.ones(3) / 3                       # smooth over 3 s so one pop doesn't count
    return np.convolve(z, k, mode="same").astype(np.float32)


def top_spikes(z: np.ndarray, n: int, min_gap: int = 60, min_z: float = 2.0) -> list[int]:
    """Seconds of the n biggest spikes, at least min_gap apart."""
    order = np.argsort(-z)
    picked: list[int] = []
    for i in order:
        if z[i] < min_z or len(picked) >= n:
            break
        if all(abs(int(i) - p) >= min_gap for p in picked):
            picked.append(int(i))
    return sorted(picked)


def peak_in(z: np.ndarray, a: float, b: float) -> tuple[float, int]:
    lo, hi = max(0, int(a)), min(len(z), int(b) + 1)
    if hi <= lo:
        return 0.0, lo
    i = lo + int(np.argmax(z[lo:hi]))
    return float(z[i]), i


def cut_audio(src: Path, start: float, dur: float, dest: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, start):.2f}",
                    "-t", f"{dur:.2f}", "-i", str(src), "-ac", "1", "-ar", "16000", str(dest)], check=True)
    return dest
