"""
ParlAID — NBA Scraper v5
=========================
ALL data sourced from credible APIs:

  FATIGUE:     ESPN public scoreboard API — checks actual game dates over past 4 days
               https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD
  LAST-10:     nba_api → leaguegamefinder (NBA.com official data)
  H2H RECORD:  nba_api → leaguegamefinder filtered by opponent
  ODDS:        The Odds API (DraftKings lines)
  INJURIES:    ESPN injuries page
  LIVE SCORES: ESPN scoreboard (called from browser, not scraper)
  NEWS:        ESPN/BR/SportsKeeda RSS + NewsAPI
  PUBLIC BETS: Odds Shark consensus scrape
  RESULTS:     Auto-graded via Odds API scores endpoint

pip install requests beautifulsoup4 feedparser nba_api
Secrets: ODDS_API_KEY, NEWS_API_KEY
"""

import json, os, re, time, requests, feedparser
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

try:
    from nba_api.stats.endpoints import leaguegamefinder, teamgamelogs
    from nba_api.stats.static import teams as nba_static
    NBA_API_OK = True
except ImportError:
    NBA_API_OK = False
    print("[WARN] nba_api not installed — pip install nba_api")

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")

ESPN_SB_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
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
# ESPN uses different abbreviations for some teams
ESPN_ABBR_FIX = {
    "GS":"GSW","SA":"SAS","NO":"NOP","NY":"NYK","OKC":"OKC",
    "WSH":"WAS","CHA":"CHA","MEM":"MEM",
}

ARENA_COORDS = {
    "ATL":(33.757,-84.396),"BOS":(42.366,-71.062),"BKN":(40.683,-73.975),
    "CHA":(35.225,-80.839),"CHI":(41.881,-87.674),"CLE":(41.496,-81.688),
    "DAL":(32.790,-96.810),"DEN":(39.749,-105.007),"DET":(42.341,-83.055),
    "GSW":(37.768,-122.388),"HOU":(29.751,-95.362),"IND":(39.764,-86.156),
    "LAC":(34.043,-118.267),"LAL":(34.043,-118.267),"MEM":(35.138,-90.051),
    "MIA":(25.782,-80.188),"MIL":(43.045,-87.917),"MIN":(44.979,-93.276),
    "NOP":(29.949,-90.082),"NYK":(40.750,-73.994),"OKC":(35.463,-97.515),
    "ORL":(28.539,-81.384),"PHI":(39.901,-75.172),"PHX":(33.446,-112.071),
    "POR":(45.532,-122.667),"SAC":(38.580,-121.500),"SAS":(29.427,-98.438),
    "TOR":(43.643,-79.379),"UTA":(40.768,-111.901),"WAS":(38.898,-77.021),
}

FEED_SOURCES = [
    "https://www.espn.com/espn/rss/nba/news",
    "https://bleacherreport.com/nba.rss",
    "https://www.sportskeeda.com/basketball/feed",
]

def haversine(c1, c2):
    import math
    r = 3958.8
    lat1,lon1 = math.radians(c1[0]),math.radians(c1[1])
    lat2,lon2 = math.radians(c2[0]),math.radians(c2[1])
    dlat=lat2-lat1; dlon=lon2-lon1
    a=math.sin(dlat/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return round(r*2*math.asin(math.sqrt(a)))

# ─────────────────────────────────────────────────────────────────────────────
# FATIGUE — ESPN scoreboard API (credible, verified source)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_espn_scoreboard_for_date(date_obj):
    """Fetch ESPN scoreboard for a specific date. Returns list of {home_abbr, away_abbr, status}."""
    games = []
    date_str = date_obj.strftime("%Y%m%d")
    url = f"{ESPN_SB_BASE}?dates={date_str}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        for ev in r.json().get("events", []):
            comp = ev["competitions"][0]
            abbrs = {}
            for c in comp.get("competitors", []):
                side = "home" if c["homeAway"] == "home" else "away"
                raw = c["team"]["abbreviation"].upper()
                abbrs[side] = ESPN_ABBR_FIX.get(raw, raw)
            status = ev["status"]["type"]["state"]  # pre/in/post
            if abbrs.get("home") and abbrs.get("away"):
                games.append({
                    "home": abbrs["home"], "away": abbrs["away"],
                    "date": date_obj, "status": status,
                })
    except Exception as e:
        print(f"[WARN] ESPN scoreboard {date_str}: {e}")
    return games

def build_fatigue_map_espn():
    """
    Pull 4 days of ESPN scoreboard data to determine:
    - Which teams played yesterday (B2B if playing today)
    - Which teams played 2+ times in last 4 days (3in4)
    - Travel miles from last road game
    Returns dict {abbr: fatigue_obj}
    """
    today = datetime.now(timezone.utc).date()
    fatigue_raw = {}  # abbr -> list of {date, was_away}

    for days_back in range(1, 5):
        d = today - timedelta(days=days_back)
        games = fetch_espn_scoreboard_for_date(d)
        for g in games:
            # Only count completed or in-progress games
            if g["status"] in ["post", "in"]:
                for side in ["home", "away"]:
                    abbr = g[side]
                    was_away = (side == "away")
                    opp_abbr = g["away"] if side == "home" else g["home"]
                    fatigue_raw.setdefault(abbr, []).append({
                        "date": g["date"], "was_away": was_away,
                        "opp": opp_abbr, "days_back": days_back
                    })
        time.sleep(0.3)  # be polite to ESPN

    # Build fatigue objects
    result = {}
    all_abbrs = set(NBA_TEAM_MAP.values())

    for abbr in all_abbrs:
        games_played = fatigue_raw.get(abbr, [])
        games_played.sort(key=lambda x: x["date"], reverse=True)

        games_last_4d = len(games_played)
        last = games_played[0] if games_played else None
        days_rest = last["days_back"] if last else 99

        # Travel miles from last road game
        travel_miles = 0
        travel_desc = ""
        for gp in games_played[:3]:
            if gp["was_away"] and gp["opp"] in ARENA_COORDS and abbr in ARENA_COORDS:
                miles = haversine(ARENA_COORDS[abbr], ARENA_COORDS[gp["opp"]])
                if miles > travel_miles:
                    travel_miles = miles
                    travel_desc = f"{miles:,} mi road trip"
                break

        # Classification
        if days_rest == 1:
            status = "B2B"; label = "Back-to-Back ⚠️"; battery = 20
        elif games_last_4d >= 3:
            status = "3IN4"; label = f"3rd in 4 nights 🔴"; battery = 35
        elif days_rest <= 2 and travel_miles > 1800:
            status = "TRAVEL"; label = f"Cross-country travel 🟣"; battery = 50
        elif days_rest <= 2:
            status = "SOME_REST"; label = "1 day rest"; battery = 68
        elif days_rest == 3:
            status = "RESTED"; label = "2 days rest ✅"; battery = 88
        else:
            status = "FRESH"; label = "Well rested ✅"; battery = 100

        last_game_str = last["date"].strftime("%b %d") if last else "—"

        result[abbr] = {
            "status": status, "label": label, "battery": battery,
            "last_game": last_game_str, "days_rest": days_rest,
            "games_last_4d": games_last_4d,
            "travel_miles": travel_miles, "travel_desc": travel_desc,
            "source": "ESPN Scoreboard API",
        }

    print(f"[INFO] Fatigue map built from ESPN for {len(result)} teams.")
    return result

# ─────────────────────────────────────────────────────────────────────────────
# LAST-10 FORM + H2H — nba_api (NBA.com official data)
# ─────────────────────────────────────────────────────────────────────────────
def get_nba_team_id(abbr):
    """Get NBA.com team ID from abbreviation."""
    teams = nba_static.get_teams()
    for t in teams:
        if t["abbreviation"] == abbr:
            return t["id"]
    return None

def build_team_game_logs():
    """
    Returns dict: {abbr: {last_10: [...], wins_10: int, losses_10: int, streak: str}}
    Uses nba_api teamgamelogs endpoint — official NBA.com data.
    """
    if not NBA_API_OK:
        return {}

    logs = {}
    season = "2025-26"

    try:
        # Get all team game logs for the season
        finder = leaguegamefinder.LeagueGameFinder(
            season_nullable=season,
            season_type_nullable="Regular Season",
            league_id_nullable="00",
            timeout=40,
        )
        df = finder.get_data_frames()[0]

        def parse_date(x):
            for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
                try: return datetime.strptime(x[:len(fmt)], fmt).date()
                except: pass
            return datetime.now().date()

        df["GAME_DATE_PARSED"] = df["GAME_DATE"].apply(parse_date)
        df = df.sort_values("GAME_DATE_PARSED", ascending=False)

        for abbr in set(NBA_TEAM_MAP.values()):
            tdf = df[df["TEAM_ABBREVIATION"] == abbr].head(15)
            if tdf.empty:
                continue

            last_10 = []
            for _, row in tdf.head(10).iterrows():
                wl = str(row.get("WL", "")).strip()
                pts = int(row.get("PTS", 0)) if row.get("PTS") else 0
                matchup = str(row.get("MATCHUP", ""))
                is_away = "@" in matchup and not matchup.startswith(abbr)
                opp = matchup.split()[-1] if matchup else "?"
                last_10.append({
                    "date": row["GAME_DATE_PARSED"].strftime("%b %d"),
                    "result": wl, "pts": pts,
                    "opp": opp, "away": is_away,
                })

            wins  = sum(1 for g in last_10 if g["result"] == "W")
            losses = len(last_10) - wins

            # Current streak
            streak_char = last_10[0]["result"] if last_10 else "?"
            streak_count = 0
            for g in last_10:
                if g["result"] == streak_char:
                    streak_count += 1
                else:
                    break
            streak = f"W{streak_count}" if streak_char == "W" else f"L{streak_count}"

            logs[abbr] = {
                "last_10": last_10,
                "wins_10": wins, "losses_10": losses,
                "form_str": "".join(g["result"] for g in last_10),
                "streak": streak,
                "source": "NBA.com (nba_api)",
            }

        print(f"[INFO] Last-10 form loaded for {len(logs)} teams.")
        time.sleep(1)  # rate limit NBA.com

    except Exception as e:
        print(f"[WARN] nba_api last-10: {e}")

    return logs


def build_h2h_records(game_list, df=None):
    """
    For each upcoming game, compute H2H record between the two teams this season.
    Uses the already-fetched leaguegamefinder dataframe.
    Returns dict: {game_key: {wins_home, wins_away, games, meetings: [...]}}
    """
    if not NBA_API_OK or df is None:
        return {}

    h2h = {}
    for g in game_list:
        home_abbr = g.get("home_abbr", "")
        away_abbr = g.get("away_abbr", "")
        key = g.get("game", "")

        # All games where home team played vs away team
        home_games = df[
            (df["TEAM_ABBREVIATION"] == home_abbr) &
            (df["MATCHUP"].str.contains(away_abbr, na=False))
        ]

        meetings = []
        for _, row in home_games.iterrows():
            wl = str(row.get("WL","")).strip()
            pts = int(row.get("PTS",0)) if row.get("PTS") else 0
            meetings.append({
                "date": str(row.get("GAME_DATE",""))[:10],
                "home_result": wl,
                "pts": pts,
            })

        home_wins = sum(1 for m in meetings if m["home_result"] == "W")
        away_wins = len(meetings) - home_wins

        h2h[key] = {
            "total_meetings": len(meetings),
            "home_wins": home_wins,
            "away_wins": away_wins,
            "meetings": meetings[:4],  # last 4 meetings
            "source": "NBA.com (nba_api)",
        }

    return h2h


# ─────────────────────────────────────────────────────────────────────────────
# ODDS + LINE MOVEMENT
# ─────────────────────────────────────────────────────────────────────────────
def ml_to_prob(ml):
    try:
        ml = int(str(ml).replace("+",""))
        return abs(ml)/(abs(ml)+100)*100 if ml<0 else 100/(ml+100)*100
    except: return 50.0

def remove_vig(p1, p2):
    t = p1+p2
    return (round(p1/t*100,1), round(p2/t*100,1)) if t else (50.0,50.0)

def conf_tier(p):
    if p>=72: return "HIGH","★★★"
    if p>=58: return "MEDIUM","★★☆"
    return "LOW","★☆☆"

def fetch_odds():
    if not ODDS_API_KEY: return []
    url = (f"https://api.the-odds-api.com/v4/sports/basketball_nba/odds/"
           f"?apiKey={ODDS_API_KEY}&regions=us&markets=h2h,spreads"
           "&oddsFormat=american&bookmakers=draftkings")
    try:
        r = requests.get(url, timeout=12); r.raise_for_status()
        d = r.json(); print(f"[INFO] Odds API: {len(d)} games."); return d
    except Exception as e:
        print(f"[ERROR] Odds API: {e}"); return []

def _raw(s):
    try: return int(str(s).replace("+",""))
    except: return None

def load_prev_odds():
    prev = {}
    try:
        with open("data.json") as f: old = json.load(f)
        for g in old.get("games", []):
            prev[g["game"]] = {
                "home_ml_raw": _raw(g.get("home_ml","N/A")),
                "away_ml_raw": _raw(g.get("away_ml","N/A")),
                "snapshot_time": old.get("last_updated",""),
            }
    except: pass
    return prev

def classify_move(old_ml, new_ml):
    if old_ml is None or new_ml is None:
        return {"direction":"none","points_moved":0,"steam":False,"label":"—",
                "old_ml":"—","new_ml":"—"}
    moved = new_ml - old_ml; steam = abs(moved) >= 8
    of = f"+{old_ml}" if old_ml>0 else str(old_ml)
    nf = f"+{new_ml}" if new_ml>0 else str(new_ml)
    if moved == 0:
        return {"direction":"none","points_moved":0,"steam":False,
                "label":"No movement","old_ml":of,"new_ml":nf}
    pfx = "🔥 STEAM" if steam else ("🟢" if moved<0 else "🔴")
    return {"direction":"shorter" if moved<0 else "longer","points_moved":abs(moved),
            "steam":steam,"label":f"{pfx} {nf} ← {of}","old_ml":of,"new_ml":nf}

def normalise_odds(raw, prev_odds, fatigue):
    games = []
    for g in raw:
        home = g.get("home_team",""); away = g.get("away_team","")
        ha = NBA_TEAM_MAP.get(home, home[:3].upper())
        aa = NBA_TEAM_MAP.get(away, away[:3].upper())
        hmr = amr = None; hmf = amf = "N/A"; spread_str = "N/A"

        for bm in g.get("bookmakers",[]):
            for mkt in bm.get("markets",[]):
                if mkt["key"] == "h2h":
                    for o in mkt.get("outcomes",[]):
                        p=o["price"]; f=f"+{p}" if p>0 else str(p)
                        if o["name"]==home: hmr=p; hmf=f
                        elif o["name"]==away: amr=p; amf=f
                elif mkt["key"] == "spreads":
                    for o in mkt.get("outcomes",[]):
                        if o["name"]==home:
                            pt=o["point"]; spread_str=f"{ha} {'+' if pt>0 else ''}{pt}"
            break

        hp,ap = remove_vig(ml_to_prob(hmr),ml_to_prob(amr)) if hmr and amr else (50.0,50.0)
        if hp>=ap: pick=home; pa=ha; pm=hmf; pp=hp
        else:      pick=away; pa=aa; pm=amf; pp=ap
        conf,stars = conf_tier(pp)
        key = f"{away} @ {home}"
        prev = prev_odds.get(key,{})
        hm = classify_move(prev.get("home_ml_raw"),hmr)
        am = classify_move(prev.get("away_ml_raw"),amr)
        hf = fatigue.get(ha,_fresh()); af = fatigue.get(aa,_fresh())

        ef = []
        if hm.get("steam") or am.get("steam"): ef.append("🔥 Sharp steam detected")
        if hf["status"] in ["B2B","3IN4"] and pick==away: ef.append(f"💤 {ha} fatigued ({hf['status']}) — source: ESPN")
        if af["status"] in ["B2B","3IN4"] and pick==home: ef.append(f"💤 {aa} fatigued ({af['status']}) — source: ESPN")
        if hf.get("travel_miles",0)>1800 and pick==away: ef.append(f"✈️ {ha} traveled {hf['travel_miles']:,} mi")
        if af.get("travel_miles",0)>1800 and pick==home: ef.append(f"✈️ {aa} traveled {af['travel_miles']:,} mi")
        if not ef: ef.append(f"📊 {round(pp-round(100-pp,1),1)}pt probability edge")

        games.append({
            "game":key,"home_team":home,"away_team":away,
            "home_abbr":ha,"away_abbr":aa,
            "home_ml":hmf,"away_ml":amf,"home_ml_raw":hmr,"away_ml_raw":amr,
            "home_prob":hp,"away_prob":ap,"spread":spread_str,"favorite":pick,
            "pick":pick,"pick_abbr":pa,"moneyline":pm,"win_probability":pp,
            "confidence":conf,"confidence_stars":stars,
            "commence_time":g.get("commence_time",""),
            "line_movement":{"home":hm,"away":am,"pick":hm if pick==home else am,
                             "snapshot_time":prev.get("snapshot_time","—")},
            "fatigue":{"home":hf,"away":af},
            "edge_factors":ef,
        })
    return games

def _fresh():
    return {"status":"FRESH","label":"Well rested ✅","battery":100,
            "last_game":"—","days_rest":99,"games_last_4d":0,
            "travel_miles":0,"travel_desc":"","source":"ESPN Scoreboard API"}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC BETTING
# ─────────────────────────────────────────────────────────────────────────────
def fetch_public_betting():
    public = {}
    for url, src in [
        ("https://www.oddsshark.com/nba/consensus-picks","Odds Shark"),
        ("https://www.sportsbettingdime.com/nba/public-betting-trends/","SportsBettingDime"),
    ]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=12)
            soup = BeautifulSoup(r.content,"html.parser")
            for row in soup.select(".op-matchup-wrapper, tr"):
                try:
                    team_els = row.select(".op-matchup-team-name, td")
                    pct_els  = row.select(".op-consensus-bars-pct, .op-pct, td")
                    if len(team_els)>=2 and len(pct_els)>=2:
                        an = team_els[0].get_text(strip=True)
                        hn = team_els[1].get_text(strip=True)
                        ap = int(re.sub(r'\D','',pct_els[0].get_text()) or 0)
                        hp = int(re.sub(r'\D','',pct_els[1].get_text()) or 0)
                        aa = NBA_TEAM_MAP.get(an, an[:3].upper())
                        ha = NBA_TEAM_MAP.get(hn, hn[:3].upper())
                        if ap+hp > 50:
                            public[(aa,ha)] = {"away_t":ap,"home_t":hp,"source":src}
                except: continue
            if public: break
        except Exception as e:
            print(f"[WARN] Public betting {src}: {e}")
    print(f"[INFO] Public betting: {len(public)} games.")
    return public

def sharp_signal(game, pub, lm):
    key = (game["away_abbr"], game["home_abbr"])
    pb  = pub.get(key)
    res = {"away_t":None,"home_t":None,"source":None,"sharp_signal":None,
           "sharp_label":"Pending — check closer to tip-off","rlm":False}
    if not pb: return res
    at,ht = pb["away_t"],pb["home_t"]
    res.update({"away_t":at,"home_t":ht,"source":pb["source"]})
    hm = lm.get("home",{}); am = lm.get("away",{})
    pub_likes_home = ht > at
    rlm = (pub_likes_home and am.get("direction")=="shorter") or \
          (not pub_likes_home and hm.get("direction")=="shorter")
    res["rlm"] = rlm
    if rlm:
        side = game["away_abbr"] if pub_likes_home else game["home_abbr"]
        res["sharp_signal"]="RLM"; res["sharp_label"]=f"⚡ RLM: Public on {'home' if pub_likes_home else 'away'}, sharp money on {side}"
    elif at>=70: res["sharp_signal"]="PUB_A"; res["sharp_label"]=f"🐑 Public heavy on {game['away_abbr']} ({at}% tickets)"
    elif ht>=70: res["sharp_signal"]="PUB_H"; res["sharp_label"]=f"🐑 Public heavy on {game['home_abbr']} ({ht}% tickets)"
    elif abs(at-ht)<=10: res["sharp_signal"]="SPLIT"; res["sharp_label"]=f"Split market: {at}% / {ht}%"
    else: res["sharp_label"]=f"Tickets: {game['away_abbr']} {at}% / {game['home_abbr']} {ht}%"
    return res


# ─────────────────────────────────────────────────────────────────────────────
# INJURIES + NEWS
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
                        "player":cells[0].get_text(strip=True),
                        "status":cells[1].get_text(strip=True),
                        "reason":cells[2].get_text(strip=True),
                    })
        print(f"[INFO] Injuries: {len(injuries)} teams from ESPN.")
    except Exception as e: print(f"[WARN] ESPN injuries: {e}")
    return injuries

def fetch_rss():
    arts = []
    for url in FEED_SOURCES:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:30]:
                arts.append({"title":e.get("title",""),"summary":e.get("summary","")[:300],
                             "link":e.get("link",""),"source":feed.feed.get("title",url),
                             "published":e.get("published","")})
        except: pass
    return arts

def game_news(t1,t2,arts):
    kw=[t1.split()[-1],t2.split()[-1],t1,t2]
    out=[]
    for a in arts:
        if any(k.lower() in (a["title"]+" "+a["summary"]).lower() for k in kw): out.append(a)
        if len(out)>=3: break
    return out

def game_news_api(t1,t2):
    if not NEWS_API_KEY: return []
    q = f"{t1.split()[-1]} OR {t2.split()[-1]} NBA"
    url=(f"https://newsapi.org/v2/everything?q={q}"
         f"&sortBy=publishedAt&language=en&pageSize=3&apiKey={NEWS_API_KEY}")
    try:
        r=requests.get(url,timeout=8).json()
        return [{"title":a.get("title",""),"link":a.get("url",""),
                 "source":(a.get("source")or{}).get("name","NewsAPI"),
                 "published":a.get("publishedAt","")} for a in r.get("articles",[])]
    except: return []


# ─────────────────────────────────────────────────────────────────────────────
# RESULTS GRADING
# ─────────────────────────────────────────────────────────────────────────────
def fetch_scores():
    if not ODDS_API_KEY: return {}
    url = f"https://api.the-odds-api.com/v4/sports/basketball_nba/scores/?apiKey={ODDS_API_KEY}&daysFrom=2"
    try:
        r=requests.get(url,timeout=10); r.raise_for_status()
        s={}
        for g in r.json():
            if not g.get("completed"): continue
            home=g["home_team"]; away=g["away_team"]
            sc={x["name"]:int(x["score"]) for x in (g.get("scores")or[]) if x.get("score")}
            s[f"{away} @ {home}"]={"home":home,"away":away,
                                   "home_score":sc.get(home,0),"away_score":sc.get(away,0)}
        return s
    except: return {}

def load_results():
    try:
        with open("results.json") as f: return json.load(f)
    except: return {"picks":[],"summary":{}}

def save_results(r): 
    with open("results.json","w") as f: json.dump(r,f,indent=2)

def grade_picks(results, scores):
    changed=False
    for p in results["picks"]:
        if p.get("graded"): continue
        sc=scores.get(p["game"])
        if not sc: continue
        winner=sc["home"] if sc["home_score"]>sc["away_score"] else sc["away"]
        won=winner==p["pick"]
        p["result"]="WIN" if won else "LOSS"; p["graded"]=True
        p["home_score"]=sc["home_score"]; p["away_score"]=sc["away_score"]
        ml=p.get("moneyline_raw",0) or 0
        p["profit"]=round((ml if ml>0 else 100/abs(ml)*100) if won else -100,2) if ml else (100 if won else -100)
        changed=True
    if changed: recalc(results)
    return changed

def recalc(results):
    from collections import defaultdict
    b=defaultdict(lambda:{"w":0,"l":0,"p":0.0})
    now=datetime.now(timezone.utc)
    for p in results["picks"]:
        if not p.get("graded"): continue
        c=p.get("confidence","LOW")
        b[c]["w"]+=1 if p["result"]=="WIN" else 0
        b[c]["l"]+=1 if p["result"]=="LOSS" else 0
        b[c]["p"]+=p.get("profit",0)
    s={}
    for c,v in b.items():
        t=v["w"]+v["l"]
        s[c]={"wins":v["w"],"losses":v["l"],"total":t,
              "win_pct":round(v["w"]/t*100,1) if t else 0,
              "roi":round(v["p"]/(t*100)*100,1) if t else 0,"profit":round(v["p"],2)}
    for lbl,days in [("7d",7),("30d",30)]:
        cut=now-timedelta(days=days)
        wp=[p for p in results["picks"] if p.get("graded") and
            datetime.fromisoformat(p.get("date","2000-01-01T00:00:00+00:00"))>=cut]
        t=len(wp); w=sum(1 for p in wp if p["result"]=="WIN"); pr=sum(p.get("profit",0) for p in wp)
        s[f"overall_{lbl}"]={"wins":w,"losses":t-w,"total":t,
                              "win_pct":round(w/t*100,1) if t else 0,
                              "roi":round(pr/(t*100)*100,1) if t else 0,"profit":round(pr,2)}
    results["summary"]=s; results["last_graded"]=now.isoformat()

def record_picks(games, results):
    existing={p["game"] for p in results["picks"]}
    now=datetime.now(timezone.utc).isoformat()
    for g in games:
        if g["game"] in existing: continue
        ml_raw=g.get("home_ml_raw") if g["pick"]==g["home_team"] else g.get("away_ml_raw")
        results["picks"].append({
            "game":g["game"],"pick":g["pick"],"pick_abbr":g["pick_abbr"],
            "moneyline":g["moneyline"],"moneyline_raw":ml_raw,
            "confidence":g["confidence"],"win_probability":g["win_probability"],
            "commence_time":g.get("commence_time",""),"date":now,
            "graded":False,"result":None,"profit":None,
        })
    cut=datetime.now(timezone.utc)-timedelta(days=90)
    results["picks"]=[p for p in results["picks"]
                      if datetime.fromisoformat(p.get("date","2000-01-01T00:00:00+00:00"))>=cut]


# ─────────────────────────────────────────────────────────────────────────────
# MASTER BUILD
# ─────────────────────────────────────────────────────────────────────────────
def build_data():
    print("=== ParlAID v5 ===")

    # Grade previous picks
    results=load_results()
    scores=fetch_scores()
    if scores: grade_picks(results,scores)

    prev_odds = load_prev_odds()

    # Fatigue from ESPN (credible, verified)
    print("[INFO] Building fatigue map from ESPN Scoreboard API...")
    fatigue = build_fatigue_map_espn()

    # Odds
    raw = fetch_odds()
    games = normalise_odds(raw, prev_odds, fatigue)
    print(f"[INFO] {len(games)} games.")

    # NBA.com last-10 form and H2H
    game_logs = {}; h2h = {}; df_all = None
    if NBA_API_OK:
        print("[INFO] Fetching NBA.com team game logs...")
        try:
            finder = leaguegamefinder.LeagueGameFinder(
                season_nullable="2025-26",
                season_type_nullable="Regular Season",
                league_id_nullable="00", timeout=40,
            )
            df_all = finder.get_data_frames()[0]
            def pd(x):
                for fmt in ["%Y-%m-%dT%H:%M:%S","%Y-%m-%d"]:
                    try: return datetime.strptime(x[:len(fmt)],fmt).date()
                    except: pass
                return datetime.now().date()
            df_all["GAME_DATE_PARSED"] = df_all["GAME_DATE"].apply(pd)
            df_all = df_all.sort_values("GAME_DATE_PARSED",ascending=False)

            all_abbrs = set(NBA_TEAM_MAP.values())
            for abbr in all_abbrs:
                tdf = df_all[df_all["TEAM_ABBREVIATION"]==abbr].head(12)
                if tdf.empty: continue
                last_10=[]
                for _,row in tdf.head(10).iterrows():
                    wl=str(row.get("WL","")).strip()
                    pts=int(row.get("PTS",0)) if row.get("PTS") else 0
                    matchup=str(row.get("MATCHUP",""))
                    opp=matchup.split()[-1] if matchup else "?"
                    last_10.append({"date":row["GAME_DATE_PARSED"].strftime("%b %d"),
                                    "result":wl,"pts":pts,"opp":opp,
                                    "away":"@" in matchup and not matchup.startswith(abbr)})
                wins=sum(1 for g in last_10 if g["result"]=="W")
                sc=last_10[0]["result"] if last_10 else "?"
                cnt=sum(1 for g in last_10 if g["result"]==sc and g==last_10[last_10.index(g)])
                stk_cnt=0
                for gm in last_10:
                    if gm["result"]==sc: stk_cnt+=1
                    else: break
                streak=f"W{stk_cnt}" if sc=="W" else f"L{stk_cnt}"
                game_logs[abbr]={"last_10":last_10,"wins_10":wins,"losses_10":10-wins,
                                  "form_str":"".join(g["result"] for g in last_10),
                                  "streak":streak,"source":"NBA.com via nba_api"}
            print(f"[INFO] Last-10 form: {len(game_logs)} teams.")
            time.sleep(0.5)

            # H2H
            for g in games:
                key=g["game"]; ha=g["home_abbr"]; aa=g["away_abbr"]
                hgames=df_all[(df_all["TEAM_ABBREVIATION"]==ha) & (df_all["MATCHUP"].str.contains(aa,na=False))]
                meetings=[]
                for _,row in hgames.iterrows():
                    wl=str(row.get("WL","")).strip(); pts=int(row.get("PTS",0)) if row.get("PTS") else 0
                    meetings.append({"date":str(row.get("GAME_DATE",""))[:10],"home_result":wl,"pts":pts})
                hw=sum(1 for m in meetings if m["home_result"]=="W")
                h2h[key]={"total_meetings":len(meetings),"home_wins":hw,"away_wins":len(meetings)-hw,
                           "meetings":meetings[:4],"source":"NBA.com via nba_api"}
            print(f"[INFO] H2H records: {len(h2h)} matchups.")
        except Exception as e:
            print(f"[WARN] nba_api game logs: {e}")

    # Injuries, news, public betting
    injuries = fetch_injuries()
    pub = fetch_public_betting()
    all_rss = fetch_rss()

    output=[]
    for g in games:
        home=g["home_team"]; away=g["away_team"]
        ha=g["home_abbr"]; aa=g["away_abbr"]
        hi=injuries.get(ha,[]); ai=injuries.get(aa,[])
        inj="; ".join(f"{p['player']} ({p['status']})" for p in (hi+ai)[:4]) or "No key injuries."
        news=game_news(home,away,all_rss) or game_news_api(home,away)
        if not game_news(home,away,all_rss): time.sleep(0.2)

        sh=sharp_signal(g,pub,g["line_movement"])
        hlog=game_logs.get(ha,{}); alog=game_logs.get(aa,{})
        gh=h2h.get(g["game"],{"total_meetings":0,"home_wins":0,"away_wins":0,"meetings":[],"source":"—"})

        # Structured briefing
        hf=g["fatigue"]["home"]; af=g["fatigue"]["away"]
        lm=g["line_movement"]; pm=lm.get("pick",{})
        trend_parts=[]
        if ha in game_logs: trend_parts.append(f"{ha} are {game_logs[ha]['wins_10']}-{game_logs[ha]['losses_10']} in their last 10 (streak: {game_logs[ha]['streak']}).")
        if aa in game_logs: trend_parts.append(f"{aa} are {game_logs[aa]['wins_10']}-{game_logs[aa]['losses_10']} in their last 10 (streak: {game_logs[aa]['streak']}).")
        if hf["status"] in ["B2B","3IN4"]: trend_parts.append(f"{ha} on {hf['label']} per ESPN schedule data.")
        if af["status"] in ["B2B","3IN4"]: trend_parts.append(f"{aa} on {af['label']} per ESPN schedule data.")
        trend=" ".join(trend_parts) or f"{g['pick']} hold a {g['win_probability']}% edge."

        matchup_parts=[]
        if gh["total_meetings"]>0:
            matchup_parts.append(f"Season series: {ha} {gh['home_wins']}-{gh['away_wins']} vs {aa} in {gh['total_meetings']} meeting{'s' if gh['total_meetings']>1 else ''}.")
        key_inj=[(p,side) for p,side in [(p,ha) for p in hi]+[(p,aa) for p in ai] if p["status"].lower() in ["out","doubtful","questionable"]]
        if key_inj: matchup_parts.append(" ".join(f"{p['player']} ({side}, {p['status']})." for p,side in key_inj[:3]))
        else: matchup_parts.append("Both rosters appear healthy — clean line.")
        matchup=" ".join(matchup_parts)

        of=pm.get("old_ml","—"); nf=pm.get("new_ml","—"); pts=pm.get("points_moved",0)
        if pm.get("steam"): steam=f"Line steamed from {of} to {nf} ({pts}pt move) — heavy professional action on {g['pick']}."
        elif pts>0: steam=f"Line moved {of}→{nf} ({pts}pts). {sh.get('sharp_label','')}"
        else: steam="Line stable since open. "+sh.get("sharp_label","")
        if sh.get("away_t") is not None: steam+=f" Public split: {aa} {sh['away_t']}% / {ha} {sh['home_t']}% of tickets."

        output.append({
            **g,
            "injury_notes":inj,
            "sharp":sh,
            "form":{
                "home":{"last_10":hlog.get("last_10",[]),"wins":hlog.get("wins_10",0),
                         "losses":hlog.get("losses_10",0),"streak":hlog.get("streak","—"),
                         "form_str":hlog.get("form_str",""),"source":hlog.get("source","—")},
                "away":{"last_10":alog.get("last_10",[]),"wins":alog.get("wins_10",0),
                         "losses":alog.get("losses_10",0),"streak":alog.get("streak","—"),
                         "form_str":alog.get("form_str",""),"source":alog.get("source","—")},
            },
            "h2h":gh,
            "briefing":{"trend":trend,"matchup":matchup,"steam":steam,"injury_notes":inj},
            "live":{"status":"pre","status_detail":"Upcoming","period":0,"clock":"",
                    "home_score":0,"away_score":0,"spread_alert":None},
            "news":[{"title":n["title"],"source":n["source"],
                     "link":n["link"],"published":n.get("published","")} for n in news[:3]],
        })

    record_picks(output,results); save_results(results)
    payload={"last_updated":datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC"),
             "total_games":len(output),"games":output,"track_record":results.get("summary",{})}
    with open("data.json","w") as f: json.dump(payload,f,indent=2)
    print(f"[DONE] data.json — {len(output)} games.")
    for g in output:
        hf=g["fatigue"]["home"]; af=g["fatigue"]["away"]
        hl=g["form"]["home"]; al=g["form"]["away"]
        print(f"  {g['game']:42s}  {g['pick']:22s}  {g['confidence']}  "
              f"H:{hf['status']}({hf['days_rest']}d)  A:{af['status']}({af['days_rest']}d)  "
              f"H-L10:{hl.get('wins',0)}-{hl.get('losses',0)}  A-L10:{al.get('wins',0)}-{al.get('losses',0)}")

if __name__=="__main__":
    build_data()
