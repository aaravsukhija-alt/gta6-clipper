"""Is the streamer on screen and the main focus? Face detection on sampled frames (OpenCV, local)."""
from pathlib import Path


def face_focus(video: Path, samples: int = 12, min_face_height: float = 0.10) -> dict:
    """Returns {'ratio': share of sampled frames with a big enough face, 'size': median face height}."""
    import cv2
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    hits, sizes, checked = 0, [], 0
    for k in range(samples):
        idx = int((k + 0.5) * total / samples) if total else k * 30
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        checked += 1
        h = frame.shape[0]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(int(h * min_face_height * 0.8),) * 2)
        best = max((fh for (_, _, _, fh) in faces), default=0) / h
        sizes.append(best)
        if best >= min_face_height:
            hits += 1
    cap.release()
    sizes.sort()
    return {"ratio": hits / checked if checked else 0.0,
            "size": sizes[len(sizes) // 2] if sizes else 0.0, "frames": checked}


TOP_SHARE = 0.40          # face cam gets the top 40% of the 9:16 frame, gameplay the rest


def _faces(video: Path, samples: int):
    import cv2
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    found, checked = [], 0
    for k in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int((k + 0.5) * total / samples) if total else k * 30)
        ok, frame = cap.read()
        if not ok:
            continue
        checked += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=5, minSize=(max(20, H // 40),) * 2)
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            found.append((x / W, y / H, w / W, h / H))
    cap.release()
    return found, checked, W, H


def layout_for(video: Path, out_w: int, out_h: int, override: dict | None = None, samples: int = 16) -> dict:
    """Pick the 9:16 layout for a clip.
    split: face-cam box (fractions of the source frame) on top, gameplay below.
    blur:  whole frame centred on a blurred copy (fallback: IRL / full-screen camera / no stable face cam).
    Also returns face_ratio = share of sampled frames where the streamer's face was found."""
    found, checked, W, H = _faces(video, samples)
    ratio = len(found) / checked if checked else 0.0
    if override and override.get("layout") == "blur":
        return {"mode": "blur", "face_ratio": ratio, "auto": False}
    if override and override.get("facecam"):
        x, y, w, h = override["facecam"]
        return {"mode": "split", "box": [x, y, w, h], "face_ratio": ratio, "auto": False}
    if ratio < 0.3 or not W:
        return {"mode": "blur", "face_ratio": ratio, "auto": True, "reason": "no stable face found"}
    med = lambda v: sorted(v)[len(v) // 2]  # noqa: E731
    fx, fy = med([f[0] + f[2] / 2 for f in found]), med([f[1] + f[3] / 2 for f in found])
    fw, fh = med([f[2] for f in found]), med([f[3] for f in found])
    spread = max(max(abs(f[0] + f[2] / 2 - fx) for f in found), max(abs(f[1] + f[3] / 2 - fy) for f in found))
    if fh >= 0.28 or spread > 0.08:
        # big face (camera is the whole screen) or the face moves around -> not a fixed face cam
        return {"mode": "blur", "face_ratio": ratio, "auto": True,
                "reason": "camera fills the screen" if fh >= 0.28 else "face moves around"}
    top_aspect = out_w / (out_h * TOP_SHARE)
    bw_px = min(W, fw * W * 3.4)
    bh_px = min(H, bw_px / top_aspect)
    bw_px = bh_px * top_aspect
    cx, cy = fx * W, fy * H + 0.12 * bh_px
    x0 = min(max(0.0, cx - bw_px / 2), W - bw_px)
    y0 = min(max(0.0, cy - bh_px / 2), H - bh_px)
    box = [round(x0 / W, 4), round(y0 / H, 4), round(bw_px / W, 4), round(bh_px / H, 4)]
    return {"mode": "split", "box": box, "face_ratio": ratio, "auto": True}
