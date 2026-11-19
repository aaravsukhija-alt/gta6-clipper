"""Phone notifications: Discord webhook and/or Telegram bot (whichever secrets are set)."""
import json
import os
import urllib.request


ISSUE_TITLE = "Clips to submit"
_ISSUE: dict = {}


def _gh(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(f"https://api.github.com{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                                          "Accept": "application/vnd.github+json", "User-Agent": "gta6-clipper",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def issue_number() -> int:
    """The open 'Clips to submit' issue (created the first time)."""
    if "n" in _ISSUE:
        return _ISSUE["n"]
    repo = os.environ["GITHUB_REPOSITORY"]
    for page in range(1, 6):
        items = _gh("GET", f"/repos/{repo}/issues?state=open&per_page=100&page={page}") or []
        hit = next((i for i in items if i.get("title") == ISSUE_TITLE and "pull_request" not in i), None)
        if hit:
            _ISSUE["n"] = hit["number"]
            return hit["number"]
        if len(items) < 100:
            break
    made = _gh("POST", f"/repos/{repo}/issues", {"title": ISSUE_TITLE, "body":
               "Every posted clip is listed here as a comment with its links and the campaign to submit it to.\n"
               "Submit each one within 30 minutes. (Comments are written automatically by the clipper.)"})
    _ISSUE["n"] = made["number"]
    return made["number"]


def comment_issue(text: str) -> bool:
    if not (os.getenv("GITHUB_TOKEN") and os.getenv("GITHUB_REPOSITORY")):
        return False
    try:
        _gh("POST", f"/repos/{os.environ['GITHUB_REPOSITORY']}/issues/{issue_number()}/comments", {"body": text})
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[notify] github issue comment failed: {e}")
        return False


def send(text: str) -> bool:
    sent = comment_issue(text)
    hook = os.getenv("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            req = urllib.request.Request(hook, data=json.dumps({"content": text[:1900]}).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "gta6-clipper"})
            urllib.request.urlopen(req, timeout=20).read()
            sent = True
        except Exception as e:  # noqa: BLE001 - a failed ping must never stop posting
            print(f"[notify] discord failed: {e}")
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            data = json.dumps({"chat_id": chat, "text": text[:4000], "disable_web_page_preview": False}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            sent = True
        except Exception as e:  # noqa: BLE001
            print(f"[notify] telegram failed: {e}")
    if not sent:
        print(f"[notify] (no Discord/Telegram configured) {text}")
    return sent
