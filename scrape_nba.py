"""
ParlAID — NBA Smart Tips Scraper v3
====================================
Features:
  1. FATIGUE: Back-to-back & 3-in-4 detection via nba_api (data.nba.com)
  2. LINE MOVEMENT: Compares current odds to previous snapshot in data.json
  3. RESULTS GRADING: Auto-grades yesterday's picks using Odds API scores endpoint
  4. CONTEXTUAL EDGE: Structured edge object written per game
  5. TRACK RECORD: Maintains rolling results.json with win/loss/ROI

pip install requests beautifulsoup4 feedparser nba_api
Secrets: ODDS_API_KEY, NEWS_API_KEY
"""

import json, os, time, re, requests, feedparser
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

try:
    from nba_api.stats.endpoints import leaguegamefinder
    from nba_api.stats.static import teams as nba_teams_static
    NBA_API_AVAILABLE = True
except ImportError:
    NBA_API_AVAILABLE = False
    print("[WARN] nba_api not installed — fatigue from schedule only.")

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

NBA_TEAM_MAP = {
    "Atlanta Hawks":"ATL","Boston Celtics":"BOS","Brooklyn Nets":"BKN",
    "Charlotte Hornets":"CHA","Chicago Bulls":"CHI","Cleveland Cavaliers":"CLE",
    "Dallas Mavericks":"DAL","Denver Nuggets":"DEN","Detroit Pistons":"DET",
    "Golden State Warriors":"GSW","Houston Rockets":"HOU","Indiana Pacers":"IND",
    "LA Clippers":"LAC","Los Angeles Clippers":"LAC","Los Angeles Lakers":"LAL",
    "LA Lakers":"LAL","Memphis Grizzlies":"MEM","Miami Heat":"MIA",
    "Milwaukee Bucks":"MIL","Minnesota Timberwolves":"MIN","New Orleans Pelicans":"NOP",
    "New York Knicks":"NYK","Oklahoma City Thunder":"OKC","Orlando Magic":"ORL",
    "Philadelphia 76ers":"PHI","Phoenix Suns":"PHX","Portland Trail Blazers":"POR",
    "Sacramento Kings":"SAC","San Antonio Spurs":"SAS","Toronto Raptors":"TOR",
    "Utah Jazz":"UTA","Washington Wizards":"WAS",
}
# Reverse map: abbr -> full name
ABBR_TO_NAME = {v: k for k, v in NBA_TEAM_MAP.items() if len(k) > 6}

# Team time zones (for travel fatigue)
TEAM_TIMEZONE = {
    "BOS":"ET","NYK":"ET","PHI":"ET","BKN":"ET","TOR":"ET",
    "MIA":"ET","ATL":"ET","CHA":"ET","ORL":"ET","WAS":"ET",
    "CHI":"CT","MIL":"CT","IND":"CT","CLE":"ET","DET":"ET",
    "MEM":"CT","NOP":"CT","SAS":"CT","HOU":"CT","DAL":"CT",
    "DEN":"MT","UTA":"MT","OKC":"CT","MIN":"CT",
    "LAL":"PT","LAC":"PT","GSW":"PT","SAC":"PT","PHX":"MT",
    "POR":"PT",
}
WEST_TEAMS = {"LAL","LAC","GSW","SAC","PHX","DEN","UTA","OKC","MIN","DAL","HOU","SAS","NOP","MEM","POR"}
EAST_TEAMS = {"BOS","NYK","PHI","BKN","TOR","MIA","ATL","CHA","ORL","WAS","CHI","MIL","IND","CLE","DET"}

FEED_SOURCES = [
    "https://www.espn.com/espn/rss/nba/news",
    "https://bleacherreport.com/nba.rss",
    "https://www.sportskeeda.com/basketball/feed",
]

# ─────────────────────────────────────────────────────────────────────────────
# 1. FATIGUE: Build per-team recent schedule from nba_api
# ─────────────────────────────────────────────────────────────────────────────
def build_fatigue_map():
    """
    Returns dict: {team_abbr: {
        'status': 'B2B' | '3IN4' | 'FRESH',
        'label': human readable,
        'battery': 0-100 (100=fresh),
        'last_game_date': 'YYYY-MM-DD',
        'games_last_4_days': int,
        'road_streak': int,  # consecutive road games
    }}
    """
    fatigue = {}
    today = datetime.now(timezone.utc).date()

    if not NBA_API_AVAILABLE:
        print("[WARN] nba_api unavailable — using ESPN schedule fallback for fatigue.")
        return build_fatigue_from_espn()

    try:
        # Pull last 10 days of games for all teams
        date_from = (today - timedelta(days=10)).strftime("%m/%d/%Y")
        date_to   = today.strftime("%m/%d/%Y")

        finder = leaguegamefinder.LeagueGameFinder(
            date_from_nullable=date_from,
            date_to_nullable=date_to,
            league_id_nullable="00",
            timeout=30,
        )
        df = finder.get_data_frames()[0]
        df["GAME_DATE"] = df["GAME_DATE"].apply(lambda x: datetime.strptime(x, "%Y-%m-%dT%H:%M:%S").date() if "T" in x else datetime.strptime(x, "%Y-%m-%d").date())

        for abbr in NBA_TEAM_MAP.values():
            team_df = df[df["TEAM_ABBREVIATION"] == abbr].sort_values("GAME_DATE", ascending=False)
            if team_df.empty:
                fatigue[abbr] = _fresh_fatigue()
                continue

            dates = list(team_df["GAME_DATE"])
            matchups = list(team_df["MATCHUP"])  # e.g. "LAL vs. BOS" or "LAL @ BOS"

            last_game = dates[0]
            days_since_last = (today - last_game).days

            # Count games in last 4 days (including today)
            g4 = sum(1 for d in dates if (today - d).days <= 4)
            # Count games in last 2 days
            g2 = sum(1 for d in dates if (today - d).days <= 2)

            # Road streak: count consecutive away games
            road_streak = 0
            for m in matchups:
                if "@" in m and not m.startswith(abbr):
                    road_streak += 1
                else:
                    break

            # Cross-timezone trip: West team playing East or vice versa
            cross_trip = False
            if len(matchups) >= 2:
                last_opp = matchups[0]
                # crude: if away team is in opposite conference
                opp_abbr = last_opp.split()[-1] if "@" in last_opp else None
                if opp_abbr:
                    if abbr in WEST_TEAMS and opp_abbr in EAST_TEAMS:
                        cross_trip = True
                    elif abbr in EAST_TEAMS and opp_abbr in WEST_TEAMS:
                        cross_trip = True

            # Determine status
            if days_since_last == 1:
                status = "B2B"
                label  = "Back-to-Back ⚠️"
                battery = 25
            elif g4 >= 3:
                status = "3IN4"
                label  = "3rd in 4 nights 🔴"
                battery = 40
            elif days_since_last == 2 and cross_trip:
                status = "TRAVEL"
                label  = "Cross-country travel fatigue"
                battery = 55
            elif days_since_last <= 2:
                status = "SOME_REST"
                label  = "1 day rest"
                battery = 70
            else:
                status = "FRESH"
                label  = "Well rested ✅"
                battery = 100

            fatigue[abbr] = {
                "status":           status,
                "label":            label,
                "battery":          battery,
                "last_game_date":   last_game.strftime("%b %d"),
                "games_last_4d":    g4,
                "road_streak":      road_streak,
                "cross_trip":       cross_trip,
            }

        print(f"[INFO] Fatigue map built for {len(fatigue)} teams via nba_api.")
    except Exception as e:
        print(f"[WARN] nba_api fatigue failed: {e}. Using ESPN fallback.")
        return build_fatigue_from_espn()

    return fatigue


def build_fatigue_from_espn():
    """Fallback: scrape ESPN schedule page to detect B2B manually."""
    fatigue = {}
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)

    try:
        # ESPN schedule for yesterday and today
        for d in [yesterday, today - timedelta(days=2)]:
            url = f"https://www.espn.com/nba/scoreboard/_/date/{d.strftime('%Y%m%d')}"
            r = requests.get(url, headers=HEADERS, timeout=10)
            soup = BeautifulSoup(r.content, "html.parser")
            # Look for team abbreviations in completed games
            for event in soup.select(".ScoreboardScoreCell__Item"):
                abbr_el = event.select_one(".ScoreboardScoreCell__TeamName--abbrev")
                if abbr_el:
                    abbr = abbr_el.get_text(strip=True)
                    days_ago = (today - d).days
                    if abbr not in fatigue:
                        fatigue[abbr] = {"last_game_date": d.strftime("%b %d"), "days_since": days_ago}
                    # update to most recent
                    elif fatigue[abbr].get("days_since", 99) > days_ago:
                        fatigue[abbr]["days_since"] = days_ago
                        fatigue[abbr]["last_game_date"] = d.strftime("%b %d")
    except Exception as e:
        print(f"[WARN] ESPN schedule fallback: {e}")

    # Now build full fatigue objects
    result = {}
    for abbr, info in fatigue.items():
        ds = info.get("days_since", 99)
        if ds <= 1:
            result[abbr] = {"status":"B2B","label":"Back-to-Back ⚠️","battery":25,
                            "last_game_date":info["last_game_date"],"games_last_4d":2,"road_streak":0,"cross_trip":False}
        elif ds <= 2:
            result[abbr] = {"status":"SOME_REST","label":"1 day rest","battery":70,
                            "last_game_date":info["last_game_date"],"games_last_4d":1,"road_streak":0,"cross_trip":False}
        else:
            result[abbr] = _fresh_fatigue(info["last_game_date"])

    # Fill missing teams
    for abbr in NBA_TEAM_MAP.values():
        if abbr not in result:
            result[abbr] = _fresh_fatigue()
    return result


def _fresh_fatigue(last_date=None):
    return {"status":"FRESH","label":"Well rested ✅","battery":100,
            "last_game_date":last_date or "—","games_last_4d":0,"road_streak":0,"cross_trip":False}


# ─────────────────────────────────────────────────────────────────────────────
# 2. ODDS & LINE MOVEMENT
# ─────────────────────────────────────────────────────────────────────────────
def ml_to_prob(ml):
    try:
        ml = int(str(ml).replace("+","").replace(" ",""))
        return abs(ml)/(abs(ml)+100)*100 if ml<0 else 100/(ml+100)*100
    except: return 50.0

def remove_vig(p1, p2):
    t = p1+p2
    if t==0: return 50.0,50.0
    return round(p1/t*100,1), round(p2/t*100,1)

def confidence_from_prob(p):
    if p>=72: return "HIGH","★★★"
    if p>=58: return "MEDIUM","★★☆"
    return "LOW","★☆☆"

def fetch_odds():
    if not ODDS_API_KEY:
        print("[WARN] No ODDS_API_KEY."); return []
    url = (
        "https://api.the-odds-api.com/v4/sports/basketball_nba/odds/"
        f"?apiKey={ODDS_API_KEY}&regions=us&markets=h2h,spreads"
        "&oddsFormat=american&bookmakers=draftkings"
    )
    try:
        r = requests.get(url, timeout=12); r.raise_for_status()
        d = r.json(); print(f"[INFO] Odds API: {len(d)} games."); return d
    except Exception as e:
        print(f"[ERROR] Odds API: {e}"); return []

def load_previous_odds():
    """Load previous data.json to extract previous ML for movement detection."""
    prev = {}
    try:
        with open("data.json") as f:
            old = json.load(f)
        for g in old.get("games", []):
            key = g.get("game","")
            prev[key] = {
                "home_ml": g.get("home_ml","N/A"),
                "away_ml": g.get("away_ml","N/A"),
                "home_ml_raw": _parse_ml_raw(g.get("home_ml","N/A")),
                "away_ml_raw": _parse_ml_raw(g.get("away_ml","N/A")),
                "snapshot_time": old.get("last_updated",""),
            }
    except: pass
    return prev

def _parse_ml_raw(s):
    try: return int(str(s).replace("+","").replace(" ",""))
    except: return None

def classify_line_movement(old_ml, new_ml, team_is_pick):
    """
    Returns dict: {direction, points_moved, steam, reverse_line_movement, label}
    Steam = big sharp move (>=8 points on ML)
    Reverse line movement = public betting one way, line moving opposite
    """
    if old_ml is None or new_ml is None:
        return {"direction":"none","points_moved":0,"steam":False,"rlm":False,"label":"—"}

    moved = new_ml - old_ml  # positive = line got worse for this team (became more expensive favourite OR worse dog)

    if moved == 0:
        return {"direction":"none","points_moved":0,"steam":False,"rlm":False,"label":"No movement"}

    # For favourites (negative ML): line moving MORE negative = sharps backing them
    # For dogs (positive ML): line moving MORE positive = sharps backing them
    steam = abs(moved) >= 8
    direction = "shorter" if moved < 0 else "longer"  # shorter = team becoming bigger fav

    label_parts = []
    if abs(moved) > 0:
        prefix = "🟢" if (moved < 0 and team_is_pick) or (moved > 0 and not team_is_pick) else "🔴"
        old_fmt = f"+{old_ml}" if old_ml > 0 else str(old_ml)
        new_fmt = f"+{new_ml}" if new_ml > 0 else str(new_ml)
        label_parts.append(f"{prefix} {new_fmt} ← {old_fmt}")

    if steam:
        label_parts.append("🔥 STEAM")

    return {
        "direction":    direction,
        "points_moved": abs(moved),
        "steam":        steam,
        "rlm":          False,  # needs public % data (not free) — left for future
        "label":        " ".join(label_parts) if label_parts else "No movement",
        "old_ml":       f"+{old_ml}" if old_ml and old_ml > 0 else str(old_ml) if old_ml else "—",
        "new_ml":       f"+{new_ml}" if new_ml and new_ml > 0 else str(new_ml) if new_ml else "—",
    }

def normalise_odds(raw, prev_odds, fatigue):
    games = []
    for g in raw:
        home = g.get("home_team",""); away = g.get("away_team","")
        home_abbr = NBA_TEAM_MAP.get(home, home[:3].upper())
        away_abbr = NBA_TEAM_MAP.get(away, away[:3].upper())

        home_ml_raw = away_ml_raw = None
        home_ml_fmt = away_ml_fmt = "N/A"
        spread_str = "N/A"

        for bm in g.get("bookmakers",[]):
            for mkt in bm.get("markets",[]):
                if mkt["key"]=="h2h":
                    for o in mkt.get("outcomes",[]):
                        p=o["price"]; fmt=f"+{p}" if p>0 else str(p)
                        if o["name"]==home: home_ml_raw=p; home_ml_fmt=fmt
                        elif o["name"]==away: away_ml_raw=p; away_ml_fmt=fmt
                elif mkt["key"]=="spreads":
                    for o in mkt.get("outcomes",[]):
                        if o["name"]==home:
                            pt=o["point"]; sign="+" if pt>0 else ""
                            spread_str=f"{home_abbr} {sign}{pt}"
            break

        if home_ml_raw is not None and away_ml_raw is not None:
            hp, ap = remove_vig(ml_to_prob(home_ml_raw), ml_to_prob(away_ml_raw))
        else: hp=ap=50.0

        if hp>=ap: pick=home; pick_abbr=home_abbr; pick_ml=home_ml_fmt; pick_prob=hp; pick_ml_raw=home_ml_raw
        else:      pick=away; pick_abbr=away_abbr; pick_ml=away_ml_fmt; pick_prob=ap; pick_ml_raw=away_ml_raw

        conf, stars = confidence_from_prob(pick_prob)
        game_key = f"{away} @ {home}"

        # Line movement
        prev = prev_odds.get(game_key, {})
        home_move = classify_line_movement(prev.get("home_ml_raw"), home_ml_raw, pick==home)
        away_move = classify_line_movement(prev.get("away_ml_raw"), away_ml_raw, pick==away)
        pick_move = home_move if pick==home else away_move

        # Fatigue for both teams
        home_fat = fatigue.get(home_abbr, _fresh_fatigue())
        away_fat = fatigue.get(away_abbr, _fresh_fatigue())

        # Contextual edge object
        edge_factors = []
        if pick_move["steam"]: edge_factors.append("🔥 Sharp steam on pick")
        if home_fat["status"] in ["B2B","3IN4"] and pick==away: edge_factors.append(f"💤 {home_abbr} on {home_fat['status']}")
        if away_fat["status"] in ["B2B","3IN4"] and pick==home: edge_factors.append(f"💤 {away_abbr} on {away_fat['status']}")
        if home_fat["cross_trip"] and pick==away: edge_factors.append(f"✈️ {home_abbr} on cross-country trip")
        if away_fat["cross_trip"] and pick==home: edge_factors.append(f"✈️ {away_abbr} cross-country away")
        if not edge_factors: edge_factors.append(f"📊 {round(pick_prob-round(100-pick_prob,1),1)}pt probability edge")

        games.append({
            "game": game_key, "home_team":home, "away_team":away,
            "home_abbr":home_abbr, "away_abbr":away_abbr,
            "home_ml":home_ml_fmt, "away_ml":away_ml_fmt,
            "home_ml_raw":home_ml_raw, "away_ml_raw":away_ml_raw,
            "home_prob":hp, "away_prob":ap,
            "spread":spread_str, "favorite":pick,
            "pick":pick, "pick_abbr":pick_abbr,
            "moneyline":pick_ml, "win_probability":pick_prob,
            "confidence":conf, "confidence_stars":stars,
            "commence_time":g.get("commence_time",""),
            "line_movement": {
                "home": home_move,
                "away": away_move,
                "pick": pick_move,
                "snapshot_time": prev.get("snapshot_time","—"),
            },
            "fatigue": {
                "home": home_fat,
                "away": away_fat,
            },
            "edge_factors": edge_factors,
        })
    return games


# ─────────────────────────────────────────────────────────────────────────────
# 3. RESULTS GRADING (auto-grade yesterday's picks)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_scores_for_date(date_str):
    """Fetch completed game scores from Odds API scores endpoint."""
    if not ODDS_API_KEY: return {}
    url = (f"https://api.the-odds-api.com/v4/sports/basketball_nba/scores/"
           f"?apiKey={ODDS_API_KEY}&daysFrom=2")
    try:
        r = requests.get(url, timeout=10); r.raise_for_status()
        scores = {}
        for g in r.json():
            if g.get("completed"):
                home = g["home_team"]; away = g["away_team"]
                key = f"{away} @ {home}"
                sc = g.get("scores") or []
                score_dict = {s["name"]: int(s["score"]) for s in sc if s.get("score")}
                scores[key] = {
                    "home": home, "away": away,
                    "home_score": score_dict.get(home, 0),
                    "away_score": score_dict.get(away, 0),
                    "completed": True,
                }
        return scores
    except Exception as e:
        print(f"[WARN] Scores fetch: {e}"); return {}

def load_results():
    try:
        with open("results.json") as f: return json.load(f)
    except: return {"picks": [], "summary": {}}

def save_results(results):
    with open("results.json","w") as f: json.dump(results, f, indent=2)

def grade_pending_picks(results, scores):
    """Grade any ungraded picks using completed scores."""
    changed = False
    for pick in results["picks"]:
        if pick.get("graded"): continue
        game = pick["game"]
        if game not in scores: continue
        sc = scores[game]
        winner = sc["home"] if sc["home_score"] > sc["away_score"] else sc["away"]
        pick_team = pick["pick"]
        pick_ml = pick.get("moneyline_raw", 0)

        won = (winner == pick_team)
        pick["result"] = "WIN" if won else "LOSS"
        pick["graded"] = True
        pick["home_score"] = sc["home_score"]
        pick["away_score"] = sc["away_score"]
        pick["actual_winner"] = winner

        # ROI: $100 flat bet
        if won:
            if pick_ml and pick_ml > 0:
                pick["profit"] = round(pick_ml, 2)
            else:
                pick["profit"] = round(100 / abs(pick_ml) * 100, 2) if pick_ml else 100
        else:
            pick["profit"] = -100

        changed = True

    if changed:
        recalc_summary(results)
    return changed

def recalc_summary(results):
    """Recalculate win rates and ROI by confidence tier."""
    from collections import defaultdict
    buckets = defaultdict(lambda: {"wins":0,"losses":0,"profit":0.0})
    cutoff_7  = datetime.now(timezone.utc) - timedelta(days=7)
    cutoff_30 = datetime.now(timezone.utc) - timedelta(days=30)

    for p in results["picks"]:
        if not p.get("graded"): continue
        conf = p.get("confidence","LOW")
        buckets[conf]["wins"]   += 1 if p["result"]=="WIN" else 0
        buckets[conf]["losses"] += 1 if p["result"]=="LOSS" else 0
        buckets[conf]["profit"] += p.get("profit",0)

    summary = {}
    for conf, b in buckets.items():
        total = b["wins"] + b["losses"]
        summary[conf] = {
            "wins": b["wins"], "losses": b["losses"], "total": total,
            "win_pct": round(b["wins"]/total*100,1) if total else 0,
            "roi": round(b["profit"]/(total*100)*100,1) if total else 0,
            "profit": round(b["profit"],2),
        }

    # 7-day and 30-day overall
    for window_label, cutoff in [("7d", cutoff_7), ("30d", cutoff_30)]:
        w_picks = [p for p in results["picks"]
                   if p.get("graded") and
                   datetime.fromisoformat(p.get("date","2000-01-01T00:00:00+00:00")) >= cutoff]
        total = len(w_picks)
        wins  = sum(1 for p in w_picks if p["result"]=="WIN")
        profit= sum(p.get("profit",0) for p in w_picks)
        summary[f"overall_{window_label}"] = {
            "wins": wins, "losses": total-wins, "total": total,
            "win_pct": round(wins/total*100,1) if total else 0,
            "roi": round(profit/(total*100)*100,1) if total else 0,
            "profit": round(profit,2),
        }

    results["summary"] = summary
    results["last_graded"] = datetime.now(timezone.utc).isoformat()

def record_picks(games, results):
    """Add today's picks to results (ungraded). Skip duplicates."""
    existing_games = {p["game"] for p in results["picks"]}
    today_str = datetime.now(timezone.utc).isoformat()
    for g in games:
        if g["game"] in existing_games: continue
        results["picks"].append({
            "game":           g["game"],
            "pick":           g["pick"],
            "pick_abbr":      g["pick_abbr"],
            "moneyline":      g["moneyline"],
            "moneyline_raw":  g.get("home_ml_raw") if g["pick"]==g["home_team"] else g.get("away_ml_raw"),
            "confidence":     g["confidence"],
            "win_probability":g["win_probability"],
            "commence_time":  g.get("commence_time",""),
            "date":           today_str,
            "graded":         False,
            "result":         None,
            "profit":         None,
        })
    # Keep only last 90 days
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    results["picks"] = [
        p for p in results["picks"]
        if datetime.fromisoformat(p.get("date","2000-01-01T00:00:00+00:00")) >= cutoff
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 4. INJURIES
# ─────────────────────────────────────────────────────────────────────────────
def fetch_injuries():
    injuries = {}
    try:
        r = requests.get("https://www.espn.com/nba/injuries", headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.content,"html.parser")
        for sec in soup.select(".ResponsiveTable"):
            t = sec.select_one(".Table__Title")
            if not t: continue
            abbr = NBA_TEAM_MAP.get(t.get_text(strip=True), t.get_text(strip=True)[:3].upper())
            for row in sec.select("tr.Table__TR--sm"):
                cells = row.select("td")
                if len(cells)>=3:
                    injuries.setdefault(abbr,[]).append({
                        "player": cells[0].get_text(strip=True),
                        "status": cells[1].get_text(strip=True),
                        "reason": cells[2].get_text(strip=True),
                    })
        print(f"[INFO] Injuries loaded for {len(injuries)} teams.")
    except Exception as e:
        print(f"[WARN] Injuries: {e}")
    return injuries


# ─────────────────────────────────────────────────────────────────────────────
# 5. NEWS
# ─────────────────────────────────────────────────────────────────────────────
def fetch_rss():
    arts = []
    for url in FEED_SOURCES:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:30]:
                arts.append({"title":e.get("title",""),"summary":e.get("summary","")[:300],
                             "link":e.get("link",""),"source":feed.feed.get("title",url),
                             "published":e.get("published","")})
        except Exception as e: print(f"[WARN] RSS {url}: {e}")
    print(f"[INFO] RSS: {len(arts)} articles.")
    return arts

def game_news_rss(t1,t2,arts):
    kw=[t1.split()[-1],t2.split()[-1],t1,t2]
    out=[]
    for a in arts:
        if any(k.lower() in (a["title"]+" "+a["summary"]).lower() for k in kw): out.append(a)
        if len(out)>=3: break
    return out

def game_news_api(t1,t2):
    if not NEWS_API_KEY: return []
    q=f"{t1.split()[-1]} OR {t2.split()[-1]} NBA"
    url=(f"https://newsapi.org/v2/everything?q={q}"
         f"&sortBy=publishedAt&language=en&pageSize=3&apiKey={NEWS_API_KEY}")
    try:
        r=requests.get(url,timeout=8).json()
        return [{"title":a.get("title",""),"summary":(a.get("description")or"")[:300],
                 "link":a.get("url",""),"source":(a.get("source")or{}).get("name","NewsAPI"),
                 "published":a.get("publishedAt","")} for a in r.get("articles",[])]
    except Exception as e: print(f"[WARN] NewsAPI: {e}"); return []


# ─────────────────────────────────────────────────────────────────────────────
# 6. REASONING
# ─────────────────────────────────────────────────────────────────────────────
def build_reasoning(game, injuries):
    ha=game["home_abbr"]; aa=game["away_abbr"]
    hi=injuries.get(ha,[]); ai=injuries.get(aa,[])
    inj="; ".join(f"{p['player']} ({p['status']})" for p in (hi+ai)[:4]) or "No significant injuries reported."
    pick=game["pick"]; prob=game["win_probability"]; dog_prob=round(100-prob,1)
    dog=game["away_team"] if pick==game["home_team"] else game["home_team"]

    fatigue_note = ""
    hf=game["fatigue"]["home"]; af=game["fatigue"]["away"]
    if hf["status"] in ["B2B","3IN4"]: fatigue_note += f" {game['home_abbr']} is on a {hf['label']}."
    if af["status"] in ["B2B","3IN4"]: fatigue_note += f" {game['away_abbr']} is on a {af['label']}."

    move_note = ""
    pm = game["line_movement"]["pick"]
    if pm["steam"]: move_note = f" 🔥 Sharp steam detected: line moved {pm['points_moved']} pts."
    elif pm["points_moved"] > 0: move_note = f" Line movement: {pm['label']}."

    r=(f"**{pick} ML ({game['moneyline']})** — {prob}% vs {dog}'s {dog_prob}%, "
       f"a {round(prob-dog_prob,1)}pt edge. Spread: {game['spread']}.{fatigue_note}{move_note} "
       f"Injuries: {inj}")
    return r, inj


# ─────────────────────────────────────────────────────────────────────────────
# MASTER BUILD
# ─────────────────────────────────────────────────────────────────────────────
def build_data():
    print("[INFO] === ParlAID scraper v3 starting ===")

    # Step 1: Grade previous picks
    print("[INFO] Grading previous picks...")
    results = load_results()
    scores  = fetch_scores_for_date(None)
    if scores:
        graded = grade_pending_picks(results, scores)
        if graded: print(f"[INFO] Graded new picks.")

    # Step 2: Load previous odds for movement
    print("[INFO] Loading previous odds snapshot...")
    prev_odds = load_previous_odds()

    # Step 3: Fatigue map
    print("[INFO] Building fatigue map...")
    fatigue = build_fatigue_map()

    # Step 4: Fetch fresh odds
    print("[INFO] Fetching live odds...")
    raw = fetch_odds()
    games = normalise_odds(raw, prev_odds, fatigue)
    print(f"[INFO] {len(games)} games normalised.")

    # Step 5: Injuries and news
    print("[INFO] Fetching injuries...")
    injuries = fetch_injuries()
    print("[INFO] Fetching RSS news...")
    all_rss = fetch_rss()

    # Step 6: Enrich each game
    output = []
    for g in games:
        home=g["home_team"]; away=g["away_team"]
        news = game_news_rss(home,away,all_rss)
        if not news:
            news = game_news_api(home,away); time.sleep(0.25)
        reasoning, inj_text = build_reasoning(g, injuries)
        output.append({
            **g,
            "reasoning":    reasoning,
            "injury_notes": inj_text,
            "news": [{"title":n["title"],"source":n["source"],
                      "link":n["link"],"published":n.get("published","")}
                     for n in news[:3]],
        })

    # Step 7: Record today's picks
    record_picks(output, results)
    save_results(results)
    print(f"[INFO] results.json updated — {len(results['picks'])} total picks.")

    # Step 8: Write data.json
    payload = {
        "last_updated": datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC"),
        "total_games":  len(output),
        "games":        output,
        "track_record": results.get("summary", {}),
    }
    with open("data.json","w") as f:
        json.dump(payload, f, indent=2)

    print(f"[DONE] data.json — {len(output)} games.")
    for g in output:
        fat_h = g["fatigue"]["home"]["status"]
        fat_a = g["fatigue"]["away"]["status"]
        mv    = g["line_movement"]["pick"]["label"]
        print(f"  {g['game']:45s}  {g['pick']:25s}  {g['confidence']}  H:{fat_h} A:{fat_a}  {mv}")


if __name__ == "__main__":
    build_data()
