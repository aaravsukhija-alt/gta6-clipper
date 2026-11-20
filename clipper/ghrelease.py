"""Public download links for clips: one GitHub Release per day (India time), clips as assets."""
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _req(method: str, url: str, body=None, ctype="application/json"):
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    req = urllib.request.Request(url, method=method, data=data, headers={
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json",
        "User-Agent": "gta6-clipper", "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read() or b"null")


def available() -> bool:
    return bool(os.getenv("GITHUB_TOKEN") and os.getenv("GITHUB_REPOSITORY"))


def day_release(day: str) -> dict:
    repo = os.environ["GITHUB_REPOSITORY"]
    tag = f"clips-{day}"
    try:
        return _req("GET", f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    return _req("POST", f"https://api.github.com/repos/{repo}/releases", {
        "tag_name": tag, "name": f"Clips {day}", "target_commitish": "main", "prerelease": True,
        "body": "Clips to post yourself. EDITED = ready to post. CLEAN = no text/captions, original audio "
                "only (add a song in the Instagram/YouTube app if you want)."})


def upload(day: str, file: Path, name: str | None = None) -> str:
    """Upload a clip to the day's release and return its direct download link."""
    rel = day_release(day)
    name = name or file.name
    for a in rel.get("assets") or []:
        if a["name"] == name:
            return a["browser_download_url"]
    url = rel["upload_url"].split("{")[0] + "?" + urllib.parse.urlencode({"name": name})
    asset = _req("POST", url, Path(file).read_bytes(), mimetypes.guess_type(name)[0] or "video/mp4")
    return asset["browser_download_url"]


def delete_assets(prefix: str) -> int:
    """Delete every release download whose file name starts with prefix (e.g. a clip id)."""
    repo = os.environ["GITHUB_REPOSITORY"]
    n = 0
    for rel in _req("GET", f"https://api.github.com/repos/{repo}/releases?per_page=50") or []:
        for a in rel.get("assets") or []:
            if a["name"].startswith(prefix):
                req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/assets/{a['id']}",
                                             method="DELETE", headers={
                                                 "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                                                 "Accept": "application/vnd.github+json", "User-Agent": "gta6-clipper"})
                urllib.request.urlopen(req, timeout=60).read()
                n += 1
    return n
