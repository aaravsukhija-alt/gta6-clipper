"""ffmpeg editing: vertical 9:16 version with blurred background, title, credit and captions."""
import shutil
import subprocess
import tempfile
from pathlib import Path

from .transcript import Seg


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_ass(segs: list[Seg], clip_start: float, clip_end: float, path: Path, w: int, h: int,
              font_name: str = "DejaVu Sans") -> int:
    """Captions for the part of the transcript inside the clip, shifted to start at 0."""
    lines = []
    for s in segs:
        a, b = max(s.start, clip_start), min(s.end, clip_end)
        if b - a < 0.15:
            continue
        lines.append(f"Dialogue: 0,{_ass_time(a - clip_start)},{_ass_time(b - clip_start)},Cap,,0,0,0,,"
                     f"{_ass_escape(s.text)}")
    path.write_text(
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {w}\nPlayResY: {h}\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{font_name},{int(h * 0.034)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,{max(2, h // 400)},0,2,{w // 12},{w // 12},{int(h * 0.20)},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


# (font file, family name) tried in order: Linux, Windows, macOS
FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVu Sans"),
    ("C:/Windows/Fonts/arialbd.ttf", "Arial"),
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "Arial"),
    ("/Library/Fonts/Arial Bold.ttf", "Arial"),
]


def pick_font(preferred: str | None) -> tuple[Path | None, str]:
    if preferred and Path(preferred).is_file():
        fam = next((f for p, f in FONT_CANDIDATES if Path(p).name == Path(preferred).name), "Arial")
        return Path(preferred), fam
    for p, fam in FONT_CANDIDATES:
        if Path(p).is_file():
            return Path(p), fam
    return None, "Arial"


def _ass_header(w: int, h: int, font_name: str, size: int, margin_v: int) -> str:
    return ("[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
            f"PlayResX: {w}\nPlayResY: {h}\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Cap,{font_name},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
            f"-1,0,0,0,100,100,0,0,1,{max(3, size // 12)},0,2,{w // 14},{w // 14},{margin_v},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")


def word_chunks(words: list, max_words: int = 3, max_len: float = 1.2) -> list[list]:
    chunks, cur = [], []
    for wd in words:
        if cur and (len(cur) >= max_words or wd[1] - cur[0][0] > max_len or wd[0] - cur[-1][1] > 0.6):
            chunks.append(cur)
            cur = []
        cur.append(wd)
    if cur:
        chunks.append(cur)
    return chunks


def write_word_ass(segs: list[Seg], clip_start: float, clip_end: float, path: Path, w: int, h: int,
                   font_name: str = "DejaVu Sans", teaser: tuple[float, float] | None = None) -> int:
    """Word-by-word captions: 1-3 words on screen, the word being spoken in yellow.
    With a teaser (t0, t1 seconds inside the clip), its words are shown first, then the clip from 0."""
    words = [wd for s in segs for wd in (s.words or [(s.start, s.end, s.text)])]
    lead = (teaser[1] - teaser[0]) if teaser else 0.0
    parts = []
    if teaser:
        parts.append((clip_start + teaser[0], clip_start + teaser[1], -(clip_start + teaser[0])))
    parts.append((clip_start, clip_end, lead - clip_start))
    events = []
    for a, b, shift in parts:
        inside = [wd for wd in words if wd[0] >= a - 0.05 and wd[1] <= b + 0.05]
        for chunk in word_chunks(inside):
            for i, (ws, we, _) in enumerate(chunk):
                nxt = chunk[i + 1][0] if i + 1 < len(chunk) else max(we, ws + 0.25)
                txt = " ".join(("{\\c&H00FFFF&}" + _ass_escape(t.upper()) + "{\\c&HFFFFFF&}") if j == i
                               else _ass_escape(t.upper()) for j, (_, _, t) in enumerate(chunk))
                st, en = max(ws, a) + shift, min(nxt, b) + shift
                if en - st >= 0.05:
                    events.append(f"Dialogue: 0,{_ass_time(st)},{_ass_time(en)},Cap,,0,0,0,,{txt}")
    path.write_text(_ass_header(w, h, font_name, int(h * 0.05), int(h * 0.27)) + "\n".join(events) + "\n",
                    encoding="utf-8")
    return len(events)


def has_audio(src: Path) -> bool:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    return bool(p.stdout.strip())


def _ff(args: list[str], cwd: Path | None = None):
    p = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                       capture_output=True, text=True, cwd=cwd)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {p.stderr.strip()[-500:]}")


ENC = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart"]


def export_original(src: Path, dest: Path) -> Path:
    """Plain horizontal cut, re-encoded to H.264/AAC so it plays and uploads everywhere."""
    _ff(["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", *ENC, str(dest)])
    return dest


def wrap_hook(text: str, max_chars: int = 18) -> str:
    """Hook on at most 2 lines so it never runs off the screen."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    if len(lines) > 2:
        lines = [lines[0], " ".join(lines[1:])]
    return "\n".join(lines)


def make_vertical(src: Path, dest: Path, headline: str, credit: str, edit: dict,
                  ass_file: Path | None = None, teaser: tuple[float, float] | None = None,
                  layout: dict | None = None) -> Path:
    """9:16 video. layout {'mode': 'split', 'box': [x, y, w, h]} = face cam on top + gameplay below;
    anything else = blurred-background layout. All helper files are copied into a temp folder and ffmpeg
    runs inside it, so the filter text only uses plain relative names (Windows paths break filters)."""
    w, h = edit["width"], edit["height"]
    src, dest = Path(src).resolve(), Path(dest).resolve()
    tmp = Path(tempfile.mkdtemp())
    split = bool(layout and layout.get("mode") == "split" and layout.get("box"))
    hook_lines = wrap_hook(headline).split("\n") if headline else []
    for i, ln in enumerate(hook_lines):
        (tmp / f"head{i}.txt").write_text(ln, encoding="utf-8")
    (tmp / "credit.txt").write_text(credit, encoding="utf-8")
    font, _ = pick_font(edit.get("font_file"))
    fontopt = ""
    if font:
        shutil.copy(font, tmp / "font.ttf")
        fontopt = "fontfile=font.ttf:"

    def text(file: str, size: int, y: str, box: bool = False) -> str:
        boxopt = ":box=1:boxcolor=black@0.65:boxborderw=18" if box else ""
        return (f"drawtext={fontopt}textfile={file}:fontsize={size}:fontcolor=white:line_spacing=8:"
                f"borderw={max(2, size // 14)}:bordercolor=black:x=(w-text_w)/2:y={y}{boxopt}")

    if split:
        bx, by, bw, bh = layout["box"]
        top_h = int(h * 0.40) // 2 * 2
        bot_h = h - top_h
        chain = (f"[0:v]split=2[a][b];"
                 f"[a]crop=iw*{bw}:ih*{bh}:iw*{bx}:ih*{by},scale={w}:{top_h}:force_original_aspect_ratio=increase,"
                 f"crop={w}:{top_h}[top];"
                 f"[b]scale=-2:{bot_h},crop={w}:{bot_h}[bot];"
                 f"[top][bot]vstack=inputs=2")
        hook_y = f"{top_h}-text_h/2"           # hook sits on the seam between face cam and gameplay
    else:
        chain = (f"[0:v]split=2[a][b];"
                 f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=25:2[bg];"
                 f"[b]scale={w}:-2[fg];"
                 f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
        hook_y = str(int(h * 0.12))
    audio_map = "0:a:0?"
    if teaser and has_audio(src):
        # hook: show the peak moment (teaser) first, then the whole clip from the start
        t0, t1 = teaser
        chain += (f",fps=30,format=yuv420p,split=2[m1][m2];"
                  f"[m1]trim=start={t0:.3f}:end={t1:.3f},setpts=PTS-STARTPTS[tv];"
                  f"[m2]setpts=PTS-STARTPTS[mv];"
                  f"[0:a:0]aresample=48000,asplit=2[a1][a2];"
                  f"[a1]atrim=start={t0:.3f}:end={t1:.3f},asetpts=PTS-STARTPTS[ta];"
                  f"[a2]asetpts=PTS-STARTPTS[ma];"
                  f"[tv][ta][mv][ma]concat=n=2:v=1:a=1[cv][ca];[cv]null")
        audio_map = "[ca]"
    size = int(h * 0.040)
    line_h = int(size * 1.35)
    for i in range(len(hook_lines)):              # one drawtext per line so every line is centred
        y = f"({hook_y})-{(len(hook_lines) - 1) * line_h // 2}+{i * line_h}"
        chain += "," + text(f"head{i}.txt", size, y, box=True)
    if credit:
        chain += "," + text("credit.txt", int(h * 0.026), f"h-{int(h * 0.07)}")
    if ass_file:
        shutil.copy(ass_file, tmp / "cap.ass")
        chain += ",ass=filename=cap.ass" + (":fontsdir=." if font else "")
    chain += ",fps=30,format=yuv420p[v]"
    try:
        _ff(["-i", str(src), "-filter_complex", chain, "-map", "[v]", "-map", audio_map, *ENC, str(dest)], cwd=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest
