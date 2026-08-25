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
        }
    return teams


def weekly_results(league_id, max_week):
    """Return scores {rid:{week:pts}}, pairs {week:[(a,b)]}, outcomes {rid:[W/L/T...]}."""
    scores, pairs, outcomes = {}, {}, {}
    for w in range(1, max_week + 1):
        try:
            mu = api(f"/league/{league_id}/matchups/{w}")
        except Exception:
            mu = []
        if not mu:
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
    return scores, pairs, outcomes


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
    Call once per completed game in ASCENDING year order so streaks
    correctly carry across a season boundary. A tie breaks both a win and
    a loss streak. Tracks the best win streak and best loss streak seen
    so far, each with the year/week span it covers.
    """
    s = state.setdefault(team_id, {
        "current_type": None, "current_len": 0, "current_start": None,
        "best_win": None, "best_loss": None,
    })
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
        scores, pairs, _ = weekly_results(league_id, MAX_WEEK)
        season_snapshots.append({"season": int(season), "teams": teams, "scores": scores, "pairs": pairs})
        for w, plist in pairs.items():
            for a, b in plist:
                sa, sb = scores.get(a, {}).get(w), scores.get(b, {}).get(w)
                if sa is None or sb is None:
                    continue
                key = frozenset({a, b})
                h2h = rivalries.setdefault(key, {"meetings": 0, "wins": {}, "points": {}})
                h2h["meetings"] += 1
                h2h["wins"].setdefault(a, 0); h2h["wins"].setdefault(b, 0)
                h2h["points"].setdefault(a, 0.0); h2h["points"].setdefault(b, 0.0)
                if sa > sb: h2h["wins"][a] += 1
                elif sb > sa: h2h["wins"][b] += 1
                else: h2h["wins"][a] += 0.5; h2h["wins"][b] += 0.5
                h2h["points"][a] += sa; h2h["points"][b] += sb
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


# --------------------------------------------------------------------------- #
#  ADAPTER: build a common `model` dict from Sleeper
# --------------------------------------------------------------------------- #
def build_model():
    state = api("/state/nfl")
    season = int(state.get("season") or datetime.utcnow().year)
    # Sleeper's "week" is the actual current NFL week (matches real games
    # being played). "display_week" is a separate UI-navigation hint Sleeper
    # uses on its own site, and it intentionally jumps ahead early in the
    # week (often right after Monday Night Football) — using it here made
    # the Matchups tab show next week's number before that week's games had
    # even happened. Prefer the real "week" value; fall back to
    # display_week only if "week" is missing for some reason.
    current_week = int(state.get("week") or state.get("display_week") or 1)
    lg = api(f"/league/{LEAGUE_ID}")
    league_name = lg.get("name") or "Sleeper League"
    lg_settings = lg.get("settings") or {}
    playoff_spots = lg_settings.get("playoff_teams") or 0
    reg_season_weeks = max(0, (lg_settings.get("playoff_week_start") or 0) - 1)

    teams = build_teams(LEAGUE_ID)
    scores, pairs, outcomes = weekly_results(LEAGUE_ID, MAX_WEEK)
    completed = sorted(pairs.keys())
    recent_weeks = completed[-3:]

    # ---- standings (current season) ----
    ordered = sorted(teams.values(), key=lambda t: (-t["wins"], t["losses"], -t["fpts"]))
    standings = []
    for i, t in enumerate(ordered, 1):
        sl, sn = streak_from_outcomes(outcomes.get(t["roster_id"], []))
        standings.append({
            "rank": i, "name": t["team_name"], "owner": t.get("owner"),
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
            d = drafts[0]
            picks = api(f"/draft/{d['draft_id']}/picks") or []
            rounds = (d.get("settings") or {}).get("rounds", 0) or max((p.get("round", 0) for p in picks), default=0)
            total = d.get("settings", {}).get("teams") or len(teams)
            grid = {}
            order = {}
            slot_to_team = {}
            for p in picks:
                slot = p.get("draft_slot")
                rnd = p.get("round")
                rid = p.get("roster_id")
                meta = p.get("metadata") or {}
                nm = f"{meta.get('first_name','')} {meta.get('last_name','')}".strip()
                grid[(rnd, slot)] = {"player": nm or p.get("player_id"), "pos": meta.get("position", ""),
                                     "team": meta.get("team", ""), "owner": teams.get(rid, {}).get("team_name", "?")}
                if slot and slot not in slot_to_team:
                    slot_to_team[slot] = teams.get(rid, {}).get("team_name", f"Slot {slot}")
            for slot in sorted(slot_to_team):
                order[slot] = slot_to_team[slot]
            draft = {"rounds": rounds, "teams": total, "grid": grid, "order": order}
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

    return {
        "league_name": league_name, "platform": "Sleeper", "season": season,
        "current_week": current_week,
        "standings": standings, "matchups": matchups, "power": power, "luck": luck,
        "activity": activity, "draft": draft, "history": hist,
        "playoff_spots": playoff_spots, "reg_season_weeks": reg_season_weeks,
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
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#0f1424;color:#e6e9f0;padding:24px}
h1{font-size:1.6rem;margin-bottom:4px}
.subtitle{color:#8a92a8;margin-bottom:20px;font-size:.95rem}

/* ---------- Hero / title banner ---------- */
.hero{position:relative;overflow:hidden;border-radius:18px;border:1px solid #2a3348;padding:40px 32px;margin-bottom:28px;background:linear-gradient(180deg,#121a30 0%,#161d30 100%)}
.hero-glow{position:absolute;inset:-40%;background:
    radial-gradient(circle at 20% 20%,rgba(255,107,53,0.28),transparent 45%),
    radial-gradient(circle at 80% 30%,rgba(168,85,247,0.24),transparent 45%),
    radial-gradient(circle at 50% 90%,rgba(79,141,255,0.22),transparent 50%);
  filter:blur(10px);pointer-events:none}
.hero-content{position:relative;z-index:1}
.hero-eyebrow{color:#8a92a8;font-size:12px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:10px}
.hero-title{margin:0 0 18px 0;font-size:42px;font-weight:800;letter-spacing:-1px;line-height:1.1;
  background:linear-gradient(90deg,#ffffff 0%,#cfd8f5 60%,#a855f7 130%);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.hero-meta{display:flex;flex-wrap:wrap;gap:8px}
.hero-chip{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:999px;font-size:13px;font-weight:600;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);color:#e6e9f0;backdrop-filter:blur(4px)}
.hero-chip-muted{color:#8a92a8;font-weight:400;background:transparent;border-color:#2a3348}
@media (max-width:640px){
  .hero{padding:24px 18px;border-radius:14px;margin-bottom:18px}
  .hero-title{font-size:28px}
  .hero-chip{font-size:12px;padding:5px 11px}
}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:18px}
.tab{background:#1a2138;border:1px solid #2a3348;color:#c2c8d8;padding:9px 16px;border-radius:8px;cursor:pointer;font-size:.9rem}
.tab.active{background:#3b82f6;color:#fff;border-color:#3b82f6}
.panel{display:none;background:#161d30;border:1px solid #2a3348;border-radius:12px;padding:20px}
.panel.active{display:block}
.section-title{font-size:1.15rem;margin-bottom:12px;color:#fff}
.section-note{color:#8a92a8;font-size:.85rem;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th{text-align:left;color:#8a92a8;padding:8px 10px;border-bottom:1px solid #2a3348;font-weight:600}
td{padding:9px 10px;border-bottom:1px solid #1f2740}
.team-cell-inner{display:flex;align-items:center;gap:10px}
.logo{width:30px;height:30px;border-radius:50%;object-fit:cover}
.team-name-main{font-weight:600;color:#e6e9f0}
.owner-name{font-size:.78rem;color:#7a82a0}
.matchup-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.matchup-card{background:#1a2138;border:1px solid #2a3348;border-radius:10px;padding:14px}
.matchup-teams{display:flex;justify-content:space-between;align-items:center;gap:8px}
.matchup-team{text-align:center;flex:1}
.team-record{color:#8a92a8;font-size:.8rem}
.proj-score{font-size:1.3rem;font-weight:700;color:#3b82f6}
.vs{color:#5a6280;font-weight:700}
.outlook{margin-top:10px;font-size:.8rem;color:#a0a6c0;line-height:1.4}
.rivalry-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.rivalry-card{background:#1a2138;border:1px solid #2a3348;border-radius:10px;padding:14px}
.rivalry-meetings{color:#8a92a8;font-size:.78rem;margin-bottom:8px}
.empty{color:#6a7090;font-style:italic;padding:14px}
.luck-good{color:#4ade80}.luck-bad{color:#f87171}
.action{color:#c2c8d8}
.subtabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}
.subtab{background:#1a2138;border:1px solid #2a3348;color:#9aa2b8;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:.82rem}
.subtab.active{background:#3b82f6;color:#fff;border-color:#3b82f6}
.subpanel{display:none}.subpanel.active{display:block}
.draft-grid{overflow-x:auto}
.draft-grid table{font-size:.78rem}
.draft-grid td,.draft-grid th{border:1px solid #1f2740;padding:5px 6px;min-width:90px;vertical-align:top}

/* ---------- Trophy Case ---------- */
.trophy-wall{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
.trophy-card{background:#1a2138;border:1px solid #2a3348;border-radius:10px;padding:18px 16px;text-align:center}
.trophy-year{color:#f59e0b;font-weight:800;font-size:.82rem;letter-spacing:.5px;margin-bottom:6px}
.trophy-icon{font-size:1.6rem;margin-bottom:8px}
.trophy-card .team-name-main{font-size:.88rem}
.trophy-record{color:#8a92a8;font-size:.78rem;margin:6px 0 4px 0}
.trophy-score{font-weight:700;font-size:.92rem;color:#3b82f6;margin-top:6px}
.trophy-vs{color:#8a92a8;font-size:.72rem;margin-top:4px}

/* ---------- League Records Book ---------- */
.record-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
.record-card{background:#1a2138;border:1px solid #2a3348;border-radius:10px;padding:16px}
.record-label{color:#8a92a8;font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.record-value{font-size:1.4rem;font-weight:800;color:#e6e9f0;margin-bottom:8px}
.record-unit{font-size:.75rem;font-weight:600;color:#8a92a8}
.record-card .team-name-main{font-size:.88rem}
.record-context{color:#8a92a8;font-size:.72rem;margin-top:6px;line-height:1.4}

/* ---------- Playoff Picture ---------- */
.playoff-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.playoff-col-title{font-size:.82rem;color:#8a92a8;text-transform:uppercase;letter-spacing:.5px;margin:0 0 10px 0}
.playoff-row{display:grid;grid-template-columns:24px 1fr auto auto auto;align-items:center;gap:10px;background:#1a2138;border:1px solid #2a3348;border-radius:8px;padding:10px 12px;margin-bottom:8px}
.playoff-seed{color:#8a92a8;font-weight:700;font-size:.82rem}
.playoff-record{font-size:.82rem;font-weight:600;white-space:nowrap}
.playoff-detail{color:#8a92a8;font-size:.72rem;white-space:nowrap}
.playoff-badge{font-size:.7rem;font-weight:700;padding:4px 9px;border-radius:20px;white-space:nowrap}
.badge-clinched{background:rgba(74,222,128,.2);color:#4ade80}
.badge-hunt{background:rgba(59,130,246,.2);color:#3b82f6}
.badge-bubble{background:rgba(245,158,11,.2);color:#f59e0b}
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
"""

JS = """
function showTab(id,btn){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));btn.classList.add('active');document.getElementById(id).classList.add('active');}
function showSubTab(id,btn){btn.parentNode.querySelectorAll('.subtab').forEach(t=>t.classList.remove('active'));btn.parentNode.parentNode.querySelectorAll('.subpanel').forEach(p=>p.classList.remove('active'));btn.classList.add('active');document.getElementById(id).classList.add('active');}
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
        detail = "Season complete" if e["remaining"] == 0 else f"{e['remaining']} games left"
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



def render_matchups(mup):
    if not mup: return "<p class='empty'>No matchup data available for this week yet.</p>"
    cards = []
    for m in mup:
        cards.append(f"""<div class='matchup-card'><div class='matchup-teams'>
          <div class='matchup-team'><div class='team-name-main'>{esc(m['away']['name'])}</div><div class='team-record'>{esc(m['away']['record'])}</div><div class='proj-score'>{m['away']['proj']:.1f}</div></div>
          <div class='vs'>@</div>
          <div class='matchup-team'><div class='team-name-main'>{esc(m['home']['name'])}</div><div class='team-record'>{esc(m['home']['record'])}</div><div class='proj-score'>{m['home']['proj']:.1f}</div></div>
          </div><p class='outlook'>{esc(m['outlook'])}</p></div>""")
    return f"<div class='matchup-grid'>{''.join(cards)}</div>"


def render_power(power):
    if not power: return "<p class='empty'>No completed weeks yet — power rankings need game data.</p>"
    rows = []
    for p in power:
        d = p['delta']
        dcls = "luck-good" if d > 0 else ("luck-bad" if d < 0 else "")
        rows.append(f"<tr><td>{p['rank']}</td><td class='team-cell'>{team_cell(p['name'], p.get('owner'))}</td><td>{p['score']}</td><td>{p['avg_pts']:.1f}</td><td>{p['avg_margin']:+.1f}</td><td class='{dcls}'>{d:+d}</td></tr>")
    return f"<table><thead><tr><th>#</th><th>Team</th><th>Score</th><th>Avg PF</th><th>Avg Margin</th><th>vs Standings</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"


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
    slots = sorted(order)
    head = "<tr><th>Round</th>" + "".join(f"<th>{esc(order[s])}</th>" for s in slots) + "</tr>"
    body = []
    for rnd in range(1, rounds + 1):
        cells = f"<td><b>{rnd}</b></td>"
        for s in slots:
            p = grid.get((rnd, s))
            if p:
                cells += f"<td><div class='team-name-main'>{esc(p['player'])}</div><div class='owner-name'>{esc(p['pos'])} · {esc(p['team'])}</div></td>"
            else:
                cells += "<td></td>"
        body.append(f"<tr>{cells}</tr>")
    return f"<div class='draft-grid'><table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table></div>"


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
            cards.append(f"""<div class='rivalry-card'><div class='rivalry-meetings'>{r['meetings']} all-time meetings</div>
              <div class='matchup-teams'><div class='matchup-team'><div class='team-name-main'>{esc(r['name_a'])}</div><div class='team-record'>{r['wins_a']}-{r['wins_b']}</div><div class='owner-name'>{r['pts_a']} pts</div></div>
              <div class='vs'>vs</div>
              <div class='matchup-team'><div class='team-name-main'>{esc(r['name_b'])}</div><div class='team-record'>{r['wins_b']}-{r['wins_a']}</div><div class='owner-name'>{r['pts_b']} pts</div></div></div></div>""")
        riv_html = f"<h2 class='section-title' style='margin-top:24px'>Rivalry Tracker</h2><p class='section-note'>All-time head-to-head across every season fetched.</p><div class='rivalry-grid'>{''.join(cards)}</div>"
    return champ_html + records_html + at_html + riv_html


def render(model):
    panels = [
        ("standings", "Standings", render_standings_section(model)),
        ("matchups", "Matchups", render_matchups(model['matchups'])),
        ("power", "Power Rankings", render_power(model['power'])),
        ("luck", "Luck Index", render_luck(model['luck'])),
        ("activity", "Recent Activity", render_activity(model['activity'])),
        ("draft", "Draft Board", render_draft(model['draft'])),
        ("history", "History", render_history(model['history'], model['season'])),
    ]
    tabs = "".join(f"<button class='tab{' active' if i==0 else ''}' onclick=\"showTab('{pid}',this)\">{label}</button>" for i, (pid, label, _) in enumerate(panels))
    body = "".join(f"<div id='{pid}' class='panel{' active' if i==0 else ''}'>{html}</div>" for i, (pid, _, html) in enumerate(panels))

    team_count = len(model['standings'])
    leader_name = model['standings'][0]['name'] if model['standings'] else "TBD"
    updated = datetime.now().strftime("%b %d, %Y %I:%M %p")
    hero = f"""<header class="hero">
  <div class="hero-glow"></div>
  <div class="hero-content">
    <div class="hero-eyebrow">Fantasy Football &middot; {model['season']} Season</div>
    <h1 class="hero-title">{esc(model['league_name'])}</h1>
    <div class="hero-meta">
      <span class="hero-chip">Week {model['current_week']}</span>
      <span class="hero-chip">{team_count} Teams</span>
      <span class="hero-chip">&#127942; {esc(leader_name)}</span>
      <span class="hero-chip hero-chip-muted">Updated {updated}</span>
    </div>
  </div>
</header>"""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(model['league_name'])} · {esc(model['platform'])} Dashboard</title><style>{CSS}</style></head>
<body>{hero}
<div class="tabs">{tabs}</div>{body}<script>{JS}</script></body></html>"""


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