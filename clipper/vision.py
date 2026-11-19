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
