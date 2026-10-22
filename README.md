# No Spoilers NBA

Full-game highlights from [Gametime Highlights](https://www.youtube.com/@TheGametimeHighlights/videos), matched to the NBA schedule, without scores anywhere.

- `update.py` pulls the schedule (ESPN) and the channel's videos (RSS + /videos page), matches them by team names and date, and writes `docs/data.json`. Only team names, logos, tip-off time and pre/live/final state are kept. No scores, no OT tags, no video titles.
- `docs/index.html` is the dashboard. Click **Watch** to play the video in the page. Watched games are remembered per browser.

## Run it on your Mac

```bash
python3 update.py
python3 -m http.server 4330 --directory docs
```

Open http://localhost:4330.

## Phone: GitHub Pages + scheduled updates

1. Create a repo on github.com and push this folder.
2. Settings → Pages → Deploy from branch → `main` / `docs`.
3. The workflow in `.github/workflows/update.yml` runs `update.py` every 30 min on game nights and commits new data.
4. On iPhone, open the Pages URL in Safari → Share → Add to Home Screen.

The repo has to be public for free Pages. That's fine: the data has no scores and nothing personal.

## Push notifications (ntfy)

1. Install the **ntfy** app (iOS/Android) and subscribe to a hard-to-guess topic, e.g. `renzo-nba-7f3k2`.
2. In the repo: Settings → Secrets → Actions → add `NTFY_TOPIC` with that topic. Optionally add a variable `DASHBOARD_URL` with your Pages URL so tapping the alert opens the dashboard.
3. You get a push like "Warriors at Lakers highlights are up". No scores.

Locally: `NTFY_TOPIC=renzo-nba-7f3k2 python3 update.py`.
