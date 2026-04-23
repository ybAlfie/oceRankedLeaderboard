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
    python oce_leaderboard.py --max-discovery 50 # cap BFS growth

Outputs (in output_dir/):
    - leaderboard.html       interactive leaderboard, open in browser
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
MIN_OCE_SEASON3_MATCHES = 2   # Min OCE Season 3 matches to keep a discovered player

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://slapshot.gg/",
}


# ---- HTTP with rate limit awareness ----

class RateTracker:
    def __init__(self):
        self.remaining = None
        self.reset_at = None

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


def fetch_json(url, rate, session):
    rate.wait_if_needed()
    time.sleep(DELAY_SECONDS)

    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
    except requests.RequestException as e:
        print(f"   ! network error on {url}: {e}")
        return None

    rate.update(resp)

    if resp.status_code == 429:
        print("   ! 429 Too Many Requests, backing off 60s")
        time.sleep(60)
        return fetch_json(url, rate, session)

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


def count_oce_season3_matches(player_data):
    """Count OCE Season 3 matches (any mode) to gauge if a player is OCE-based."""
    return sum(
        1 for m in (player_data or {}).get("match_history", []) or []
        if m.get("region") == OCE_REGION
        and (m.get("created") or "") >= SEASON3_START
    )


def fetch_player(pid, rate, session, cache):
    if pid in cache:
        return cache[pid]
    player = get_player(pid, rate, session)
    ranked = get_ranked(pid, rate, session)
    cache[pid] = {"player": player, "ranked": ranked}
    return cache[pid]


# ---- HTML output ----

def build_html(players):
    data_json = json.dumps([
        {
            "pos": i + 1,
            "name": p["name"],
            "rank": p["rank"] or "",
            "rating": p["rating"] if p["rating"] is not None else "",
            "matches": p["matches_played"] if p["matches_played"] is not None else 0,
            "highest_rank": p["highest_rank"] or "",
            "highest_rating": p["highest_rating"] if p["highest_rating"] is not None else "",
            "id": p["id"],
        }
        for i, p in enumerate(players)
    ], ensure_ascii=False)

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
<div class="controls">
  <input class="search" type="text" placeholder="Search name..." id="search">
  <button class="toggle" id="toggleZero">Hide 0 matches</button>
  <button class="toggle" id="toggleUnranked">Hide unranked</button>
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
let hideZero = false, hideUnranked = false, searchTerm = '';

function filtered() {{
  return ALL.filter(p => {{
    if (hideZero && p.matches === 0) return false;
    if (hideUnranked && (p.rank === 'Unranked' || p.rank === '')) return false;
    if (searchTerm && !p.name.toLowerCase().includes(searchTerm)) return false;
    return true;
  }});
}}

function sorted(rows) {{
  return [...rows].sort((a, b) => {{
    let av = a[sortCol], bv = b[sortCol];
    if (sortCol === 'rank' || sortCol === 'highest_rank') {{
      av = rankVal(av); bv = rankVal(bv);
    }} else if (typeof av === 'string') {{
      av = av.toLowerCase(); bv = bv.toLowerCase();
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
  hideUnranked = !hideUnranked; this.classList.toggle('active', hideUnranked); render();
}});
document.getElementById('search').addEventListener('input', function() {{
  searchTerm = this.value.toLowerCase(); render();
}});

render();
</script>
</body>
</html>"""


def build_simple_html(players, last_updated=None):
    RANK_IMG = {
        'Platinum III': 'ranks/plat_iii.png', 'Platinum II': 'ranks/plat_ii.png',
        'Platinum I':   'ranks/plat_i.png',
        'Gold III':     'ranks/gold_iii.png',  'Gold II':  'ranks/gold_ii.png',
        'Gold I':       'ranks/gold_i.png',
        'Silver III':   'ranks/silver_iii.png','Silver II':'ranks/silver_ii.png',
        'Silver I':     'ranks/silver_i.png',
        'Bronze III':   'ranks/bronze_iii.png','Bronze II':'ranks/bronze_ii.png',
        'Bronze I':     'ranks/bronze_i.png',
        'Unranked':     'ranks/unranked.png',
    }
    RANK_COLOR = {
        'Platinum III': '#a78bfa', 'Platinum II': '#a78bfa', 'Platinum I': '#a78bfa',
        'Gold III':     '#f5b731', 'Gold II':     '#f5b731', 'Gold I':     '#f5b731',
        'Silver III':   '#a8bdd0', 'Silver II':   '#a8bdd0', 'Silver I':   '#a8bdd0',
        'Bronze III':   '#cd7f32', 'Bronze II':   '#cd7f32', 'Bronze I':   '#cd7f32',
        'Unranked':     '#7a8da6',
    }

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
    <p class="subtitle">Season 3 &mdash; oce-east</p>
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
    <span class="last-updated">Last Updated: {last_updated}</span>
  </div>
  <table id="lb">
    <tbody>
{rows_html}
    </tbody>
  </table>
</div>
<script>
const RANK_ORDER = [
  'Platinum III','Platinum II','Platinum I',
  'Gold III','Gold II','Gold I',
  'Silver III','Silver II','Silver I',
  'Bronze III','Bronze II','Bronze I',
  'Unranked',''
];
const RANK_IMG = {{
  'Platinum III':'ranks/plat_iii.png','Platinum II':'ranks/plat_ii.png','Platinum I':'ranks/plat_i.png',
  'Gold III':'ranks/gold_iii.png','Gold II':'ranks/gold_ii.png','Gold I':'ranks/gold_i.png',
  'Silver III':'ranks/silver_iii.png','Silver II':'ranks/silver_ii.png','Silver I':'ranks/silver_i.png',
  'Bronze III':'ranks/bronze_iii.png','Bronze II':'ranks/bronze_ii.png','Bronze I':'ranks/bronze_i.png',
  'Unranked':'ranks/unranked.png',
}};
const RANK_COLOR = {{
  'Platinum III':'#a78bfa','Platinum II':'#a78bfa','Platinum I':'#a78bfa',
  'Gold III':'#f5b731','Gold II':'#f5b731','Gold I':'#f5b731',
  'Silver III':'#a8bdd0','Silver II':'#a8bdd0','Silver I':'#a8bdd0',
  'Bronze III':'#cd7f32','Bronze II':'#cd7f32','Bronze I':'#cd7f32',
  'Unranked':'#7a8da6',
}};
const PODIUM = ['top-1','top-2','top-3'];

const rankVal = r => {{ const i = RANK_ORDER.indexOf(r); return i === -1 ? 99 : i; }};
const rankCellHtml = r => {{
  const img = RANK_IMG[r] ? `<img src="${{RANK_IMG[r]}}" alt="${{r}}">` : '';
  const color = RANK_COLOR[r] || '#7a8da6';
  return `${{img}}<span style="color:${{color}}">${{r || 'Unranked'}}</span>`;
}};

const search       = document.getElementById('search');
const hideUnranked = document.getElementById('hideUnranked');
const sortPeak     = document.getElementById('sortPeak');
const tbody        = document.querySelector('#lb tbody');
const allRows      = Array.from(tbody.querySelectorAll('tr'));

function update() {{
  const term      = search.value.toLowerCase();
  const noUnranked = hideUnranked.checked;
  const byPeak    = sortPeak.checked;

  let visible = allRows.filter(row => {{
    const name = row.querySelector('td.name').textContent.toLowerCase();
    const chkRank = byPeak ? row.dataset.highestRank : row.dataset.rank;
    const isUnranked = chkRank === 'Unranked' || chkRank === '';
    return name.includes(term) && !(noUnranked && isUnranked);
  }});

  if (byPeak) {{
    visible.sort((a, b) => {{
      const rv = rankVal(a.dataset.highestRank) - rankVal(b.dataset.highestRank);
      return rv !== 0 ? rv : Number(b.dataset.highestRating) - Number(a.dataset.highestRating);
    }});
  }} else {{
    visible.sort((a, b) => Number(b.dataset.rating) - Number(a.dataset.rating));
  }}

  // reorder DOM
  allRows.forEach(r => {{ r.style.display = 'none'; PODIUM.forEach(c => r.classList.remove(c)); }});
  visible.forEach((r, i) => {{
    tbody.appendChild(r);
    r.style.display = '';
    r.querySelector('td.pos').textContent = i + 1;
    if (i < 3) r.classList.add(PODIUM[i]);
    // swap rank cell and rating
    const rank  = byPeak ? r.dataset.highestRank  : r.dataset.rank;
    const rating = byPeak ? r.dataset.highestRating : r.dataset.rating;
    r.querySelector('td.rank-cell').innerHTML = rankCellHtml(rank);
    r.querySelector('td.rating').textContent  = rating || '—';
  }});
}}

search.addEventListener('input', update);
hideUnranked.addEventListener('change', update);
sortPeak.addEventListener('change', update);
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


def save_known_ids(players_path, ids):
    Path(players_path).write_text(
        json.dumps(sorted(ids, key=int), indent=2), encoding="utf-8"
    )


def run(players_path, output_dir, discover=True, max_discovery=DEFAULT_MAX_DISCOVERY, resume=False):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_path = output_dir / "raw_data.json"
    known_ids = load_known_ids(players_path, raw_path)

    rate = RateTracker()
    session = requests.Session()

    cache = {}
    if resume and raw_path.exists():
        with open(raw_path) as f:
            cache = json.load(f)
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
        print(f"\n=== Phase 2: BFS discovery (cap: {max_discovery}) ===")
        visited = set(cache.keys())
        queue = deque(known_ids)

        while queue and len(new_ids) < max_discovery:
            pid = queue.popleft()
            player_data = cache.get(pid, {}).get("player")
            if not player_data:
                continue

            for match in player_data.get("match_history", []) or []:
                if not is_qualifying_match(match):
                    continue
                for new_id in extract_match_player_ids(match):
                    if new_id in visited or new_id.startswith("bot-"):
                        continue
                    visited.add(new_id)
                    data = fetch_player(new_id, rate, session, cache)
                    pd = (data.get("player") or {})
                    if count_oce_season3_matches(pd) < MIN_OCE_SEASON3_MATCHES:
                        continue
                    new_ids.append(new_id)
                    print(f"[discover {len(new_ids)}] id={new_id} ({pd.get('username', '?')})")
                    queue.append(new_id)
                    if len(new_ids) >= max_discovery:
                        break
                if len(new_ids) >= max_discovery:
                    break

    # ---- Save updated ID list ----
    all_ids = list(dict.fromkeys(known_ids + new_ids))
    save_known_ids(players_path, all_ids)

    # ---- Build leaderboard ----
    leaderboard = []
    for pid, data in cache.items():
        pd = data.get("player") or {}
        r = data.get("ranked") or {}
        uname = (pd.get("username") or "").strip()
        if not uname:
            continue
        leaderboard.append({
            "name": uname,
            "id": pid,
            "rank": (r.get("rank") or {}).get("name", ""),
            "rating": r.get("rating"),
            "matches_played": r.get("matches_played"),
            "highest_rank": (r.get("highest_rank") or {}).get("name", ""),
            "highest_rating": r.get("highest_rating"),
        })
    leaderboard.sort(key=lambda x: (x["rating"] is None, -(x["rating"] or 0)))

    with open(output_dir / "full_leaderboard.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=[
            "position", "name", "rank", "rating", "matches_played",
            "highest_rank", "highest_rating", "id",
        ])
        w.writeheader()
        for i, p in enumerate(leaderboard, 1):
            w.writerow({"position": i, **p})

    with open(raw_path, "w") as f:
        json.dump(cache, f, indent=2)

    with open(output_dir / "leaderboard.html", "w", encoding="utf-8") as f:
        f.write(build_html(leaderboard))

    with open(output_dir / "leaderboard_simple.html", "w", encoding="utf-8") as f:
        mtime = datetime.fromtimestamp(raw_path.stat().st_mtime)
        last_updated = f"{mtime.day} {mtime.strftime('%B %Y')}"
        f.write(build_simple_html(leaderboard, last_updated))

    print(f"\nDone! Written to {output_dir}/")
    print(f"  leaderboard.html         interactive leaderboard")
    print(f"  leaderboard_simple.html  simplified display leaderboard")
    print(f"  full_leaderboard.csv     {len(leaderboard)} players")
    print(f"  players.json             {len(all_ids)} known IDs ({len(new_ids)} new this run)")


# ---- CLI ----

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--players", default="players.json",
                        help="Path to the player ID list (default: players.json)")
    parser.add_argument("--output", default="output",
                        help="Directory to write outputs to (default: output)")
    parser.add_argument("--no-discover", action="store_true",
                        help="Skip Phase 2 BFS discovery")
    parser.add_argument("--max-discovery", type=int, default=DEFAULT_MAX_DISCOVERY,
                        help=f"Cap on discovered players (default: {DEFAULT_MAX_DISCOVERY})")
    parser.add_argument("--delay", type=float, default=DELAY_SECONDS,
                        help=f"Seconds between requests (default: {DELAY_SECONDS})")
    parser.add_argument("--resume", action="store_true",
                        help="Load output/raw_data.json and skip already-fetched IDs")
    args = parser.parse_args()

    DELAY_SECONDS = args.delay
    run(args.players, args.output,
        discover=not args.no_discover,
        max_discovery=args.max_discovery,
        resume=args.resume)
