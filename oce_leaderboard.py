#!/usr/bin/env python3
"""
OCE Slapshot: Rebound Leaderboard Builder

Maintains a list of known player IDs in players.json, fetches ranked data for
each, discovers new OCE players via BFS through match histories, and outputs
a sorted leaderboard.

Usage:
    python oce_leaderboard.py                    # full run
    python oce_leaderboard.py --no-discover      # skip BFS, just refresh known IDs
    python oce_leaderboard.py --resume           # skip already-cached IDs
    python oce_leaderboard.py --deep-search      # BFS follows customs too, not just casual
    python oce_leaderboard.py --max-discovery 50 # cap BFS growth
    python oce_leaderboard.py --quick 440        # cheap update: refresh seed (id=440) +
                                                 # everyone in their recent matches, then
                                                 # BFS one+ level for new players
    python oce_leaderboard.py --import-ids ids.txt  # merge IDs scraped from a text file
                                                 # into players.json (run a normal update
                                                 # afterwards to fetch + verify them)

Outputs (in docs/):
    - index.html              main leaderboard (served by GitHub Pages)
    - leaderboard_detailed.html detailed leaderboard with sortable stats
    - full_leaderboard.csv   raw CSV of all players sorted by rating
    - players.json           persisted list of all known player IDs
    - raw_data.json          full API response cache
"""

import argparse
import csv
import json
import time
from datetime import datetime
from collections import deque
from pathlib import Path

import requests


# ---- Config ----

BASE_URL = "https://slapshot.gg"
OCE_REGION = "oce-east"
DELAY_SECONDS = 1           # Pause between requests; RateTracker handles the ~10/30s cap
DEFAULT_MAX_DISCOVERY = 500 # Cap on new players to discover via BFS

SEASON3_START = "2025-09-23"  # Only follow matches from this date onward
CASUAL_MATCH_TYPE = "casual"  # Ranked game mode; excludes pond, customs
MIN_RANKED_MATCHES = 1        # Min ranked matches_played to include a discovered player

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://slapshot.gg/",
}

# Rank presentation — single source of truth, shared by Python row builders
# and injected as JSON into the client-side renderers.
RANK_IMG = {
    'Legend':       'ranks/legend.png',
    'Diamond':      'ranks/diamond.png',
    'Platinum III': 'ranks/plat_iii.png', 'Platinum II': 'ranks/plat_ii.png', 'Platinum I': 'ranks/plat_i.png',
    'Gold III':     'ranks/gold_iii.png', 'Gold II':     'ranks/gold_ii.png', 'Gold I':     'ranks/gold_i.png',
    'Silver III':   'ranks/silver_iii.png','Silver II':  'ranks/silver_ii.png','Silver I':   'ranks/silver_i.png',
    'Bronze III':   'ranks/bronze_iii.png','Bronze II':  'ranks/bronze_ii.png','Bronze I':   'ranks/bronze_i.png',
    'Unranked':     'ranks/unranked.png',
}
RANK_COLOR = {
    'Legend':       '#4fc3f7',
    'Diamond':      '#4fc3f7',
    'Platinum III': '#a78bfa', 'Platinum II': '#a78bfa', 'Platinum I': '#a78bfa',
    'Gold III':     '#f5b731', 'Gold II':     '#f5b731', 'Gold I':     '#f5b731',
    'Silver III':   '#a8bdd0', 'Silver II':   '#a8bdd0', 'Silver I':   '#a8bdd0',
    'Bronze III':   '#cd7f32', 'Bronze II':   '#cd7f32', 'Bronze I':   '#cd7f32',
    'Unranked':     '#7a8da6',
}
RANK_THRESHOLD = {
    'Legend': 2080, 'Diamond': 2000,
    'Platinum III': 1920, 'Platinum II': 1820, 'Platinum I': 1740,
    'Gold III': 1660, 'Gold II': 1580, 'Gold I': 1490,
    'Silver III': 1440, 'Silver II': 1380, 'Silver I': 1320,
    'Bronze III': 1260, 'Bronze II': 1200, 'Bronze I': 1140,
}


# ---- HTTP with rate limit awareness ----

class RateTracker:
    def __init__(self, delay=DELAY_SECONDS):
        self.remaining = None
        self.reset_at = None
        self.delay = delay

    def update(self, resp):
        if "x-ratelimit-remaining" in resp.headers:
            try:
                self.remaining = int(resp.headers["x-ratelimit-remaining"])
                self.reset_at = float(resp.headers["x-ratelimit-reset"])
            except (ValueError, TypeError):
                pass

    def wait_if_needed(self):
        if self.remaining is not None and self.remaining <= 2 and self.reset_at:
            wait = max(0, self.reset_at - time.time()) + 1
            if wait > 0:
                print(f"   [rate limit low, sleeping {wait:.1f}s]")
                time.sleep(wait)


def fetch_json(url, rate, session, max_429_retries=3):
    for attempt in range(max_429_retries + 1):
        rate.wait_if_needed()
        time.sleep(rate.delay)

        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
        except requests.RequestException as e:
            print(f"   ! network error on {url}: {e}")
            return None

        rate.update(resp)

        if resp.status_code == 429:
            if attempt >= max_429_retries:
                print(f"   ! 429 after {attempt} retries, giving up on {url}")
                return None
            print(f"   ! 429 Too Many Requests, backing off 60s (attempt {attempt + 1})")
            time.sleep(60)
            continue

        if resp.status_code in (404, 403):
            return None

        if resp.status_code >= 400:
            print(f"   ! HTTP {resp.status_code} on {url}")
            return None

        try:
            return resp.json()
        except ValueError:
            print(f"   ! non-JSON response from {url}")
            return None
    return None


def get_player(pid, rate, session):
    return fetch_json(f"{BASE_URL}/api/game/players/{pid}", rate, session)


def get_ranked(pid, rate, session):
    return fetch_json(f"{BASE_URL}/api/game/players/{pid}/ranked", rate, session)


# ---- Helpers ----

def extract_match_player_ids(match):
    ids = set()
    game_stats = (match or {}).get("game_stats") or {}
    for p in game_stats.get("players", []) or []:
        pid = (
            p.get("game_user_id")
            or p.get("player_id")
            or p.get("id")
            or p.get("user_id")
        )
        if pid is not None:
            ids.add(str(pid))
    return ids


def is_qualifying_match(match):
    """True if match is OCE casual ranked from Season 3 onward."""
    return (
        match.get("region") == OCE_REGION
        and match.get("match_type") == CASUAL_MATCH_TYPE
        and (match.get("created") or "") >= SEASON3_START
    )


def is_oce_season3_match(match):
    """True if match is any mode in OCE from Season 3 onward (used for deep BFS)."""
    return (
        match.get("region") == OCE_REGION
        and (match.get("created") or "") >= SEASON3_START
    )



def is_oce_majority(player_data):
    """True if OCE games >= non-OCE games in the player's last 10 matches."""
    oce = non_oce = 0
    for m in (player_data or {}).get("match_history", []) or []:
        if oce + non_oce >= 10:
            break
        if m.get("region") == OCE_REGION:
            oce += 1
        else:
            non_oce += 1
    return oce >= non_oce


def is_eligible(player_data, ranked_data):
    """Single source of truth for leaderboard eligibility."""
    if ((ranked_data or {}).get("matches_played") or 0) < MIN_RANKED_MATCHES:
        return False
    if not is_oce_majority(player_data):
        return False
    return True


def calc_recent_stats(pid, player_data):
    """Wins, losses, win rate and goals/game from the last 10 OCE ranked matches in history."""
    wins = goals = games = 0
    for m in (player_data or {}).get("match_history", []) or []:
        if games >= 10:
            break
        if not is_qualifying_match(m):
            continue
        stats = next(
            (p.get("stats", {}) for p in (m.get("game_stats") or {}).get("players", [])
             if str(p.get("game_user_id")) == str(pid)),
            None
        )
        if stats is None:
            continue
        games += 1
        wins  += stats.get("wins", 0)
        goals += stats.get("goals", 0)
    losses = games - wins
    if games == 0:
        return {
            "win_rate": None, "goals_per_game": None,
            "recent_games": 0, "wins": 0, "losses": 0,
        }
    return {
        "win_rate":      round(wins / games * 100),
        "goals_per_game": round(goals / games, 1),
        "recent_games":  games,
        "wins":          wins,
        "losses":        losses,
    }


def fetch_player(pid, rate, session, cache, force=False):
    if pid in cache and not force:
        return cache[pid]
    player = get_player(pid, rate, session)
    ranked = get_ranked(pid, rate, session)
    cache[pid] = {"player": player, "ranked": ranked}
    return cache[pid]


def discover_bfs(initial_queue, cache, rate, session, max_discovery,
                 deep_search, seen_matches=None, blocklist=None,
                 refresh_known_through_depth=0):
    """BFS-walk match histories from `initial_queue`.

    Always discovers + fetches new (uncached) eligible players.

    `refresh_known_through_depth` (default 0): force-refresh known players
    encountered up to this many hops from the initial queue. 0 = current
    behaviour (skip known players). Higher values catch ELO drift in players
    who weren't direct opponents but were one or more hops away.

    IDs in `blocklist` are never fetched, refreshed, or queued.

    Returns newly-discovered IDs in discovery order.
    """
    match_filter = is_oce_season3_match if deep_search else is_qualifying_match
    if blocklist is None:
        blocklist = set()
    if seen_matches is None:
        seen_matches = set()

    new_ids = []
    refreshed = set()
    queued = set(pid for pid in initial_queue if pid not in blocklist)
    queue = deque((pid, 0) for pid in queued)

    while queue and len(new_ids) < max_discovery:
        pid, depth = queue.popleft()
        player_data = cache.get(pid, {}).get("player")
        if not player_data:
            continue
        child_depth = depth + 1

        for match in player_data.get("match_history", []) or []:
            if not match_filter(match):
                continue
            match_id = match.get("id") or match.get("match_id")
            if match_id is not None:
                if match_id in seen_matches:
                    continue
                seen_matches.add(match_id)

            for participant in extract_match_player_ids(match):
                if (not participant.isdigit() or participant == pid
                        or participant in blocklist):
                    continue

                if participant not in cache:
                    data = fetch_player(participant, rate, session, cache)
                    if not is_eligible(data.get("player"), data.get("ranked")):
                        continue
                    new_ids.append(participant)
                    uname = (data.get("player") or {}).get("username", "?")
                    print(f"[discover {len(new_ids)}] id={participant} ({uname})")
                    if participant not in queued:
                        queue.append((participant, child_depth))
                        queued.add(participant)
                elif (child_depth <= refresh_known_through_depth
                      and participant not in refreshed):
                    refreshed.add(participant)
                    fetch_player(participant, rate, session, cache, force=True)
                    uname = (cache.get(participant, {}).get("player") or {}).get("username", "?")
                    print(f"[refresh d={child_depth}] id={participant} ({uname})")
                    if participant not in queued:
                        queue.append((participant, child_depth))
                        queued.add(participant)

                if len(new_ids) >= max_discovery:
                    break
            if len(new_ids) >= max_discovery:
                break
    return new_ids


# ---- HTML output ----

def build_html(players, last_updated=None):
    data_json = json.dumps([
        {
            "pos": i + 1,
            "name": p["name"],
            "rank": p["rank"] or "",
            "rating": p["rating"] if p["rating"] is not None else "",
            "matches": p["matches_played"] if p["matches_played"] is not None else 0,
            "highest_rank": p["highest_rank"] or "",
            "highest_rating": p["highest_rating"] if p["highest_rating"] is not None else "",
            "win_rate": p.get("win_rate") if p.get("win_rate") is not None else "",
            "goals_per_game": p.get("goals_per_game") if p.get("goals_per_game") is not None else "",
            "recent_games": p.get("recent_games") or 0,
            "wins": p.get("wins") or 0,
            "losses": p.get("losses") or 0,
            "id": p["id"],
        }
        for i, p in enumerate(players)
    ], ensure_ascii=False)
    last_updated_html = (
        f'<p class="subtitle" style="margin-top:2px;">Last updated: {last_updated}</p>'
        if last_updated else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OCE Slapshot Leaderboard</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0f1117; color: #e2e8f0; font-family: system-ui, sans-serif; padding: 24px; }}
  h1 {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 4px; color: #fff; }}
  .subtitle {{ color: #64748b; font-size: 0.85rem; margin-bottom: 20px; }}
  .controls {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; align-items: center; }}
  .search {{ background: #1e2230; border: 1px solid #2d3348; color: #e2e8f0; padding: 7px 12px;
             border-radius: 6px; font-size: 0.9rem; width: 220px; }}
  .search::placeholder {{ color: #475569; }}
  .search:focus {{ outline: none; border-color: #4f8ef7; }}
  .toggle {{ background: #1e2230; border: 1px solid #2d3348; color: #94a3b8; padding: 7px 14px;
             border-radius: 6px; font-size: 0.85rem; cursor: pointer; transition: all .15s; }}
  .toggle.active {{ background: #4f8ef7; border-color: #4f8ef7; color: #fff; }}
  .count {{ color: #64748b; font-size: 0.85rem; margin-left: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
  thead th {{ background: #161b27; color: #94a3b8; font-weight: 600; text-align: left;
              padding: 10px 14px; border-bottom: 1px solid #2d3348; white-space: nowrap;
              cursor: pointer; user-select: none; position: sticky; top: 0; z-index: 1; }}
  thead th:hover {{ color: #e2e8f0; }}
  thead th.sorted {{ color: #4f8ef7; }}
  thead th .arrow {{ margin-left: 4px; font-size: 0.7rem; opacity: 0.6; }}
  thead th.sorted .arrow {{ opacity: 1; }}
  tbody tr {{ border-bottom: 1px solid #1a1f2e; transition: background .1s; }}
  tbody tr:hover {{ background: #1a2035; }}
  td {{ padding: 9px 14px; }}
  td.pos {{ color: #475569; width: 48px; }}
  td.name {{ font-weight: 500; color: #fff; }}
  td.name a {{ color: inherit; text-decoration: none; }}
  td.name a:hover {{ color: #4f8ef7; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .rank-badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px;
                 font-size: 0.78rem; font-weight: 600; }}
  .rank-Platinum\\ I  {{ background:#2a1f6e; color:#a78bfa; }}
  .rank-Gold\\ III    {{ background:#2d2200; color:#f59e0b; }}
  .rank-Gold\\ II     {{ background:#2d2200; color:#fbbf24; }}
  .rank-Gold\\ I      {{ background:#2d2200; color:#fcd34d; }}
  .rank-Silver\\ III  {{ background:#1e2530; color:#94a3b8; }}
  .rank-Silver\\ II   {{ background:#1e2530; color:#cbd5e1; }}
  .rank-Silver\\ I    {{ background:#1e2530; color:#e2e8f0; }}
  .rank-Bronze\\ III  {{ background:#2a1a0e; color:#d97706; }}
  .rank-Bronze\\ II   {{ background:#2a1a0e; color:#b45309; }}
  .rank-Bronze\\ I    {{ background:#2a1a0e; color:#92400e; }}
  .rank-Unranked      {{ background:#1a1f2e; color:#475569; }}
  .rank-             {{ background:#1a1f2e; color:#475569; }}
  .rank-badge {{ display: inline-flex; align-items: center; gap: 5px; }}
  .rank-badge img {{ width: 20px; height: 20px; object-fit: contain; }}
</style>
</head>
<body>
<h1>OCE Slapshot Leaderboard</h1>
<p class="subtitle">Season 3 &mdash; oce-east</p>
{last_updated_html}
<div class="controls">
  <input class="search" type="text" placeholder="Search name..." id="search">
  <button class="toggle" id="toggleZero">Hide 0 matches</button>
  <button class="toggle" id="toggleUnranked">Hide unranked</button>
  <button class="toggle" id="toggleRanked">Hide ranked</button>
  <span class="count" id="count"></span>
</div>
<table>
  <thead>
    <tr>
      <th data-col="pos">#<span class="arrow">&#8597;</span></th>
      <th data-col="name">Name<span class="arrow">&#8597;</span></th>
      <th data-col="rank">Rank<span class="arrow">&#8597;</span></th>
      <th data-col="rating" class="sorted">Rating<span class="arrow">&#8595;</span></th>
      <th data-col="matches">Matches<span class="arrow">&#8597;</span></th>
      <th data-col="highest_rank">Highest Rank<span class="arrow">&#8597;</span></th>
      <th data-col="highest_rating">Highest Rating<span class="arrow">&#8597;</span></th>
      <th data-col="win_rate">Win % (L10)<span class="arrow">&#8597;</span></th>
      <th data-col="recent_games">W-L (L10)<span class="arrow">&#8597;</span></th>
      <th data-col="goals_per_game">Goals/G (L10)<span class="arrow">&#8597;</span></th>
    </tr>
  </thead>
  <tbody id="tbody"></tbody>
</table>
<script>
const RANK_IMG = {{
  'Platinum III': 'ranks/plat_iii.png',
  'Platinum II':  'ranks/plat_ii.png',
  'Platinum I':   'ranks/plat_i.png',
  'Gold III':     'ranks/gold_iii.png',
  'Gold II':      'ranks/gold_ii.png',
  'Gold I':       'ranks/gold_i.png',
  'Silver III':   'ranks/silver_iii.png',
  'Silver II':    'ranks/silver_ii.png',
  'Silver I':     'ranks/silver_i.png',
  'Bronze III':   'ranks/bronze_iii.png',
  'Bronze II':    'ranks/bronze_ii.png',
  'Bronze I':     'ranks/bronze_i.png',
  'Unranked':     'ranks/unranked.png',
}};

const rankBadge = r => {{
  const img = RANK_IMG[r] ? `<img src="${{RANK_IMG[r]}}" alt="">` : '';
  return `<span class="rank-badge rank-${{r}}">${{img}}${{r || 'Unranked'}}</span>`;
}};

const RANK_ORDER = [
  'Platinum I','Gold III','Gold II','Gold I',
  'Silver III','Silver II','Silver I',
  'Bronze III','Bronze II','Bronze I','Unranked',''
];
const rankVal = r => {{ const i = RANK_ORDER.indexOf(r); return i === -1 ? 99 : i; }};

const ALL = {data_json};

let sortCol = 'rating', sortAsc = false;
let hideZero = false, hideUnranked = false, hideRanked = false, searchTerm = '';

function filtered() {{
  return ALL.filter(p => {{
    if (hideZero && p.matches === 0) return false;
    if (hideUnranked && (p.rank === 'Unranked' || p.rank === '')) return false;
    if (hideRanked && p.rank !== 'Unranked' && p.rank !== '') return false;
    if (searchTerm && !p.name.toLowerCase().includes(searchTerm)) return false;
    return true;
  }});
}}

function sorted(rows) {{
  return [...rows].sort((a, b) => {{
    let av = a[sortCol], bv = b[sortCol];
    if (sortCol === 'rank' || sortCol === 'highest_rank') {{
      av = rankVal(av); bv = rankVal(bv);
    }} else if (typeof av === 'string' || typeof bv === 'string') {{
      av = String(av).toLowerCase(); bv = String(bv).toLowerCase();
    }}
    if (av === '' || av === null) av = sortAsc ? Infinity : -Infinity;
    if (bv === '' || bv === null) bv = sortAsc ? Infinity : -Infinity;
    return sortAsc ? (av > bv ? 1 : av < bv ? -1 : 0) : (av < bv ? 1 : av > bv ? -1 : 0);
  }});
}}

function render() {{
  const rows = sorted(filtered());
  document.getElementById('count').textContent = rows.length + ' players';
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = rows.map((p, i) => `
    <tr>
      <td class="pos">${{i + 1}}</td>
      <td class="name"><a href="https://slapshot.gg/players/${{p.id}}" target="_blank" rel="noopener">${{p.name}}</a></td>
      <td>${{rankBadge(p.rank)}}</td>
      <td class="num">${{p.rating}}</td>
      <td class="num">${{p.matches}}</td>
      <td>${{p.highest_rank ? rankBadge(p.highest_rank) : '—'}}</td>
      <td class="num">${{p.highest_rating}}</td>
      <td class="num">${{p.win_rate !== '' ? p.win_rate + '%' : '—'}}</td>
      <td class="num">${{p.recent_games > 0 ? p.wins + '-' + p.losses : '—'}}</td>
      <td class="num">${{p.goals_per_game !== '' ? p.goals_per_game : '—'}}</td>
    </tr>`).join('');
}}

document.querySelectorAll('thead th').forEach(th => {{
  th.addEventListener('click', () => {{
    const col = th.dataset.col;
    if (sortCol === col) sortAsc = !sortAsc;
    else {{ sortCol = col; sortAsc = col === 'name'; }}
    document.querySelectorAll('thead th').forEach(t => {{
      t.classList.remove('sorted');
      t.querySelector('.arrow').innerHTML = '&#8597;';
    }});
    th.classList.add('sorted');
    th.querySelector('.arrow').innerHTML = sortAsc ? '&#8593;' : '&#8595;';
    render();
  }});
}});

document.getElementById('toggleZero').addEventListener('click', function() {{
  hideZero = !hideZero; this.classList.toggle('active', hideZero); render();
}});
document.getElementById('toggleUnranked').addEventListener('click', function() {{
  hideUnranked = !hideUnranked;
  if (hideUnranked && hideRanked) {{
    hideRanked = false;
    document.getElementById('toggleRanked').classList.remove('active');
  }}
  this.classList.toggle('active', hideUnranked);
  render();
}});
document.getElementById('toggleRanked').addEventListener('click', function() {{
  hideRanked = !hideRanked;
  if (hideRanked && hideUnranked) {{
    hideUnranked = false;
    document.getElementById('toggleUnranked').classList.remove('active');
  }}
  this.classList.toggle('active', hideRanked);
  render();
}});
document.getElementById('search').addEventListener('input', function() {{
  searchTerm = this.value.toLowerCase(); render();
}});

render();
</script>
</body>
</html>"""


def build_simple_html(players, last_updated=None):
    PODIUM_CLASS = {1: "top-1", 2: "top-2", 3: "top-3"}

    rows = []
    for i, p in enumerate(players, 1):
        rank = p.get("rank") or ""
        img = RANK_IMG.get(rank, "")
        color = RANK_COLOR.get(rank, "#7a8da6")
        img_tag = f'<img src="{img}" alt="{rank}">' if img else ""
        extra_class = PODIUM_CLASS.get(i, "")
        highest_rank = p.get("highest_rank") or ""
        highest_rating = p.get("highest_rating") or 0
        rows.append(f"""
  <tr class="{extra_class}" data-rank="{rank}" data-rating="{p['rating'] or 0}" data-highest-rank="{highest_rank}" data-highest-rating="{highest_rating}">
    <td class="pos">{i}</td>
    <td class="name"><a href="https://slapshot.gg/players/{p['id']}" target="_blank" rel="noopener">{p['name']}</a></td>
    <td class="rank-cell">
      {img_tag}
      <span style="color:{color}">{rank or "Unranked"}</span>
    </td>
    <td class="rating">{p['rating'] if p['rating'] is not None else "—"}</td>
  </tr>""")

    rows_html = "\n".join(rows)
    rank_img_json = json.dumps(RANK_IMG, ensure_ascii=False)
    rank_color_json = json.dumps(RANK_COLOR, ensure_ascii=False)
    rank_threshold_json = json.dumps(RANK_THRESHOLD, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OCE Ranked Leaderboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Teko:wght@400;500;600;700&family=Rajdhani:wght@400;500;600;700&family=Bebas+Neue&display=swap" rel="stylesheet">
<style>
  :root {{
    --navy-deep:  #0a1628;
    --navy:       #142238;
    --navy-mid:   #1c3054;
    --cyan:       #4fc3f7;
    --gold:       #f5b731;
    --text:       #e8edf4;
    --muted:      #7a8da6;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--navy-deep);
    background-image:
      radial-gradient(ellipse at 20% 0%, rgba(27,107,158,0.12) 0%, transparent 60%),
      radial-gradient(ellipse at 80% 100%, rgba(245,183,49,0.06) 0%, transparent 50%);
    color: var(--text);
    font-family: 'Rajdhani', sans-serif;
    min-height: 100vh;
    padding: 40px 24px;
  }}
  header {{
    text-align: center;
    margin-bottom: 40px;
  }}
  h1 {{
    font-family: 'Bebas Neue', sans-serif;
    font-size: 3.5rem;
    letter-spacing: 0.15em;
    color: var(--text);
    line-height: 1;
  }}
  h1 .accent {{
    color: var(--gold);
  }}
  .subtitle {{
    font-family: 'Teko', sans-serif;
    font-size: 1rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--cyan);
    margin-top: 6px;
  }}
  .wrap {{
    max-width: 780px;
    margin: 0 auto;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
  }}
  tr {{
    transition: background 0.15s;
    border-radius: 8px;
  }}
  tbody tr {{
    background: rgba(28, 48, 84, 0.45);
    border-bottom: 4px solid var(--navy-deep);
  }}
  tbody tr:hover {{ background: rgba(79,195,247,0.07); }}
  td {{
    padding: 14px 16px;
    vertical-align: middle;
  }}
  td:first-child {{ border-radius: 8px 0 0 8px; }}
  td:last-child  {{ border-radius: 0 8px 8px 0; }}
  td.pos {{
    font-family: 'Teko', sans-serif;
    font-size: 1.1rem;
    color: var(--muted);
    width: 48px;
    text-align: right;
    padding-right: 20px;
  }}
  td.name {{
    font-family: 'Bebas Neue', sans-serif;
    font-size: 2rem;
    letter-spacing: 0.05em;
  }}
  td.name a {{
    color: var(--text);
    text-decoration: none;
    transition: color 0.15s;
  }}
  td.name a:hover {{ color: var(--cyan); }}
  td.rank-cell {{
    display: flex;
    align-items: center;
    gap: 10px;
    font-family: 'Teko', sans-serif;
    font-size: 1.4rem;
    font-weight: 500;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    white-space: nowrap;
  }}
  td.rank-cell img {{
    width: 40px;
    height: 40px;
    object-fit: contain;
    flex-shrink: 0;
  }}
  td.rating {{
    font-family: 'Bebas Neue', sans-serif;
    font-size: 1.8rem;
    color: var(--cyan);
    text-align: right;
    letter-spacing: 0.04em;
  }}

  /* Controls */
  .controls {{
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 20px;
    flex-wrap: wrap;
  }}
  .search {{
    background: #142238;
    border: 1px solid rgba(79,195,247,0.15);
    border-radius: 8px;
    color: var(--text);
    font-family: 'Rajdhani', sans-serif;
    font-size: 1rem;
    font-weight: 500;
    padding: 8px 14px;
    width: 240px;
    outline: none;
    transition: border-color 0.15s;
  }}
  .search::placeholder {{ color: var(--muted); }}
  .search:focus {{ border-color: var(--cyan); }}
  .toggle-label {{
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: 'Teko', sans-serif;
    font-size: 1.1rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
    cursor: pointer;
    user-select: none;
  }}
  .toggle-label input {{ display: none; }}
  .checkmark {{
    width: 18px;
    height: 18px;
    border: 1.5px solid rgba(79,195,247,0.3);
    border-radius: 4px;
    background: #142238;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: border-color 0.15s, background 0.15s;
    flex-shrink: 0;
  }}
  .toggle-label input:checked + .checkmark {{
    background: var(--cyan);
    border-color: var(--cyan);
  }}
  .toggle-label input:checked + .checkmark::after {{
    content: '';
    display: block;
    width: 5px;
    height: 9px;
    border: 2px solid #0a1628;
    border-top: none;
    border-left: none;
    transform: rotate(45deg) translate(-1px, -1px);
  }}
  .toggle-label:hover .checkmark {{ border-color: var(--cyan); }}
  .last-updated {{
    font-family: 'Teko', sans-serif;
    font-size: 1rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
    margin-left: auto;
  }}

  /* Rank dividers */
  .rank-divider {{ background: transparent !important; border-bottom: none !important; }}
  .rank-divider:hover {{ background: transparent !important; }}
  .rank-divider td {{ padding: 8px 16px 4px; }}
  .rank-divider.empty {{ opacity: 0.3; }}
  .divider-inner {{
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  .div-line {{
    flex: 1;
    height: 1px;
    background: rgba(255,255,255,0.08);
  }}
  .divider-inner img {{ width: 20px; height: 20px; object-fit: contain; flex-shrink: 0; }}
  .divider-rank {{
    font-family: 'Teko', sans-serif;
    font-size: 0.9rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    white-space: nowrap;
  }}
  .divider-threshold {{
    font-family: 'Teko', sans-serif;
    font-size: 0.82rem;
    color: var(--muted);
    letter-spacing: 0.04em;
    white-space: nowrap;
  }}

  /* Podium rows */
  .top-1 {{
    background: linear-gradient(90deg, rgba(197,155,42,0.22) 0%, rgba(197,155,42,0.10) 60%, rgba(28,48,84,0.45) 100%) !important;
    border-left: 3px solid #f5b731;
  }}
  .top-1 td.pos {{ color: #f5b731; font-size: 1.3rem; }}
  .top-1 td.name a {{ color: #f5d442; }}

  .top-2 {{
    background: linear-gradient(90deg, rgba(168,189,208,0.18) 0%, rgba(168,189,208,0.08) 60%, rgba(28,48,84,0.45) 100%) !important;
    border-left: 3px solid #a8bdd0;
  }}
  .top-2 td.pos {{ color: #a8bdd0; font-size: 1.3rem; }}
  .top-2 td.name a {{ color: #c8dae8; }}

  .top-3 {{
    background: linear-gradient(90deg, rgba(205,127,50,0.18) 0%, rgba(205,127,50,0.08) 60%, rgba(28,48,84,0.45) 100%) !important;
    border-left: 3px solid #cd7f32;
  }}
  .top-3 td.pos {{ color: #cd7f32; font-size: 1.3rem; }}
  .top-3 td.name a {{ color: #e0974a; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>OCE <span class="accent">Ranked Leaderboard</span></h1>
    <p class="subtitle">Slapshot: Rebound &mdash; Season 3</p>
  </header>
  <div class="controls">
    <input class="search" type="text" id="search" placeholder="Search player...">
    <label class="toggle-label">
      <input type="checkbox" id="hideUnranked">
      <span class="checkmark"></span>
      Hide Unranked
    </label>
    <label class="toggle-label">
      <input type="checkbox" id="sortPeak">
      <span class="checkmark"></span>
      Sort by Peak Rank
    </label>
    <label class="toggle-label">
      <input type="checkbox" id="showDividers">
      <span class="checkmark"></span>
      Rank Dividers
    </label>
    <span class="last-updated"><span id="playerCount"></span> Players &nbsp;&bull;&nbsp; Last Updated: {last_updated}</span>
  </div>
  <table id="lb">
    <tbody>
{rows_html}
    </tbody>
  </table>
</div>
<script>
const RANK_IMG = {rank_img_json};
const RANK_COLOR = {rank_color_json};
const RANK_THRESHOLD = {rank_threshold_json};
const PODIUM = ['top-1','top-2','top-3'];

const rankCellHtml = r => {{
  const img = RANK_IMG[r] ? `<img src="${{RANK_IMG[r]}}" alt="${{r}}">` : '';
  const color = RANK_COLOR[r] || '#7a8da6';
  return `${{img}}<span style="color:${{color}}">${{r || 'Unranked'}}</span>`;
}};

const DIVIDER_TIERS = Object.entries(RANK_THRESHOLD)
  .map(([rank, threshold]) => ({{ rank, threshold }}))
  .sort((a, b) => b.threshold - a.threshold);

const makeDivider = (rank, isEmpty) => {{
  const tr = document.createElement('tr');
  tr.className = 'rank-divider' + (isEmpty ? ' empty' : '');
  const color = RANK_COLOR[rank] || '#7a8da6';
  const img   = RANK_IMG[rank] ? `<img src="${{RANK_IMG[rank]}}" alt="${{rank}}">` : '';
  const thr   = RANK_THRESHOLD[rank] != null ? RANK_THRESHOLD[rank] : '';
  tr.innerHTML = `<td colspan="4"><div class="divider-inner">
    <span class="div-line"></span>
    ${{img}}
    <span class="divider-rank" style="color:${{color}}">${{rank}}</span>
    ${{thr ? `<span class="divider-threshold">${{thr}}+</span>` : ''}}
    <span class="div-line"></span>
  </div></td>`;
  return tr;
}};

const search        = document.getElementById('search');
const hideUnranked  = document.getElementById('hideUnranked');
const sortPeak      = document.getElementById('sortPeak');
const showDividers  = document.getElementById('showDividers');
const tbody         = document.querySelector('#lb tbody');
const allRows       = Array.from(tbody.querySelectorAll('tr'));
document.getElementById('playerCount').textContent = allRows.length;

function getElo(row, byPeak) {{
  return Number(byPeak ? row.dataset.highestRating : row.dataset.rating) || 0;
}}

function placeRow(r, truePos, byPeak) {{
  tbody.appendChild(r);
  r.style.display = '';
  r.querySelector('td.pos').textContent = truePos + 1;
  PODIUM.forEach(c => r.classList.remove(c));
  if (truePos < 3) r.classList.add(PODIUM[truePos]);
  const rank   = byPeak ? r.dataset.highestRank  : r.dataset.rank;
  const rating = byPeak ? r.dataset.highestRating : r.dataset.rating;
  r.querySelector('td.rank-cell').innerHTML = rankCellHtml(rank);
  r.querySelector('td.rating').textContent  = rating || '—';
}}

function update() {{
  const term       = search.value.toLowerCase();
  const noUnranked = hideUnranked.checked;
  const byPeak     = sortPeak.checked;
  const withDivs   = showDividers.checked;

  tbody.querySelectorAll('.rank-divider').forEach(d => d.remove());
  allRows.forEach(r => r.style.display = 'none');

  // Sort all rows first to establish true positions
  const hideCheck = row => {{
    const chkRank = byPeak ? row.dataset.highestRank : row.dataset.rank;
    return chkRank === 'Unranked' || chkRank === '';
  }};
  const allSorted = [...allRows].sort((a, b) => getElo(b, byPeak) - getElo(a, byPeak));
  // Assign true position ignoring search term (but still respecting hide-unranked)
  const truePosMap = new Map();
  let tp = 0;
  allSorted.forEach(r => {{
    if (noUnranked && hideCheck(r)) return;
    truePosMap.set(r, tp++);
  }});

  // Now filter by search term for display
  const visible = allSorted.filter(r => {{
    if (noUnranked && hideCheck(r)) return false;
    return r.querySelector('td.name').textContent.toLowerCase().includes(term);
  }});
  document.getElementById('playerCount').textContent = visible.length;

  if (!withDivs) {{
    visible.forEach(r => placeRow(r, truePosMap.get(r), byPeak));
    return;
  }}

  // Threshold-based divider insertion — show all tiers, grey empty ones
  const placed = new Set();
  for (const {{ rank, threshold }} of DIVIDER_TIERS) {{
    const inTier = visible.filter(r => !placed.has(r) && getElo(r, byPeak) >= threshold);
    tbody.appendChild(makeDivider(rank, inTier.length === 0));
    inTier.forEach(r => {{ placed.add(r); placeRow(r, truePosMap.get(r), byPeak); }});
  }}
  visible.filter(r => !placed.has(r)).forEach(r => placeRow(r, truePosMap.get(r), byPeak));
}}

search.addEventListener('input', update);
hideUnranked.addEventListener('change', update);
sortPeak.addEventListener('change', update);
showDividers.addEventListener('change', update);
</script>
</body>
</html>"""


# ---- Main pipeline ----

def load_known_ids(players_path, raw_path):
    """Load IDs from players.json, seeding from raw_data.json if it doesn't exist yet."""
    p = Path(players_path)
    if p.exists():
        ids = json.loads(p.read_text(encoding="utf-8"))
        print(f"Loaded {len(ids)} known IDs from {players_path}")
        return list(dict.fromkeys(str(i) for i in ids))  # dedup, preserve order

    r = Path(raw_path)
    if r.exists():
        ids = list(json.loads(r.read_text(encoding="utf-8")).keys())
        print(f"players.json not found — seeded {len(ids)} IDs from {raw_path}")
        return ids

    print("No players.json or raw_data.json found — starting fresh")
    return []


def _id_sort_key(x):
    s = str(x)
    return (0, int(s)) if s.isdigit() else (1, s)


def atomic_write_json(path, data, indent=None):
    """Write JSON to a temp file then replace the target — survives Ctrl-C mid-write."""
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=indent), encoding="utf-8")
    tmp.replace(p)


def save_known_ids(players_path, ids):
    deduped = sorted({str(i) for i in ids}, key=_id_sort_key)
    atomic_write_json(players_path, deduped, indent=2)


def load_blocklist(blocklist_path):
    """Load manually-excluded IDs that should never appear on the leaderboard."""
    p = Path(blocklist_path)
    if not p.exists():
        return set()
    try:
        return {str(i) for i in json.loads(p.read_text(encoding="utf-8"))}
    except (ValueError, json.JSONDecodeError):
        print(f"Warning: couldn't parse {blocklist_path}, treating as empty.")
        return set()


def save_blocklist(blocklist_path, ids):
    deduped = sorted({str(i) for i in ids}, key=_id_sort_key)
    atomic_write_json(blocklist_path, deduped, indent=2)


def show_stats(output_dir, starting_rating=1400):
    """Print ELO pool stats for eligible OCE players in the cache."""
    raw_path = Path(output_dir) / "raw_data.json"
    if not raw_path.exists():
        print(f"No raw_data.json at {raw_path} — run the script first.")
        return

    with open(raw_path, encoding="utf-8") as f:
        cache = json.load(f)

    ratings = []
    for data in cache.values():
        pd = data.get("player") or {}
        rd = data.get("ranked") or {}
        if not is_eligible(pd, rd):
            continue
        rating = rd.get("rating")
        if rating is not None:
            ratings.append(rating)

    if not ratings:
        print("No eligible rated players in cache.")
        return

    n = len(ratings)
    total = sum(ratings)
    mean = total / n
    expected = n * starting_rating
    deviation = total - expected
    ratings_sorted = sorted(ratings)
    median = ratings_sorted[n // 2] if n % 2 else (ratings_sorted[n // 2 - 1] + ratings_sorted[n // 2]) / 2

    print(f"\n=== ELO pool stats (eligible OCE players) ===")
    print(f"  Players:            {n}")
    print(f"  Total ELO:          {total:,}")
    print(f"  Mean / Median:      {mean:.1f} / {median}")
    print(f"  Min / Max:          {min(ratings)} / {max(ratings)}")
    print(f"\n  Starting rating:    {starting_rating}")
    print(f"  Expected pool:      {expected:,}  (n * {starting_rating})")
    print(f"  Deviation:          {deviation:+,}  (actual - expected)")

    if deviation > 0:
        print(f"\n  -> {deviation:,} ELO net imported into OCE pool")
        print(f"     (overseas players losing on OCE server donate ELO in)")
    elif deviation < 0:
        print(f"\n  -> {-deviation:,} ELO net missing from OCE pool")
        print(f"     (could be: overseas players winning + leaving with ELO,")
        print(f"      OCE players we haven't discovered, or system decay)")
    else:
        print(f"\n  -> pool perfectly balanced (vanishingly unlikely, double-check inputs)")


def import_ids_from_file(text_path, players_path):
    """Extract every unique numeric ID from a messy text file and merge into players.json.

    Tolerates `ID\\tName`, `ID Name`, comma-thousands (`116,462`), and skips junk
    rows (`-`, `?`, `0`, `-------`, `REPLAQ`, blanks). The first whitespace-
    delimited token on each line is treated as the candidate ID.
    """
    text = Path(text_path).read_text(encoding="utf-8")
    extracted = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        token = line.split(None, 1)[0]
        digits = token.replace(",", "")
        if digits.isdigit() and int(digits) > 0:
            extracted.add(digits)

    existing = set()
    p = Path(players_path)
    if p.exists():
        existing = {str(i) for i in json.loads(p.read_text(encoding="utf-8"))}

    new = extracted - existing
    merged = existing | extracted
    save_known_ids(players_path, merged)

    print(f"Imported from {text_path}")
    print(f"  parsed:    {len(extracted)} unique IDs")
    print(f"  existing:  {len(existing)} already in {players_path}")
    print(f"  new:       {len(new)} added")
    print(f"  total:     {len(merged)} known IDs after merge")
    if new:
        sample = sorted(new, key=int)[:10]
        more = f" ... (+{len(new) - len(sample)} more)" if len(new) > len(sample) else ""
        print(f"  sample new IDs: {', '.join(sample)}{more}")
    print(f"\nNext: run `python oce_leaderboard.py --no-discover` to fetch + verify them.")


def build_and_write_outputs(cache, known_ids, new_ids, players_path, output_dir,
                            blocklist=None):
    """Prune IDs, build leaderboard, write CSV/HTML/raw_data."""
    output_dir = Path(output_dir)
    raw_path = output_dir / "raw_data.json"
    if blocklist is None:
        blocklist = set()

    # Pruning rules:
    #   * blocked IDs never reach players.json
    #   * keep IDs whose fetch failed this run (ranked/player is None) — retry next run
    #   * prune IDs where fetch succeeded but matches_played < MIN_RANKED_MATCHES
    #   * prune IDs that aren't OCE-majority (they pollute every future run otherwise)
    all_ids = [pid for pid in dict.fromkeys(known_ids + new_ids) if pid not in blocklist]
    save_ids = []
    for pid in all_ids:
        cached = cache.get(pid)
        if not cached:
            save_ids.append(pid)
            continue
        ranked = cached.get("ranked")
        player = cached.get("player")
        if ranked is None or player is None:
            save_ids.append(pid)
            continue
        if (ranked.get("matches_played") or 0) < MIN_RANKED_MATCHES:
            continue
        if not is_oce_majority(player):
            continue
        save_ids.append(pid)
    save_known_ids(players_path, save_ids)

    leaderboard = []
    for pid, data in cache.items():
        if pid in blocklist:
            continue
        pd = data.get("player") or {}
        r = data.get("ranked") or {}
        uname = (pd.get("username") or "").strip()
        if not uname:
            continue
        if not is_eligible(pd, r):
            continue
        recent = calc_recent_stats(pid, pd)
        leaderboard.append({
            "name": uname,
            "id": pid,
            "rank": (r.get("rank") or {}).get("name", ""),
            "rating": r.get("rating"),
            "matches_played": r.get("matches_played"),
            "highest_rank": (r.get("highest_rank") or {}).get("name", ""),
            "highest_rating": r.get("highest_rating"),
            "win_rate": recent["win_rate"],
            "goals_per_game": recent["goals_per_game"],
            "recent_games": recent["recent_games"],
            "wins": recent["wins"],
            "losses": recent["losses"],
        })
    leaderboard.sort(key=lambda x: (x["rating"] is None, -(x["rating"] or 0)))

    with open(output_dir / "full_leaderboard.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=[
            "position", "name", "rank", "rating", "matches_played",
            "highest_rank", "highest_rating", "win_rate", "goals_per_game", "id",
        ], extrasaction="ignore")
        w.writeheader()
        for i, p in enumerate(leaderboard, 1):
            w.writerow({"position": i, **p})

    atomic_write_json(raw_path, cache, indent=2)

    mtime = datetime.fromtimestamp(raw_path.stat().st_mtime)
    last_updated = f"{mtime.day} {mtime.strftime('%B %Y %H:%M')}"

    with open(output_dir / "leaderboard_detailed.html", "w", encoding="utf-8") as f:
        f.write(build_html(leaderboard, last_updated))

    with open(output_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(build_simple_html(leaderboard, last_updated))

    pruned = len(all_ids) - len(save_ids)
    print(f"\nDone! Written to {output_dir}/")
    print(f"  index.html                main leaderboard")
    print(f"  leaderboard_detailed.html detailed leaderboard with stats")
    print(f"  full_leaderboard.csv     {len(leaderboard)} players")
    print(f"  players.json             {len(save_ids)} known IDs ({len(new_ids)} new, {pruned} pruned)")


def block_id(pid, blocklist_path, players_path, output_dir):
    """Add `pid` to the blocklist, remove from players.json + raw_data.json, rebuild outputs."""
    pid = str(pid)
    blocked = load_blocklist(blocklist_path)
    if pid in blocked:
        print(f"id={pid} already in {blocklist_path}")
        return
    blocked.add(pid)
    save_blocklist(blocklist_path, blocked)
    print(f"Added id={pid} to {blocklist_path}")

    players_p = Path(players_path)
    if players_p.exists():
        existing = json.loads(players_p.read_text(encoding="utf-8"))
        if pid in {str(i) for i in existing}:
            save_known_ids(players_path, [i for i in existing if str(i) != pid])
            print(f"Removed id={pid} from {players_path}")

    raw_path = Path(output_dir) / "raw_data.json"
    if not raw_path.exists():
        print(f"No {raw_path} to clean. Done.")
        return
    with open(raw_path, encoding="utf-8") as f:
        cache = json.load(f)
    removed_from_cache = pid in cache
    if removed_from_cache:
        cached_player = (cache[pid].get("player") or {}).get("username", "?")
        del cache[pid]
        print(f"Removed id={pid} ({cached_player}) from {raw_path}; rebuilding outputs...")
    else:
        print(f"id={pid} was not in {raw_path}; rebuilding outputs to apply blocklist...")

    known_after = json.loads(Path(players_path).read_text(encoding="utf-8")) \
        if Path(players_path).exists() else []
    build_and_write_outputs(cache, [str(i) for i in known_after], [],
                            players_path, output_dir, blocklist=blocked)


def unblock_id(pid, blocklist_path):
    """Remove `pid` from the blocklist. Does not refetch — run a full update afterwards if needed."""
    pid = str(pid)
    blocked = load_blocklist(blocklist_path)
    if pid not in blocked:
        print(f"id={pid} not in {blocklist_path}")
        return
    blocked.discard(pid)
    save_blocklist(blocklist_path, blocked)
    print(f"Removed id={pid} from {blocklist_path}.")
    print(f"Run `py oce_leaderboard.py` (or --quick) to re-fetch them if they should be on the board.")


def run(players_path, output_dir, discover=True, max_discovery=DEFAULT_MAX_DISCOVERY,
        resume=False, deep_search=False, delay=DELAY_SECONDS, blocklist_path="blocked_ids.json"):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_path = output_dir / "raw_data.json"
    known_ids = load_known_ids(players_path, raw_path)
    blocklist = load_blocklist(blocklist_path)
    if blocklist:
        before = len(known_ids)
        known_ids = [pid for pid in known_ids if pid not in blocklist]
        if len(known_ids) < before:
            print(f"Blocklist filtered {before - len(known_ids)} ID(s) from known list.")

    rate = RateTracker(delay=delay)
    session = requests.Session()

    cache = {}
    if resume and raw_path.exists():
        with open(raw_path) as f:
            cache = json.load(f)
        for pid in blocklist & set(cache.keys()):
            del cache[pid]
        print(f"Resumed cache: {len(cache)} IDs already fetched\n")

    # ---- Phase 1: fetch all known IDs ----
    print(f"\n=== Phase 1: fetching {len(known_ids)} known IDs ===")
    for i, pid in enumerate(known_ids, 1):
        if pid in cache:
            print(f"[{i}/{len(known_ids)}] id={pid} (cached)")
            continue
        print(f"[{i}/{len(known_ids)}] id={pid}")
        fetch_player(pid, rate, session, cache)

    # ---- Phase 2: BFS discovery ----
    new_ids = []
    if discover:
        mode = "deep (all OCE S3 modes)" if deep_search else "standard (OCE casual S3)"
        print(f"\n=== Phase 2: BFS discovery [{mode}] (cap: {max_discovery}) ===")
        new_ids = discover_bfs(known_ids, cache, rate, session, max_discovery,
                               deep_search, blocklist=blocklist)

    build_and_write_outputs(cache, known_ids, new_ids, players_path, output_dir,
                            blocklist=blocklist)


def run_quick(seed_id, players_path, output_dir, deep_search=False,
              delay=DELAY_SECONDS, max_discovery=DEFAULT_MAX_DISCOVERY,
              blocklist_path="blocked_ids.json", depth=1):
    """Targeted refresh seeded from a single player ID.

    Skips full Phase 1; force-refreshes the seed and everyone in the seed's
    recent qualifying matches (so their ELOs are current); then BFS-walks
    those participants' histories to discover any new players. Cheap update
    after a session of games when only a handful of players' ratings moved.

    `depth` controls how far the refresh reaches. depth=1 (default) only
    refreshes seed + direct opponents; the BFS phase only fetches genuinely
    new players. depth=2 also force-refreshes known players one hop further
    (opponents-of-opponents); depth=3 keeps going. Each extra level costs
    more API calls.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw_data.json"

    known_ids = load_known_ids(players_path, raw_path)
    blocklist = load_blocklist(blocklist_path)
    known_ids = [pid for pid in known_ids if pid not in blocklist]

    seed_id = str(seed_id)
    if seed_id in blocklist:
        print(f"  ! seed id={seed_id} is in blocklist, aborting")
        return

    cache = {}
    if raw_path.exists():
        with open(raw_path) as f:
            cache = json.load(f)
        for pid in blocklist & set(cache.keys()):
            del cache[pid]
        print(f"Loaded cache: {len(cache)} IDs\n")

    rate = RateTracker(delay=delay)
    session = requests.Session()

    print(f"=== Quick refresh seeded from id={seed_id} ===\n")

    seed_data = fetch_player(seed_id, rate, session, cache, force=True)
    if not seed_data.get("player"):
        print(f"  ! couldn't fetch seed player {seed_id}, aborting")
        return
    seed_uname = (seed_data["player"] or {}).get("username", "?")
    print(f"Seed: {seed_uname}\n")

    match_filter = is_oce_season3_match if deep_search else is_qualifying_match
    participants = set()
    seen_matches = set()
    for match in (seed_data["player"].get("match_history") or []):
        if not match_filter(match):
            continue
        match_id = match.get("id") or match.get("match_id")
        if match_id is not None:
            seen_matches.add(match_id)
        for pid in extract_match_player_ids(match):
            if pid.isdigit() and pid not in blocklist:
                participants.add(pid)
    participants.discard(seed_id)

    if not participants:
        print(f"No qualifying matches in seed's history — nothing to refresh.\n"
              f"(Try --deep-search if you want customs included.)")
        build_and_write_outputs(cache, known_ids, [], players_path, output_dir,
                                blocklist=blocklist)
        return

    print(f"Found {len(participants)} unique participants in seed's qualifying matches\n")

    known_set = set(known_ids)
    print(f"=== Refreshing participants ===")
    for i, pid in enumerate(sorted(participants, key=int), 1):
        tag = "(known)" if pid in known_set else "(NEW)"
        print(f"[{i}/{len(participants)}] id={pid} {tag}")
        fetch_player(pid, rate, session, cache, force=True)

    refresh_through = max(0, depth - 1)
    refresh_note = (f", refresh known up to {refresh_through} hop(s) deeper"
                    if refresh_through > 0 else "")
    print(f"\n=== BFS from seed's matches (cap: {max_discovery}{refresh_note}) ===")
    bfs_new = discover_bfs(participants, cache, rate, session, max_discovery,
                           deep_search, seen_matches=seen_matches, blocklist=blocklist,
                           refresh_known_through_depth=refresh_through)

    # Anything we touched that isn't already known and is eligible should be saved.
    new_ids = []
    seen_pids = set()
    for pid in [seed_id] + sorted(participants, key=int) + bfs_new:
        if pid in known_set or pid in seen_pids or pid in blocklist:
            continue
        seen_pids.add(pid)
        data = cache.get(pid, {})
        if is_eligible(data.get("player"), data.get("ranked")):
            new_ids.append(pid)

    build_and_write_outputs(cache, known_ids, new_ids, players_path, output_dir,
                            blocklist=blocklist)


# ---- CLI ----

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--players", default="players.json",
                        help="Path to the player ID list (default: players.json)")
    parser.add_argument("--output", default="docs",
                        help="Directory to write outputs to (default: docs)")
    parser.add_argument("--no-discover", action="store_true",
                        help="Skip Phase 2 BFS discovery")
    parser.add_argument("--max-discovery", type=int, default=DEFAULT_MAX_DISCOVERY,
                        help=f"Cap on discovered players (default: {DEFAULT_MAX_DISCOVERY})")
    parser.add_argument("--delay", type=float, default=DELAY_SECONDS,
                        help=f"Seconds between requests (default: {DELAY_SECONDS})")
    parser.add_argument("--resume", action="store_true",
                        help="Load docs/raw_data.json and skip already-fetched IDs")
    parser.add_argument("--deep-search", action="store_true",
                        help="BFS follows all OCE S3 match types (not just casual) to find players in customs")
    parser.add_argument("--quick", metavar="ID", default=None,
                        help="Quick refresh: skip Phase 1; force-refresh seed ID + everyone in their "
                             "recent qualifying matches; BFS-walk those histories for new players. "
                             "Use this after a play session when only a few ratings have moved.")
    parser.add_argument("--quick-depth", type=int, default=1,
                        help="With --quick, refresh known players within N hops of seed "
                             "(default 1 = seed + direct opponents only; 2 = also "
                             "opponents-of-opponents; etc.). Higher = more API calls.")
    parser.add_argument("--import-ids", metavar="FILE", default=None,
                        help="Parse a messy text file for numeric player IDs and merge them into "
                             "players.json. Skips non-numeric tokens, strips comma-thousands. "
                             "Run a normal update afterwards to fetch + verify.")
    parser.add_argument("--stats", action="store_true",
                        help="Print ELO pool stats from cache and exit (no fetching).")
    parser.add_argument("--blocklist", default="blocked_ids.json",
                        help="Path to manually-excluded IDs (default: blocked_ids.json). "
                             "These are filtered out of every run.")
    parser.add_argument("--block", metavar="ID", default=None,
                        help="Add ID to the blocklist, remove from players.json + raw_data.json, "
                             "and rebuild outputs. Use for overseas players who slip past "
                             "the OCE-majority rule.")
    parser.add_argument("--unblock", metavar="ID", default=None,
                        help="Remove ID from the blocklist.")
    args = parser.parse_args()

    if args.stats:
        show_stats(args.output)
        raise SystemExit(0)

    if args.import_ids:
        import_ids_from_file(args.import_ids, args.players)
        raise SystemExit(0)

    if args.block:
        block_id(args.block, args.blocklist, args.players, args.output)
        raise SystemExit(0)

    if args.unblock:
        unblock_id(args.unblock, args.blocklist)
        raise SystemExit(0)

    if args.quick:
        run_quick(args.quick, args.players, args.output,
                  deep_search=args.deep_search,
                  delay=args.delay,
                  max_discovery=args.max_discovery,
                  blocklist_path=args.blocklist,
                  depth=args.quick_depth)
    else:
        run(args.players, args.output,
            discover=not args.no_discover,
            max_discovery=args.max_discovery,
            resume=args.resume,
            deep_search=args.deep_search,
            delay=args.delay,
            blocklist_path=args.blocklist)
