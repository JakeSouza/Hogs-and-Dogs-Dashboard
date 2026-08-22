# Sleeper Fantasy Football Dashboard

Auto-updating dashboard for a Sleeper fantasy football league, published via
GitHub Pages. Regenerates daily via GitHub Actions.

## Setup

1. **Create this repo** and copy in `sleeper_dashboard.py`, `requirements.txt`,
   and `.github/workflows/update.yml`.
2. **Add a GitHub Secret**: `LEAGUE_ID` — your Sleeper league id (the long
   number in your league's sleeper.app URL).
   Optional secret: `HISTORY_START_YEAR` (default `2018`) — earliest season to
   pull for the History tab.
3. **Enable GitHub Pages**: Settings → Pages → Deploy from branch → `main` /
   `(root)`.
4. Run the **Actions → Update Dashboard** workflow once (or wait for the daily
   schedule). It commits `index.html`, which Pages then serves.

> No API keys needed — Sleeper's API is free and read-only.

## Run locally

```bash
LEAGUE_ID=123456789012345678 python sleeper_dashboard.py
open index.html
```

## Co-managers

Sleeper rosters expose both `owner_id` and a `co_owners` list. Every manager is
pulled and joined with ` & `.
