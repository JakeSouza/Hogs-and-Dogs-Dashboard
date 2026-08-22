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
            if u:
                owner_names.append(u.get("display_name") or u.get("username"))
        owner = " & ".join(owner_names) if owner_names else None
        u = user_by_id.get(r.get("owner_id"), {})
        meta = u.get("metadata") or {}
        team_name = meta.get("team_name") or u.get("display_name") or f"Team {rid}"
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


def champion(league_id, ordered):
    try:
        br = api(f"/league/{league_id}/winners_bracket") or []
    except Exception:
        br = []
    if br:
        decided = [m for m in br if m.get("w") is not None]
        if decided:
            final = max(decided, key=lambda m: m.get("r", 0))
            win_rid = final["w"]
            for t in ordered:
                if t["roster_id"] == win_rid:
                    return t
    return ordered[0] if ordered else None


def fetch_history(current_league_id, current_season, start_year):
    champions, season_standings, all_time, rivalries = [], {}, {}, {}
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
        ch = champion(league_id, ordered)
        if ch:
            champions.append({"year": int(season), "name": ch["team_name"], "owner": ch.get("owner")})
        scores, pairs, _ = weekly_results(league_id, MAX_WEEK)
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
    return champions, season_standings, all_time, rivalries


# --------------------------------------------------------------------------- #
#  ADAPTER: build a common `model` dict from Sleeper
# --------------------------------------------------------------------------- #
def build_model():
    state = api("/state/nfl")
    season = int(state.get("season") or datetime.utcnow().year)
    display_week = int(state.get("display_week") or state.get("week") or 1)
    lg = api(f"/league/{LEAGUE_ID}")
    league_name = lg.get("name") or "Sleeper League"

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
        mu = api(f"/league/{LEAGUE_ID}/matchups/{display_week}") or []
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
    for w in range(max(1, display_week - 4), display_week + 1):
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
    champions, season_standings, all_time, rivalries = fetch_history(LEAGUE_ID, season, HISTORY_START_YEAR)
    hist = {
        "champions": champions,
        "season_standings": season_standings,
        "all_time": sorted(all_time.values(), key=lambda e: (-e["win_pct"], -e["pf"])),
        "rivalries": [],
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
        "current_week": display_week,
        "standings": standings, "matchups": matchups, "power": power, "luck": luck,
        "activity": activity, "draft": draft, "history": hist,
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


def render_history(hist, current_season):
    champs = hist['champions']
    if champs:
        crows = [f"<tr><td>{c['year']}</td><td class='team-cell'>{team_cell(c['name'], c.get('owner'))}</td></tr>" for c in champs]
        champ_html = f"<h2 class='section-title'>League Champions</h2><table><thead><tr><th>Season</th><th>Champion</th></tr></thead><tbody>{''.join(crows)}</tbody></table>"
    else:
        champ_html = "<h2 class='section-title'>League Champions</h2><p class='section-note'>No completed prior seasons found yet.</p>"
    sub_nav, panels = [], []
    seasons = sorted(hist['season_standings'].keys(), reverse=True)
    for i, year in enumerate(seasons):
        tab_id = f"std-{year}"
        sub_nav.append(f"<button class='subtab{' active' if i==0 else ''}' onclick=\"showSubTab('{tab_id}',this)\">{year}</button>")
        with_pa = (year != current_season)
        panels.append(f"<div id='{tab_id}' class='subpanel{' active' if i==0 else ''}'>{standings_table(hist['season_standings'][year], with_pa=with_pa, with_streak=False)}</div>")
    seasons_html = ""
    if seasons:
        seasons_html = f"<h2 class='section-title' style='margin-top:24px'>Season Standings</h2><div class='subtabs'>{''.join(sub_nav)}</div>{''.join(panels)}"
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
    return champ_html + seasons_html + at_html + riv_html


def render(model):
    s = model['standings']
    panels = [
        ("standings", "Standings", standings_table(s)),
        ("matchups", f"Matchups (Week {model['current_week']})", render_matchups(model['matchups'])),
        ("power", "Power Rankings", render_power(model['power'])),
        ("luck", "Luck Index", render_luck(model['luck'])),
        ("activity", "Recent Activity", render_activity(model['activity'])),
        ("draft", "Draft Board", render_draft(model['draft'])),
        ("history", "History", render_history(model['history'], model['season'])),
    ]
    tabs = "".join(f"<button class='tab{' active' if i==0 else ''}' onclick=\"showTab('{pid}',this)\">{label}</button>" for i, (pid, label, _) in enumerate(panels))
    body = "".join(f"<div id='{pid}' class='panel{' active' if i==0 else ''}'>{html}</div>" for i, (pid, _, html) in enumerate(panels))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(model['league_name'])} · {esc(model['platform'])} Dashboard</title><style>{CSS}</style></head>
<body><h1>{esc(model['league_name'])}</h1><div class="subtitle">{esc(model['platform'])} Fantasy Football · {model['season']} season · auto-updated daily</div>
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
