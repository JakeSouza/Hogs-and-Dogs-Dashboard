"""
Standalone diagnostic — run this against your real league to see exactly
what Sleeper is returning, without needing to touch the dashboard script.

Usage:
    python diagnose_draft.py <LEAGUE_ID>
"""
import sys
import json
import urllib.request

LEAGUE_ID = sys.argv[1] if len(sys.argv) > 1 else input("League ID: ").strip()

def api(path):
    url = f"https://api.sleeper.app/v1{path}"
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read().decode())

print(f"=== Drafts for league {LEAGUE_ID} ===")
drafts = api(f"/league/{LEAGUE_ID}/drafts") or []
print(f"Found {len(drafts)} draft object(s) on record.\n")

for i, d in enumerate(drafts):
    print(f"--- Draft #{i}: {d.get('draft_id')} ---")
    print(f"  status: {d.get('status')}")
    print(f"  type: {d.get('type')}")
    print(f"  season: {d.get('season')}")
    settings = d.get("settings") or {}
    print(f"  settings.rounds: {settings.get('rounds')}")
    print(f"  settings.teams: {settings.get('teams')}")
    print(f"  draft_order: {d.get('draft_order')}")
    print(f"  slot_to_roster_id: {d.get('slot_to_roster_id')}")

    picks = api(f"/draft/{d['draft_id']}/picks") or []
    print(f"  total picks returned: {len(picks)}")
    if picks:
        rounds_seen = sorted(set(p.get("round") for p in picks))
        slots_seen = sorted(set(p.get("draft_slot") for p in picks))
        print(f"  rounds seen in picks: {rounds_seen}")
        print(f"  draft_slot values seen: {slots_seen}")
        print(f"  first pick raw: {json.dumps(picks[0], indent=2)}")
        print(f"  last pick raw: {json.dumps(picks[-1], indent=2)}")
        # check for any picks with missing player_id or empty metadata
        empty = [p for p in picks if not p.get("player_id")]
        print(f"  picks with no player_id at all: {len(empty)}")
    print()