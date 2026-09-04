"""
Sleeper Fantasy Football Dashboard Generator
=============================================
Generates a self-contained HTML dashboard (standings, matchups, power
rankings, luck index, recent activity, draft board, history) for a Sleeper
fantasy football league using Sleeper's free, no-auth read-only API.

SETUP
-----
  pip install requests
Set the league id (and optional history start year) via env vars / GitHub
Secrets:
  LEAGUE_ID=123456789012345678
  HISTORY_START_YEAR=2018
  OUTPUT_FILE=index.html

USAGE
-----
  python sleeper_dashboard.py
"""
import os
import json
import html
import urllib.request
import urllib.error
from datetime import datetime

BASE = "https://api.sleeper.app/v1"


def _env_or_default(name, default):
    value = os.environ.get(name)
    return value if value else default


LEAGUE_ID = _env_or_default("LEAGUE_ID", "")
OUTPUT_FILE = _env_or_default("OUTPUT_FILE", "index.html")
RECENT_ACTIVITY_COUNT = int(_env_or_default("RECENT_ACTIVITY_COUNT", "25"))
HISTORY_START_YEAR = int(_env_or_default("HISTORY_START_YEAR", "2018"))
MAX_WEEK = 18  # NFL regular season + small buffer

# Number of rounds in this league's rookie-only draft. This always drives
# the future Draft Capital board directly — it's intentionally NOT derived
# from the actual most recent draft's round count, since that draft could
# be a much longer startup/auction draft (e.g. 20+ rounds to fill a full
# roster) that has nothing to do with how long a future rookie-only draft
# actually runs.
ROOKIE_DRAFT_ROUNDS = int(_env_or_default("ROOKIE_DRAFT_ROUNDS", "3"))
# How many future seasons of rookie picks to show on the Draft Capital board.
DRAFT_CAPITAL_YEARS_AHEAD = int(_env_or_default("DRAFT_CAPITAL_YEARS_AHEAD", "3"))

# Weekly parlay tracking (1 leg submitted per manager, 12 legs total) lives
# entirely outside Sleeper's data model — there is no odds/betting endpoint
# in the public API. Two backends are supported, checked in this priority
# order:
#
#   1. Firebase Firestore (live, multi-device, no login required) — set
#      FIREBASE_CONFIG to the JSON web-app config from your Firebase
#      project. See FIREBASE_SETUP.md for the one-time setup this requires
#      (create a free project, publish firestore.rules, register a web
#      app). Every manager submits their own leg from their own device;
#      grading is a batch "load week, tap results, save" flow. Nothing
#      about this needs a code change or repo update week to week.
#   2. Local JSON file + browser localStorage (fallback, single-user) — used
#      automatically when Firebase isn't configured. See load_parlay_weeks()
#      below for the file format. Fine for a commissioner entering all 12
#      legs themselves; doesn't sync across devices/managers.
PARLAY_FILE = _env_or_default("PARLAY_FILE", "parlay.json")
FIREBASE_CONFIG = _env_or_default("FIREBASE_CONFIG", "")

# Superlatives: 2 categories.
#   - STAT_SUPERLATIVE_DEFS: computed automatically every generation from
#     league data (see compute_stat_superlatives()).
#   - VOTED_SUPERLATIVES: decided by league vote at season's end — these
#     stay empty placeholders all season, filled in manually once voted.
VOTED_SUPERLATIVES = [
    "Best Trader",
    "Best Trash Talker",
    "Best Locker Room Guy",
    "Wild Card",
    "Biggest Fall Off",
    "Most Improved",
]

# Sleeper's public API does NOT expose real/legal names anywhere — only
# 'username' and 'display_name' (both user-chosen handles). There is no
# first_name/last_name/real_name field to fall back on like ESPN has, so
# showing actual manager names is only possible with a manual mapping.
# Fill this in with your league members' real names, keyed by their
# Sleeper username OR user_id (either works; username is usually easier to
# read here). Anyone not listed will fall back to their display name.
#   MANAGER_NAME_OVERRIDES = {
#       "jsouzzz": "Jake Souza",
#       "some_other_username": "Jane Doe",
#   }
MANAGER_NAME_OVERRIDES = {
    # "username_or_user_id": "Real Name",
}


# --------------------------------------------------------------------------- #
#  API ACCESS LAYER  (the only part that is Sleeper-specific)
# --------------------------------------------------------------------------- #
def api(path):
    url = BASE + path
    req = urllib.request.Request(url, headers={"User-Agent": "sleeper-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def avatar_url(avatar_id):
    return f"https://sleepercdn.com/avatars/{avatar_id}" if avatar_id else ""


def manager_name(u):
    """Best-effort real name for a Sleeper user.

    Sleeper's API doesn't provide real names at all (only username and
    display_name — both self-chosen handles), so this checks
    MANAGER_NAME_OVERRIDES first. It also checks a couple of unofficial
    fields ('real_name', metadata first_name/last_name) in case a future
    API version or a specific league happens to populate them, but for most
    leagues those will be empty and this will fall through to the override
    map, and finally to display_name/username if nothing is configured."""
    if not isinstance(u, dict):
        return None

    username = (u.get("username") or "").strip()
    user_id = (u.get("user_id") or "").strip()
    if username in MANAGER_NAME_OVERRIDES:
        return MANAGER_NAME_OVERRIDES[username]
    if user_id in MANAGER_NAME_OVERRIDES:
        return MANAGER_NAME_OVERRIDES[user_id]

    # Unofficial/undocumented fields — populated for essentially no one on
    # Sleeper today, but harmless to check in case that ever changes.
    real = (u.get("real_name") or "").strip()
    if real:
        return real
    meta = u.get("metadata") or {}
    first = (meta.get("first_name") or "").strip()
    last = (meta.get("last_name") or "").strip()
    combined = f"{first} {last}".strip()
    if combined:
        return combined

    # Fallback: display name / username (the chosen handle)
    return (u.get("display_name") or u.get("username") or "").strip() or None


_PLAYERS = None
def get_players():
    global _PLAYERS
    if _PLAYERS is None:
        try:
            _PLAYERS = api("/players/nfl") or {}
        except Exception:
            _PLAYERS = {}
    return _PLAYERS


def player_display(pid):
    p = get_players().get(str(pid))
    if p:
        fn = p.get("first_name") or ""
        ln = p.get("last_name") or ""
        full = f"{fn} {ln}".strip()
        return full or p.get("player_id") or str(pid)
    s = str(pid)
    if len(s) <= 3 and s == s.upper():
        return f"{s} D/ST"
    return str(pid)


def player_age(pid):
    """Age in whole years from Sleeper's player birth_date, or None if unknown
    (common for D/ST entries and some rookies not yet fully populated)."""
    p = get_players().get(str(pid))
    if not p:
        return None
    bd = p.get("birth_date")
    if not bd:
        return None
    try:
        y, m, d = [int(x) for x in bd.split("-")]
    except Exception:
        return None
    today = datetime.utcnow().date()
    return today.year - y - ((today.month, today.day) < (m, d))


def build_teams(league_id):
    rosters = api(f"/league/{league_id}/rosters")
    users = api(f"/league/{league_id}/users")
    user_by_id = {u["user_id"]: u for u in users}
    teams = {}
    for r in rosters:
        rid = r["roster_id"]
        owner_ids = []
        if r.get("owner_id"):
            owner_ids.append(r["owner_id"])
        for co in (r.get("co_owners") or []):
            if co and co not in owner_ids:
                owner_ids.append(co)
        owner_names = []
        for oid in owner_ids:
            u = user_by_id.get(oid)
            name = manager_name(u)
            if name:
                owner_names.append(name)
        owner = " & ".join(owner_names) if owner_names else None
        u = user_by_id.get(r.get("owner_id"), {})
        meta = u.get("metadata") or {}
        team_name = meta.get("team_name") or manager_name(u) or f"Team {rid}"
        s = r.get("settings") or {}
        fpts = (s.get("fpts") or 0) + (s.get("fpts_decimal") or 0) / 100.0
        fpts_against = (s.get("fpts_against") or 0) + (s.get("fpts_against_decimal") or 0) / 100.0
        teams[rid] = {
            "roster_id": rid,
            "team_name": team_name,
            "owner": owner,
            "wins": s.get("wins", 0),
            "losses": s.get("losses", 0),
            "ties": s.get("ties", 0),
            "fpts": fpts,
            "fpts_against": fpts_against,
            "avatar": avatar_url(u.get("avatar")),
            "players": r.get("players") or [],
        }
    return teams


def weekly_results(league_id, max_week):
    """Return scores {rid:{week:pts}}, pairs {week:[(a,b)]}, outcomes {rid:[W/L/T...]},
    starter_pts {week:{rid:{player_id:pts}}} (starters only — bench points
    don't count toward a team's score, so they're excluded from head-to-head
    "top scorer" calculations that use this), and season_pts {player_id:pts}
    (every rostered player, starters AND bench, summed across the whole
    range — used for the draft board's performance heat map, where a
    bench stash still counts as part of "how has this pick performed")."""
    scores, pairs, outcomes, starter_pts, season_pts = {}, {}, {}, {}, {}
    for w in range(1, max_week + 1):
        try:
            mu = api(f"/league/{league_id}/matchups/{w}")
        except Exception:
            mu = []
        if not mu:
            continue
        # Sleeper returns 0 (not null) for every roster's points on a week
        # that's scheduled but hasn't actually been played yet — the schedule
        # slot exists for the rest of the season even in week 1. Without this
        # check, every not-yet-played matchup gets counted as a real 0-0
        # "meeting" (inflating head-to-head history, rivalries, records, etc.
        # for weeks that never happened). A week where literally every
        # roster shows 0 is treated as not played and skipped entirely.
        if all(not (t.get("custom_points") or t.get("points")) for t in mu):
            continue
        by_matchup = {}
        for t in mu:
            rid = t.get("roster_id")
            pts = t.get("custom_points")
            if pts is None:
                pts = t.get("points")
            if pts is None:
                continue
            pts = float(pts)
            scores.setdefault(rid, {})[w] = pts
            by_matchup.setdefault(t.get("matchup_id"), []).append((rid, pts))
            starters = t.get("starters") or []
            pp = t.get("players_points") or {}
            starter_pts.setdefault(w, {})[rid] = {pid: (pp.get(pid) or 0) for pid in starters if pid and pid != "0"}
            for pid, ppts in pp.items():
                if pid and pid != "0":
                    season_pts[pid] = season_pts.get(pid, 0) + (ppts or 0)
        for lst in by_matchup.values():
            if len(lst) == 2:
                (a, sa), (b, sb) = lst
                pairs.setdefault(w, []).append((a, b))
                if sa > sb:
                    outcomes.setdefault(a, []).append("W"); outcomes.setdefault(b, []).append("L")
                elif sb > sa:
                    outcomes.setdefault(b, []).append("W"); outcomes.setdefault(a, []).append("L")
                else:
                    outcomes.setdefault(a, []).append("T"); outcomes.setdefault(b, []).append("T")
    return scores, pairs, outcomes, starter_pts, season_pts


def streak_from_outcomes(seq):
    if not seq:
        return ("-", 0)
    last = seq[-1]
    n = 0
    for o in reversed(seq):
        if o == last:
            n += 1
        else:
            break
    label = {"W": "W", "L": "L", "T": "T"}.get(last, last)
    return (label, n)


def champion_and_runnerup(league_id, ordered, playoff_week_start):
    """
    Identifies the season's champion and runner-up from the winners
    bracket, and best-effort looks up the actual championship-game score.
    Falls back to (#1 seed, None, None) if the bracket data isn't
    available or hasn't finished (e.g. mid-season).
    """
    try:
        br = api(f"/league/{league_id}/winners_bracket") or []
    except Exception:
        br = []
    champ = ordered[0] if ordered else None
    runnerup = None
    score = None
    if br:
        decided = [m for m in br if m.get("w") is not None]
        if decided:
            final = max(decided, key=lambda m: m.get("r", 0))
            win_rid, lose_rid = final.get("w"), final.get("l")
            for t in ordered:
                if t["roster_id"] == win_rid:
                    champ = t
                if lose_rid is not None and t["roster_id"] == lose_rid:
                    runnerup = t
            if lose_rid is not None and playoff_week_start:
                champ_week = playoff_week_start + int(final.get("r", 1)) - 1
                score = _fetch_championship_score(league_id, champ_week, win_rid, lose_rid)
    return champ, runnerup, score


def _fetch_championship_score(league_id, week, win_rid, lose_rid):
    """Best-effort final score of the championship game; None if it can't be found."""
    try:
        mu = api(f"/league/{league_id}/matchups/{week}") or []
    except Exception:
        return None
    pts = {}
    for t in mu:
        rid = t.get("roster_id")
        if rid in (win_rid, lose_rid):
            p = t.get("custom_points")
            if p is None:
                p = t.get("points")
            if p is not None:
                pts[rid] = round(float(p), 1)
    if win_rid in pts and lose_rid in pts:
        return pts[win_rid], pts[lose_rid]
    return None


def _update_streak(state, team_id, outcome, year, week, name, owner):
    """
    Rolling win/loss streak tracker keyed by team identity (roster_id).
    Call once per completed game in ASCENDING year order. Streaks are
    contained to a single season — crossing into a new year always breaks
    a streak in progress, even if the outcome type would otherwise have
    continued it, so "longest win/loss streak" reflects one season's run
    rather than one stitched across a season boundary. A tie also breaks
    both a win and a loss streak. Tracks the best win streak and best loss
    streak seen so far, each with the year/week span it covers.
    """
    s = state.setdefault(team_id, {
        "current_type": None, "current_len": 0, "current_start": None,
        "best_win": None, "best_loss": None, "last_year": None,
    })
    if s["last_year"] is not None and year != s["last_year"]:
        s["current_type"], s["current_len"], s["current_start"] = None, 0, None
    s["last_year"] = year
    if outcome == "T":
        s["current_type"], s["current_len"], s["current_start"] = None, 0, None
        return
    if outcome == s["current_type"]:
        s["current_len"] += 1
    else:
        s["current_type"] = outcome
        s["current_len"] = 1
        s["current_start"] = (year, week)
    entry = {
        "length": s["current_len"], "name": name, "owner": owner,
        "start_year": s["current_start"][0], "start_week": s["current_start"][1],
        "end_year": year, "end_week": week,
    }
    key = "best_win" if outcome == "W" else "best_loss"
    if s[key] is None or entry["length"] > s[key]["length"]:
        s[key] = entry


def fetch_history(current_league_id, current_season, start_year):
    champions, season_standings, all_time, rivalries = [], {}, {}, {}
    records = {
        "highest_score": None, "lowest_score": None, "biggest_blowout": None,
        "closest_game": None, "most_points_loss": None,
    }
    streak_state = {}
    season_snapshots = []  # collected newest-first; replayed ascending for streaks

    league_id, season = current_league_id, current_season
    while league_id and int(season) >= start_year:
        try:
            lg = api(f"/league/{league_id}")
        except Exception:
            break
        teams = build_teams(league_id)
        ordered = sorted(teams.values(), key=lambda t: (-t["wins"], t["losses"], -t["fpts"]))
        season_standings[int(season)] = [{
            "rank": i + 1, "name": t["team_name"], "owner": t.get("owner"),
            "wins": t["wins"], "losses": t["losses"], "ties": t["ties"], "pf": t["fpts"],
        } for i, t in enumerate(ordered)]
        playoff_week_start = (lg.get("settings") or {}).get("playoff_week_start")
        champ, runnerup, champ_score = champion_and_runnerup(league_id, ordered, playoff_week_start)
        # Only record a champion for seasons that have actually finished — the
        # in-progress current season has no winners_bracket result yet, and
        # champion_and_runnerup() falls back to the #1 standings seed in that
        # case, which would incorrectly crown whoever's leading mid-season.
        if champ and int(season) != int(current_season):
            champions.append({
                "year": int(season), "name": champ["team_name"], "owner": champ.get("owner"),
                "runnerup_name": runnerup["team_name"] if runnerup else None,
                "runnerup_owner": runnerup.get("owner") if runnerup else None,
                "record": f"{champ['wins']}-{champ['losses']}" + (f"-{champ['ties']}" if champ.get("ties") else ""),
                "score": champ_score,
            })
        scores, pairs, _, starter_pts, _ = weekly_results(league_id, MAX_WEEK)
        season_snapshots.append({"season": int(season), "teams": teams, "scores": scores, "pairs": pairs})
        for w, plist in pairs.items():
            for a, b in plist:
                sa, sb = scores.get(a, {}).get(w), scores.get(b, {}).get(w)
                if sa is None or sb is None:
                    continue
                key = frozenset({a, b})
                h2h = rivalries.setdefault(key, {"meetings": 0, "wins": {}, "points": {}, "games": [], "player_pts": {}})
                h2h["meetings"] += 1
                h2h["wins"].setdefault(a, 0); h2h["wins"].setdefault(b, 0)
                h2h["points"].setdefault(a, 0.0); h2h["points"].setdefault(b, 0.0)
                if sa > sb: h2h["wins"][a] += 1
                elif sb > sa: h2h["wins"][b] += 1
                else: h2h["wins"][a] += 0.5; h2h["wins"][b] += 0.5
                h2h["points"][a] += sa; h2h["points"][b] += sb
                h2h["games"].append({"season": int(season), "week": w, "a": a, "b": b, "a_score": sa, "b_score": sb})
                wk_starter_pts = starter_pts.get(w, {})
                for rid in (a, b):
                    bucket = h2h["player_pts"].setdefault(rid, {})
                    for pid, pts in wk_starter_pts.get(rid, {}).items():
                        bucket[pid] = bucket.get(pid, 0) + (pts or 0)
        for t in teams.values():
            e = all_time.setdefault(t["roster_id"], {
                "name": t["team_name"], "owner": None, "wins": 0, "losses": 0,
                "ties": 0, "pf": 0.0, "seasons": 0})
            e["name"] = t["team_name"]
            if t.get("owner"): e["owner"] = t["owner"]
            e["wins"] += t["wins"]; e["losses"] += t["losses"]; e["ties"] += t["ties"]
            e["pf"] += t["fpts"]; e["seasons"] += 1
        prev = lg.get("previous_league_id")
        if not prev or prev == league_id:
            break
        league_id, season = prev, int(season) - 1
    for e in all_time.values():
        g = e["wins"] + e["losses"] + e["ties"]
        e["win_pct"] = (e["wins"] + 0.5 * e["ties"]) / g if g else 0

    # Records book: replay every season's games in ASCENDING year order
    # (season_snapshots was built newest-first while walking previous_league_id
    # backward) so win/loss streaks correctly carry across season boundaries.
    for snap in reversed(season_snapshots):
        year, teams, scores, pairs = snap["season"], snap["teams"], snap["scores"], snap["pairs"]
        for w in sorted(pairs.keys()):
            for a, b in pairs[w]:
                sa, sb = scores.get(a, {}).get(w), scores.get(b, {}).get(w)
                if sa is None or sb is None:
                    continue
                ta, tb = teams.get(a, {}), teams.get(b, {})
                name_a, owner_a = ta.get("team_name", f"Team {a}"), ta.get("owner")
                name_b, owner_b = tb.get("team_name", f"Team {b}"), tb.get("owner")
                outcome_a = "W" if sa > sb else ("L" if sb > sa else "T")
                outcome_b = "W" if sb > sa else ("L" if sa > sb else "T")

                _update_streak(streak_state, a, outcome_a, year, w, name_a, owner_a)
                _update_streak(streak_state, b, outcome_b, year, w, name_b, owner_b)

                for rid, nm, ow, sc, oc in ((a, name_a, owner_a, sa, outcome_a), (b, name_b, owner_b, sb, outcome_b)):
                    if records["highest_score"] is None or sc > records["highest_score"]["value"]:
                        records["highest_score"] = {"value": sc, "name": nm, "owner": ow, "year": year, "week": w}
                    if records["lowest_score"] is None or sc < records["lowest_score"]["value"]:
                        records["lowest_score"] = {"value": sc, "name": nm, "owner": ow, "year": year, "week": w}
                    if oc == "L" and (records["most_points_loss"] is None or sc > records["most_points_loss"]["value"]):
                        records["most_points_loss"] = {"value": sc, "name": nm, "owner": ow, "year": year, "week": w}

                margin = round(abs(sa - sb), 1)
                if sa >= sb:
                    game = {"margin": margin, "winner": name_a, "winner_owner": owner_a, "loser": name_b, "loser_owner": owner_b,
                            "winner_score": sa, "loser_score": sb, "year": year, "week": w, "tie": sa == sb}
                else:
                    game = {"margin": margin, "winner": name_b, "winner_owner": owner_b, "loser": name_a, "loser_owner": owner_a,
                            "winner_score": sb, "loser_score": sa, "year": year, "week": w, "tie": False}
                if records["biggest_blowout"] is None or margin > records["biggest_blowout"]["margin"]:
                    records["biggest_blowout"] = game
                if records["closest_game"] is None or margin < records["closest_game"]["margin"]:
                    records["closest_game"] = game

    for s in streak_state.values():
        if s["best_win"] and (records.get("longest_win_streak") is None or s["best_win"]["length"] > records["longest_win_streak"]["length"]):
            records["longest_win_streak"] = s["best_win"]
        if s["best_loss"] and (records.get("longest_loss_streak") is None or s["best_loss"]["length"] > records["longest_loss_streak"]["length"]):
            records["longest_loss_streak"] = s["best_loss"]
    records.setdefault("longest_win_streak", None)
    records.setdefault("longest_loss_streak", None)

    return champions, season_standings, all_time, rivalries, records


def fetch_transaction_history(current_league_id, current_season, start_year):
    """
    Walks the dynasty league_id chain (same previous_league_id pattern as
    fetch_history, run as a separate pass since it needs different endpoints
    per season — every week's transactions plus that season's draft picks).
    Produces two things:

      - acquisition: {(roster_id, player_id str): {"season","week","method"}}
        the MOST RECENT acquisition event for each player currently useful
        for computing tenure. "method" is one of "Startup Draft",
        "Rookie Draft", "Trade", "Waiver", "Free Agent".
      - trade_log: every completed trade across the tracked seasons, newest
        first, with the full multi-team player+pick package each side
        received (dynasty trades routinely include future picks, not just
        players, so this captures both).

    This fetches every week of every historical season (transactions aren't
    available in bulk), so it's noticeably heavier than fetch_history — fine
    for a script meant to run periodically (e.g. a daily GitHub Action), not
    meant to be called on every page load.
    """
    acquisition = {}
    trade_log = []
    league_id, season = current_league_id, current_season
    while league_id and int(season) >= start_year:
        try:
            lg = api(f"/league/{league_id}")
        except Exception:
            break
        season_int = int(season)
        teams = build_teams(league_id)
        is_startup = not lg.get("previous_league_id")  # true genesis season of this dynasty chain

        try:
            drafts = api(f"/league/{league_id}/drafts") or []
        except Exception:
            drafts = []
        for d in drafts:
            try:
                picks = api(f"/draft/{d['draft_id']}/picks") or []
            except Exception:
                picks = []
            for p in picks:
                rid, pid = p.get("roster_id"), p.get("player_id")
                if rid is None or pid is None:
                    continue
                key = (rid, str(pid))
                ev = {"season": season_int, "week": 0, "method": "Startup Draft" if is_startup else "Rookie Draft"}
                prev = acquisition.get(key)
                if prev is None or (ev["season"], ev["week"]) >= (prev["season"], prev["week"]):
                    acquisition[key] = ev

        for w in range(1, MAX_WEEK + 1):
            try:
                txs = api(f"/league/{league_id}/transactions/{w}") or []
            except Exception:
                txs = []
            for tx in txs:
                if tx.get("status") != "complete":
                    continue
                ttype = tx.get("type", "")
                adds = tx.get("adds") or {}
                drops = tx.get("drops") or {}
                ts = tx.get("status_updated") or tx.get("created") or 0

                if ttype == "trade":
                    roster_ids = tx.get("roster_ids") or []
                    pkg = {rid: {"players_in": [], "players_out": [], "picks_in": []} for rid in roster_ids}
                    for pid, rid in adds.items():
                        pkg.setdefault(rid, {"players_in": [], "players_out": [], "picks_in": []})
                        pkg[rid]["players_in"].append(player_display(pid))
                    for pid, rid in drops.items():
                        pkg.setdefault(rid, {"players_in": [], "players_out": [], "picks_in": []})
                        pkg[rid]["players_out"].append(player_display(pid))
                    for dp in (tx.get("draft_picks") or []):
                        owner = dp.get("owner_id")
                        pkg.setdefault(owner, {"players_in": [], "players_out": [], "picks_in": []})
                        orig_name = teams.get(dp.get("roster_id"), {}).get("team_name", "?")
                        pkg[owner]["picks_in"].append(f"{dp.get('season')} Rd {dp.get('round')} (orig. {orig_name})")
                    date_str = datetime.utcfromtimestamp(ts / 1000).strftime("%b %d, %Y") if ts else ""
                    trade_log.append({
                        "date": date_str, "ts": ts, "season": season_int, "week": w,
                        "teams": [{
                            "name": teams.get(rid, {}).get("team_name", f"Team {rid}"),
                            "gets": pkg.get(rid, {}).get("players_in", []) + pkg.get(rid, {}).get("picks_in", []),
                            "gives": pkg.get(rid, {}).get("players_out", []),
                        } for rid in roster_ids],
                    })
                    for pid, rid in adds.items():
                        key = (rid, str(pid))
                        ev = {"season": season_int, "week": w, "method": "Trade"}
                        prev = acquisition.get(key)
                        if prev is None or (ev["season"], ev["week"]) >= (prev["season"], prev["week"]):
                            acquisition[key] = ev
                else:
                    label = "Waiver" if ttype == "waiver" else "Free Agent"
                    for pid, rid in adds.items():
                        key = (rid, str(pid))
                        ev = {"season": season_int, "week": w, "method": label}
                        prev = acquisition.get(key)
                        if prev is None or (ev["season"], ev["week"]) >= (prev["season"], prev["week"]):
                            acquisition[key] = ev

        prev_league = lg.get("previous_league_id")
        if not prev_league or prev_league == league_id:
            break
        league_id, season = prev_league, int(season) - 1

    trade_log.sort(key=lambda t: -t.get("ts", 0))
    for t in trade_log:
        t.pop("ts", None)
    return acquisition, trade_log


def compute_roster_ages(teams):
    """Average/oldest/youngest age per team from each roster's full player pool
    (bench + IR included, since dynasty rosters carry stashes that matter)."""
    rows = []
    for t in teams.values():
        ages = []
        for pid in t.get("players", []):
            a = player_age(pid)
            if a is not None:
                ages.append((a, player_display(pid)))
        if not ages:
            continue
        avg_age = sum(a for a, _ in ages) / len(ages)
        oldest = max(ages, key=lambda x: x[0])
        youngest = min(ages, key=lambda x: x[0])
        rows.append({
            "name": t["team_name"], "owner": t.get("owner"), "logo": t.get("avatar"),
            "avg_age": round(avg_age, 1), "counted": len(ages),
            "oldest_name": oldest[1], "oldest_age": oldest[0],
            "youngest_name": youngest[1], "youngest_age": youngest[0],
        })
    rows.sort(key=lambda r: r["avg_age"])
    return rows


def compute_player_tenure(teams, acquisition, current_season):
    """
    How long each currently-rostered player has been on their team, using
    the most recent acquisition event found by fetch_transaction_history.
    Players with no event at all predate the tracked history window (they
    were already rostered at HISTORY_START_YEAR) — tenure for those is a
    labeled lower bound, not exact.
    """
    rows = []
    for t in teams.values():
        for pid in t.get("players", []):
            ev = acquisition.get((t["roster_id"], str(pid)))
            p = get_players().get(str(pid)) or {}
            if ev:
                seasons = max(current_season - ev["season"] + 1, 1)
                if ev["method"] in ("Startup Draft", "Rookie Draft"):
                    label = f"{ev['method']}, {ev['season']}"
                else:
                    label = f"{ev['method']}, Wk {ev['week']} {ev['season']}"
                approx = False
            else:
                seasons = max(current_season - HISTORY_START_YEAR + 1, 1)
                label = f"On roster since {HISTORY_START_YEAR} or earlier"
                approx = True
            rows.append({
                "team": t["team_name"], "player": player_display(pid), "pos": p.get("position", ""),
                "age": player_age(pid), "acquired_label": label, "seasons": seasons, "approx": approx,
            })
    rows.sort(key=lambda r: (r["team"], -r["seasons"]))
    return rows


def fetch_draft_capital(league_id, teams, base_rounds, seasons_ahead, current_season):
    """
    Future rookie-draft pick ownership board. Sleeper's /traded_picks
    endpoint tracks the LATEST hop for any pick that's changed hands —
    (season, round, original roster_id) -> current owner_id and the
    previous_owner_id it was most recently acquired from — which is exactly
    "who they got the extra pick from" for picks traded more than once too.
    """
    try:
        traded = api(f"/league/{league_id}/traded_picks") or []
    except Exception:
        traded = []
    overrides = {}
    for tp in traded:
        try:
            tseason = int(tp.get("season"))
        except (TypeError, ValueError):
            continue
        overrides[(tseason, tp.get("round"), tp.get("roster_id"))] = {
            "owner_id": tp.get("owner_id"), "previous_owner_id": tp.get("previous_owner_id"),
        }

    board = []
    for season in range(current_season + 1, current_season + 1 + seasons_ahead):
        rows = []
        for rnd in range(1, base_rounds + 1):
            for rid, t in teams.items():
                ov = overrides.get((season, rnd, rid))
                owner_id = ov["owner_id"] if ov else rid
                traded_flag = owner_id != rid
                via_id = ov["previous_owner_id"] if (ov and traded_flag) else None
                rows.append({
                    "round": rnd,
                    "original_name": t["team_name"],
                    "owner_name": teams.get(owner_id, {}).get("team_name", f"Team {owner_id}"),
                    "traded": traded_flag,
                    "via_name": teams.get(via_id, {}).get("team_name") if via_id is not None else None,
                })
        rows.sort(key=lambda r: (r["round"], r["original_name"]))
        board.append({"season": season, "rows": rows})
    return board


def build_h2h_lookup(rivalries, teams, name_by_id):
    """All-time head-to-head detail for every pair that's played, keyed
    'lowRosterId-highRosterId' for the Matchups tab's team-picker tool."""
    data = {}
    for pair, h2h in rivalries.items():
        ids = sorted(pair)
        if len(ids) < 2:
            continue
        a, b = ids
        games = h2h.get("games", [])
        closest = min(games, key=lambda g: abs(g["a_score"] - g["b_score"])) if games else None
        blowout = max(games, key=lambda g: abs(g["a_score"] - g["b_score"])) if games else None

        def top_scorer(rid, roster_ids):
            best = None
            for pid, pts in h2h.get("player_pts", {}).get(rid, {}).items():
                if str(pid) not in roster_ids:
                    continue
                if best is None or pts > best[1]:
                    best = (pid, pts)
            return {"name": player_display(best[0]), "pts": round(best[1], 1)} if best else None

        roster_a = {str(p) for p in teams.get(a, {}).get("players", [])}
        roster_b = {str(p) for p in teams.get(b, {}).get("players", [])}

        def game_summary(g):
            return {"margin": round(abs(g["a_score"] - g["b_score"]), 1), "season": g["season"], "week": g["week"]}

        data[f"{a}-{b}"] = {
            "a_id": a, "b_id": b,
            "a_name": name_by_id.get(a, f"Team {a}"), "b_name": name_by_id.get(b, f"Team {b}"),
            "meetings": h2h["meetings"],
            "wins_a": h2h["wins"].get(a, 0), "wins_b": h2h["wins"].get(b, 0),
            "pts_a": round(h2h["points"].get(a, 0), 1), "pts_b": round(h2h["points"].get(b, 0), 1),
            "closest": game_summary(closest) if closest else None,
            "blowout": game_summary(blowout) if blowout else None,
            "top_scorer_a": top_scorer(a, roster_a),
            "top_scorer_b": top_scorer(b, roster_b),
        }
    return data


def load_parlay_weeks():
    """
    Weekly parlay tracking (1 leg submitted per manager toward a shared
    12-leg parlay) is entirely outside Sleeper's data model — there's no
    odds/betting endpoint in the public API — so it's sourced from a small,
    manually-maintained JSON file (PARLAY_FILE) instead. Expected shape:

      [
        {"week": 1, "season": 2026, "legs": [
            {"manager": "Jake", "pick": "Justin Jefferson anytime TD", "result": "hit"},
            {"manager": "Dana", "pick": "Chiefs -3.5", "result": "miss"},
            ... one entry per manager's submitted leg ...
        ]},
        ...
      ]

    "result" is "hit", "miss", or "pending" (omit/leave pending before the
    week's games finish). A missing or malformed file is treated as "no
    data yet" rather than an error — the tab just shows a placeholder with
    the expected format until the file exists.
    """
    if not PARLAY_FILE or not os.path.exists(PARLAY_FILE):
        return []
    try:
        with open(PARLAY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def compute_parlay_summary(weeks):
    if not weeks:
        return None
    weekly, manager_stats = [], {}
    parlays_hit = parlays_decided = 0
    for wk in sorted(weeks, key=lambda w: (w.get("season", 0), w.get("week", 0))):
        legs = wk.get("legs") or []
        results = [l.get("result") for l in legs]
        if results and all(r == "hit" for r in results):
            outcome = "hit"
        elif any(r == "miss" for r in results):
            outcome = "miss"
        else:
            outcome = "pending"
        if outcome in ("hit", "miss"):
            parlays_decided += 1
            if outcome == "hit":
                parlays_hit += 1
        for l in legs:
            mgr = l.get("manager", "Unknown")
            st = manager_stats.setdefault(mgr, {"hits": 0, "misses": 0, "pending": 0})
            r = l.get("result")
            if r == "hit": st["hits"] += 1
            elif r == "miss": st["misses"] += 1
            else: st["pending"] += 1
        weekly.append({"season": wk.get("season"), "week": wk.get("week"), "legs": legs, "outcome": outcome})
    leaderboard = []
    for mgr, st in manager_stats.items():
        decided = st["hits"] + st["misses"]
        rate = (st["hits"] / decided * 100) if decided else 0
        leaderboard.append({"manager": mgr, **st, "rate": round(rate, 1), "decided": decided})
    leaderboard.sort(key=lambda x: (-x["rate"], -x["decided"]))
    return {"weekly": weekly, "leaderboard": leaderboard, "parlays_hit": parlays_hit, "parlays_decided": parlays_decided}


def compute_transaction_counts(league_id, max_week):
    """Total completed transactions per roster this season (waiver adds,
    free-agent adds, and trades — each party to a trade gets +1). Powers
    the Most Active / Worst GM superlatives."""
    counts = {}
    for w in range(1, max_week + 1):
        try:
            txs = api(f"/league/{league_id}/transactions/{w}") or []
        except Exception:
            txs = []
        for tx in txs:
            if tx.get("status") != "complete":
                continue
            roster_ids = set(tx.get("roster_ids") or [])
            if not roster_ids:
                adds = tx.get("adds") or {}
                drops = tx.get("drops") or {}
                roster_ids = set(adds.values()) | set(drops.values())
            for rid in roster_ids:
                counts[rid] = counts.get(rid, 0) + 1
    return counts


def compute_strength_of_schedule(pairs, standings):
    """
    Games played against a CURRENTLY top-half opponent, per team — a simple
    "hardest schedule" proxy. Sleeper doesn't expose historical week-by-week
    rank, so this uses final/current standings rather than each opponent's
    strength at the time they were actually played — a reasonable proxy,
    not a precise point-in-time calculation.
    """
    if not standings:
        return {}
    team_count = len(standings)
    top_n = max(1, -(-team_count // 2))  # ceil(team_count / 2)
    top_ids = {s["roster_id"] for s in standings[:top_n]}
    counts = {s["roster_id"]: 0 for s in standings}
    for plist in pairs.values():
        for a, b in plist:
            if a in counts and b in top_ids:
                counts[a] += 1
            if b in counts and a in top_ids:
                counts[b] += 1
    return counts


def compute_dookie_bracket_winner(league_id, teams):
    """Winner of the losers bracket (the 'Dookie Bracket'), once decided."""
    try:
        br = api(f"/league/{league_id}/losers_bracket") or []
    except Exception:
        br = []
    decided = [m for m in br if m.get("w") is not None]
    if not decided:
        return None
    final = max(decided, key=lambda m: m.get("r", 0))
    t = teams.get(final.get("w"))
    return {"name": t["team_name"], "owner": t.get("owner")} if t else None


def compute_stat_superlatives(league_id, teams, standings, pairs, parlay_summary):
    """
    The 6 stat-tracked superlatives, computed fresh every generation. Each
    is a dict shaped like the SUPERLATIVES cards: name, id (for the
    Best Parlay Picker card's client-side-fill hook, see render_superlatives),
    description, leader, value.
    """
    out = []

    dookie = compute_dookie_bracket_winner(league_id, teams)
    out.append({
        "name": "Dookie Bracket Winner", "id": "splat-dookie",
        "description": "Winner of the losers bracket.",
        "leader": dookie["name"] if dookie else "TBD", "owner": dookie.get("owner") if dookie else None,
        "value": "\U0001F4A9" if dookie else "Pending",
    })

    reg = standings[0] if standings else None
    out.append({
        "name": "Regular Season Winner", "id": "splat-regseason",
        "description": "Best regular season record.",
        "leader": reg["name"] if reg else "TBD", "owner": reg.get("owner") if reg else None,
        "value": f"{reg['wins']}-{reg['losses']}" if reg else "",
    })

    # Every team gets a 0-default entry, not just teams that appear in a
    # transaction — otherwise a team with zero moves is silently excluded
    # from the dict entirely and can never win "Worst GM".
    tx_counts = {rid: 0 for rid in teams}
    for rid, count in compute_transaction_counts(league_id, MAX_WEEK).items():
        tx_counts[rid] = count
    if tx_counts:
        most_id = max(tx_counts, key=tx_counts.get)
        least_id = min(tx_counts, key=tx_counts.get)
        most_team, least_team = teams.get(most_id, {}), teams.get(least_id, {})
        def _moves(n): return f"{n} move" + ("" if n == 1 else "s")
        most_val, least_val = _moves(tx_counts[most_id]), _moves(tx_counts[least_id])
    else:
        most_team = least_team = {}
        most_val = least_val = "Pending"
    out.append({
        "name": "Most Active", "id": "splat-active",
        "description": "Most total transactions (waivers + trades) this season.",
        "leader": most_team.get("team_name", "TBD"), "owner": most_team.get("owner"), "value": most_val,
    })
    out.append({
        "name": "Worst GM", "id": "splat-worstgm",
        "description": "Fewest total transactions this season.",
        "leader": least_team.get("team_name", "TBD"), "owner": least_team.get("owner"), "value": least_val,
    })

    sos = compute_strength_of_schedule(pairs, standings)
    if sos:
        hardest_id = max(sos, key=sos.get)
        hardest_team = teams.get(hardest_id, {})
        hardest_val = f"{sos[hardest_id]} top-half game" + ("" if sos[hardest_id] == 1 else "s")
    else:
        hardest_team = {}
        hardest_val = "Pending"
    out.append({
        "name": "Hardest Schedule", "id": "splat-hardsched",
        "description": "Most matchups against a currently top-half opponent.",
        "leader": hardest_team.get("team_name", "TBD"), "owner": hardest_team.get("owner"), "value": hardest_val,
    })

    # Best Parlay Picker: only fully knowable server-side when the parlay
    # tracker uses the local-file backend (its data is already in `parlay_summary`
    # at generation time). For the live Firebase backend, this card renders as
    # "Loading…" and is filled in client-side once that tab's data loads —
    # see the splat-parlay id hooks in the Weekly Parlay JS loader.
    if parlay_summary and parlay_summary["leaderboard"]:
        top = max(parlay_summary["leaderboard"], key=lambda r: (r["hits"], r["rate"]))
        out.append({
            "name": "Best Parlay Picker", "id": "splat-parlay",
            "description": "Most successful legs submitted to the weekly parlay.",
            "leader": top["manager"], "owner": None, "value": f"{top['hits']} hits ({top['rate']}%)",
        })
    else:
        out.append({
            "name": "Best Parlay Picker", "id": "splat-parlay",
            "description": "Most successful legs submitted to the weekly parlay.",
            "leader": "Loading…", "owner": None, "value": "",
        })

    return out


# --------------------------------------------------------------------------- #
#  ADAPTER: build a common `model` dict from Sleeper
# --------------------------------------------------------------------------- #
def delta_color(delta, dead_zone=3, max_delta=15):
    """
    Green the more a player has risen vs. draft slot, red the more they've
    fallen. Moves of `dead_zone` spots or fewer are treated as noise and
    stay neutral; the gradient only ramps up beyond that. Uses the exact
    same forest-green/firebrick-red convention as the ESPN dashboard's
    draft board, so the heat map reads the same way across both.
    """
    if delta is None or abs(delta) <= dead_zone:
        return "#181d29", "#232938"  # neutral panel/border, no meaningful change or no data
    span = max(max_delta - dead_zone, 1)
    magnitude = min(abs(delta) - dead_zone, span) / span  # 0..1
    intensity = 0.25 + 0.65 * magnitude
    if delta > 0:
        r, g, b = 34, 139, 34   # forest green
    else:
        r, g, b = 178, 34, 34   # firebrick red
    bg = f"rgba({r},{g},{b},{intensity:.2f})"
    border = f"rgba({r},{g},{b},{min(intensity + 0.25, 1):.2f})"
    return bg, border


def compute_draft_rank_data(picks, season_pts, is_auction=False):
    """
    For each drafted player: their "draft value rank" vs. their current
    positional rank (rank by total fantasy points scored this season among
    every player drafted at that position), plus the delta between them —
    positive means outperforming their draft value, negative means
    underperforming. Powers the draft board's heat map.

    "Draft value rank" means different things depending on draft type:
      - Snake: the Nth player at that position actually taken, in real
        pick order — pick order genuinely reflects perceived value in a
        snake draft (earlier = more valued).
      - Auction: nomination order carries no value information at all —
        two players nominated back to back can sell for $60 and $2. The
        actual dollar amount spent is the real signal of perceived value,
        so rank is based on cost (rank 1 = the most expensive player at
        that position), not the arbitrary order they were nominated in.
    """
    if is_auction:
        def sort_key(p):
            try:
                amt = float((p.get("metadata") or {}).get("amount") or 0)
            except (TypeError, ValueError):
                amt = 0
            return -amt  # highest $ spent first -> rank 1, matching "1st pick = most valued"
        ordered_picks = sorted(picks, key=sort_key)
    else:
        ordered_picks = sorted(picks, key=lambda p: p.get("pick_no") or 0)

    position_counters = {}
    player_pos = {}
    draft_pos_rank = {}
    player_cost = {}
    for p in ordered_picks:
        pid = p.get("player_id")
        meta = p.get("metadata") or {}
        pos = meta.get("position")
        if not pos or not pid:
            continue
        position_counters[pos] = position_counters.get(pos, 0) + 1
        draft_pos_rank[pid] = position_counters[pos]
        player_pos[pid] = pos
        if is_auction:
            try:
                player_cost[pid] = float(meta.get("amount") or 0)
            except (TypeError, ValueError):
                player_cost[pid] = 0

    by_position = {}
    for pid, pos in player_pos.items():
        by_position.setdefault(pos, []).append(pid)
    current_pos_rank = {}
    for pos, pids in by_position.items():
        ranked = sorted(pids, key=lambda pid: -season_pts.get(pid, 0))
        for i, pid in enumerate(ranked, start=1):
            current_pos_rank[pid] = i

    rank_data = {}
    for pid, dpr in draft_pos_rank.items():
        cpr = current_pos_rank.get(pid)
        delta = (dpr - cpr) if cpr else None
        rank_data[pid] = {"position": player_pos[pid], "draft_pos_rank": dpr,
                           "current_pos_rank": cpr, "delta": delta,
                           "cost": player_cost.get(pid) if is_auction else None}
    return rank_data


def build_model():
    state = api("/state/nfl")
    season = int(state.get("season") or datetime.utcnow().year)
    season_type = state.get("season_type", "regular")  # "pre", "regular", or "post"
    # Sleeper's "week" is a raw NFL calendar-week counter that keeps
    # incrementing through the preseason before the fantasy regular season
    # has even started — during preseason it shows preseason week numbers
    # (1-3), not a fantasy week. "leg" is what Sleeper itself documents as
    # "week of regular season", so that's the field to use once season_type
    # is "regular". Before then, no fantasy week has happened yet.
    if season_type == "regular":
        current_week = int(state.get("leg") or state.get("week") or 1)
    else:
        current_week = 1
    season_not_started = season_type != "regular"
    season_start_date = state.get("season_start_date")
    lg = api(f"/league/{LEAGUE_ID}")
    league_name = lg.get("name") or "Sleeper League"
    lg_settings = lg.get("settings") or {}
    playoff_spots = lg_settings.get("playoff_teams") or 0
    reg_season_weeks = max(0, (lg_settings.get("playoff_week_start") or 0) - 1)

    teams = build_teams(LEAGUE_ID)
    scores, pairs, outcomes, _, season_pts = weekly_results(LEAGUE_ID, MAX_WEEK)
    completed = sorted(pairs.keys())
    recent_weeks = completed[-3:]

    # ---- standings (current season) ----
    ordered = sorted(teams.values(), key=lambda t: (-t["wins"], t["losses"], -t["fpts"]))
    standings = []
    for i, t in enumerate(ordered, 1):
        sl, sn = streak_from_outcomes(outcomes.get(t["roster_id"], []))
        standings.append({
            "rank": i, "roster_id": t["roster_id"], "name": t["team_name"], "owner": t.get("owner"),
            "wins": t["wins"], "losses": t["losses"], "ties": t["ties"],
            "pf": t["fpts"], "pa": t["fpts_against"], "streak": f"{sl}{sn}" if sn else "-",
            "logo": t.get("avatar"),
        })

    # ---- matchups (current week) ----
    matchups = []
    try:
        mu = api(f"/league/{LEAGUE_ID}/matchups/{current_week}") or []
    except Exception:
        mu = []
    by_matchup = {}
    for t in mu:
        pts = t.get("custom_points")
        if pts is None: pts = t.get("points")
        by_matchup.setdefault(t.get("matchup_id"), []).append((t.get("roster_id"), float(pts) if pts is not None else None))
    # season averages for outlook
    avg_pts = {}
    for rid, wk in scores.items():
        vals = [v for v in wk.values()]
        avg_pts[rid] = sum(vals) / len(vals) if vals else 0
    for lst in by_matchup.values():
        if len(lst) != 2: continue
        (ra, pa), (rb, pb) = lst
        ta, tb = teams.get(ra, {}), teams.get(rb, {})
        rec = lambda x: f"{x.get('wins',0)}-{x.get('losses',0)}" + (f"-{x.get('ties',0)}" if x.get('ties') else "")
        # favorite by season average (or points if live)
        fav_a = (pa if pa is not None else avg_pts.get(ra, 0)) >= (pb if pb is not None else avg_pts.get(rb, 0))
        fav, dog = (ta, tb) if fav_a else (tb, ta)
        fp = pa if pa is not None else avg_pts.get(ra, 0)
        dp = pb if pb is not None else avg_pts.get(rb, 0)
        gap = round(abs(fp - dp), 1)
        matchups.append({
            "away": {"name": ta.get("team_name", "TBD"), "record": rec(ta), "proj": (round(pa, 1) if pa is not None else avg_pts.get(ra, 0))},
            "home": {"name": tb.get("team_name", "TBD"), "record": rec(tb), "proj": (round(pb, 1) if pb is not None else avg_pts.get(rb, 0))},
            "outlook": matchup_outlook(fav.get("team_name", "TBD"), dog.get("team_name", "TBD"), gap),
        })

    # ---- power rankings ----
    power = []
    if completed:
        raw = []
        for t in teams.values():
            rid = t["roster_id"]
            pts = [scores.get(rid, {}).get(w) for w in completed if scores.get(rid, {}).get(w) is not None]
            margins = []
            for w in completed:
                for a, b in pairs.get(w, []):
                    if a == rid or b == rid:
                        opp = b if a == rid else a
                        ms, os_ = scores.get(rid, {}).get(w), scores.get(opp, {}).get(w)
                        if ms is not None and os_ is not None: margins.append(ms - os_)
            rpts = [scores.get(rid, {}).get(w) for w in recent_weeks if scores.get(rid, {}).get(w) is not None]
            raw.append({"team": t, "avg_pts": sum(pts)/len(pts) if pts else 0,
                        "avg_margin": sum(margins)/len(margins) if margins else 0,
                        "avg_recent": sum(rpts)/len(rpts) if rpts else (sum(pts)/len(pts) if pts else 0)})

        def norm(vals):
            lo, hi = min(vals), max(vals)
            return [50.0 if hi == lo else (v - lo)/(hi - lo)*100 for v in vals]
        np_ = norm([r["avg_pts"] for r in raw]); nm = norm([r["avg_margin"] for r in raw]); nr = norm([r["avg_recent"] for r in raw])
        for i, r in enumerate(raw):
            r["score"] = round(0.45*np_[i] + 0.25*nm[i] + 0.30*nr[i], 1)
        raw.sort(key=lambda r: -r["score"])
        std_rank = {t["roster_id"]: i+1 for i, t in enumerate(ordered)}
        for pr, r in enumerate(raw, 1):
            t = r["team"]; sr = std_rank.get(t["roster_id"], pr)
            power.append({"rank": pr, "name": t["team_name"], "owner": t.get("owner"),
                           "score": r["score"], "avg_pts": round(r["avg_pts"], 1),
                           "avg_margin": round(r["avg_margin"], 1), "delta": sr - pr})

    # ---- luck index (all-play) ----
    luck = []
    if completed and len(teams) >= 2:
        ap = {rid: [] for rid in teams}
        for w in completed:
            wk_scores = {rid: scores.get(rid, {}).get(w) for rid in teams if scores.get(rid, {}).get(w) is not None}
            n = len(wk_scores)
            if n < 2: continue
            for rid, sc in wk_scores.items():
                beat = sum(1 for o, os_ in wk_scores.items() if o != rid and sc > os_)
                ap[rid].append(beat / (n - 1))
        for t in teams.values():
            lst = ap.get(t["roster_id"], [])
            if not lst: continue
            exp = sum(lst)/len(lst)
            g = t["wins"] + t["losses"] + t["ties"]
            act = (t["wins"] + 0.5*t["ties"])/g if g else 0
            l = act - exp
            lbl = "Lucky" if l > 0.12 else ("Unlucky" if l < -0.12 else "About Right")
            luck.append({"name": t["team_name"], "owner": t.get("owner"),
                         "actual": round(act*100, 1), "expected": round(exp*100, 1),
                         "luck": round(l*100, 1), "label": lbl})
        luck.sort(key=lambda x: -x["luck"])

    # ---- recent activity ----
    activity = []
    for w in range(max(1, current_week - 4), current_week + 1):
        try:
            txs = api(f"/league/{LEAGUE_ID}/transactions/{w}") or []
        except Exception:
            txs = []
        for tx in txs:
            if tx.get("status") != "complete": continue
            ts = tx.get("status_updated") or tx.get("created") or 0
            ttype = tx.get("type", "")
            adds = tx.get("adds") or {}
            drops = tx.get("drops") or {}
            tm = {rid: teams.get(rid, {}).get("team_name", f"Team {rid}") for rid in set(list(adds.values()) + list(drops.values()))}
            date_str = datetime.utcfromtimestamp(ts/1000).strftime("%b %d, %Y") if ts else ""
            if ttype == "trade":
                for pid, rid in adds.items():
                    activity.append({"date": date_str, "team": tm.get(rid, "?"), "action": "Traded for", "player": player_display(pid), "ts": ts})
                for pid, rid in drops.items():
                    activity.append({"date": date_str, "team": tm.get(rid, "?"), "action": "Traded away", "player": player_display(pid), "ts": ts})
            else:
                label = "Waiver add" if ttype == "waiver" else "Free agent add"
                for pid, rid in adds.items():
                    activity.append({"date": date_str, "team": tm.get(rid, "?"), "action": label, "player": player_display(pid), "ts": ts})
                for pid, rid in drops.items():
                    activity.append({"date": date_str, "team": tm.get(rid, "?"), "action": "Dropped", "player": player_display(pid), "ts": ts})
    activity.sort(key=lambda x: -x.get("ts", 0))
    activity = activity[:RECENT_ACTIVITY_COUNT]
    for a in activity: a.pop("ts", None)

    # ---- draft board ----
    draft = None
    try:
        drafts = api(f"/league/{LEAGUE_ID}/drafts") or []
        if drafts:
            # A league can have more than one draft object on record (e.g. a
            # restarted or aborted draft left over from setup, or a leftover
            # mock). Blindly taking drafts[0] can silently pick a stale,
            # incomplete one — the board dimensions (rounds/teams) come out
            # right from settings, but most cells are empty despite the real
            # draft having finished. Fetch picks for every draft object on
            # record and keep whichever is actually marked complete, breaking
            # ties by whichever has the most picks recorded.
            candidates = []
            for dr in drafts:
                try:
                    dpicks = api(f"/draft/{dr['draft_id']}/picks") or []
                except Exception:
                    dpicks = []
                candidates.append((dr, dpicks))
            candidates.sort(key=lambda c: (c[0].get("status") == "complete", len(c[1])), reverse=True)
            d, picks = candidates[0]

            total = d.get("settings", {}).get("teams") or len(teams)

            # The grid's row axis is each team's OWN chronological pick order
            # (their 1st player acquired, 2nd, 3rd...) rather than Sleeper's
            # raw "round" field. For a snake draft these are identical, since
            # every team picks exactly once per round in lockstep — but for
            # an auction draft, "round" is really just "which slice of the
            # overall sequential pick order this fell into," NOT "this
            # team's Nth acquisition": one team can win 2 players while
            # another wins 0 in the same "round" number, since there's no
            # synchronized turn order in an auction. Using raw "round" as the
            # grid key left real gaps in every team's column even though
            # each team genuinely won a full roster's worth of picks — this
            # per-team sequential index is correct for both draft types and
            # always fills every column with zero gaps.
            picks_by_roster = {}
            for p in picks:
                picks_by_roster.setdefault(p.get("roster_id"), []).append(p)
            for rid in picks_by_roster:
                picks_by_roster[rid].sort(key=lambda p: p.get("pick_no") or 0)

            grid = {}
            roster_columns = {}
            first_slot_seen = {}
            rounds = 0
            for rid, plist in picks_by_roster.items():
                roster_columns[rid] = teams.get(rid, {}).get("team_name", f"Team {rid}")
                first_slot_seen[rid] = plist[0].get("draft_slot") if plist else None
                for i, p in enumerate(plist, start=1):
                    pid = p.get("player_id")
                    meta = p.get("metadata") or {}
                    nm = player_display(pid) if pid else ""
                    if not nm or nm == str(pid):
                        # fall back to draft metadata if the player isn't in
                        # the players database for some reason (e.g. a very
                        # recent rookie not yet synced)
                        nm = f"{meta.get('first_name','')} {meta.get('last_name','')}".strip() or nm or str(pid)
                    grid[(i, rid)] = {"player": nm, "pos": meta.get("position", ""),
                                       "team": meta.get("team", ""), "owner": roster_columns[rid],
                                       "player_id": pid, "pick_no": p.get("pick_no")}
                rounds = max(rounds, len(plist))
            rounds = rounds or (d.get("settings") or {}).get("rounds", 0)

            # Column order: for a real snake draft, showing columns in
            # original draft-slot order (who picked 1st, 2nd, ...) is the
            # familiar, meaningful convention. For an auction (or any type
            # where draft_slot isn't a stable per-team seat), fall back to
            # alphabetical by team name since there's no "pick order" to
            # preserve.
            if (d.get("type") or "snake") == "snake" and all(v is not None for v in first_slot_seen.values()):
                ordered_rids = sorted(roster_columns, key=lambda r: first_slot_seen[r])
            else:
                ordered_rids = sorted(roster_columns, key=lambda r: roster_columns[r])
            order = {rid: roster_columns[rid] for rid in ordered_rids}
            is_auction = (d.get("type") or "snake") == "auction"
            rank_data = compute_draft_rank_data(picks, season_pts, is_auction=is_auction)
            draft = {"rounds": rounds, "teams": total, "grid": grid, "order": order,
                     "rank_data": rank_data, "is_auction": is_auction}
    except Exception:
        draft = None

    # ---- history ----
    champions, season_standings, all_time, rivalries, records = fetch_history(LEAGUE_ID, season, HISTORY_START_YEAR)
    hist = {
        "champions": champions,
        "season_standings": season_standings,
        "all_time": sorted(all_time.values(), key=lambda e: (-e["win_pct"], -e["pf"])),
        "rivalries": [],
        "records": records,
    }
    name_by_id = {t["roster_id"]: t["team_name"] for t in teams.values()}
    ranked = sorted(rivalries.items(), key=lambda kv: -kv[1]["meetings"])[:8]
    for pair, data in ranked:
        ids = list(pair)
        if len(ids) < 2: continue
        a, b = ids[0], ids[1]
        hist["rivalries"].append({
            "name_a": name_by_id.get(a, f"Team {a}"), "name_b": name_by_id.get(b, f"Team {b}"),
            "meetings": data["meetings"], "wins_a": int(data["wins"].get(a, 0)), "wins_b": int(data["wins"].get(b, 0)),
            "pts_a": round(data["points"].get(a, 0), 1), "pts_b": round(data["points"].get(b, 0), 1),
        })

    # ---- front office: roster ages, player tenure, draft capital ----
    acquisition, trade_log = fetch_transaction_history(LEAGUE_ID, season, HISTORY_START_YEAR)
    roster_ages = compute_roster_ages(teams)
    player_tenure = compute_player_tenure(teams, acquisition, season)
    # Deliberately NOT derived from the actual last draft's round count —
    # that draft could be a much longer startup/auction draft (e.g. 20+
    # rounds to fill a full roster), which says nothing about how many
    # rounds a future rookie-only draft runs. ROOKIE_DRAFT_ROUNDS is the
    # single source of truth for the Draft Capital board's round count.
    draft_capital = fetch_draft_capital(LEAGUE_ID, teams, ROOKIE_DRAFT_ROUNDS, DRAFT_CAPITAL_YEARS_AHEAD, season)

    # ---- head-to-head lookup (Matchups tab tool) ----
    h2h_lookup = build_h2h_lookup(rivalries, teams, name_by_id)

    # ---- superlatives (stat-tracked ones; voted ones are static, see VOTED_SUPERLATIVES) ----
    local_parlay_summary = compute_parlay_summary(load_parlay_weeks())
    stat_superlatives = compute_stat_superlatives(LEAGUE_ID, teams, standings, pairs, local_parlay_summary)

    # ---- manager -> active roster, for the Weekly Parlay pick dropdown ----
    manager_rosters = {}
    for t in teams.values():
        key = t.get("owner") or t["team_name"]
        manager_rosters[key] = sorted(
            (
                {"name": player_display(pid), "pos": (get_players().get(str(pid)) or {}).get("position", "")}
                for pid in t.get("players", [])
            ),
            key=lambda p: p["name"],
        )

    return {
        "league_name": league_name, "platform": "Sleeper", "season": season,
        "current_week": current_week, "season_not_started": season_not_started,
        "season_start_date": season_start_date,
        "standings": standings, "matchups": matchups, "power": power, "luck": luck,
        "activity": activity, "draft": draft, "history": hist,
        "playoff_spots": playoff_spots, "reg_season_weeks": reg_season_weeks,
        "roster_ages": roster_ages, "player_tenure": player_tenure,
        "draft_capital": draft_capital, "trade_log": trade_log, "h2h": h2h_lookup,
        "stat_superlatives": stat_superlatives, "manager_rosters": manager_rosters,
    }


def matchup_outlook(fav, dog, gap):
    seed = sum(ord(c) for c in (fav + dog))
    close = [f"This one's a coin flip. {fav} holds the slimmest of edges over {dog}, projected to win by just {gap} points.",
             f"{fav} and {dog} are neck and neck, separated by only {gap} projected points.",
             f"Too close to call. {fav} edges {dog} by {gap} points — one big play could flip it."]
    moderate = [f"{fav} enters as the favorite over {dog}, projected to win by about {gap} points.",
                f"On paper {fav} has the edge, out-projecting {dog} by {gap} points.",
                f"{fav} looks like the safer bet against {dog} this week, favored by roughly {gap} points."]
    blowout = [f"{fav} is projected to run away with it, out-scoring {dog} by a lopsided {gap} points.",
               f"This has blowout potential — {fav} is favored by {gap} points over {dog}.",
               f"The numbers aren't kind to {dog}, with {fav} projected to win by {gap} points."]
    pool = close if gap < 8 else (moderate if gap < 20 else blowout)
    return pool[seed % len(pool)]


# --------------------------------------------------------------------------- #
#  RENDERER  (identical across platforms — only consumes `model`)
# --------------------------------------------------------------------------- #
def esc(s):
    return html.escape("" if s is None else str(s))


def team_cell(name, owner=None, logo=None):
    logo_html = f"<img src='{esc(logo)}' class='logo'>" if logo else ""
    owner_html = f"<div class='owner-name'>{esc(owner)}</div>" if owner else ""
    return f"<div class='team-cell-inner'>{logo_html}<div><div class='team-name-main'>{esc(name)}</div>{owner_html}</div></div>"


CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#0a0d13;color:#f4f6fa;padding:24px}
h1,h2,h3{font-family:'Archivo Black',sans-serif}
h1{font-size:1.6rem;margin-bottom:4px}
.subtitle{color:#8a94a8;margin-bottom:20px;font-size:.95rem}

/* ---------- Hero / title banner ---------- */
.hero{position:relative;overflow:hidden;border-radius:14px;border:1px solid #232938;padding:36px 32px;margin-bottom:28px;background:linear-gradient(115deg,#0a0d13 60%,#10141f 100%)}
.hero-glow{position:absolute;top:-40px;right:-40px;width:220px;height:220px;background:#ff5b1f;opacity:.14;clip-path:polygon(30% 0,100% 0,100% 70%,70% 100%,0 100%,0 30%);pointer-events:none}
.hero-content{position:relative;z-index:1}
.hero-eyebrow{font-family:'Oswald',sans-serif;color:#ff5b1f;font-size:12px;font-weight:600;letter-spacing:2.5px;text-transform:uppercase;margin-bottom:10px}
.hero-title{font-family:'Archivo Black',sans-serif;text-transform:uppercase;margin:0 0 18px 0;font-size:42px;font-weight:400;letter-spacing:-1px;line-height:1.05;
  background:linear-gradient(90deg,#ffffff 0%,#cfd8f5 55%,#ff5b1f 130%);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.hero-meta{display:flex;flex-wrap:wrap;gap:8px}
.hero-chip{display:inline-flex;align-items:center;gap:6px;padding:7px 16px;border-radius:0;clip-path:polygon(8px 0,100% 0,100% 100%,0 100%,0 8px);font-family:'Oswald',sans-serif;font-size:13px;font-weight:600;letter-spacing:.3px;background:#181d29;border-left:3px solid #1e6fff;color:#f4f6fa}
.hero-chip-muted{color:#8a94a8;font-weight:400;background:transparent;border-left-color:#232938}
.hero-chip-est{border-left-color:#ffd23f;color:#ffd23f}
@media (max-width:640px){
  .hero{padding:24px 18px;border-radius:14px;margin-bottom:18px}
  .hero-title{font-size:28px}
  .hero-chip{font-size:12px;padding:5px 11px}
}
.ticker{background:#1e6fff;color:#fff;font-family:'Oswald',sans-serif;font-weight:600;font-size:12px;letter-spacing:.4px;padding:9px 0;overflow:hidden;white-space:nowrap;border-radius:10px;margin-bottom:18px}
.ticker-track{display:inline-block;padding-left:100%;animation:ticker-scroll 38s linear infinite}
.ticker-track span{display:inline-block;padding-right:56px}
.ticker-track span::after{content:'';display:inline-block;width:6px;height:6px;border-radius:50%;background:#ffd23f;margin:0 0 1px 56px;vertical-align:middle}
@keyframes ticker-scroll{from{transform:translateX(0)}to{transform:translateX(-100%)}}
@media (prefers-reduced-motion:reduce){.ticker-track{animation:none;padding-left:16px}}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:18px}
.tab{font-family:'Oswald',sans-serif;text-transform:uppercase;letter-spacing:.6px;background:#181d29;border:1px solid #232938;color:#c2c8d8;padding:9px 16px;border-radius:8px;cursor:pointer;font-size:.82rem;font-weight:600}
.tab.active{background:#ff5b1f;color:#fff;border-color:#ff5b1f}
.panel{display:none;background:#12161f;border:1px solid #232938;border-top:3px solid;border-image:linear-gradient(90deg,#1e6fff,#ff5b1f) 1;border-radius:12px;padding:20px}
.panel.active{display:block}
.section-title{position:relative;padding-left:14px;font-size:1.15rem;text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px;color:#fff}
.section-title::before{content:'';position:absolute;left:0;top:3px;bottom:3px;width:4px;border-radius:0;background:linear-gradient(180deg,#1e6fff,#ff5b1f)}
.section-note{color:#8a94a8;font-size:.85rem;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th{font-family:'Oswald',sans-serif;text-align:left;color:#8a94a8;padding:8px 10px;border-bottom:1px solid #232938;font-weight:600;font-size:.72rem;text-transform:uppercase;letter-spacing:.8px}
td{padding:9px 10px;border-bottom:1px solid #1f2740}
.team-cell-inner{display:flex;align-items:center;gap:10px}
.logo{width:30px;height:30px;border-radius:50%;object-fit:cover;border:2px solid rgba(30,111,255,0.35)}
.team-name-main{font-weight:600;color:#f4f6fa}
.owner-name{font-size:.78rem;color:#7a82a0}
.matchup-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.matchup-card{background:#181d29;border:1px solid #232938;border-top:3px solid #1e6fff;border-radius:6px;padding:14px}
.matchup-teams{display:flex;justify-content:space-between;align-items:center;gap:8px}
.matchup-team{text-align:center;flex:1}
.team-record{color:#8a94a8;font-size:.8rem}
.proj-score{font-size:1.3rem;font-weight:700;color:#1e6fff}
.vs{color:#5a6280;font-weight:700}
.outlook{margin-top:10px;font-size:.8rem;color:#a0a6c0;line-height:1.4}
.rivalry-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.rivalry-card{background:#181d29;border:1px solid #232938;border-top:3px solid #ff5b1f;border-radius:6px;padding:14px}
.rivalry-meetings{color:#8a94a8;font-size:.78rem;margin-bottom:8px}
.empty{color:#6a7090;font-style:italic;padding:14px}
.luck-good{color:#4ade80}.luck-bad{color:#f87171}
.action{color:#c2c8d8}
.subtabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}
.subtab{font-family:'Oswald',sans-serif;text-transform:uppercase;letter-spacing:.5px;background:#181d29;border:1px solid #232938;color:#9aa2b8;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:.75rem;font-weight:600}
.subtab.active{background:#1e6fff;color:#fff;border-color:#1e6fff}
.subpanel{display:none}.subpanel.active{display:block}
.tenure-team-panel{display:none}.tenure-team-panel.active{display:block}
.team-select-row{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.team-select-row label{font-family:'Oswald',sans-serif;text-transform:uppercase;letter-spacing:.5px;font-size:.78rem;color:#8a94a8}
.team-select-row select{background:#0a0d13;border:1px solid #232938;color:#f4f6fa;padding:8px 12px;border-radius:7px;font-size:.85rem}
.team-select-row select:focus{outline:none;border-color:#1e6fff}
.draft-board-wrap{display:flex;flex-direction:column;gap:14px}
.legend{display:flex;flex-wrap:wrap;gap:16px;font-size:.78rem;color:#8a94a8;font-family:'Oswald',sans-serif;letter-spacing:.3px}
.legend .dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}
.draft-grid{overflow-x:auto}
.draft-grid table.draft-board{font-size:.78rem;border-collapse:separate;border-spacing:6px}
.draft-grid .draft-board th{font-family:'Oswald',sans-serif;text-transform:uppercase;letter-spacing:.6px;white-space:nowrap;padding:4px 8px}
.draft-grid .draft-board th.round-label,.draft-grid .draft-board td.round-label{text-align:center;color:#8a94a8;font-family:'JetBrains Mono',monospace;font-weight:700;width:30px}
.draft-grid .draft-board td{padding:0;vertical-align:top;min-width:130px}
.draft-cell{background:#181d29;border:1px solid #232938;border-radius:6px;padding:8px 10px;min-height:64px}
.draft-cell .player{font-weight:600;font-size:.82rem;color:#f4f6fa;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:150px}
.draft-cell .drafted-by{font-size:.7rem;color:#8a94a8;margin-top:2px}
.draft-cell .rank-move{font-family:'JetBrains Mono',monospace;font-size:.68rem;color:#c2c8d8;margin-top:5px}
.draft-cell .bid{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:.72rem;color:#ffd23f;margin-top:4px}

/* ---------- Trophy Case ---------- */
.trophy-wall{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
.trophy-card{background:#181d29;border:1px solid #232938;border-radius:10px;padding:18px 16px;text-align:center}
.trophy-year{color:#ffd23f;font-weight:800;font-size:.82rem;letter-spacing:.5px;margin-bottom:6px}
.trophy-icon{font-size:1.6rem;margin-bottom:8px}
.trophy-card .team-name-main{font-size:.88rem}
.trophy-record{color:#8a94a8;font-size:.78rem;margin:6px 0 4px 0}
.trophy-score{font-weight:700;font-size:.92rem;color:#1e6fff;margin-top:6px}
.trophy-vs{color:#8a94a8;font-size:.72rem;margin-top:4px}

/* ---------- League Records Book ---------- */
.record-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
.record-card{background:#181d29;border:1px solid #232938;border-radius:10px;padding:16px}
.record-label{color:#8a94a8;font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.record-value{font-size:1.4rem;font-weight:800;color:#f4f6fa;margin-bottom:8px}
.record-unit{font-size:.75rem;font-weight:600;color:#8a94a8}
.record-card .team-name-main{font-size:.88rem}
.record-context{color:#8a94a8;font-size:.72rem;margin-top:6px;line-height:1.4}

/* ---------- Playoff Picture ---------- */
.playoff-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.playoff-col-title{font-family:'Oswald',sans-serif;font-size:.82rem;color:#8a94a8;text-transform:uppercase;letter-spacing:1px;font-weight:600;margin:0 0 10px 0}
.playoff-row{display:grid;grid-template-columns:24px 1fr auto auto auto;align-items:center;gap:10px;background:#181d29;border:1px solid #232938;border-radius:8px;padding:10px 12px;margin-bottom:8px}
.playoff-seed{color:#8a94a8;font-weight:700;font-size:.82rem}
.playoff-record{font-size:.82rem;font-weight:600;white-space:nowrap}
.playoff-detail{color:#8a94a8;font-size:.72rem;white-space:nowrap}
.playoff-badge{font-size:.7rem;font-weight:700;padding:4px 9px;border-radius:20px;white-space:nowrap}
.badge-clinched{background:rgba(74,222,128,.2);color:#4ade80}
.badge-hunt{background:rgba(30,111,255,.2);color:#1e6fff}
.badge-bubble{background:rgba(255,210,63,.2);color:#ffd23f}
.badge-eliminated{background:rgba(248,113,113,.2);color:#f87171}
@media (max-width:640px){
  .trophy-wall,.record-grid{grid-template-columns:1fr}
  .playoff-grid{grid-template-columns:1fr}
  .playoff-row{grid-template-columns:20px 1fr;grid-template-areas:"seed team" "record record" "detail detail" "badge badge";row-gap:4px}
  .playoff-seed{grid-area:seed}
  .playoff-row .team-cell{grid-area:team}
  .playoff-record{grid-area:record}
  .playoff-detail{grid-area:detail}
  .playoff-badge{grid-area:badge;justify-self:start}
}

/* ---------- Head-to-Head lookup (Matchups tab) ---------- */
.h2h-picker{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.h2h-picker select{background:#181d29;border:1px solid #232938;color:#f4f6fa;padding:8px 12px;border-radius:8px;font-size:.9rem;flex:1}

/* ---------- Weekly Parlay: shared entry-form styling ---------- */
.parlay-entry{background:#181d29;border:1px solid #232938;border-radius:12px;padding:18px;margin-top:10px}
.parlay-entry-row{display:grid;grid-template-columns:1fr 2fr auto auto;gap:8px;margin-bottom:8px;align-items:center}
.parlay-entry-header{grid-template-columns:1fr 1fr auto}
.parlay-entry-row input,.parlay-entry-row select{background:#0a0d13;border:1px solid #232938;color:#f4f6fa;padding:8px 11px;border-radius:7px;font-size:.85rem;width:100%;transition:border-color .15s ease}
.parlay-entry-row input:focus,.parlay-entry-row select:focus{outline:none;border-color:#1e6fff}
.parlay-entry-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.btn-primary{background:linear-gradient(135deg,#1e6fff,#ff5b1f);color:#fff;border:none;padding:10px 20px;border-radius:8px;cursor:pointer;font-weight:700;font-size:.85rem;box-shadow:0 2px 8px rgba(30,111,255,0.25);transition:transform .12s ease,box-shadow .12s ease}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 4px 14px rgba(30,111,255,0.35)}
.btn-secondary{background:#0a0d13;color:#cfd5e6;border:1px solid #232938;padding:9px 16px;border-radius:8px;cursor:pointer;font-size:.85rem;transition:border-color .15s ease}
.btn-secondary:hover{border-color:#1e6fff}
.btn-remove{background:transparent;color:#8a94a8;border:1px solid #232938;width:32px;height:32px;border-radius:7px;cursor:pointer;font-size:.9rem;line-height:1}
.btn-remove:hover{color:#f87171;border-color:#f87171}

/* Numbered leg-builder rows with a fill-progress bar */
.parlay-builder-row{display:grid;grid-template-columns:26px 1fr 1fr auto auto;align-items:center;gap:10px;padding:6px 0}
.parlay-builder-num{width:24px;height:24px;border-radius:50%;background:#0a0d13;border:1px solid #232938;color:#8a94a8;display:flex;align-items:center;justify-content:center;font-size:.68rem;font-weight:800;flex-shrink:0}
.parlay-progress-wrap{display:flex;align-items:center;gap:10px;margin:4px 0 14px}
.parlay-progress{flex:1;height:6px;border-radius:4px;background:#0a0d13;overflow:hidden}
.parlay-progress-fill{height:100%;background:linear-gradient(90deg,#1e6fff,#ff5b1f);transition:width .25s ease;border-radius:4px}
.parlay-progress-label{font-size:.72rem;color:#8a94a8;white-space:nowrap;font-weight:600}

/* Status messages as colored alert boxes instead of plain text */
.parlay-alert{padding:10px 14px;border-radius:8px;font-size:.83rem;margin-top:10px;border:1px solid transparent;font-weight:600}
.parlay-alert.ok{background:rgba(74,222,128,.1);border-color:rgba(74,222,128,.35);color:#4ade80}
.parlay-alert.err{background:rgba(248,113,113,.1);border-color:rgba(248,113,113,.35);color:#f87171}
.parlay-alert.info{background:rgba(30,111,255,.1);border-color:rgba(30,111,255,.35);color:#8ab4f8}

/* Season-record stat band at the top of the tab */
.parlay-summary-hero{display:flex;align-items:center;justify-content:space-around;flex-wrap:wrap;gap:18px;position:relative;overflow:hidden;background:linear-gradient(135deg,#141b30 0%,#0a0d13 100%);border:1px solid #232938;border-radius:14px;padding:20px 24px;margin-bottom:20px}
.parlay-summary-hero::before{content:'';position:absolute;inset:-40%;background:radial-gradient(circle at 15% 20%,rgba(255,91,31,.16),transparent 45%),radial-gradient(circle at 85% 80%,rgba(30,111,255,.16),transparent 45%);pointer-events:none}
.parlay-summary-stat{position:relative;z-index:1;text-align:center;min-width:90px}
.parlay-summary-stat .psnum{font-size:1.9rem;font-weight:400;font-family:'Archivo Black',sans-serif;line-height:1.1}
.parlay-summary-stat .pslbl{font-size:.68rem;color:#8a94a8;text-transform:uppercase;letter-spacing:.6px;margin-top:3px}

/* Leaderboard: ranked rows with medal colors + hit-rate bars */
.parlay-leaderboard{display:flex;flex-direction:column;gap:8px}
.parlay-lb-row{display:grid;grid-template-columns:32px 1fr auto;align-items:center;gap:12px;background:#12161f;border:1px solid #232938;border-radius:10px;padding:10px 14px}
.parlay-lb-rank{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:.78rem;background:#0a0d13;color:#8a94a8;flex-shrink:0}
.parlay-lb-rank.pr1{background:linear-gradient(135deg,#f5d76e,#c9a227);color:#241b00}
.parlay-lb-rank.pr2{background:linear-gradient(135deg,#dfe1e8,#a9adba);color:#1a1a1f}
.parlay-lb-rank.pr3{background:linear-gradient(135deg,#e0a06a,#a8622f);color:#241300}
.parlay-lb-name{font-weight:700;font-size:.9rem}
.parlay-lb-bar-track{height:5px;border-radius:4px;background:#0a0d13;overflow:hidden;margin-top:6px}
.parlay-lb-bar-fill{height:100%;border-radius:4px;background:linear-gradient(90deg,#1e6fff,#ff5b1f)}
.parlay-lb-right{text-align:right;flex-shrink:0}
.parlay-lb-rate{font-weight:400;font-size:1.05rem;font-family:'Archivo Black',sans-serif}
.parlay-lb-record{color:#8a94a8;font-size:.72rem;white-space:nowrap}

/* Weekly breakdown as "parlay slip" ticket cards */
.parlay-slip{background:#12161f;border:1px solid #232938;border-radius:12px;overflow:hidden;margin-bottom:14px}
.parlay-slip-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 16px;border-bottom:1px dashed #232938}
.parlay-slip-title{font-weight:600;font-family:'Oswald',sans-serif;text-transform:uppercase;letter-spacing:.4px;font-size:.85rem}
.parlay-slip-status{font-size:.68rem;font-weight:800;letter-spacing:.5px;text-transform:uppercase;padding:4px 12px;border-radius:20px;white-space:nowrap}
.parlay-slip-status.st-hit{background:rgba(74,222,128,.18);color:#4ade80}
.parlay-slip-status.st-miss{background:rgba(248,113,113,.18);color:#f87171}
.parlay-slip-status.st-pending{background:rgba(30,111,255,.18);color:#1e6fff}
.parlay-leg{display:flex;align-items:center;gap:12px;padding:9px 16px}
.parlay-leg:not(:last-child){border-bottom:1px solid #181d29}
.parlay-leg-icon{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:.7rem;flex-shrink:0;font-weight:800}
.parlay-leg-icon.ic-hit{background:rgba(74,222,128,.18);color:#4ade80}
.parlay-leg-icon.ic-miss{background:rgba(248,113,113,.18);color:#f87171}
.parlay-leg-icon.ic-pending{background:rgba(138,148,168,.18);color:#8a94a8}
.parlay-leg-body{flex:1;min-width:0}
.parlay-leg-manager{font-size:.68rem;color:#8a94a8;text-transform:uppercase;letter-spacing:.4px}
.parlay-leg-pick{font-weight:600;font-size:.9rem}
.pq-pos{color:#8a94a8;font-weight:500;font-size:.76rem}

/* ---------- Weekly Parlay: week status band ---------- */
.parlay-week-band{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;background:linear-gradient(135deg,#141b30 0%,#0a0d13 100%);border:1px solid #232938;border-radius:12px;padding:16px 22px;margin-bottom:18px;position:relative;overflow:hidden}
.parlay-week-band::before{content:'';position:absolute;inset:-50%;background:radial-gradient(circle at 20% 30%,rgba(255,91,31,.14),transparent 45%),radial-gradient(circle at 80% 70%,rgba(30,111,255,.14),transparent 45%);pointer-events:none}
.pq-week-band-title{position:relative;z-index:1;font-family:'Oswald',sans-serif;text-transform:uppercase;letter-spacing:.5px;font-size:1.1rem;font-weight:600}
.pq-week-band-stats{position:relative;z-index:1;display:flex;gap:18px;flex-wrap:wrap}
.pq-week-band-stat{text-align:center}
.pq-week-band-stat .pwn{font-size:1.1rem;font-weight:400;font-family:'Archivo Black',sans-serif}
.pq-week-band-stat .pwl{font-size:.65rem;color:#8a94a8;text-transform:uppercase;letter-spacing:.5px}

/* ---------- Weekly Parlay: picks table + team stats ---------- */
.parlay-card{background:#12161f;border:1px solid #232938;border-radius:12px;padding:20px;margin-bottom:8px}
.parlay-week-selector{display:flex;gap:14px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.parlay-week-selector label{display:flex;align-items:center;gap:6px;font-size:.82rem;color:#8a94a8}
.parlay-week-selector input{background:#0a0d13;border:1px solid #232938;color:#f4f6fa;padding:8px 11px;border-radius:7px;width:90px;font-size:.85rem}
.parlay-week-selector input:focus{outline:none;border-color:#1e6fff}
.parlay-table-wrap{overflow-x:auto;margin-bottom:6px}
.parlay-table{width:100%;border-collapse:collapse;font-size:.88rem}
.parlay-table th{text-align:left;color:#8a94a8;padding:8px 10px;border-bottom:1px solid #232938;font-weight:600;font-size:.72rem;text-transform:uppercase;letter-spacing:.5px}
.parlay-table td{padding:7px 8px;border-bottom:1px solid #181d29;vertical-align:middle;border-left:3px solid transparent;transition:background-color .15s ease}
.parlay-table td:first-child{border-left:3px solid transparent}
.parlay-table tr.pq-row-hit td:first-child{border-left-color:#4ade80;background:rgba(74,222,128,.05)}
.parlay-table tr.pq-row-miss td:first-child{border-left-color:#f87171;background:rgba(248,113,113,.05)}
.parlay-table tr.pq-row-hit td,.parlay-table tr.pq-row-miss td{background:inherit}
.parlay-table td select{width:100%;min-width:120px;background:#0a0d13;border:1px solid #232938;color:#f4f6fa;padding:7px 9px;border-radius:7px;font-size:.85rem}
.parlay-table td select:focus{outline:none;border-color:#1e6fff}
.parlay-table td select.pq-row-result option[value="hit"]{color:#4ade80}
.parlay-table td select.pq-row-result option[value="miss"]{color:#f87171}

/* Team Stats as cards instead of a plain table */
.pq-stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
.pq-stat-card{background:#12161f;border:1px solid #232938;border-radius:12px;padding:16px 18px;position:relative;overflow:hidden}
.pq-stat-card.pq-stat-hit-streak{border-color:rgba(74,222,128,.4)}
.pq-stat-card.pq-stat-miss-streak{border-color:rgba(248,113,113,.4)}
.pq-stat-name{font-family:'Oswald',sans-serif;text-transform:uppercase;letter-spacing:.4px;font-weight:600;font-size:.92rem;margin-bottom:10px}
.pq-stat-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.pq-stat-label{font-size:.68rem;color:#8a94a8;text-transform:uppercase;letter-spacing:.5px}
.pq-stat-record{font-size:1.15rem;font-weight:400;font-family:'Archivo Black',sans-serif}
.pq-stat-fav{font-size:.82rem;color:#cfd5e6;margin-top:6px;padding-top:10px;border-top:1px solid #181d29}

@media (max-width:640px){
  .h2h-picker{flex-direction:column;align-items:stretch}
  .parlay-entry-row{grid-template-columns:1fr}
  .parlay-builder-row{grid-template-columns:22px 1fr;grid-template-areas:"num manager" ". pick" ". result" ". remove";row-gap:6px}
  .parlay-builder-num{grid-area:num}
  .parlay-summary-hero{justify-content:center;text-align:center}
  .parlay-week-band{flex-direction:column;align-items:flex-start}
}
"""

JS = """
function showTab(id,btn){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));btn.classList.add('active');document.getElementById(id).classList.add('active');}
function showSubTab(id,btn){btn.parentNode.querySelectorAll('.subtab').forEach(t=>t.classList.remove('active'));btn.parentNode.parentNode.querySelectorAll('.subpanel').forEach(p=>p.classList.remove('active'));btn.classList.add('active');document.getElementById(id).classList.add('active');}
function showTenureTeam(team){document.querySelectorAll('.tenure-team-panel').forEach(function(p){p.classList.toggle('active', p.getAttribute('data-team') === team);});}
function escHtml(s){
  return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function populatePickSelect(pickEl, manager, currentPick){
  // Fills a "pick" <select> with the chosen manager's current active roster.
  // If currentPick doesn't match anyone currently on that roster (they were
  // traded/dropped since this leg was submitted), it's still added as a
  // selected-but-labeled option so editing an old week never silently
  // blanks out what was actually picked.
  if (!pickEl) return;
  var roster = (window.MANAGER_ROSTERS && window.MANAGER_ROSTERS[manager]) || [];
  var seen = false;
  var opts = roster.map(function(p){
    if (p.name === currentPick) seen = true;
    var label = p.name + (p.pos ? ' (' + p.pos + ')' : '');
    return '<option value="' + escHtml(p.name) + '"' + (p.name === currentPick ? ' selected' : '') + '>' + escHtml(label) + '</option>';
  }).join('');
  if (currentPick && !seen) {
    opts += '<option value="' + escHtml(currentPick) + '" selected>' + escHtml(currentPick) + ' (not on current roster)</option>';
  }
  pickEl.innerHTML = '<option value="">' + (manager ? 'Select player…' : 'Select manager first…') + '</option>' + opts;
}
function updateParlayProgress(prefix){
  var wrap = document.getElementById(prefix === 'px' ? 'parlayLegRows' : 'shLegRows');
  var fill = document.getElementById(prefix + 'ProgressFill');
  var lbl = document.getElementById(prefix + 'ProgressLabel');
  if (!wrap || !fill || !lbl) return;
  var rows = wrap.querySelectorAll('.parlay-builder-row');
  var total = rows.length, filled = 0;
  rows.forEach(function(row){
    var mgr = row.querySelector('.' + prefix + '-manager');
    var pick = row.querySelector('.' + prefix + '-pick');
    if (mgr && pick && mgr.value && pick.value) filled++;
  });
  fill.style.width = (total ? Math.round((filled / total) * 100) : 0) + '%';
  lbl.textContent = filled + ' / ' + total + ' legs filled';
}
function setParlayAlert(elId, message, type){
  var el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = message ? '<div class="parlay-alert ' + (type || 'info') + '">' + escHtml(message) + '</div>' : '';
}
function updateBestParlayPickerCard(stats){
  // Fills the Superlatives tab's "Best Parlay Picker" card once live parlay
  // data loads (Firebase backend only — the local-file backend's version is
  // already baked in server-side). No-op if that card isn't on the page,
  // or there's no data yet.
  var valueEl = document.getElementById('splat-parlay-value');
  var leaderEl = document.getElementById('splat-parlay-leader');
  if (!valueEl || !leaderEl || !stats || !stats.length) return;
  var top = stats.slice().sort(function(a,b){
    if (b.hits !== a.hits) return b.hits - a.hits;
    var ra = a.hit_rate_pct == null ? -1 : a.hit_rate_pct, rb = b.hit_rate_pct == null ? -1 : b.hit_rate_pct;
    return rb - ra;
  })[0];
  valueEl.textContent = top.hits + ' hits (' + (top.hit_rate_pct == null ? '—' : top.hit_rate_pct + '%') + ')';
  leaderEl.innerHTML = '<div class="team-cell-inner"><div><div class="team-name-main">' + escHtml(top.manager) + '</div></div></div>';
}
function updateH2H(){
  var a=document.getElementById('h2hA'), b=document.getElementById('h2hB');
  if(!a||!b||!window.H2H_DATA) return;
  var idA=parseInt(a.value,10), idB=parseInt(b.value,10);
  var out=document.getElementById('h2hResult');
  if(!out) return;
  if(idA===idB){ out.innerHTML="<p class='empty'>Pick two different teams.</p>"; return; }
  var lo=Math.min(idA,idB), hi=Math.max(idA,idB);
  var d=window.H2H_DATA[lo+'-'+hi];
  if(!d){ out.innerHTML="<p class='empty'>These teams haven't played each other yet.</p>"; return; }
  var flip = (idA !== d.a_id);
  var nameA = flip ? d.b_name : d.a_name, nameB = flip ? d.a_name : d.b_name;
  var winsA = flip ? d.wins_b : d.wins_a, winsB = flip ? d.wins_a : d.wins_b;
  var ptsA = flip ? d.pts_b : d.pts_a, ptsB = flip ? d.pts_a : d.pts_b;
  var topA = flip ? d.top_scorer_b : d.top_scorer_a, topB = flip ? d.top_scorer_a : d.top_scorer_b;
  var html = "<div class='record-grid'>";
  html += "<div class='record-card'><div class='record-label'>All-Time Record</div><div class='record-value'>"+winsA+" - "+winsB+"</div><div class='record-context'>"+nameA+" vs "+nameB+" &middot; "+d.meetings+" meeting"+(d.meetings===1?"":"s")+"</div></div>";
  html += "<div class='record-card'><div class='record-label'>Total Points</div><div class='record-value'>"+ptsA.toFixed(1)+" &ndash; "+ptsB.toFixed(1)+"</div><div class='record-context'>"+nameA+" vs "+nameB+"</div></div>";
  if(d.closest){ html += "<div class='record-card'><div class='record-label'>Closest Game</div><div class='record-value'>"+d.closest.margin.toFixed(1)+" <span class='record-unit'>pt margin</span></div><div class='record-context'>Week "+d.closest.week+", "+d.closest.season+"</div></div>"; }
  if(d.blowout){ html += "<div class='record-card'><div class='record-label'>Biggest Blowout</div><div class='record-value'>"+d.blowout.margin.toFixed(1)+" <span class='record-unit'>pt margin</span></div><div class='record-context'>Week "+d.blowout.week+", "+d.blowout.season+"</div></div>"; }
  if(topA){ html += "<div class='record-card'><div class='record-label'>"+nameA+"'s Top Scorer vs "+nameB+"</div><div class='record-value'>"+topA.pts.toFixed(1)+"</div><div class='record-context'>"+topA.name+" &middot; currently rostered</div></div>"; }
  if(topB){ html += "<div class='record-card'><div class='record-label'>"+nameB+"'s Top Scorer vs "+nameA+"</div><div class='record-value'>"+topB.pts.toFixed(1)+"</div><div class='record-context'>"+topB.name+" &middot; currently rostered</div></div>"; }
  html += "</div>";
  out.innerHTML = html;
}
if (document.getElementById('h2hA')) { updateH2H(); }

/* ---------- Weekly Parlay entry form (single-browser draft + export) ---------- */
function addParlayLegRow(manager, pick, result){
  var wrap = document.getElementById('parlayLegRows');
  if(!wrap) return;
  var row = document.createElement('div');
  row.className = 'parlay-builder-row';
  var num = wrap.children.length + 1;
  var managers = Object.keys(window.MANAGER_ROSTERS || {}).sort();
  var mgrOpts = '<option value="">Select manager…</option>' + managers.map(function(m){
    return '<option value="' + escHtml(m) + '"' + (m === manager ? ' selected' : '') + '>' + escHtml(m) + '</option>';
  }).join('');
  var resultOpts = ['pending','hit','miss'].map(function(r){
    return "<option value='"+r+"'"+(r===(result||'pending')?' selected':'')+">"+r+"</option>";
  }).join('');
  row.innerHTML =
    `<div class="parlay-builder-num">${num}</div>` +
    `<select class="px-manager" onchange="onParlayRowManagerChange(this)">${mgrOpts}</select>` +
    `<select class="px-pick"></select>` +
    `<select class="px-result">${resultOpts}</select>` +
    `<button type="button" class="btn-remove" onclick="this.parentNode.remove();updateParlayProgress('px');">&times;</button>`;
  wrap.appendChild(row);
  populatePickSelect(row.querySelector('.px-pick'), manager || '', pick || '');
  row.querySelector('.px-manager').addEventListener('change', function(){ updateParlayProgress('px'); });
  row.querySelector('.px-pick').addEventListener('change', function(){ updateParlayProgress('px'); });
  updateParlayProgress('px');
}
function onParlayRowManagerChange(sel){
  var row = sel.parentNode;
  populatePickSelect(row.querySelector('.px-pick'), sel.value, '');
}
function loadParlayDraft(){
  try { return JSON.parse(localStorage.getItem('parlayDraftWeeks') || '[]'); } catch(e){ return []; }
}
function saveParlayDraft(weeks){
  try { localStorage.setItem('parlayDraftWeeks', JSON.stringify(weeks)); } catch(e){}
}
function mergedParlayWeeks(){
  var base = (window.PARLAY_DATA || []).slice();
  var draft = loadParlayDraft();
  draft.forEach(function(dw){
    var idx = -1;
    for (var i=0;i<base.length;i++){ if (base[i].season===dw.season && base[i].week===dw.week){ idx=i; break; } }
    if (idx >= 0) base[idx] = dw; else base.push(dw);
  });
  return base;
}
function loadParlayWeek(){
  var season = parseInt(document.getElementById('pxSeason').value, 10);
  var week = parseInt(document.getElementById('pxWeek').value, 10);
  var all = mergedParlayWeeks();
  var found = null;
  for (var i=0;i<all.length;i++){ if (all[i].season===season && all[i].week===week){ found=all[i]; break; } }
  var wrap = document.getElementById('parlayLegRows');
  wrap.innerHTML = '';
  var legs = (found && found.legs) || [];
  var count = Math.max(legs.length, 12);
  for (var j=0;j<count;j++){
    var l = legs[j] || {};
    addParlayLegRow(l.manager, l.pick, l.result);
  }
  setParlayAlert('parlayStatus', found ? 'Loaded existing week — edit and save to update it.' : 'New week — fill in legs, then Save Draft.', 'info');
}
function saveParlayWeek(){
  var season = parseInt(document.getElementById('pxSeason').value, 10);
  var week = parseInt(document.getElementById('pxWeek').value, 10);
  if (!season || !week){ setParlayAlert('parlayStatus', 'Enter a season and week first.', 'err'); return; }
  var rows = document.querySelectorAll('#parlayLegRows .parlay-builder-row');
  var legs = [];
  rows.forEach(function(row){
    var manager = row.querySelector('.px-manager').value.trim();
    var pick = row.querySelector('.px-pick').value.trim();
    var result = row.querySelector('.px-result').value;
    if (manager || pick) legs.push({manager: manager, pick: pick, result: result});
  });
  var draft = loadParlayDraft();
  var idx = -1;
  for (var i=0;i<draft.length;i++){ if (draft[i].season===season && draft[i].week===week){ idx=i; break; } }
  var entry = {season: season, week: week, legs: legs};
  if (idx >= 0) draft[idx] = entry; else draft.push(entry);
  saveParlayDraft(draft);
  setParlayAlert('parlayStatus', 'Saved to this browser (' + legs.length + ' leg' + (legs.length === 1 ? '' : 's') + '). Download the file below to make it permanent.', 'ok');
}
function downloadParlayJSON(){
  var all = mergedParlayWeeks();
  var blob = new Blob([JSON.stringify(all, null, 2)], {type: 'application/json'});
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url; a.download = window.PARLAY_FILE_NAME || 'parlay.json';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
if (document.getElementById('parlayLegRows')) { loadParlayWeek(); }

/* ---------- Weekly Parlay (Firebase Firestore-backed live version) ---------- */
var PQ_ICONS = { hit: ['&#10003;', 'ic-hit'], miss: ['&#10007;', 'ic-miss'], pending: ['&#8226;', 'ic-pending'] };
function pqIcon(result){ return PQ_ICONS[result] || PQ_ICONS.pending; }
function pqSlug(s){ return String(s || '').toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, ''); }
function pqDb(){ return firebase.firestore(); }

async function pqFetchAllLegs(){
  var snap = await pqDb().collection('parlayLegs').get();
  var legs = [];
  snap.forEach(function(d){ legs.push(d.data()); });
  return legs;
}
async function pqSaveResults(entries){
  var batch = pqDb().batch();
  entries.forEach(function(e){
    var id = e.season + '-' + e.week + '-' + pqSlug(e.manager);
    batch.set(pqDb().collection('parlayLegs').doc(id), {
      season: e.season, week: e.week, manager: e.manager, pick: e.pick, result: e.result,
      updatedAt: firebase.firestore.FieldValue.serverTimestamp(),
    }, {merge: true});
  });
  await batch.commit();
}

function pqTeamOptionsHtml(selected){
  var managers = Object.keys(window.MANAGER_ROSTERS || {}).sort();
  return '<option value="">Select team&hellip;</option>' + managers.map(function(m){
    return '<option value="' + escHtml(m) + '"' + (m === selected ? ' selected' : '') + '>' + escHtml(m) + '</option>';
  }).join('');
}
function pqApplyRowTint(tr, resultVal){
  tr.classList.remove('pq-row-hit', 'pq-row-miss');
  if (resultVal === 'hit') tr.classList.add('pq-row-hit');
  else if (resultVal === 'miss') tr.classList.add('pq-row-miss');
}
function pqAddPicksRow(manager, pick, result){
  var tbody = document.getElementById('pqPicksBody');
  if (!tbody) return;
  var resultVal = result || 'pending';
  var tr = document.createElement('tr');
  tr.innerHTML =
    '<td><select class="pq-row-team">' + pqTeamOptionsHtml(manager || '') + '</select></td>' +
    '<td><select class="pq-row-player"></select></td>' +
    '<td><select class="pq-row-result">' +
      '<option value="pending"' + (resultVal === 'pending' ? ' selected' : '') + '>TBD</option>' +
      '<option value="hit"' + (resultVal === 'hit' ? ' selected' : '') + '>Hit</option>' +
      '<option value="miss"' + (resultVal === 'miss' ? ' selected' : '') + '>Miss</option>' +
    '</select></td>' +
    '<td><button type="button" class="btn-remove pq-row-remove">&times;</button></td>';
  tbody.appendChild(tr);
  pqApplyRowTint(tr, resultVal);
  var teamSel = tr.querySelector('.pq-row-team');
  var pickSel = tr.querySelector('.pq-row-player');
  var resultSel = tr.querySelector('.pq-row-result');
  populatePickSelect(pickSel, manager || '', pick || '');
  teamSel.addEventListener('change', function(){ populatePickSelect(pickSel, teamSel.value, ''); });
  resultSel.addEventListener('change', function(){ pqApplyRowTint(tr, resultSel.value); });
  tr.querySelector('.pq-row-remove').addEventListener('click', function(){ tr.remove(); });
}
function pqRenderWeekBand(season, week, filteredLegs){
  var titleEl = document.querySelector('#pqWeekBand .pq-week-band-title');
  var statsEl = document.getElementById('pqWeekBandStats');
  if (!titleEl || !statsEl) return;
  titleEl.textContent = 'Week ' + week + ', ' + season;
  var submitted = filteredLegs.length;
  var hits = filteredLegs.filter(function(l){ return l.result === 'hit'; }).length;
  var misses = filteredLegs.filter(function(l){ return l.result === 'miss'; }).length;
  var graded = hits + misses;
  statsEl.innerHTML =
    '<div class="pq-week-band-stat"><div class="pwn">' + submitted + '</div><div class="pwl">Submitted</div></div>' +
    '<div class="pq-week-band-stat"><div class="pwn">' + graded + ' / ' + submitted + '</div><div class="pwl">Graded</div></div>' +
    '<div class="pq-week-band-stat"><div class="pwn">' + hits + '-' + misses + '</div><div class="pwl">Hit-Miss</div></div>';
}
function pqLoadWeek(){
  var season = parseInt(document.getElementById('pqSeason').value, 10);
  var week = parseInt(document.getElementById('pqWeek').value, 10);
  var tbody = document.getElementById('pqPicksBody');
  if (!tbody) return;
  if (!season || !week){ setParlayAlert('pqSaveStatus', 'Enter a season and week first.', 'err'); return; }
  setParlayAlert('pqSaveStatus', 'Loading…', 'info');
  pqFetchAllLegs().then(function(legs){
    var filtered = legs.filter(function(l){ return Number(l.season) === season && Number(l.week) === week; });
    tbody.innerHTML = '';
    if (filtered.length){
      filtered.forEach(function(l){ pqAddPicksRow(l.manager, l.pick, l.result); });
    } else {
      // Nothing submitted yet for this week — pre-fill one row per known
      // manager so there's nothing to set up before filling it in.
      Object.keys(window.MANAGER_ROSTERS || {}).sort().forEach(function(m){ pqAddPicksRow(m, '', 'pending'); });
    }
    setParlayAlert('pqSaveStatus', '', null);
    pqRenderWeekBand(season, week, filtered);
    pqRenderTeamStats(legs);
    pqRenderHistory(legs);
  }).catch(function(err){
    setParlayAlert('pqSaveStatus', 'Could not load that week.', 'err'); console.error(err);
  });
}
function pqSavePicks(){
  var season = parseInt(document.getElementById('pqSeason').value, 10);
  var week = parseInt(document.getElementById('pqWeek').value, 10);
  if (!season || !week){ setParlayAlert('pqSaveStatus', 'Enter a season and week first.', 'err'); return; }
  var rows = document.querySelectorAll('#pqPicksBody tr');
  var entries = [];
  rows.forEach(function(tr){
    var manager = tr.querySelector('.pq-row-team').value;
    var pick = tr.querySelector('.pq-row-player').value;
    var result = tr.querySelector('.pq-row-result').value;
    if (manager && pick) entries.push({season: season, week: week, manager: manager, pick: pick, result: result});
  });
  if (!entries.length){ setParlayAlert('pqSaveStatus', 'Fill in at least one team + player before saving.', 'err'); return; }
  setParlayAlert('pqSaveStatus', 'Saving…', 'info');
  pqSaveResults(entries).then(function(){
    setParlayAlert('pqSaveStatus', 'Saved ' + entries.length + ' pick' + (entries.length === 1 ? '' : 's') + ' for Week ' + week + ', ' + season + '.', 'ok');
    pqLoadWeek(); // refresh table + team stats + history with what was just saved
  }).catch(function(err){
    setParlayAlert('pqSaveStatus', 'Could not save: ' + err.message, 'err');
  });
}

/* ---- Team Stats: record, current streak, most-picked player, this season ---- */
function pqComputeStreak(sortedDecidedLegs){
  if (!sortedDecidedLegs.length) return {type: null, length: 0};
  var lastType = sortedDecidedLegs[sortedDecidedLegs.length - 1].result;
  var len = 0;
  for (var i = sortedDecidedLegs.length - 1; i >= 0; i--){
    if (sortedDecidedLegs[i].result === lastType) len++; else break;
  }
  return {type: lastType, length: len};
}
function pqRenderTeamStats(allLegs){
  var wrap = document.getElementById('pqTeamStats');
  if (!wrap) return;
  var season = window.PQ_CURRENT_SEASON;
  var thisSeasonLegs = allLegs.filter(function(l){ return Number(l.season) === season; });
  var byManager = {};
  thisSeasonLegs.forEach(function(l){
    byManager[l.manager] = byManager[l.manager] || [];
    byManager[l.manager].push(l);
  });
  var managers = Object.keys(window.MANAGER_ROSTERS || {}).sort();
  var rows = managers.map(function(m){
    var legs = (byManager[m] || []).slice().sort(function(a,b){ return a.week - b.week; });
    var hits = legs.filter(function(l){ return l.result === 'hit'; }).length;
    var misses = legs.filter(function(l){ return l.result === 'miss'; }).length;
    var decided = legs.filter(function(l){ return l.result === 'hit' || l.result === 'miss'; });
    var streak = pqComputeStreak(decided);
    var pickCounts = {};
    legs.forEach(function(l){ pickCounts[l.pick] = (pickCounts[l.pick] || 0) + 1; });
    var favPlayer = null, favCount = 0;
    Object.keys(pickCounts).forEach(function(p){ if (pickCounts[p] > favCount){ favCount = pickCounts[p]; favPlayer = p; } });
    return {manager: m, hits: hits, misses: misses, streak: streak, favPlayer: favPlayer, favCount: favCount};
  });
  var rowsHtml = rows
    .slice()
    .sort(function(a,b){ return (b.hits - b.misses) - (a.hits - a.misses); })
    .map(function(r){
      var streakLabel = r.streak.length ? (r.streak.length + (r.streak.type === 'hit' ? 'W' : 'L')) : '—';
      var streakCls = r.streak.type === 'hit' ? 'st-hit' : (r.streak.type === 'miss' ? 'st-miss' : 'st-pending');
      var cardCls = r.streak.length >= 2 && r.streak.type === 'hit' ? ' pq-stat-hit-streak' : (r.streak.length >= 2 && r.streak.type === 'miss' ? ' pq-stat-miss-streak' : '');
      return '<div class="pq-stat-card' + cardCls + '">' +
        '<div class="pq-stat-name">' + escHtml(r.manager) + '</div>' +
        '<div class="pq-stat-row"><span class="pq-stat-label">Record</span><span class="pq-stat-record">' + r.hits + '-' + r.misses + '</span></div>' +
        '<div class="pq-stat-row"><span class="pq-stat-label">Streak</span><span class="parlay-slip-status ' + streakCls + '">' + streakLabel + '</span></div>' +
        '<div class="pq-stat-fav">' + (r.favPlayer ? '&#11088; ' + escHtml(r.favPlayer) + ' <span class="pq-pos">(' + r.favCount + 'x)</span>' : 'No picks yet') + '</div>' +
        '</div>';
    }).join('');
  wrap.innerHTML = '<div class="pq-stat-grid">' + rowsHtml + '</div>';
}

function pqLoadAndRenderHistory(){
  var statusEl = document.getElementById('pqStatus');
  if (!statusEl) return;
  pqFetchAllLegs().then(function(legs){ pqRenderHistory(legs); })
    .catch(function(err){
      statusEl.textContent = 'Could not load parlay data — check FIREBASE_CONFIG and that firestore.rules has been published.';
      console.error(err);
    });
}
function pqRenderHistory(legs){
  var statusEl = document.getElementById('pqStatus');
  var heroEl = document.getElementById('pqHero');
  var summaryEl = document.getElementById('pqSummary');
  var weeklyEl = document.getElementById('pqWeekly');
  if (!legs.length){
    statusEl.textContent = 'No picks yet — submit one above to get started.';
    heroEl.innerHTML = ''; summaryEl.innerHTML = ''; weeklyEl.innerHTML = '';
    return;
  }
  statusEl.textContent = '';

  var byWeek = {};
  legs.forEach(function(l){
    var key = l.season + '-' + l.week;
    byWeek[key] = byWeek[key] || {season: l.season, week: l.week, legs: []};
    byWeek[key].legs.push(l);
  });
  var weeksSorted = Object.keys(byWeek).map(function(k){ return byWeek[k]; })
    .sort(function(a,b){ return (b.season - a.season) || (b.week - a.week); });

  var parlaysHit = 0, parlaysDecided = 0;
  var managerStats = {};
  weeksSorted.forEach(function(w){
    var results = w.legs.map(function(l){ return l.result; });
    w.outcome = results.length && results.every(function(r){ return r === 'hit'; }) ? 'hit'
      : (results.some(function(r){ return r === 'miss'; }) ? 'miss' : 'pending');
    if (w.outcome !== 'pending'){ parlaysDecided++; if (w.outcome === 'hit') parlaysHit++; }
    w.legs.forEach(function(l){
      var st = managerStats[l.manager] = managerStats[l.manager] || {manager: l.manager, hits: 0, misses: 0};
      if (l.result === 'hit') st.hits++;
      else if (l.result === 'miss') st.misses++;
    });
  });
  var rate = parlaysDecided ? Math.round((parlaysHit / parlaysDecided) * 100) : 0;
  heroEl.innerHTML =
    '<div class="parlay-summary-hero">' +
      '<div class="parlay-summary-stat"><div class="psnum">' + parlaysHit + '-' + (parlaysDecided - parlaysHit) + '</div><div class="pslbl">Season Record</div></div>' +
      '<div class="parlay-summary-stat"><div class="psnum">' + rate + '%</div><div class="pslbl">Perfect-Week Rate</div></div>' +
      '<div class="parlay-summary-stat"><div class="psnum">' + weeksSorted.length + '</div><div class="pslbl">Weeks Tracked</div></div>' +
    '</div>';

  var statsList = Object.keys(managerStats).map(function(m){
    var s = managerStats[m];
    var decided = s.hits + s.misses;
    s.rate = decided ? Math.round((s.hits / decided) * 1000) / 10 : null;
    return s;
  }).sort(function(a,b){ var ra = a.rate == null ? -1 : a.rate, rb = b.rate == null ? -1 : b.rate; return rb - ra; });
  updateBestParlayPickerCard(statsList.map(function(s){ return {manager: s.manager, hits: s.hits, hit_rate_pct: s.rate}; }));
  var maxHits = statsList.reduce(function(m,s){ return Math.max(m, s.hits); }, 0);
  var lbRows = statsList.map(function(s, i){
    var rank = i + 1, rankCls = rank <= 3 ? ' pr' + rank : '';
    var barPct = maxHits ? Math.round((s.hits / maxHits) * 100) : 0;
    return '<div class="parlay-lb-row"><div class="parlay-lb-rank' + rankCls + '">' + rank + '</div>' +
      '<div><div class="parlay-lb-name">' + escHtml(s.manager) + '</div><div class="parlay-lb-bar-track"><div class="parlay-lb-bar-fill" style="width:' + barPct + '%"></div></div></div>' +
      '<div class="parlay-lb-right"><div class="parlay-lb-rate">' + (s.rate == null ? '—' : s.rate + '%') + '</div>' +
      '<div class="parlay-lb-record">' + s.hits + '-' + s.misses + '</div></div></div>';
  }).join('');
  summaryEl.innerHTML = '<h3 class="playoff-col-title" style="margin:18px 0 10px">Leg Leaderboard</h3><div class="parlay-leaderboard">' + lbRows + '</div>';

  weeklyEl.innerHTML = weeksSorted.map(function(w){
    var legsHtml = w.legs.map(function(l){
      var iconInfo = pqIcon(l.result);
      return '<div class="parlay-leg"><div class="parlay-leg-icon ' + iconInfo[1] + '">' + iconInfo[0] + '</div>' +
        '<div class="parlay-leg-body"><div class="parlay-leg-manager">' + escHtml(l.manager) + '</div>' +
        '<div class="parlay-leg-pick">' + escHtml(l.pick) + '</div></div></div>';
    }).join('');
    return '<div class="parlay-slip"><div class="parlay-slip-head"><div class="parlay-slip-title">Week ' + w.week + ', ' + w.season + '</div>' +
      '<div class="parlay-slip-status st-' + w.outcome + '">' + w.outcome + '</div></div><div>' + legsHtml + '</div></div>';
  }).join('');
}

if (window.FIREBASE_CONFIG) {
  firebase.initializeApp(window.FIREBASE_CONFIG);
  document.getElementById('pqLoadWeekBtn').addEventListener('click', pqLoadWeek);
  document.getElementById('pqAddRowBtn').addEventListener('click', function(){ pqAddPicksRow('', '', 'pending'); });
  document.getElementById('pqSaveBtn').addEventListener('click', pqSavePicks);
  pqLoadWeek(); // auto-loads the current season/week by default
}
"""


def standings_table(rows, with_pa=True, with_streak=True):
    head = "<tr><th>#</th><th>Team</th><th>Record</th><th>PF</th>"
    if with_pa: head += "<th>PA</th>"
    if with_streak: head += "<th>Streak</th>"
    head += "</tr>"
    body = []
    for r in rows:
        rec = f"{r['wins']}-{r['losses']}" + (f"-{r['ties']}" if r.get('ties') else "")
        cells = f"<td>{r['rank']}</td><td class='team-cell'>{team_cell(r['name'], r.get('owner'), r.get('logo'))}</td><td>{rec}</td><td>{r['pf']:.1f}</td>"
        if with_pa: cells += f"<td>{r['pa']:.1f}</td>"
        if with_streak: cells += f"<td>{esc(r.get('streak','-'))}</td>"
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>"


def compute_playoff_picture(standings, playoff_spots, reg_season_weeks):
    """
    Approximates the current playoff picture from live standings math —
    see the ESPN dashboard's compute_playoff_picture for the full
    rationale. Clinch/elimination calls are conservative: a team is only
    marked Clinched or Eliminated when the math is airtight regardless of
    who wins which remaining games. Seeding follows the same win-loss-PF
    order as the Standings table; Sleeper's own tiebreakers may differ
    slightly.
    """
    if not playoff_spots or not reg_season_weeks or not standings:
        return None
    entries = []
    for s in standings:
        games_played = s["wins"] + s["losses"] + s.get("ties", 0)
        remaining = max(0, reg_season_weeks - games_played)
        entries.append(dict(s, remaining=remaining, max_possible_wins=s["wins"] + remaining))

    in_the_hunt = entries[:playoff_spots]
    outside = entries[playoff_spots:]
    last_in = in_the_hunt[-1] if in_the_hunt else None

    for e in in_the_hunt:
        e["clinched"] = bool(outside) and all(e["wins"] > o["max_possible_wins"] for o in outside)
        e["status"] = "Clinched" if e["clinched"] else "In the hunt"
    for e in outside:
        e["eliminated"] = last_in is not None and e["max_possible_wins"] < last_in["wins"]
        e["status"] = "Eliminated" if e["eliminated"] else "On the bubble"
        e["games_back"] = round(((last_in["wins"] - e["wins"]) + (e["losses"] - last_in["losses"])) / 2, 1) if last_in else 0

    return {
        "playoff_spots": playoff_spots,
        "in_the_hunt": in_the_hunt,
        "outside": outside,
        "season_over": all(e["remaining"] == 0 for e in entries),
    }


def render_playoff_picture(picture):
    if not picture:
        return "<p class='empty'>Playoff settings unavailable for this league.</p>"

    def row(e, badge_class):
        rec = f"{e['wins']}-{e['losses']}" + (f"-{e['ties']}" if e.get('ties') else "")
        detail = "Season complete" if e["remaining"] == 0 else f"{e['remaining']} game{'s' if e['remaining'] != 1 else ''} left"
        return f"""<div class='playoff-row'><div class='playoff-seed'>{e['rank']}</div>
          <div class='team-cell'>{team_cell(e['name'], e.get('owner'), e.get('logo'))}</div>
          <div class='playoff-record'>{rec}</div><div class='playoff-detail'>{detail}</div>
          <div class='playoff-badge {badge_class}'>{e['status']}</div></div>"""

    in_rows = "".join(row(e, "badge-clinched" if e["clinched"] else "badge-hunt") for e in picture["in_the_hunt"])
    out_rows = "".join(
        row(e, "badge-eliminated" if e["eliminated"] else "badge-bubble") for e in picture["outside"]
    ) or "<p class='empty'>Every team in the league makes the playoffs.</p>"

    note = "The regular season has wrapped — this reflects the final playoff field." if picture["season_over"] else \
        "Updates automatically as more of the regular season completes."

    return f"""<p class='section-note'>Top {picture['playoff_spots']} make the playoffs. {note}</p>
    <div class='playoff-grid'>
      <div class='playoff-col'><h3 class='playoff-col-title'>In the Playoffs</h3>{in_rows}</div>
      <div class='playoff-col'><h3 class='playoff-col-title'>On the Outside</h3>{out_rows}</div>
    </div>"""


def render_standings_section(model):
    picture = compute_playoff_picture(model["standings"], model["playoff_spots"], model["reg_season_weeks"])
    sub_nav = "<button class='subtab active' onclick=\"showSubTab('std-current',this)\">Current</button>" \
              "<button class='subtab' onclick=\"showSubTab('std-playoffs',this)\">Playoff Picture</button>"
    return (f"<div class='subtabs'>{sub_nav}</div>"
            f"<div id='std-current' class='subpanel active'>{standings_table(model['standings'])}</div>"
            f"<div id='std-playoffs' class='subpanel'>{render_playoff_picture(picture)}</div>")



def render_matchups(model):
    mup = model['matchups']
    if not mup:
        cards_html = "<p class='empty'>No matchup data available for this week yet.</p>"
    else:
        cards = []
        for m in mup:
            cards.append(f"""<div class='matchup-card'><div class='matchup-teams'>
              <div class='matchup-team'><div class='team-name-main'>{esc(m['away']['name'])}</div><div class='team-record'>{esc(m['away']['record'])}</div><div class='proj-score'>{m['away']['proj']:.1f}</div></div>
              <div class='vs'>@</div>
              <div class='matchup-team'><div class='team-name-main'>{esc(m['home']['name'])}</div><div class='team-record'>{esc(m['home']['record'])}</div><div class='proj-score'>{m['home']['proj']:.1f}</div></div>
              </div><p class='outlook'>{esc(m['outlook'])}</p></div>""")
        cards_html = f"<div class='matchup-grid'>{''.join(cards)}</div>"

    teams_sorted = sorted(model['standings'], key=lambda s: s['name'])
    if len(teams_sorted) < 2:
        return cards_html
    opts_a = "".join(f"<option value='{s['roster_id']}'{' selected' if i == 0 else ''}>{esc(s['name'])}</option>" for i, s in enumerate(teams_sorted))
    opts_b = "".join(f"<option value='{s['roster_id']}'{' selected' if i == 1 else ''}>{esc(s['name'])}</option>" for i, s in enumerate(teams_sorted))
    h2h_json = json.dumps(model['h2h'])
    h2h_tool = f"""<h2 class="section-title" style="margin-top:28px">Head-to-Head Lookup</h2>
    <p class="section-note">Pick two teams to see their all-time series &mdash; record, points, closest game, biggest blowout, and each side's currently-rostered top scorer against the other.</p>
    <div class="h2h-picker">
      <select id="h2hA" onchange="updateH2H()">{opts_a}</select>
      <span class="vs">vs</span>
      <select id="h2hB" onchange="updateH2H()">{opts_b}</select>
    </div>
    <div id="h2hResult" class="h2h-result"></div>
    <script>window.H2H_DATA = {h2h_json};</script>"""
    return cards_html + h2h_tool


def render_power(power):
    if not power: return "<p class='empty'>No completed weeks yet — power rankings need game data.</p>"
    rows = []
    for p in power:
        d = p['delta']
        dcls = "luck-good" if d > 0 else ("luck-bad" if d < 0 else "")
        rows.append(f"<tr><td>{p['rank']}</td><td class='team-cell'>{team_cell(p['name'], p.get('owner'))}</td><td>{p['score']}</td><td>{p['avg_pts']:.1f}</td><td>{p['avg_margin']:+.1f}</td><td class='{dcls}'>{d:+d}</td></tr>")
    return f"<table><thead><tr><th>#</th><th>Team</th><th>Score</th><th>Avg PF</th><th>Avg Margin</th><th>vs Standings</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"


def render_power_section(model):
    sub_nav = ("<button class='subtab active' onclick=\"showSubTab('pw-power',this)\">Power Rankings</button>"
               "<button class='subtab' onclick=\"showSubTab('pw-luck',this)\">Luck Index</button>")
    return (f"<div class='subtabs'>{sub_nav}</div>"
            f"<div id='pw-power' class='subpanel active'>{render_power(model['power'])}</div>"
            f"<div id='pw-luck' class='subpanel'>{render_luck(model['luck'])}</div>")


def render_luck(luck):
    if not luck: return "<p class='empty'>No completed weeks yet — luck index needs game data.</p>"
    rows = []
    for l in luck:
        cls = "luck-good" if l['luck'] > 0 else ("luck-bad" if l['luck'] < 0 else "")
        rows.append(f"<tr><td class='team-cell'>{team_cell(l['name'], l.get('owner'))}</td><td>{l['actual']}%</td><td>{l['expected']}%</td><td class='{cls}'>{l['luck']:+.1f}%</td><td>{esc(l['label'])}</td></tr>")
    return f"<table><thead><tr><th>Team</th><th>Actual Win%</th><th>Expected (All-Play)</th><th>Luck</th><th>Verdict</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"


def render_activity(act):
    if not act: return "<p class='empty'>No recent activity found.</p>"
    rows = [f"<tr><td>{esc(a['date'])}</td><td>{esc(a['team'])}</td><td class='action'>{esc(a['action'])}</td><td>{esc(a['player'])}</td></tr>" for a in act]
    return f"<table><thead><tr><th>Date</th><th>Team</th><th>Action</th><th>Player</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"


def render_draft(draft):
    if not draft: return "<p class='empty'>No draft data available yet.</p>"
    rounds = draft['rounds']; order = draft['order']; grid = draft['grid']
    rank_data = draft.get('rank_data') or {}
    is_auction = draft.get('is_auction', False)
    slots = list(order.keys())  # already in the correct column order; don't re-sort (keys are roster_ids, not seat numbers)

    def render_cell(p):
        info = rank_data.get(p.get("player_id"))
        bg, border = delta_color(info["delta"] if info else None)
        cost_line = ""
        if is_auction and info and info.get("cost") is not None:
            cost_line = f"<div class='bid'>${info['cost']:.0f}</div>"
        if info and info["delta"] is not None:
            pos = info["position"]
            arrow = "&#9650;" if info["delta"] > 0 else ("&#9660;" if info["delta"] < 0 else "&#8211;")
            rank_line = f"<div class='rank-move'>{esc(pos)}{info['draft_pos_rank']} &rarr; {esc(pos)}{info['current_pos_rank']} {arrow}</div>"
        elif info:
            rank_line = f"<div class='rank-move'>{esc(info['position'])}{info['draft_pos_rank']} &rarr; n/a</div>"
        else:
            rank_line = "<div class='rank-move'>&nbsp;</div>"
        return f"""<div class="draft-cell" style="background:{bg};border-color:{border};">
          <div class="player" title="{esc(p['player'])}">{esc(p['player'])}</div>
          <div class="drafted-by">{esc(p.get('pos',''))} &middot; {esc(p.get('team',''))}</div>
          {rank_line}
          {cost_line}
        </div>"""

    head = "<tr><th class='round-label'>#</th>" + "".join(f"<th>{esc(order[s])}</th>" for s in slots) + "</tr>"
    body = []
    for rnd in range(1, rounds + 1):
        cells = f"<td class='round-label'>{rnd}</td>"
        for s in slots:
            p = grid.get((rnd, s))
            cells += f"<td>{render_cell(p) if p else ''}</td>"
        body.append(f"<tr>{cells}</tr>")

    if is_auction:
        note = "<p class='section-note'>Auction draft — the heat map ranks each position by $ spent (not nomination order, which carries no value signal on its own) against current fantasy points scored. Green = outproducing their price tag; red = underproducing it.</p>"
        legend = f"""
        {note}
        <div class="legend">
          <span><i class="dot" style="background:rgba(34,139,34,0.7)"></i> Outperforming $ spent (4+ spots)</span>
          <span><i class="dot" style="background:#181d29"></i> Within 3 spots of $ rank / no data</span>
          <span><i class="dot" style="background:rgba(178,34,34,0.7)"></i> Underperforming $ spent (4+ spots)</span>
        </div>"""
    else:
        legend = """
        <div class="legend">
          <span><i class="dot" style="background:rgba(34,139,34,0.7)"></i> Outperforming draft slot (4+ spots)</span>
          <span><i class="dot" style="background:#181d29"></i> Within 3 spots of draft slot / no data</span>
          <span><i class="dot" style="background:rgba(178,34,34,0.7)"></i> Underperforming draft slot (4+ spots)</span>
        </div>"""

    table = f"<table class='draft-board'><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>"
    return f"<div class='draft-board-wrap'>{legend}<div class='draft-grid'>{table}</div></div>"


def render_trade_history(trade_log):
    if not trade_log:
        return "<p class='empty'>No trades found in the tracked history window.</p>"
    cards = []
    for t in trade_log:
        sides = "".join(
            f"<div class='matchup-team'><div class='team-name-main'>{esc(side['name'])}</div>"
            f"<div class='owner-name'>Gets: {esc(', '.join(side['gets']) or '—')}</div></div>"
            for side in t["teams"]
        )
        cards.append(f"<div class='rivalry-card'><div class='rivalry-meetings'>{esc(t['date'])} &middot; Week {t['week']}, {t['season']}</div><div class='matchup-teams'>{sides}</div></div>")
    return f"<div class='rivalry-grid'>{''.join(cards)}</div>"


def render_roster_ages(rows):
    if not rows:
        return "<p class='empty'>No roster age data available (players missing birth dates).</p>"
    body = "".join(
        f"<tr><td class='team-cell'>{team_cell(r['name'], r.get('owner'), r.get('logo'))}</td>"
        f"<td>{r['avg_age']}</td><td>{esc(r['oldest_name'])} ({r['oldest_age']})</td>"
        f"<td>{esc(r['youngest_name'])} ({r['youngest_age']})</td><td>{r['counted']}</td></tr>"
        for r in rows
    )
    return (f"<p class='section-note'>Average age across each team's full rostered player pool (bench/IR included). Sorted youngest to oldest roster.</p>"
            f"<table><thead><tr><th>Team</th><th>Avg Age</th><th>Oldest</th><th>Youngest</th><th>Players Counted</th></tr></thead><tbody>{body}</tbody></table>")


def render_player_tenure(rows):
    if not rows:
        return "<p class='empty'>No tenure data available yet.</p>"

    by_team = {}
    for r in rows:
        by_team.setdefault(r["team"], []).append(r)
    team_names = sorted(by_team.keys())
    options = "".join(f"<option value='{esc(t)}'>{esc(t)}</option>" for t in team_names)

    note = ("* Tenure is a lower bound — this player was already on the roster at the start of "
            "the tracked history window, so the true acquisition date may be earlier."
            if any(r["approx"] for r in rows) else "")

    panels = []
    for i, t in enumerate(team_names):
        body = "".join(
            f"<tr><td>{esc(r['player'])}</td><td>{esc(r['pos'])}</td>"
            f"<td>{r['age'] if r['age'] is not None else '-'}</td>"
            f"<td>{esc(r['acquired_label'])}{'*' if r['approx'] else ''}</td>"
            f"<td>{r['seasons']}</td></tr>"
            for r in by_team[t]
        )
        panels.append(
            f"<div class='tenure-team-panel{' active' if i == 0 else ''}' data-team=\"{esc(t)}\">"
            f"<table><thead><tr><th>Player</th><th>Pos</th><th>Age</th><th>Acquired</th><th>Seasons</th></tr></thead>"
            f"<tbody>{body}</tbody></table></div>"
        )

    return (f"<p class='section-note'>How long each currently-rostered player has been with their team.</p>"
            f"<div class='team-select-row'><label for='tenureTeamSelect'>Team</label>"
            f"<select id='tenureTeamSelect' onchange=\"showTenureTeam(this.value)\">{options}</select></div>"
            f"{''.join(panels)}"
            + (f"<p class='section-note' style='margin-top:10px'>{note}</p>" if note else ""))


def render_draft_capital(board):
    if not board:
        return "<p class='empty'>No draft pick data available.</p>"
    sections = []
    for season_block in board:
        rows = "".join(
            f"<tr><td>{r['round']}</td><td>{esc(r['original_name'])}</td>"
            f"<td>{esc(r['owner_name'])}"
            + (f" <span class='trophy-vs'>(via {esc(r['via_name'])})</span>" if r['traded'] and r.get('via_name') else "")
            + "</td></tr>"
            for r in season_block["rows"]
        )
        sections.append(
            f"<h3 class='playoff-col-title' style='margin:18px 0 10px'>{season_block['season']} Draft</h3>"
            f"<table><thead><tr><th>Round</th><th>Original Owner</th><th>Current Owner</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    return "<p class='section-note'>Who currently holds each future rookie-draft pick, including trade attribution.</p>" + "".join(sections)


def render_front_office(model):
    sub_nav = ("<button class='subtab active' onclick=\"showSubTab('fo-ages',this)\">Roster Ages</button>"
               "<button class='subtab' onclick=\"showSubTab('fo-tenure',this)\">Player Tenure</button>"
               "<button class='subtab' onclick=\"showSubTab('fo-capital',this)\">Draft Capital</button>"
               "<button class='subtab' onclick=\"showSubTab('fo-activity',this)\">Recent Activity</button>"
               "<button class='subtab' onclick=\"showSubTab('fo-trades',this)\">Trade History</button>")
    return (f"<div class='subtabs'>{sub_nav}</div>"
            f"<div id='fo-ages' class='subpanel active'>{render_roster_ages(model['roster_ages'])}</div>"
            f"<div id='fo-tenure' class='subpanel'>{render_player_tenure(model['player_tenure'])}</div>"
            f"<div id='fo-capital' class='subpanel'>{render_draft_capital(model['draft_capital'])}</div>"
            f"<div id='fo-activity' class='subpanel'>{render_activity(model['activity'])}</div>"
            f"<div id='fo-trades' class='subpanel'>{render_trade_history(model['trade_log'])}</div>")


def render_superlatives(stat_superlatives, voted_names):
    stat_cards = "".join(
        f"<div class='record-card'>"
        f"<div class='record-label'>{esc(s['name'])}</div>"
        f"<div class='record-value' id='{s['id']}-value'>{esc(s.get('value', ''))}</div>"
        f"<div id='{s['id']}-leader'>{team_cell(s.get('leader', 'TBD'), s.get('owner'))}</div>"
        f"<div class='record-context'>{esc(s.get('description', ''))}</div></div>"
        for s in stat_superlatives
    )
    voted_cards = "".join(
        f"<div class='record-card'>"
        f"<div class='record-label'>{esc(name)}</div>"
        f"<div class='record-value'>&mdash;</div>"
        f"{team_cell('Vote pending')}"
        f"<div class='record-context'>Decided by league vote at season's end.</div></div>"
        for name in voted_names
    )
    return (f"<h2 class='section-title'>Superlatives</h2>"
            f"<p class='section-note'>Tracked automatically throughout the season.</p>"
            f"<div class='record-grid'>{stat_cards}</div>"
            f"<h3 class='playoff-col-title' style='margin:24px 0 10px'>End-of-Season (Voted)</h3>"
            f"<p class='section-note'>Empty until the league votes at season's end.</p>"
            f"<div class='record-grid'>{voted_cards}</div>")


def render_parlay(weeks, model):
    """
    Renders the Weekly Parlay tab, picking a backend in priority order:
      1. Firebase Firestore (FIREBASE_CONFIG set to valid JSON with at
         least a projectId) — live, multi-device, no login. See
         FIREBASE_SETUP.md and firestore.rules.
      2. Local JSON file + localStorage — used both when FIREBASE_CONFIG
         isn't set at all, AND when it's set but can't be parsed/is
         missing required fields (with a visible warning banner in that
         second case, so a misconfigured value fails loudly instead of
         silently reverting with no explanation).

    Either way, an HTML comment right before the tab's content records
    which backend was actually selected and why — view the page's source
    (Ctrl+U / Cmd+Option+U in most browsers) and search for "Weekly Parlay
    backend" to check this in seconds without inspecting environment
    variables directly.
    """
    if not FIREBASE_CONFIG:
        return "<!-- Weekly Parlay backend: LOCAL (FIREBASE_CONFIG is not set) -->\n" + render_parlay_local(weeks, model)

    try:
        parsed = json.loads(FIREBASE_CONFIG)
        if not isinstance(parsed, dict) or not parsed.get("projectId"):
            raise ValueError("parsed JSON is missing a \"projectId\" field")
    except Exception as e:
        warning = (
            "<div class='parlay-alert err' style='margin-bottom:16px'>"
            f"FIREBASE_CONFIG is set but could not be used ({esc(str(e))}). Falling back to "
            "local-file mode until this is fixed &mdash; check that the environment variable "
            "holds the exact JSON object from the Firebase console, with no missing quotes or "
            "shell-escaping issues (wrap it in single quotes if setting it via a shell export). "
            "See FIREBASE_SETUP.md."
            "</div>"
        )
        return (
            f"<!-- Weekly Parlay backend: LOCAL (FIREBASE_CONFIG is set but invalid: {esc(str(e))}) -->\n"
            + warning + render_parlay_local(weeks, model)
        )

    return f"<!-- Weekly Parlay backend: FIREBASE (projectId={esc(parsed.get('projectId', ''))}) -->\n" + render_parlay_firebase(model)


def render_parlay_firebase(model):
    """
    Firebase-backed Weekly Parlay tab.

    Default view is a single editable table for the CURRENT season/week —
    Team, Player, Result columns — pre-populated with one row per known
    manager so there's nothing to set up before filling it in. Changing the
    season/week fields and hitting Load switches to editing a different
    week; Save Picks batch-writes every filled-in row at once, whether
    that's the first pass (picks only, results TBD) or a later pass after
    results come in (same rows, just with Result changed from TBD).

    Below that: a season-long Team Stats table (record, current streak,
    most-picked player), then the all-time History (leaderboard + every
    past week as a "parlay slip" card).

    Firebase's client SDK talks to Firestore directly from the browser, so
    nothing here requires touching this repo again to keep tracking weekly
    picks — see FIREBASE_SETUP.md for the one-time setup.
    """
    manager_rosters_json = json.dumps(model.get('manager_rosters', {}))
    firebase_config_json = json.dumps(json.loads(FIREBASE_CONFIG)) if FIREBASE_CONFIG else "null"
    return f"""<h2 class="section-title">Weekly Parlay</h2>
    <div id="pqStatus" class="section-note">Loading&hellip;</div>

    <div id="pqWeekBand" class="parlay-week-band">
      <div class="pq-week-band-title">Week {model['current_week']}, {model['season']}</div>
      <div class="pq-week-band-stats" id="pqWeekBandStats"></div>
    </div>

    <div class="parlay-card">
      <div class="parlay-week-selector">
        <label>Season <input type="number" id="pqSeason" value="{model['season']}"></label>
        <label>Week <input type="number" id="pqWeek" value="{model['current_week']}"></label>
        <button type="button" class="btn-secondary" id="pqLoadWeekBtn">Load</button>
      </div>

      <div class="parlay-table-wrap">
        <table class="parlay-table">
          <thead><tr><th>Team</th><th>Player</th><th>Result</th><th></th></tr></thead>
          <tbody id="pqPicksBody"></tbody>
        </table>
      </div>
      <div class="parlay-entry-actions">
        <button type="button" class="btn-secondary" id="pqAddRowBtn">+ Add Row</button>
        <button type="button" class="btn-primary" id="pqSaveBtn">Save Picks</button>
      </div>
      <div id="pqSaveStatus"></div>
    </div>

    <h3 class="playoff-col-title" style="margin:28px 0 10px">&#128202; Team Stats &middot; {model['season']} Season</h3>
    <div id="pqTeamStats"></div>

    <h3 class="playoff-col-title" style="margin:28px 0 10px">&#127942; History</h3>
    <div id="pqHero"></div>
    <div id="pqSummary"></div>
    <div id="pqWeekly"></div>

    <script>
      window.FIREBASE_CONFIG = {firebase_config_json};
      window.MANAGER_ROSTERS = {manager_rosters_json};
      window.PQ_CURRENT_SEASON = {model['season']};
    </script>"""


def _parlay_result_icon(result):
    return {"hit": ("&#10003;", "ic-hit"), "miss": ("&#10007;", "ic-miss")}.get(result, ("&#8226;", "ic-pending"))


def render_parlay_local(weeks, model):
    """
    Renders the Weekly Parlay tab, including an in-browser entry form.

    That form is a single-user workflow, not a shared multi-manager one:
    it saves drafts to THIS browser's localStorage as you fill them in,
    and a "Download updated parlay.json" button exports the merged result
    (file data + your drafts) for you to commit — at which point it becomes
    the real source of truth and shows up for everyone on the next
    regenerate. It intentionally doesn't try to sync across devices or
    submit anywhere on its own, since a static HTML page has nowhere to
    send that data to. Fine for a commissioner entering all 12 legs
    themselves; not a fit if you want each manager submitting their own.
    """
    summary = compute_parlay_summary(weeks)
    header = "<h2 class='section-title'>Weekly Parlay</h2>"
    if summary:
        decided = summary["parlays_decided"]
        rate = (summary["parlays_hit"] / decided * 100) if decided else 0
        header += f"""<div class="parlay-summary-hero">
          <div class="parlay-summary-stat"><div class="psnum">{summary['parlays_hit']}-{decided - summary['parlays_hit']}</div><div class="pslbl">Season Record</div></div>
          <div class="parlay-summary-stat"><div class="psnum">{rate:.0f}%</div><div class="pslbl">Perfect-Week Rate</div></div>
          <div class="parlay-summary-stat"><div class="psnum">{len(summary['weekly'])}</div><div class="pslbl">Weeks Tracked</div></div>
        </div>"""

        max_hits = max((r["hits"] for r in summary["leaderboard"]), default=0)
        lb_rows = []
        for i, r in enumerate(summary["leaderboard"], 1):
            rank_cls = f" pr{i}" if i <= 3 else ""
            bar_pct = round((r["hits"] / max_hits * 100), 1) if max_hits else 0
            lb_rows.append(f"""<div class="parlay-lb-row">
              <div class="parlay-lb-rank{rank_cls}">{i}</div>
              <div><div class="parlay-lb-name">{esc(r['manager'])}</div>
                <div class="parlay-lb-bar-track"><div class="parlay-lb-bar-fill" style="width:{bar_pct}%"></div></div></div>
              <div class="parlay-lb-right"><div class="parlay-lb-rate">{r['rate']}%</div><div class="parlay-lb-record">{r['hits']}-{r['misses']}</div></div>
            </div>""")
        lb_html = (f"<h3 class='playoff-col-title' style='margin:18px 0 10px'>Leg Leaderboard</h3>"
                   f"<div class='parlay-leaderboard'>{''.join(lb_rows)}</div>")

        weeks_html = []
        for wk in reversed(summary["weekly"]):
            legs_html = "".join(
                (lambda icon, cls: f"""<div class="parlay-leg">
                  <div class="parlay-leg-icon {cls}">{icon}</div>
                  <div class="parlay-leg-body"><div class="parlay-leg-manager">{esc(l.get('manager',''))}</div>
                  <div class="parlay-leg-pick">{esc(l.get('pick',''))}</div></div></div>""")(*_parlay_result_icon(l.get("result")))
                for l in wk["legs"]
            )
            weeks_html.append(f"""<div class="parlay-slip">
              <div class="parlay-slip-head"><div class="parlay-slip-title">Week {wk['week']}, {wk['season']}</div>
              <div class="parlay-slip-status st-{wk['outcome']}">{esc(wk['outcome'].title())}</div></div>
              <div>{legs_html}</div></div>""")
        body_html = lb_html + "<h3 class='playoff-col-title' style='margin:18px 0 10px'>Weekly Breakdown</h3>" + "".join(weeks_html)
    else:
        header += (f"<p class='section-note'>No parlay data yet — use the entry form below to add this week's legs, "
                   f"or hand-edit <code>{esc(PARLAY_FILE)}</code> directly.</p>")
        body_html = ""

    manager_rosters_json = json.dumps(model.get('manager_rosters', {}))
    parlay_json = json.dumps(weeks)

    entry_form = f"""
    <h3 class="playoff-col-title" style="margin:24px 0 10px">Add / Edit a Week (commissioner entry)</h3>
    <p class="section-note">Fills a week's legs and saves a draft in this browser as you go. Nothing here syncs to
    other managers or updates the live page on its own &mdash; use "Download updated {esc(PARLAY_FILE)}" when a week
    is final, then commit that file so the next regenerate picks it up for everyone.</p>
    <div class="parlay-entry">
      <div class="parlay-entry-row parlay-entry-header">
        <input type="number" id="pxSeason" placeholder="Season" value="{model['season']}">
        <input type="number" id="pxWeek" placeholder="Week" value="{model['current_week']}">
        <button type="button" class="btn-secondary" onclick="loadParlayWeek()">Load Week</button>
      </div>
      <div class="parlay-progress-wrap">
        <div class="parlay-progress"><div class="parlay-progress-fill" id="pxProgressFill" style="width:0%"></div></div>
        <div class="parlay-progress-label" id="pxProgressLabel">0 / 12 legs filled</div>
      </div>
      <div id="parlayLegRows"></div>
      <div class="parlay-entry-actions">
        <button type="button" class="btn-secondary" onclick="addParlayLegRow()">+ Add Leg</button>
        <button type="button" class="btn-secondary" onclick="saveParlayWeek()">Save Draft (this browser)</button>
        <button type="button" class="btn-primary" onclick="downloadParlayJSON()">Download Updated {esc(PARLAY_FILE)}</button>
      </div>
      <div id="parlayStatus"></div>
    </div>
    <script>
      window.MANAGER_ROSTERS = {manager_rosters_json};
      window.PARLAY_DATA = {parlay_json};
      window.PARLAY_FILE_NAME = {json.dumps(PARLAY_FILE)};
    </script>"""

    return header + body_html + entry_form


def _streak_span_label(entry):
    if entry["start_year"] == entry["end_year"]:
        if entry["start_week"] == entry["end_week"]:
            return f"Week {entry['start_week']}, {entry['start_year']}"
        return f"Weeks {entry['start_week']}\u2013{entry['end_week']}, {entry['start_year']}"
    return f"{entry['start_year']} Wk{entry['start_week']} \u2013 {entry['end_year']} Wk{entry['end_week']}"


def render_trophy_case(champions):
    if not champions:
        return "<h2 class='section-title'>Hall of Fame &middot; Trophy Case</h2><p class='section-note'>No completed prior seasons found yet.</p>"
    cards = []
    for c in champions:
        score = c.get("score")
        matchup_line = ""
        if c.get("runnerup_name"):
            if score:
                cs, rs = score
                matchup_line = f"<div class='trophy-score'>{cs:.1f} &ndash; {rs:.1f} <span class='trophy-vs'>vs {esc(c['runnerup_name'])}</span></div>"
            else:
                matchup_line = f"<div class='trophy-vs'>def. {esc(c['runnerup_name'])}</div>"
        cards.append(f"""<div class='trophy-card'><div class='trophy-year'>{c['year']}</div>
          <div class='trophy-icon'>&#127942;</div>
          {team_cell(c['name'], c.get('owner'))}
          <div class='trophy-record'>{esc(c.get('record',''))}</div>
          {matchup_line}</div>""")
    return f"<h2 class='section-title'>Hall of Fame &middot; Trophy Case</h2><p class='section-note'>Every champion from every season fetched, in one wall.</p><div class='trophy-wall'>{''.join(cards)}</div>"


def render_records_book(records):
    if not records or not records.get("highest_score"):
        return "<h2 class='section-title' style='margin-top:24px'>League Records Book</h2><p class='section-note'>Not enough historical data yet to compute records.</p>"

    cards = []
    hs = records["highest_score"]
    cards.append(f"""<div class='record-card'><div class='record-label'>&#128293; Highest Single-Week Score</div>
      <div class='record-value'>{hs['value']:.1f}</div>{team_cell(hs['name'], hs.get('owner'))}
      <div class='record-context'>Week {hs['week']}, {hs['year']}</div></div>""")

    ls = records["lowest_score"]
    cards.append(f"""<div class='record-card'><div class='record-label'>&#128703; Toilet Bowl (Lowest Score)</div>
      <div class='record-value'>{ls['value']:.1f}</div>{team_cell(ls['name'], ls.get('owner'))}
      <div class='record-context'>Week {ls['week']}, {ls['year']}</div></div>""")

    bo = records.get("biggest_blowout")
    if bo:
        cards.append(f"""<div class='record-card'><div class='record-label'>&#128165; Biggest Blowout</div>
          <div class='record-value'>{bo['margin']:.1f} <span class='record-unit'>pt margin</span></div>
          {team_cell(bo['winner'], bo.get('winner_owner'))}
          <div class='record-context'>beat {esc(bo['loser'])} {bo['winner_score']:.1f}&ndash;{bo['loser_score']:.1f} &middot; Week {bo['week']}, {bo['year']}</div></div>""")

    cg = records.get("closest_game")
    if cg:
        tie_note = " (Tie)" if cg.get("tie") else ""
        verb = "tied" if cg.get("tie") else "edged"
        cards.append(f"""<div class='record-card'><div class='record-label'>&#127919; Closest Game{tie_note}</div>
          <div class='record-value'>{cg['margin']:.1f} <span class='record-unit'>pt margin</span></div>
          {team_cell(cg['winner'], cg.get('winner_owner'))}
          <div class='record-context'>{verb} {esc(cg['loser'])} {cg['winner_score']:.1f}&ndash;{cg['loser_score']:.1f} &middot; Week {cg['week']}, {cg['year']}</div></div>""")

    ws = records.get("longest_win_streak")
    if ws:
        cards.append(f"""<div class='record-card'><div class='record-label'>&#128200; Longest Win Streak</div>
          <div class='record-value'>{ws['length']} <span class='record-unit'>games</span></div>
          {team_cell(ws['name'], ws.get('owner'))}
          <div class='record-context'>{_streak_span_label(ws)}</div></div>""")

    lsk = records.get("longest_loss_streak")
    if lsk:
        cards.append(f"""<div class='record-card'><div class='record-label'>&#128201; Longest Losing Streak</div>
          <div class='record-value'>{lsk['length']} <span class='record-unit'>games</span></div>
          {team_cell(lsk['name'], lsk.get('owner'))}
          <div class='record-context'>{_streak_span_label(lsk)}</div></div>""")

    mpl = records.get("most_points_loss")
    if mpl:
        cards.append(f"""<div class='record-card'><div class='record-label'>&#128148; Most Points in a Loss</div>
          <div class='record-value'>{mpl['value']:.1f}</div>{team_cell(mpl['name'], mpl.get('owner'))}
          <div class='record-context'>Week {mpl['week']}, {mpl['year']}</div></div>""")

    return f"<h2 class='section-title' style='margin-top:24px'>League Records Book</h2><p class='section-note'>All-time records across every season fetched. Pure trivia &mdash; bragging rights only.</p><div class='record-grid'>{''.join(cards)}</div>"


def render_history(hist, current_season):
    champ_html = render_trophy_case(hist['champions'])
    records_html = render_records_book(hist.get('records'))
    at_html = ""
    if hist['all_time']:
        at_rows = []
        for i, e in enumerate(hist['all_time'], 1):
            rec = f"{e['wins']}-{e['losses']}" + (f"-{e['ties']}" if e.get('ties') else "")
            at_rows.append(f"<tr><td>{i}</td><td class='team-cell'>{team_cell(e['name'], e.get('owner'))}</td><td>{rec}</td><td>{e['win_pct']*100:.1f}%</td><td>{e['pf']:.1f}</td><td>{e['seasons']}</td></tr>")
        at_html = f"<h2 class='section-title' style='margin-top:24px'>All-Time</h2><table><thead><tr><th>#</th><th>Team</th><th>Record</th><th>Win%</th><th>Total PF</th><th>Seasons</th></tr></thead><tbody>{''.join(at_rows)}</tbody></table>"
    riv_html = ""
    if hist['rivalries']:
        cards = []
        for r in hist['rivalries']:
            meeting_word = "meeting" if r['meetings'] == 1 else "meetings"
            cards.append(f"""<div class='rivalry-card'><div class='rivalry-meetings'>{r['meetings']} all-time {meeting_word}</div>
              <div class='matchup-teams'><div class='matchup-team'><div class='team-name-main'>{esc(r['name_a'])}</div><div class='team-record'>{r['wins_a']}-{r['wins_b']}</div><div class='owner-name'>{r['pts_a']} pts</div></div>
              <div class='vs'>vs</div>
              <div class='matchup-team'><div class='team-name-main'>{esc(r['name_b'])}</div><div class='team-record'>{r['wins_b']}-{r['wins_a']}</div><div class='owner-name'>{r['pts_b']} pts</div></div></div></div>""")
        riv_html = f"<h2 class='section-title' style='margin-top:24px'>Rivalry Tracker</h2><p class='section-note'>All-time head-to-head across every season fetched.</p><div class='rivalry-grid'>{''.join(cards)}</div>"
    return champ_html + records_html + at_html + riv_html


def build_ticker_html(model):
    """
    Signature Broadcast Desk element: a scrolling strip of short headline
    facts assembled from whatever real data is available this run (league
    leader, most recent trade, most recent champion, one superlative
    highlight). Gracefully falls back to just the leader/season line for a
    brand-new league with no history or trades yet, rather than showing
    nothing.
    """
    items = []
    standings = model.get("standings") or []
    if standings:
        leader = standings[0]
        items.append(f"{leader['name'].upper()} LEADS AT {leader['wins']}-{leader['losses']}")

    trades = model.get("trade_log") or []
    if trades:
        t = trades[0]
        sides = t.get("teams") or []
        if len(sides) >= 2:
            a, b = sides[0], sides[1]
            got = (a.get("gets") or ["a deal"])[0]
            items.append(f"TRADE: {a['name'].upper()} ACQUIRES {got.upper()} FROM {b['name'].upper()}")

    champions = (model.get("history") or {}).get("champions") or []
    if champions:
        c = champions[0]
        items.append(f"{c['name'].upper()} WON THE {c['year']} CHAMPIONSHIP")

    superlatives = model.get("stat_superlatives") or []
    pick = next((s for s in superlatives if s.get("leader") and s["leader"] != "TBD" and s["leader"] != "Loading…"), None)
    if pick:
        items.append(f"{pick['name'].upper()}: {pick['leader'].upper()}")

    if not items:
        items = [f"WEEK {model['current_week']} · {model['season']} SEASON"]

    spans = "".join(f"<span>{esc(item)}</span>" for item in items)
    return f'<div class="ticker"><div class="ticker-track">{spans}{spans}</div></div>'


def render(model):
    panels = [
        ("standings", "Standings", render_standings_section(model)),
        ("matchups", "Matchups", render_matchups(model)),
        ("power", "Power Rankings", render_power_section(model)),
        ("frontoffice", "Front Office", render_front_office(model)),
        ("draft", "Draft Board", render_draft(model['draft'])),
        ("superlatives", "Superlatives", render_superlatives(model['stat_superlatives'], VOTED_SUPERLATIVES)),
        ("parlay", "Weekly Parlay", render_parlay(load_parlay_weeks(), model)),
        ("history", "History", render_history(model['history'], model['season'])),
    ]
    tabs = "".join(f"<button class='tab{' active' if i==0 else ''}' onclick=\"showTab('{pid}',this)\">{label}</button>" for i, (pid, label, _) in enumerate(panels))
    body = "".join(f"<div id='{pid}' class='panel{' active' if i==0 else ''}'>{html}</div>" for i, (pid, _, html) in enumerate(panels))
    ticker_html = build_ticker_html(model)

    team_count = len(model['standings'])
    leader_name = model['standings'][0]['name'] if model['standings'] else "TBD"
    updated = datetime.now().strftime("%b %d, %Y %I:%M %p")
    season_years = list(model['history'].get('season_standings', {}).keys())
    est_year = min(season_years) if season_years else model['season']
    if model.get('season_not_started'):
        start = model.get('season_start_date')
        week_chip = f"Preseason &middot; Starts {esc(start)}" if start else "Preseason"
    else:
        week_chip = f"Week {model['current_week']}"
    hero = f"""<header class="hero">
  <div class="hero-glow"></div>
  <div class="hero-content">
    <div class="hero-eyebrow">Dynasty Franchise &middot; {model['season']} Season</div>
    <h1 class="hero-title">{esc(model['league_name'])}</h1>
    <div class="hero-meta">
      <span class="hero-chip hero-chip-est">Est. {est_year}</span>
      <span class="hero-chip">{week_chip}</span>
      <span class="hero-chip">{team_count} Teams</span>
      <span class="hero-chip">&#127942; {esc(leader_name)}</span>
      <span class="hero-chip hero-chip-muted">Updated {updated}</span>
    </div>
  </div>
</header>"""

    firebase_scripts = ""
    if FIREBASE_CONFIG:
        # Firebase's "compat" SDK build exposes a plain global `firebase` object
        # usable in an ordinary classic <script> — no bundler/ES-module import
        # ordering to worry about, which matters here since the JS below is one
        # big classic script relying on `firebase` already being defined by the
        # time it runs.
        firebase_scripts = (
            '<script src="https://www.gstatic.com/firebasejs/10.13.0/firebase-app-compat.js"></script>'
            '<script src="https://www.gstatic.com/firebasejs/10.13.0/firebase-firestore-compat.js"></script>'
        )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(model['league_name'])} · {esc(model['platform'])} Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=Oswald:wght@500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style></head>
<body>{ticker_html}{hero}
<div class="tabs">{tabs}</div>{body}{firebase_scripts}<script>{JS}</script></body></html>"""


def main():
    if not LEAGUE_ID:
        raise SystemExit("Set the LEAGUE_ID env var (your Sleeper league id).")
    model = build_model()
    out = render(model)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"Wrote {OUTPUT_FILE} ({len(out)} bytes) for {model['league_name']}")


if __name__ == "__main__":
    main()
