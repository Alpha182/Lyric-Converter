#!/usr/bin/env python3
"""Download a Spotify track's audio as mp3 via spotmate.online (unofficial).

    python fetch_track.py <spotify url or track id> <out.mp3>

Flow (mirrors the site's own frontend):
    GET  /en1                    -> session cookies + csrf token
    POST /getTrackData           -> track metadata (name, artists, cover)
    POST /convert {"urls": url}  -> {"task_id": ...} (or an immediate url)
    GET  /tasks/<id>             -> poll until data.status == "finished"
    GET  <download url>          -> the mp3

The service is best effort: if it changes or blocks, this raises FetchError
with a readable message instead of crashing the caller.
"""
import json, re, sys, time, urllib.request, http.cookiejar

BASE = "https://spotmate.online"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
TRACK_ID = re.compile(r"^[0-9A-Za-z]{22}$")
POLL_EVERY = 4.0     # seconds between /tasks polls
POLL_MAX = 45        # ~3 minutes of polling before giving up


class FetchError(Exception):
    pass


def track_url(link_or_id):
    """Normalize a pasted link or bare 22-char id to an open.spotify.com URL."""
    s = (link_or_id or "").strip()
    if TRACK_ID.match(s):
        return "https://open.spotify.com/track/" + s
    m = re.search(r"open\.spotify\.com/(?:[a-z-]+/)?track/([0-9A-Za-z]{22})", s)
    if m:
        return "https://open.spotify.com/track/" + m.group(1)
    raise FetchError("That doesn't look like a Spotify track link or id.")


def track_id(link_or_id):
    return track_url(link_or_id).rsplit("/", 1)[1]


class Session:
    def __init__(self):
        cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        self.op.addheaders = [("User-Agent", UA), ("Accept", "*/*")]
        try:
            page = self.op.open(BASE + "/en1", timeout=20).read().decode("utf-8", "replace")
        except Exception as e:
            raise FetchError(f"spotmate.online is unreachable ({e}).")
        m = re.search(r'name="csrf-token" content="([^"]+)"', page)
        if not m:
            raise FetchError("spotmate.online didn't hand out a session (layout changed?).")
        self.csrf = m.group(1)

    def post_json(self, path, payload):
        req = urllib.request.Request(
            BASE + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-CSRF-TOKEN": self.csrf,
                     "Origin": BASE, "Referer": BASE + "/en1"})
        return json.loads(self.op.open(req, timeout=30).read().decode("utf-8", "replace"))

    def get_json(self, path):
        return json.loads(self.op.open(BASE + path, timeout=20).read().decode("utf-8", "replace"))


def _find_url(obj):
    """First http(s) url in any of the fields the site's own poller checks."""
    if not isinstance(obj, dict):
        return None
    for k in ("url", "download_url", "downloadUrl", "link", "file", "location"):
        v = obj.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


def fetch(link_or_id, out_path, log=lambda s: None):
    """Download the track to out_path; returns spotmate's track metadata dict."""
    url = track_url(link_or_id)
    s = Session()

    meta = {}
    try:
        meta = s.post_json("/getTrackData", {"spotify_url": url})
    except Exception:
        pass                                   # metadata is a nice-to-have
    name = meta.get("name") or "track"
    artists = ", ".join(a.get("name", "") for a in meta.get("artists", []) or [])
    log(f"converting: {name}{' by ' + artists if artists else ''}")

    try:
        conv = s.post_json("/convert", {"urls": url})
    except Exception as e:
        raise FetchError(f"convert request failed ({e}).")
    dl = None
    if conv.get("error") is False and _find_url(conv):
        dl = _find_url(conv)
    task = conv.get("task_id") or conv.get("taskId")
    if not dl and not task:
        raise FetchError("spotmate refused: " + str(
            conv.get("status") or conv.get("message") or conv.get("data") or conv)[:300])

    for attempt in range(POLL_MAX):
        if dl:
            break
        time.sleep(POLL_EVERY)
        try:
            payload = s.get_json(f"/tasks/{task}")
        except Exception:
            continue
        if payload.get("error"):
            raise FetchError("conversion failed: " + str(
                payload.get("message") or payload.get("status") or "unknown error")[:300])
        info = payload.get("data") or {}
        status = str(info.get("status") or info.get("state") or "").lower()
        log(f"converting ({status or 'queued'}, {attempt + 1})")
        if status == "finished":
            dl = _find_url(info) or _find_url(info.get("result") or {}) or _find_url(payload)
            if not dl:
                raise FetchError("conversion finished but no download link was provided.")
        elif status in ("failed", "error", "expired", "cancelled"):
            raise FetchError("conversion " + status + ": " + str(
                info.get("message") or info.get("error") or "no details")[:300])
    if not dl:
        raise FetchError("conversion timed out after ~3 minutes; try again.")

    log("downloading audio")
    req = urllib.request.Request(dl, headers={"User-Agent": UA, "Referer": BASE + "/en1"})
    with s.op.open(req, timeout=120) as r, open(out_path, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    import os
    size = os.path.getsize(out_path)
    if size < 100_000:                          # a real song is never this small
        raise FetchError(f"download came back suspiciously small ({size} bytes).")
    log(f"saved {size / 1048576:.1f} MB")
    return meta


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip())
    try:
        fetch(sys.argv[1], sys.argv[2], log=lambda s: print("  " + s, flush=True))
        print("done -> " + sys.argv[2])
    except FetchError as e:
        sys.exit("error: " + str(e))
