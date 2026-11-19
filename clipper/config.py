import copy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
STATE_FILE = ROOT / "state" / "processed.json"
LINKS_FILE = ROOT / "links.txt"

DEFAULTS = {
    "streamers": [],
    "keywords": ["gta 6", "gta vi", "gta6"],
    "context_terms": [],
    "hype_phrases": [],
    "discovery": {"recent_videos_per_channel": 8, "max_age_days": 7, "max_videos_per_run": 4},
    "transcript": {"languages": ["en"], "caption_wait_runs": 2, "whisper_fallback": True,
                   "whisper_model": "base", "whisper_language": None, "whisper_max_minutes": 480},
    "selection": {"clips_per_video": 3, "target_seconds": 45, "min_seconds": 15, "max_seconds": 59,
                  "min_score": 4,
                  "weights": {"keyword": 3, "context": 1, "hype": 1, "reaction_tag": 2, "most_replayed": 6}},
    "edit": {"vertical": True, "keep_original": True, "burn_captions": True, "headline": "GTA 6 MOMENT",
             "font_file": None,
             "width": 1080, "height": 1920},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ROOT / "config.yaml"
    with open(p, encoding="utf-8") as f:
        cfg = _merge(DEFAULTS, yaml.safe_load(f) or {})
    for key in ("keywords", "context_terms", "hype_phrases"):
        cfg[key] = [str(x).lower().strip() for x in cfg.get(key) or [] if str(x).strip()]
    sel = cfg["selection"]
    if not (0 < sel["min_seconds"] <= sel["target_seconds"] <= sel["max_seconds"]):
        raise ValueError("selection: need 0 < min_seconds <= target_seconds <= max_seconds")
    return cfg


def read_links(path: Path = LINKS_FILE) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
