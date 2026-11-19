"""Score the transcript and pick the best non-overlapping GTA 6 moments."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from .textmatch import REACTION_TAG, count_terms
from .transcript import Seg


@dataclass
class Moment:
    start: float
    end: float
    score: float
    reasons: list[str] = field(default_factory=list)
    excerpt: str = ""


def _heat_peak(heatmap: list[dict], a: float, b: float) -> float:
    """Highest "Most replayed" value (0..1) inside [a, b]."""
    return max((h["value"] for h in heatmap if min(b, h["end_time"]) > max(a, h["start_time"])), default=0.0)


def find_moments(segs: list[Seg], cfg: dict, heatmap: list[dict] | None = None,
                 title_relevant: bool = False, duration: float | None = None) -> list[Moment]:
    sel = cfg["selection"]
    w = sel["weights"]
    heatmap = heatmap or []
    L, lo_len, hi_len = sel["target_seconds"], sel["min_seconds"], sel["max_seconds"]
    if duration is None:
        duration = max([s.end for s in segs] + [h["end_time"] for h in heatmap] + [0.0])
    if duration <= 0:
        return []

    # per-segment scores
    seg_score, seg_kw, seg_why = [], [], []
    for s in segs:
        kw = count_terms(s.text, cfg["keywords"])
        ctx = count_terms(s.text, cfg["context_terms"])
        hype = count_terms(s.text, cfg["hype_phrases"])
        tags = REACTION_TAG.findall(s.text)
        seg_score.append(w["keyword"] * sum(kw.values()) + w["context"] * sum(ctx.values())
                         + w["hype"] * sum(hype.values()) + w["reaction_tag"] * len(tags))
        seg_kw.append(sum(kw.values()))
        seg_why.append(list(kw) + list(ctx) + list(hype) + [f"[{t.lower()}]" for t in tags])
    mids = [(s.start + s.end) / 2 for s in segs]

    # candidate windows start a little before each scored line, and around replay peaks
    starts = {max(0.0, s.start - 3) for s, sc in zip(segs, seg_score) if sc > 0}
    for h in heatmap:
        starts.add(max(0.0, (h["start_time"] + h["end_time"]) / 2 - L / 2))

    cands = []
    for a in starts:
        b = min(duration, a + L)
        i, j = bisect_left(mids, a), bisect_right(mids, b)
        text_score = sum(seg_score[i:j])
        kw_hits = sum(seg_kw[i:j])
        if not title_relevant and kw_hits == 0:
            continue                                   # not about GTA 6
        heat = _heat_peak(heatmap, a, b)
        score = text_score + w["most_replayed"] * heat
        if score < sel["min_score"]:
            continue
        why = sorted({r for k in range(i, j) for r in seg_why[k]})
        if heat:
            why.append(f"most-replayed {heat:.2f}")
        cands.append((score, a, b, i, j, why))

    chosen: list[Moment] = []
    for score, a, b, i, j, why in sorted(cands, key=lambda c: (-c[0], c[1])):
        # snap to whole caption lines with a little breathing room
        start = max(0.0, segs[i].start - 0.6) if j > i else a
        end = min(duration, segs[j - 1].end + 0.8) if j > i else b
        if end - start > hi_len:
            end = start + hi_len
        if end - start < lo_len:
            end = min(duration, start + lo_len)
            start = max(0.0, min(start, end - lo_len))
        if end - start < min(lo_len, duration) - 0.01:
            continue
        if any(start < m.end and m.start < end for m in chosen):
            continue
        excerpt = " ".join(s.text for s in segs[i:j])
        chosen.append(Moment(round(start, 2), round(end, 2), round(score, 2), why,
                             excerpt[:400] + ("..." if len(excerpt) > 400 else "")))
        if len(chosen) >= sel["clips_per_video"]:
            break
    return sorted(chosen, key=lambda m: m.start)
