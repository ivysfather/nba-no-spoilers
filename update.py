#!/usr/bin/env python3
"""Build a spoiler-free NBA highlights feed.

Pulls the schedule from ESPN and highlight videos from the Gametime Highlights
YouTube channel, matches them up, and writes docs/data.json. Scores, game
status details (OT, etc.) and video titles never leave this script.

Usage:
  python3 update.py              # last 3 days + today
  python3 update.py --days 7     # look back further
Set NTFY_TOPIC to get a phone push when a new highlight is matched.
"""
import argparse
import datetime as dt
import json
import os
import re
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
DATA = ROOT / "docs" / "data.json"
SEEN = ROOT / "state" / "videos.json"  # every video we have ever seen, so RSS's 15-item limit doesn't lose any
CHANNEL_ID = "UC0LrZO9wORIqn_aRJtKdgfA"
ET = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


# ---------- schedule ----------

def fetch_games(day):
    """Games for one ET date. Keeps only teams, tip-off and pre/in/post state."""
    raw = json.loads(get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={day:%Y%m%d}"))
    games = []
    for e in raw.get("events", []):
        comp = e["competitions"][0]
        teams = {}
        for c in comp["competitors"]:
            t = c["team"]
            teams[c["homeAway"]] = {
                "name": t["displayName"],
                "nick": t["shortDisplayName"],
                "abbr": t["abbreviation"],
                "logo": t.get("logo", ""),
            }
        games.append({
            "id": e["id"],
            "date": day.isoformat(),
            "tipoff": e["date"],
            "state": e["status"]["type"]["state"],  # pre / in / post — never the detail text
            "home": teams["home"],
            "away": teams["away"],
        })
    return games


# ---------- videos ----------

def fetch_rss():
    xml = get(f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}")
    vids = []
    for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        vid = re.search(r"<yt:videoId>(.*?)</yt:videoId>", entry).group(1)
        title = re.search(r"<title>(.*?)</title>", entry).group(1)
        published = re.search(r"<published>(.*?)</published>", entry).group(1)
        vids.append({"id": vid, "title": title, "published": published})
    return vids


def fetch_channel_page():
    """Backfill: the /videos page lists ~30 uploads (no publish timestamps)."""
    html = get(f"https://www.youtube.com/channel/{CHANNEL_ID}/videos?hl=en&gl=US")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        return []
    vids = []

    def walk(o):
        if isinstance(o, dict):
            if "lockupViewModel" in o:
                lv = o["lockupViewModel"]
                try:
                    title = lv["metadata"]["lockupMetadataViewModel"]["title"]["content"]
                    vids.append({"id": lv["contentId"], "title": title, "published": None})
                except KeyError:
                    pass
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(json.loads(m.group(1)))
    return vids


def load_seen():
    if SEEN.exists():
        return json.loads(SEEN.read_text())
    return {}


def merge_videos(seen, fresh):
    for v in fresh:
        old = seen.get(v["id"])
        # RSS is English and has a timestamp; prefer it over the page version
        if old is None or (v["published"] and not old.get("published")):
            seen[v["id"]] = v
    return seen


# ---------- matching ----------

def title_date(title):
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", title)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def is_full_game(title):
    t = title.lower()
    return "highlights" in t or "melhores momentos" in t


def match(games, videos):
    """Map game id -> video id. A video must name both teams and fit the date."""
    out = {}
    for v in videos:
        if not is_full_game(v["title"]):
            continue
        t = v["title"].lower()
        vdate = title_date(v["title"])
        candidates = []
        for g in games:
            if g["home"]["nick"].lower() not in t or g["away"]["nick"].lower() not in t:
                continue
            if vdate and vdate != g["date"]:
                continue
            if v.get("published") and v["published"] < g["tipoff"]:
                continue
            candidates.append(g)
        if candidates:
            g = max(candidates, key=lambda g: g["tipoff"])  # most recent meeting
            out.setdefault(g["id"], v["id"])
    return out


# ---------- notify ----------

def notify(topic, game):
    msg = f"{game['away']['nick']} at {game['home']['nick']} highlights are up"
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=msg.encode(),
        headers={"Title": "NBA highlights", "Tags": "basketball", "Click": os.environ.get("DASHBOARD_URL", "")},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # a failed push shouldn't break the update
        print(f"ntfy failed: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()

    today = dt.datetime.now(ET).date()
    games = []
    for i in range(args.days, -2, -1):  # back N days through tomorrow
        games += fetch_games(today - dt.timedelta(days=i))

    seen = load_seen()
    for fetcher in (fetch_rss, fetch_channel_page):
        try:
            seen = merge_videos(seen, fetcher())
        except Exception as e:
            print(f"{fetcher.__name__} failed: {e}")
    SEEN.parent.mkdir(exist_ok=True)
    SEEN.write_text(json.dumps(seen, indent=1))

    matches = match(games, list(seen.values()))

    previous = None  # first run: fill the page quietly, no pushes
    if DATA.exists():
        previous = {g["id"] for g in json.loads(DATA.read_text())["games"] if g.get("video")}

    topic = os.environ.get("NTFY_TOPIC")
    for g in games:
        g["video"] = matches.get(g["id"])
        if topic and previous is not None and g["video"] and g["id"] not in previous:
            notify(topic, g)

    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps({
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "games": games,
    }, indent=1))
    found = sum(1 for g in games if g["video"])
    print(f"{len(games)} games, {found} with highlights -> {DATA}")


if __name__ == "__main__":
    main()
