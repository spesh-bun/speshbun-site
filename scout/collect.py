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
    "max_artist_plays": 1000000,   # ceiling on an artist's lifetime plays across all releases
    "max_views": None,        # optional ceiling per track; None means no limit
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


# Practice material and production-line music: backing tracks, play-alongs,
# click and metronome versions, tempo-labelled loops, karaoke. Checked against
# the track title and the artist name.
PRACTICE_MARKERS = [re.compile(p) for p in (
    r"\bplay[- ]?along\b",
    r"\b(?:backing|jam|click|drum|practice|guide) tracks?\b",
    r"\bclick version\b",
    r"\bmetronome\b",
    r"\b\d{2,3} ?bpm\b",
    r"\bdrumless\b",
    r"\bminus one\b",
    r"\bkaraoke\b",
    r"\bno (?:drums|bass|guitar|piano|shaker|vocals)\b",
    r"\bfor (?:drums|drummers|bass|bassists|guitarists) (?:only|practice)\b",
    r"\b(?:funk|rock|jazz|blues|bossa|latin|drum) (?:beat|loop|groove) ?#? ?\d+\b",
)]


def is_practice(*texts):
    for text in texts:
        low = (text or "").lower()
        if any(p.search(low) for p in PRACTICE_MARKERS):
            return True
    return False


# Covers. The scout is after original work, so a track is dropped when its
# title is a well-known standard or its credits name a famous songwriter.
# Traditional songs are left alone on purpose.
STANDARDS = {
    # bossa nova and Brazilian
    "wave", "dindi", "recado bossa nova", "the gift", "chega de saudade", "no more blues",
    "garota de ipanema", "the girl from ipanema", "girl from ipanema", "samba de verao",
    "summer samba", "so nice", "desafinado", "corcovado", "quiet nights of quiet stars",
    "agua de beber", "insensatez", "how insensitive", "one note samba", "samba de uma nota so",
    "blue bossa", "a felicidade", "manha de carnaval", "black orpheus", "triste", "meditacao",
    "mas que nada", "so danco samba", "o barquinho", "aguas de marco", "waters of march",
    "the shadow of your smile", "tristeza", "berimbau", "o pato",
    # jazz standards
    "fly me to the moon", "autumn leaves", "summertime", "take five", "all the things you are",
    "misty", "my funny valentine", "body and soul", "caravan", "night and day", "blue monk",
    "round midnight", "so what", "all blues", "footprints", "cantaloupe island", "nuages",
    "minor swing", "troublant bolero", "take the a train", "georgia on my mind", "stardust",
    # boleros
    "besame mucho", "sabor a mi", "contigo aprendi", "solamente una vez", "quizas quizas quizas",
    "perfidia", "historia de un amor", "el reloj", "la barca", "contigo en la distancia",
    "dos gardenias", "veinte anos", "sin ti", "rayito de luna", "la gloria eres tu",
    # pop and rock songs that get covered constantly
    "somewhere over the rainbow", "over the rainbow", "here comes the sun", "yesterday",
    "hallelujah", "crimson and clover", "blackbird", "imagine", "let it be", "what a wonderful world",
    "the sound of silence", "hotel california", "wonderwall", "creep", "summer breeze",
}
FAMOUS_WRITERS = re.compile(
    r"\b(?:jobim|bonfa|vinicius de moraes|menescal|carlos lyra|baden powell|reinhardt|lennon|mccartney"
    r"|george harrison|bob dylan|leonard cohen|gershwin|cole porter|ellington|strayhorn|jerome kern"
    r"|richard rodgers|hammerstein|harold arlen|irving berlin|hoagy carmichael|thelonious monk"
    r"|coltrane|wayne shorter|herbie hancock|chick corea|mancini|bacharach|consuelo velazquez"
    r"|manzanero|cantoral|osvaldo farres|agustin lara|kenny dorham|paul desmond|brubeck)\b")
CREDIT_LINE = re.compile(r"^(?:composer|writer|lyricist|songwriter|composer lyricist|author)s?:\s*(.+)$", re.M)


def plain(text):
    """Lower case, accents removed, bracketed parts and 'feat.' credits cut."""
    import unicodedata
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[\(\[].*?[\)\]]", " ", t)
    t = re.sub(r"\b(?:feat|ft|featuring)\b.*$", " ", t)
    t = re.sub(r"\s+-\s+(?:live|remaster(?:ed)?|acoustic|instrumental|demo).*$", " ", t)
    return re.sub(r"[^a-z0-9 ]+", " ", t).strip()


def is_cover(title, description):
    if re.sub(r"\s+", " ", plain(title)) in STANDARDS:
        return True
    import unicodedata
    desc = unicodedata.normalize("NFKD", description or "").encode("ascii", "ignore").decode().lower()
    return any(FAMOUS_WRITERS.search(m.group(1)) for m in CREDIT_LINE.finditer(desc))


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
    retried = False
    if not ids:
        # YouTube's search sometimes comes back empty for no reason. Try once
        # more with a different slice of years (this costs a second search).
        retried = True
        cutoff = after + (before - after) * (0.25 + 0.75 * rng.random())
        data = api_get("search", {
            "part": "id", "q": search["q"], "type": "video", "order": "date",
            "videoCategoryId": "10", "maxResults": opts["results_per_search"],
            "publishedAfter": after.strftime(stamp),
            "publishedBefore": cutoff.strftime(stamp),
        }, key)
        ids = [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]
    counts = {"returned": len(ids), "retried": retried, "releases": 0, "too_many_plays": 0,
              "wrong_length": 0, "ai": 0, "practice": 0, "cover": 0, "no_embed": 0, "kept": 0,
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
            if opts["max_views"] is not None and views > opts["max_views"]:
                counts["too_many_plays"] += 1
                continue
            if secs < opts["min_seconds"] or secs > opts["max_minutes"] * 60:
                counts["wrong_length"] += 1
                continue
            if says_ai(sn.get("title"), sn.get("description"), " ".join(sn.get("tags", []))) \
                    or v.get("status", {}).get("containsSyntheticMedia"):
                counts["ai"] += 1
                continue
            if is_practice(sn.get("title"), sn.get("channelTitle")):
                counts["practice"] += 1
                continue
            if is_cover(sn.get("title"), sn.get("description")):
                counts["cover"] += 1
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


def channel_stats(channel_ids, key):
    """Audience figures YouTube reports for each artist channel: one small
    request covers 50 channels, and it uses none of the daily searches."""
    out = []
    for i in range(0, len(channel_ids), 50):
        data = api_get("channels", {"part": "snippet,statistics",
                                    "id": ",".join(channel_ids[i:i + 50])}, key)
        for c in data.get("items", []):
            st = c.get("statistics", {})
            hidden = bool(st.get("hiddenSubscriberCount"))
            out.append({
                "channel_id": c["id"],
                "artist": re.sub(r"\s+-\s+Topic$", "", c["snippet"].get("title", "")),
                "total_plays": int(st.get("viewCount", 0) or 0),
                "tracks": int(st.get("videoCount", 0) or 0),
                "subscribers": None if hidden else int(st.get("subscriberCount", 0) or 0),
            })
    return out


def find_artist_channel(name, key):
    """Finds the channel YouTube auto-generates for an artist's releases
    ("<name> - Topic"). Costs one search."""
    data = api_get("search", {"part": "snippet", "q": name, "type": "video",
                              "videoCategoryId": "10", "maxResults": 25}, key)
    want = name.strip().lower() + " - topic"
    for item in data.get("items", []):
        sn = item.get("snippet", {})
        if sn.get("channelTitle", "").strip().lower() == want:
            return sn.get("channelId")
    return None


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
        # Background check: drop artists whose lifetime plays are over the ceiling.
        counts["artist_too_big"] = 0
        if fresh and opts["max_artist_plays"] is not None:
            try:
                stats = {a["channel_id"]: a["total_plays"]
                         for a in channel_stats(sorted({t["channel_id"] for t in fresh}), key)}
            except ApiError as err:
                stats = {}
                counts["artist_check_error"] = str(err)
            checked = []
            for t in fresh:
                t["artist_plays"] = stats.get(t["channel_id"])
                if t["artist_plays"] is not None and t["artist_plays"] > opts["max_artist_plays"]:
                    counts["artist_too_big"] += 1
                else:
                    checked.append(t)
            fresh = checked
            counts["kept"] = len(fresh)
        out["tracks"].extend(fresh)
        out["searches"].append(dict({"style": search.get("style", ""), "q": search["q"]}, **counts))
        print(f"  {search['q']!r}: {counts}")

    # One track per artist: of the tracks found for an artist, keep the most played.
    best = {}
    for t in out["tracks"]:
        cur = best.get(t["channel_id"])
        if cur is None or t["views"] > cur["views"]:
            best[t["channel_id"]] = t
    out["tracks_before_one_per_artist"] = len(out["tracks"])
    out["tracks"] = [t for t in out["tracks"] if best[t["channel_id"]] is t]

    # Background check: audience figures for artists, by channel or by name.
    ids = list(req.get("channel_ids", []))
    named = {}
    for name in req.get("artist_names", []):
        try:
            cid = find_artist_channel(name, key)
        except ApiError as err:
            print(f"  FAILED lookup of {name!r}: {err}")
            cid = None
        named[name] = cid
        if cid:
            ids.append(cid)
    if ids or named:
        try:
            out["artists"] = channel_stats(list(dict.fromkeys(ids)), key)
        except ApiError as err:
            out["artists_error"] = str(err)
        out["artist_names"] = named
        print(f"  audience figures for {len(out.get('artists', []))} artists")
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
