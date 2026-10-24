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
import subprocess
import time
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

FAVORITES = {"Warriors", "Timberwolves"}


def notify(topic, game):
    fav = bool({game["away"]["nick"], game["home"]["nick"]} & FAVORITES)
    msg = f"{game['away']['nick']} at {game['home']['nick']} highlights are up"
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=msg.encode(),
        headers={"Title": "Your team's highlights" if fav else "NBA highlights",
                 "Tags": "star,basketball" if fav else "basketball",
                 "Priority": "high" if fav else "default",
                 "Click": os.environ.get("DASHBOARD_URL", "")},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # a failed push shouldn't break the update
        print(f"ntfy failed: {e}")


def run_once(days):
    """One pass: schedule + videos -> docs/data.json. Returns (games, newly matched game ids)."""
    today = dt.datetime.now(ET).date()
    games = []
    for i in range(days, -2, -1):  # back N days through tomorrow
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
    new = []
    for g in games:
        g["video"] = matches.get(g["id"])
        if previous is not None and g["video"] and g["id"] not in previous:
            new.append(g["id"])
            if topic:
                notify(topic, g)

    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps({
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "games": games,
    }, indent=1))
    found = sum(1 for g in games if g["video"])
    print(f"{len(games)} games, {found} with highlights -> {DATA}")
    return games, new


# ---------- watch mode (one GitHub job per night) ----------
# GitHub's cron skips most runs, so instead of many short scheduled runs, one job
# stays up through the night and checks on a timer tied to each game's tip-off.

FAV_DUE = dt.timedelta(hours=2, minutes=30)   # start looking for favorites at tip + 2.5h
GAME_DUE = dt.timedelta(hours=3)              # everyone else at tip + 3h
CHECK_EVERY = 5 * 60
JOB_LIMIT = dt.timedelta(hours=5, minutes=40)  # GitHub kills jobs at 6h
GIVE_UP_HOUR = 11                              # stop waiting at 11am ET


def game_night(now):
    """Games that tip before noon ET belong to the previous night."""
    return (now - dt.timedelta(hours=12)).date().isoformat()


def publish(message):
    """Commit and push data changes (watch mode runs inside GitHub Actions)."""
    subprocess.run(["git", "add", "docs/data.json", "state/videos.json"], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-q", "-m", message], check=True)
    for _ in range(3):
        subprocess.run(["git", "pull", "-q", "--rebase", "-X", "theirs"], check=False)
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            return
        time.sleep(10)
    print("push failed")


def watch(days):
    start = dt.datetime.now(dt.timezone.utc)
    night = game_night(dt.datetime.now(ET))
    games, _ = run_once(days)
    publish("Update highlights")
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        pending = [g for g in games if g["date"] == night and not g["video"]]
        if not pending:
            print(f"All {night} games have highlights. Done.")
            return
        if dt.datetime.now(ET).hour == GIVE_UP_HOUR:
            print(f"Still missing {len(pending)} at {GIVE_UP_HOUR}am ET. Giving up for tonight.")
            return
        def due(g):
            fav = bool({g["home"]["nick"], g["away"]["nick"]} & FAVORITES)
            return dt.datetime.fromisoformat(g["tipoff"].replace("Z", "+00:00")) + (FAV_DUE if fav else GAME_DUE)

        next_due = min(due(g) for g in pending)
        if now >= next_due:
            games, new = run_once(days)
            if new:
                publish(f"Highlights: {len(new)} new")
            wait = CHECK_EVERY
        else:
            wait = min((next_due - now).total_seconds(), 30 * 60)
            print(f"Next check {next_due:%H:%M} UTC ({len(pending)} pending)")
        if now - start > JOB_LIMIT:
            if any(g["date"] == night and not g["video"] for g in games):
                print("Still pending; handing off to a fresh job.")
                subprocess.run(["gh", "workflow", "run", "watch.yml"], check=False)
            return
        time.sleep(wait)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--watch", action="store_true", help="stay up through tonight's games (GitHub Actions)")
    args = ap.parse_args()
    if args.watch:
        watch(args.days)
    else:
        run_once(args.days)


if __name__ == "__main__":
    main()
