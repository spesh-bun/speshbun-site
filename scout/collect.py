#!/usr/bin/env python3
"""
Speshbun scout collector. Runs on GitHub Actions; needs no PC.

It reads each request file in scout/requests/ that has no matching file in
scout/results/, runs the searches it lists against the YouTube Data API, and
writes what it found to scout/results/<same name>.

What it looks for: finished releases that distributors (DistroKid, CD Baby,
TuneCore and others) deliver to YouTube as plain audio tracks. Those carry a
description that starts "Provided to YouTube by ...". Everything else that a
search returns (gig clips, lessons, vlogs) is counted and dropped.

The API key comes from the YOUTUBE_API_KEY secret and is never printed.
Each search costs one of the 100 daily searches. No third-party packages.
"""

import datetime as dt
import json
import os
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUESTS = HERE / "requests"
RESULTS = HERE / "results"
API = "https://www.googleapis.com/youtube/v3/"

DEFAULTS = {
    "max_views": 1500,        # plays ceiling per track
    "min_seconds": 90,
    "max_minutes": 15,
    "released_before": "2023-01-01",   # keeps clear of the AI-generated flood
    "released_after": "2014-06-01",   # YouTube began making these tracks around then
    "results_per_search": 50,
}

# Self-described AI music, or a point made of denying it. Whole phrases only.
AI_MARKERS = [re.compile(p) for p in (
    r"\bai[- ]generated\b",
    r"\bgenerated (?:by|with|using) (?:an? )?ai\b",
    r"\b(?:made|created|composed|produced|written) (?:entirely |fully |partly )?"
    r"(?:by|with|using) (?:an? )?ai\b",
    r"\bai[- ]?(?:music|songs?|band|artist|singer|vocals?|voice|cover)\b",
    r"\b(?:suno|udio)(?:\.com|\.ai| ?ai\b| v?\d)",
    r"#(?:ai(?:music|songs?|generated|cover)|suno\w*|udio\w*|noai\w*)\b",
    r"\b(?:not|non|without|zero|0%)[- ]+(?:any |using |use of )?ai\b",
    r"\bai[- ]free\b",
    r"\b100% human\b",
)]


class ApiError(Exception):
    pass


def api_get(endpoint, params, key):
    url = API + endpoint + "?" + urllib.parse.urlencode(dict(params, key=key))
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body)["error"]["message"]
        except Exception:
            msg = body[:200]
        # Never include the URL in an error: it contains the key.
        raise ApiError(f"YouTube API error {err.code} on {endpoint}: {msg}")
    except urllib.error.URLError as err:
        raise ApiError(f"Could not reach YouTube ({endpoint}): {err.reason}")


def iso_seconds(text):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text or "")
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def says_ai(*texts):
    for text in texts:
        low = (text or "").lower()
        if any(p.search(low) for p in AI_MARKERS):
            return True
    return False


def read_release(description):
    """Pulls the distributor and release date out of an auto-generated
    description. Returns (distributor, released) or (None, None)."""
    desc = description or ""
    m = re.match(r"\s*Provided to YouTube by (.+)", desc)
    if not m:
        return None, None
    distributor = m.group(1).strip()
    r = re.search(r"Released on:\s*(\d{4}-\d{2}-\d{2})", desc)
    return distributor, (r.group(1) if r else None)


def run_search(search, opts, key, rng):
    """One search: returns (kept tracks, counts)."""
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    after = dt.datetime.strptime(opts["released_after"], "%Y-%m-%d")
    before = dt.datetime.strptime(opts["released_before"], "%Y-%m-%d")
    # A search returns the newest uploads before a cutoff, so a random cutoff
    # in the window gives a different slice of years on every run.
    cutoff = after + (before - after) * (0.25 + 0.75 * rng.random())
    data = api_get("search", {
        "part": "id", "q": search["q"], "type": "video", "order": "date",
        "videoCategoryId": "10", "maxResults": opts["results_per_search"],
        "publishedAfter": after.strftime(stamp),
        "publishedBefore": cutoff.strftime(stamp),
    }, key)
    ids = [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]
    counts = {"returned": len(ids), "releases": 0, "too_many_plays": 0,
              "wrong_length": 0, "ai": 0, "no_embed": 0, "kept": 0,
              "uploads_up_to": cutoff.strftime("%Y-%m")}
    kept = []
    for i in range(0, len(ids), 50):
        vids = api_get("videos", {
            "part": "snippet,statistics,contentDetails,status",
            "id": ",".join(ids[i:i + 50]),
        }, key)
        for v in vids.get("items", []):
            sn = v["snippet"]
            distributor, released = read_release(sn.get("description"))
            if not distributor:
                continue                      # not a distributor release
            counts["releases"] += 1
            views = int(v.get("statistics", {}).get("viewCount", 0) or 0)
            secs = iso_seconds(v.get("contentDetails", {}).get("duration"))
            if views > opts["max_views"]:
                counts["too_many_plays"] += 1
                continue
            if secs < opts["min_seconds"] or secs > opts["max_minutes"] * 60:
                counts["wrong_length"] += 1
                continue
            if says_ai(sn.get("title"), sn.get("description"), " ".join(sn.get("tags", []))) \
                    or v.get("status", {}).get("containsSyntheticMedia"):
                counts["ai"] += 1
                continue
            embeddable = bool(v.get("status", {}).get("embeddable", True))
            if not embeddable:
                counts["no_embed"] += 1       # counted, still kept: links work
            counts["kept"] += 1
            kept.append({
                "video_id": v["id"],
                "title": sn.get("title", ""),
                "artist": re.sub(r"\s+-\s+Topic$", "", sn.get("channelTitle", "")),
                "channel_id": sn.get("channelId", ""),
                "views": views,
                "minutes": round(secs / 60, 1),
                "uploaded": sn.get("publishedAt", "")[:10],
                "released": released,
                "distributor": distributor,
                "embeddable": embeddable,
                "style": search.get("style", ""),
                "query": search["q"],
            })
    return kept, counts


def run_request(path, key):
    req = json.loads(path.read_text(encoding="utf-8"))
    opts = dict(DEFAULTS, **{k: v for k, v in req.items() if k in DEFAULTS})
    rng = random.Random(req.get("seed"))
    out = {"request": path.name, "ran_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "settings": opts, "searches": [], "tracks": []}
    seen = set()
    for search in req.get("searches", []):
        try:
            kept, counts = run_search(search, opts, key, rng)
        except ApiError as err:
            out["searches"].append({"style": search.get("style", ""), "q": search["q"], "error": str(err)})
            print(f"  FAILED {search['q']!r}: {err}")
            continue
        fresh = [t for t in kept if t["video_id"] not in seen]
        seen.update(t["video_id"] for t in fresh)
        out["tracks"].extend(fresh)
        out["searches"].append(dict({"style": search.get("style", ""), "q": search["q"]}, **counts))
        print(f"  {search['q']!r}: {counts}")
    return out


def main():
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    pending = [p for p in sorted(REQUESTS.glob("*.json")) if not (RESULTS / p.name).exists()]
    if not pending:
        print("No new requests.")
        return 0
    if not key:
        print("The YOUTUBE_API_KEY secret is not set for this repository, so nothing was run.")
        return 1
    RESULTS.mkdir(exist_ok=True)
    for path in pending:
        print(f"Request {path.name}")
        result = run_request(path, key)
        (RESULTS / path.name).write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  {len(result['tracks'])} tracks kept")
    return 0


if __name__ == "__main__":
    sys.exit(main())
