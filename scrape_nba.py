"""
ParlAID — NBA Scraper v4
========================
Data sources:
  LIVE SCORES:    ESPN public API (site.api.espn.com) — no key required
  ODDS:           The Odds API / DraftKings
  PUBLIC BETTING: Odds Shark consensus scrape (real ticket %)
  FATIGUE:        nba_api live scoreboard + schedule
  INJURIES:       ESPN injuries page
  NEWS:           ESPN/BR/SportsKeeda RSS + NewsAPI
  RESULTS:        Auto-graded via Odds API scores endpoint

pip install requests beautifulsoup4 feedparser nba_api
Secrets: ODDS_API_KEY, NEWS_API_KEY
"""

import json, os, re, time, requests, feedparser
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

try:
    from nba_api.live.nba.endpoints import scoreboard as live_sb
    from nba_api.stats.endpoints import leaguegamefinder
    NBA_API_OK = True
except ImportError:
    NBA_API_OK = False
    print("[WARN] nba_api not installed.")

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
ESPN_SB      = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_ODDS_SB = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{eid}/competitions/{eid}/odds"
HEADERS      = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

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
WEST_TEAMS = {"LAL","LAC","GSW","SAC","PHX","DEN","UTA","OKC","MIN","DAL","HOU","SAS","NOP","MEM","POR"}
EAST_TEAMS = {"BOS","NYK","PHI","BKN","TOR","MIA","ATL","CHA","ORL","WAS","CHI","MIL","IND","CLE","DET"}

FEED_SOURCES = [
    "https://www.espn.com/espn/rss/nba/news",
    "https://bleacherreport.com/nba.rss",
    "https://www.sportskeeda.com/basketball/feed",
]

# ─────────────────────────────────────────────────────────────────────────────
# FEATURE 1: ESPN LIVE SCOREBOARD
# ─────────────────────────────────────────────────────────────────────────────
def fetch_espn_scoreboard():
    """
    Returns dict keyed by normalised game string e.g. 'LAL @ WAS':
    {
      espn_id, status, status_detail, period, clock,
      home_score, away_score, home_abbr, away_abbr,
      spread_alert: None | 'SPREAD_DANGER' | 'COVER_COMFORT' | 'LIVE_HEDGE'
    }
    """
    games = {}
    try:
        r = requests.get(ESPN_SB, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        for event in data.get("events", []):
            comp    = event["competitions"][0]
            status  = event["status"]["type"]
            state   = status["state"]          # pre, in, post
            detail  = status.get("shortDetail","")
            period  = event["status"].get("period", 0)
            clock   = event["status"].get("displayClock","")

            competitors = comp.get("competitors", [])
            scores = {}
            abbrs  = {}
            for c in competitors:
                side = "home" if c["homeAway"] == "home" else "away"
                scores[side] = int(c.get("score", 0))
                abbrs[side]  = c["team"]["abbreviation"].upper()

            home_a = abbrs.get("home","")
            away_a = abbrs.get("away","")
            key    = f"{away_a} @ {home_a}"  # matches our game key format

            # Try to get spread from ESPN competition odds
            espn_spread = None
            try:
                for odds_obj in comp.get("odds", []):
                    det = odds_obj.get("details","")   # e.g. "LAL -15.5"
                    if det:
                        espn_spread = det
                        break
            except: pass

            # Spread danger logic (only meaningful in-game)
            spread_alert = None
            if state == "in" and espn_spread and period >= 3:
                diff = scores["home"] - scores["away"]
                try:
                    sp_parts = espn_spread.split()
                    sp_val   = float(sp_parts[-1])
                    fav_side = "home" if sp_parts[0] == home_a else "away"
                    # diff from fav perspective
                    fav_diff = diff if fav_side == "home" else -diff
                    needed   = -sp_val  # fav needs to win by more than this

                    if period == 4:
                        gap = needed - fav_diff
                        if gap > 8:
                            spread_alert = "SPREAD_DANGER"   # fav in trouble
                        elif gap < -10:
                            spread_alert = "COVER_COMFORT"   # comfortably covering
                        elif abs(gap) <= 3:
                            spread_alert = "LIVE_HEDGE"      # within a field goal
                except: pass

            games[key] = {
                "espn_id":      event.get("id",""),
                "status":       state,          # pre / in / post
                "status_detail":detail,
                "period":       period,
                "clock":        clock,
                "home_score":   scores.get("home", 0),
                "away_score":   scores.get("away", 0),
                "home_abbr":    home_a,
                "away_abbr":    away_a,
                "espn_spread":  espn_spread,
                "spread_alert": spread_alert,
            }
        print(f"[INFO] ESPN scoreboard: {len(games)} games.")
    except Exception as e:
        print(f"[WARN] ESPN scoreboard: {e}")
    return games


# ─────────────────────────────────────────────────────────────────────────────
# FEATURE 2: PUBLIC BETTING — ODDS SHARK SCRAPE
# ─────────────────────────────────────────────────────────────────────────────
def fetch_public_betting():
    """
    Scrapes Odds Shark consensus page for real ticket percentages.
    Returns dict keyed by (away_abbr, home_abbr) tuple:
    {
      away_tickets_pct, home_tickets_pct,
      away_money_pct, home_money_pct,
      sharp_signal: 'SHARP_AWAY' | 'SHARP_HOME' | 'PUBLIC' | None,
      rlm: bool,  # reverse line movement detected
      sharp_label: str,
    }
    """
    public = {}
    # Odds Shark consensus — updated hourly, no auth required
    url = "https://www.oddsshark.com/nba/consensus-picks"
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(r.content, "html.parser")
        rows = soup.select(".op-matchup-wrapper, .op-item-row-wrapper")
        for row in rows:
            try:
                teams_el = row.select(".op-matchup-team-name")
                pct_els  = row.select(".op-consensus-bars-pct, .op-pct")
                if len(teams_el) < 2 or len(pct_els) < 2: continue
                away_name = teams_el[0].get_text(strip=True)
                home_name = teams_el[1].get_text(strip=True)
                away_pct  = int(re.sub(r'\D','', pct_els[0].get_text()) or 0)
                home_pct  = int(re.sub(r'\D','', pct_els[1].get_text()) or 0)
                away_a    = NBA_TEAM_MAP.get(away_name, away_name[:3].upper())
                home_a    = NBA_TEAM_MAP.get(home_name, home_name[:3].upper())
                key       = (away_a, home_a)
                public[key] = {
                    "away_tickets_pct": away_pct,
                    "home_tickets_pct": home_pct,
                    "source": "Odds Shark",
                }
            except: continue
        print(f"[INFO] Odds Shark public betting: {len(public)} games.")
    except Exception as e:
        print(f"[WARN] Odds Shark scrape: {e}")

    # Also try SportsBettingDime which has a cleaner table
    if not public:
        try:
            url2 = "https://www.sportsbettingdime.com/nba/public-betting-trends/"
            r2 = requests.get(url2, headers=HEADERS, timeout=12)
            soup2 = BeautifulSoup(r2.content, "html.parser")
            for row in soup2.select("tr"):
                cells = row.select("td")
                if len(cells) >= 5:
                    try:
                        away_name = cells[0].get_text(strip=True)
                        home_name = cells[1].get_text(strip=True)
                        away_pct  = int(re.sub(r'\D','', cells[2].get_text()) or 0)
                        home_pct  = int(re.sub(r'\D','', cells[3].get_text()) or 0)
                        away_a    = NBA_TEAM_MAP.get(away_name, away_name[:3].upper())
                        home_a    = NBA_TEAM_MAP.get(home_name, home_name[:3].upper())
                        if away_pct + home_pct > 0:
                            public[(away_a, home_a)] = {
                                "away_tickets_pct": away_pct,
                                "home_tickets_pct": home_pct,
                                "source": "SportsBettingDime",
                            }
                    except: continue
            print(f"[INFO] SportsBettingDime: {len(public)} games.")
        except Exception as e:
            print(f"[WARN] SBD scrape: {e}")

    return public


def enrich_sharp_signal(game, public_data, line_movement):
    """
    Derive sharp vs public signal by comparing ticket % to line movement.
    Sharp signal = line moves AGAINST the majority public side (reverse line movement).
    """
    key    = (game["away_abbr"], game["home_abbr"])
    pb     = public_data.get(key)
    result = {
        "away_tickets_pct": None,
        "home_tickets_pct": None,
        "source": None,
        "sharp_signal": None,
        "sharp_label": "Insufficient data",
        "rlm": False,
    }

    if not pb:
        return result

    away_t  = pb["away_tickets_pct"]
    home_t  = pb["home_tickets_pct"]
    result.update({"away_tickets_pct": away_t, "home_tickets_pct": home_t, "source": pb["source"]})

    # Reverse Line Movement: public favours one side but line moved to the other
    pick_is_home  = game["pick"] == game["home_team"]
    home_mov      = line_movement.get("home", {})
    away_mov      = line_movement.get("away", {})

    public_likes_home = home_t > away_t
    # Line moved "shorter" (bigger fav) = sharp backing that team
    line_moved_home_shorter = home_mov.get("direction") == "shorter"
    line_moved_away_shorter = away_mov.get("direction") == "shorter"

    rlm = (public_likes_home and line_moved_away_shorter) or \
          (not public_likes_home and line_moved_home_shorter)
    result["rlm"] = rlm

    # Classify
    if rlm:
        sharp_side = game["away_abbr"] if public_likes_home else game["home_abbr"]
        result["sharp_signal"] = "SHARP_AWAY" if public_likes_home else "SHARP_HOME"
        result["sharp_label"] = (
            f"⚡ RLM Alert: {home_t}% of public on "
            f"{'home' if public_likes_home else 'away'}, line moving opposite — "
            f"Sharp money on {sharp_side}"
        )
    elif away_t >= 70:
        result["sharp_signal"] = "PUBLIC_AWAY"
        result["sharp_label"] = f"🐑 Public heavy on {game['away_abbr']} ({away_t}% tickets)"
    elif home_t >= 70:
        result["sharp_signal"] = "PUBLIC_HOME"
        result["sharp_label"] = f"🐑 Public heavy on {game['home_abbr']} ({home_t}% tickets)"
    elif abs(away_t - home_t) <= 10:
        result["sharp_signal"] = "SPLIT"
        result["sharp_label"] = f"Market split: {away_t}% / {home_t}% — no clear lean"
    else:
        result["sharp_label"] = f"Tickets: {game['away_abbr']} {away_t}% / {game['home_abbr']} {home_t}%"

    return result


# ─────────────────────────────────────────────────────────────────────────────
# FEATURE 3: ENHANCED FATIGUE (with travel miles)
# ─────────────────────────────────────────────────────────────────────────────
# Approximate arena coordinates for travel distance
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

def haversine(c1, c2):
    """Distance in miles between two (lat,lon) coords."""
    import math
    lat1,lon1 = [math.radians(x) for x in c1]
    lat2,lon2 = [math.radians(x) for x in c2]
    dlat=lat2-lat1; dlon=lon2-lon1
    a=math.sin(dlat/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return round(3958.8*2*math.asin(math.sqrt(a)))

def build_fatigue_map():
    fatigue = {}
    today   = datetime.now(timezone.utc).date()

    if not NBA_API_OK:
        return build_fatigue_espn_fallback()

    try:
        date_from = (today - timedelta(days=10)).strftime("%m/%d/%Y")
        date_to   = today.strftime("%m/%d/%Y")
        finder    = leaguegamefinder.LeagueGameFinder(
            date_from_nullable=date_from, date_to_nullable=date_to,
            league_id_nullable="00", timeout=30,
        )
        df = finder.get_data_frames()[0]
        def parse_date(x):
            for fmt in ["%Y-%m-%dT%H:%M:%S","%Y-%m-%d"]:
                try: return datetime.strptime(x[:len(fmt)], fmt).date()
                except: pass
            return today
        df["GAME_DATE"] = df["GAME_DATE"].apply(parse_date)

        for abbr in set(NBA_TEAM_MAP.values()):
            tdf = df[df["TEAM_ABBREVIATION"]==abbr].sort_values("GAME_DATE",ascending=False)
            if tdf.empty: fatigue[abbr]=_fresh(); continue

            dates    = list(tdf["GAME_DATE"])
            matchups = list(tdf.get("MATCHUP", []))
            last_game= dates[0]
            days_rest= (today - last_game).days
            g4       = sum(1 for d in dates if (today-d).days<=4)

            # Travel miles: last away game opponent location
            travel_miles = 0
            travel_desc  = ""
            for i, m in enumerate(matchups[:3]):
                if "@" in str(m) and not str(m).startswith(abbr):
                    # Team was on road — find opponent
                    parts  = str(m).split("@")
                    opp    = parts[-1].strip().split()[0] if len(parts)>1 else None
                    if opp and opp in ARENA_COORDS and abbr in ARENA_COORDS:
                        miles = haversine(ARENA_COORDS[abbr], ARENA_COORDS[opp])
                        if miles > travel_miles:
                            travel_miles = miles
                            travel_desc  = f"{miles:,} mi road trip"
                    break

            cross_trip = travel_miles > 1500

            if days_rest == 1:
                status="B2B"; label="Back-to-Back ⚠️"; battery=25
            elif g4>=3:
                status="3IN4"; label=f"3rd in 4 nights 🔴"; battery=40
            elif days_rest<=2 and cross_trip:
                status="TRAVEL"; label=f"Cross-country trip 🟣"; battery=50
            elif days_rest<=2:
                status="SOME_REST"; label="1 day rest"; battery=70
            else:
                status="FRESH"; label="Well rested ✅"; battery=100

            fatigue[abbr]={
                "status":status,"label":label,"battery":battery,
                "last_game":last_game.strftime("%b %d"),
                "games_last_4d":g4,"days_rest":days_rest,
                "travel_miles":travel_miles,"travel_desc":travel_desc,
                "cross_trip":cross_trip,
            }
        print(f"[INFO] Fatigue map: {len(fatigue)} teams.")
    except Exception as e:
        print(f"[WARN] Fatigue nba_api: {e}"); return build_fatigue_espn_fallback()
    return fatigue

def build_fatigue_espn_fallback():
    """Minimal fallback — marks all teams fresh."""
    return {a: _fresh() for a in set(NBA_TEAM_MAP.values())}

def _fresh(last=None):
    return {"status":"FRESH","label":"Well rested ✅","battery":100,
            "last_game":last or "—","games_last_4d":0,"days_rest":3,
            "travel_miles":0,"travel_desc":"","cross_trip":False}


# ─────────────────────────────────────────────────────────────────────────────
# ODDS + LINE MOVEMENT
# ─────────────────────────────────────────────────────────────────────────────
def ml_to_prob(ml):
    try:
        ml=int(str(ml).replace("+",""))
        return abs(ml)/(abs(ml)+100)*100 if ml<0 else 100/(ml+100)*100
    except: return 50.0

def remove_vig(p1,p2):
    t=p1+p2
    if t==0: return 50.0,50.0
    return round(p1/t*100,1),round(p2/t*100,1)

def conf_tier(p):
    if p>=72: return "HIGH","★★★"
    if p>=58: return "MEDIUM","★★☆"
    return "LOW","★☆☆"

def fetch_odds():
    if not ODDS_API_KEY: print("[WARN] No ODDS_API_KEY."); return []
    url=(f"https://api.the-odds-api.com/v4/sports/basketball_nba/odds/"
         f"?apiKey={ODDS_API_KEY}&regions=us&markets=h2h,spreads"
         "&oddsFormat=american&bookmakers=draftkings")
    try:
        r=requests.get(url,timeout=12); r.raise_for_status()
        d=r.json(); print(f"[INFO] Odds API: {len(d)} games."); return d
    except Exception as e:
        print(f"[ERROR] Odds API: {e}"); return []

def _parse_raw(s):
    try: return int(str(s).replace("+",""))
    except: return None

def load_prev_odds():
    prev={}
    try:
        with open("data.json") as f: old=json.load(f)
        for g in old.get("games",[]):
            prev[g["game"]]={
                "home_ml_raw":_parse_raw(g.get("home_ml","N/A")),
                "away_ml_raw":_parse_raw(g.get("away_ml","N/A")),
                "snapshot_time":old.get("last_updated",""),
            }
    except: pass
    return prev

def classify_movement(old_ml, new_ml):
    if old_ml is None or new_ml is None:
        return {"direction":"none","points_moved":0,"steam":False,
                "label":"—","old_ml":"—","new_ml":"—"}
    moved=new_ml-old_ml; steam=abs(moved)>=8
    old_f=f"+{old_ml}" if old_ml>0 else str(old_ml)
    new_f=f"+{new_ml}" if new_ml>0 else str(new_ml)
    if moved==0:
        return {"direction":"none","points_moved":0,"steam":False,
                "label":"No movement","old_ml":old_f,"new_ml":new_f}
    direction="shorter" if moved<0 else "longer"
    tag="🔥 STEAM" if steam else ("🟢" if moved<0 else "🔴")
    label=f"{tag} {new_f} ← {old_f}"
    return {"direction":direction,"points_moved":abs(moved),"steam":steam,
            "label":label,"old_ml":old_f,"new_ml":new_f}

def normalise_odds(raw, prev_odds, fatigue):
    games=[]
    for g in raw:
        home=g.get("home_team",""); away=g.get("away_team","")
        ha=NBA_TEAM_MAP.get(home,home[:3].upper())
        aa=NBA_TEAM_MAP.get(away,away[:3].upper())
        hmr=amr=None; hmf=amf="N/A"; spread_str="N/A"

        for bm in g.get("bookmakers",[]):
            for mkt in bm.get("markets",[]):
                if mkt["key"]=="h2h":
                    for o in mkt.get("outcomes",[]):
                        p=o["price"]; f=f"+{p}" if p>0 else str(p)
                        if o["name"]==home: hmr=p; hmf=f
                        elif o["name"]==away: amr=p; amf=f
                elif mkt["key"]=="spreads":
                    for o in mkt.get("outcomes",[]):
                        if o["name"]==home:
                            pt=o["point"]; spread_str=f"{ha} {'+' if pt>0 else ''}{pt}"
            break

        hp,ap=remove_vig(ml_to_prob(hmr),ml_to_prob(amr)) if hmr and amr else (50.0,50.0)
        if hp>=ap: pick=home; pa=ha; pm=hmf; pp=hp
        else:       pick=away; pa=aa; pm=amf; pp=ap

        conf,stars=conf_tier(pp)
        key=f"{away} @ {home}"
        prev=prev_odds.get(key,{})
        hm=classify_movement(prev.get("home_ml_raw"),hmr)
        am=classify_movement(prev.get("away_ml_raw"),amr)

        hf=fatigue.get(ha,_fresh()); af=fatigue.get(aa,_fresh())

        # Edge factors
        ef=[]
        if (hm.get("steam") or am.get("steam")): ef.append("🔥 Sharp steam detected")
        if hf["status"] in ["B2B","3IN4"] and pick==away: ef.append(f"💤 {ha} fatigued ({hf['status']})")
        if af["status"] in ["B2B","3IN4"] and pick==home: ef.append(f"💤 {aa} fatigued ({af['status']})")
        if hf.get("travel_miles",0)>1500 and pick==away: ef.append(f"✈️ {ha} traveled {hf['travel_miles']:,} mi")
        if af.get("travel_miles",0)>1500 and pick==home: ef.append(f"✈️ {aa} traveled {af['travel_miles']:,} mi")
        if not ef: ef.append(f"📊 {round(pp-round(100-pp,1),1)}pt probability edge")

        games.append({
            "game":key,"home_team":home,"away_team":away,
            "home_abbr":ha,"away_abbr":aa,
            "home_ml":hmf,"away_ml":amf,
            "home_ml_raw":hmr,"away_ml_raw":amr,
            "home_prob":hp,"away_prob":ap,
            "spread":spread_str,"favorite":pick,
            "pick":pick,"pick_abbr":pa,"moneyline":pm,
            "win_probability":pp,"confidence":conf,"confidence_stars":stars,
            "commence_time":g.get("commence_time",""),
            "line_movement":{"home":hm,"away":am,"pick":hm if pick==home else am,
                             "snapshot_time":prev.get("snapshot_time","—")},
            "fatigue":{"home":hf,"away":af},
            "edge_factors":ef,
        })
    return games


# ─────────────────────────────────────────────────────────────────────────────
# INJURIES
# ─────────────────────────────────────────────────────────────────────────────
def fetch_injuries():
    injuries={}
    try:
        r=requests.get("https://www.espn.com/nba/injuries",headers=HEADERS,timeout=10)
        soup=BeautifulSoup(r.content,"html.parser")
        for sec in soup.select(".ResponsiveTable"):
            t=sec.select_one(".Table__Title")
            if not t: continue
            abbr=NBA_TEAM_MAP.get(t.get_text(strip=True),t.get_text(strip=True)[:3].upper())
            for row in sec.select("tr.Table__TR--sm"):
                cells=row.select("td")
                if len(cells)>=3:
                    injuries.setdefault(abbr,[]).append({
                        "player":cells[0].get_text(strip=True),
                        "status":cells[1].get_text(strip=True),
                        "reason":cells[2].get_text(strip=True),
                    })
        print(f"[INFO] Injuries: {len(injuries)} teams.")
    except Exception as e: print(f"[WARN] Injuries: {e}")
    return injuries


# ─────────────────────────────────────────────────────────────────────────────
# NEWS
# ─────────────────────────────────────────────────────────────────────────────
def fetch_rss():
    arts=[]
    for url in FEED_SOURCES:
        try:
            feed=feedparser.parse(url)
            for e in feed.entries[:30]:
                arts.append({"title":e.get("title",""),"summary":e.get("summary","")[:300],
                             "link":e.get("link",""),"source":feed.feed.get("title",url),
                             "published":e.get("published","")})
        except Exception as e: print(f"[WARN] RSS: {e}")
    print(f"[INFO] RSS: {len(arts)} articles.")
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
    q=f"{t1.split()[-1]} OR {t2.split()[-1]} NBA"
    url=(f"https://newsapi.org/v2/everything?q={q}"
         f"&sortBy=publishedAt&language=en&pageSize=3&apiKey={NEWS_API_KEY}")
    try:
        r=requests.get(url,timeout=8).json()
        return [{"title":a.get("title",""),"link":a.get("url",""),
                 "source":(a.get("source")or{}).get("name","NewsAPI"),
                 "published":a.get("publishedAt","")} for a in r.get("articles",[])]
    except: return []


# ─────────────────────────────────────────────────────────────────────────────
# FEATURE 4: STRUCTURED BRIEFING OBJECT
# ─────────────────────────────────────────────────────────────────────────────
def build_structured_briefing(game, injuries, sharp):
    """
    Builds a structured 3-section object:
      trend    — ATS/form trend
      matchup  — key player vs defence matchup
      steam    — line/market signal
    """
    ha=game["home_abbr"]; aa=game["away_abbr"]
    hi=injuries.get(ha,[]); ai=injuries.get(aa,[])
    inj="; ".join(f"{p['player']} ({p['status']})" for p in (hi+ai)[:4]) or "No key injuries."

    lm=game["line_movement"]
    pm=lm.get("pick",{})
    op=pm.get("old_ml","—"); np=pm.get("new_ml","—")
    pts=pm.get("points_moved",0)

    hf=game["fatigue"]["home"]; af=game["fatigue"]["away"]
    hmi=hf.get("travel_miles",0); ami=af.get("travel_miles",0)

    # TREND section
    trend_parts=[]
    if game["pick"]==game["home_team"]:
        trend_parts.append(f"{ha} hold a {game['win_probability']}% implied win probability at home.")
    else:
        trend_parts.append(f"{aa} carry a {game['win_probability']}% implied win probability on the road.")
    if hf["status"] in ["B2B","3IN4"]:
        trend_parts.append(f"{ha} are on a {hf['label']} — NBA teams on B2B historically win ~42% of games.")
    if af["status"] in ["B2B","3IN4"]:
        trend_parts.append(f"{aa} are on a {af['label']}.")
    if hmi>1500: trend_parts.append(f"{ha} have traveled {hmi:,} miles recently ({hf.get('travel_desc','')}).")
    if ami>1500: trend_parts.append(f"{aa} have traveled {ami:,} miles recently ({af.get('travel_desc','')}).")
    trend=" ".join(trend_parts) or f"{game['pick']} hold a {game['win_probability']}% edge."

    # MATCHUP section
    key_inj_home=[p for p in hi if p["status"].lower() in ["out","doubtful","questionable"]]
    key_inj_away=[p for p in ai if p["status"].lower() in ["out","doubtful","questionable"]]
    matchup_parts=[]
    if key_inj_home: matchup_parts.append(f"{ha} injury watch: {', '.join(p['player']+' ('+p['status']+')' for p in key_inj_home[:2])}.")
    if key_inj_away: matchup_parts.append(f"{aa} injury watch: {', '.join(p['player']+' ('+p['status']+')' for p in key_inj_away[:2])}.")
    if not matchup_parts: matchup_parts.append(f"Both rosters appear healthy — a clean line to bet.")
    matchup=" ".join(matchup_parts)

    # STEAM section
    if pm.get("steam"):
        steam=f"Line opened at {op}, steamed to {np} — {pts} pt move indicates heavy professional action on {game['pick']}."
    elif pts>0:
        steam=f"Line has moved from {op} to {np} ({pts} pts). "
        if sharp.get("rlm"):
            steam+=sharp.get("sharp_label","")
        else:
            steam+="Gradual movement consistent with normal public action."
    else:
        steam="Line has held firm since open — market consensus is stable."

    # Add public sentiment
    at=sharp.get("away_tickets_pct"); ht=sharp.get("home_tickets_pct")
    if at is not None and ht is not None:
        steam+=f" Public split: {aa} {at}% / {ha} {ht}% of tickets. "
        if sharp.get("rlm"): steam+="⚡ Reverse line movement detected — sharp money opposing public."

    return {"trend":trend, "matchup":matchup, "steam":steam, "injury_notes":inj}


# ─────────────────────────────────────────────────────────────────────────────
# RESULTS GRADING
# ─────────────────────────────────────────────────────────────────────────────
def fetch_scores_for_grading():
    if not ODDS_API_KEY: return {}
    url=f"https://api.the-odds-api.com/v4/sports/basketball_nba/scores/?apiKey={ODDS_API_KEY}&daysFrom=2"
    try:
        r=requests.get(url,timeout=10); r.raise_for_status()
        scores={}
        for g in r.json():
            if not g.get("completed"): continue
            home=g["home_team"]; away=g["away_team"]
            sc={s["name"]:int(s["score"]) for s in (g.get("scores") or []) if s.get("score")}
            scores[f"{away} @ {home}"]={
                "home":home,"away":away,
                "home_score":sc.get(home,0),"away_score":sc.get(away,0),
            }
        return scores
    except Exception as e: print(f"[WARN] Scores: {e}"); return {}

def load_results():
    try:
        with open("results.json") as f: return json.load(f)
    except: return {"picks":[],"summary":{}}

def save_results(r):
    with open("results.json","w") as f: json.dump(r,f,indent=2)

def grade_picks(results,scores):
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
        p["profit"]=round((ml if ml>0 else 100/abs(ml)*100) if won else -100,2)
        changed=True
    if changed: recalc(results)
    return changed

def recalc(results):
    from collections import defaultdict
    b=defaultdict(lambda:{"wins":0,"losses":0,"profit":0.0})
    now=datetime.now(timezone.utc)
    for p in results["picks"]:
        if not p.get("graded"): continue
        c=p.get("confidence","LOW")
        b[c]["wins"]   +=1 if p["result"]=="WIN" else 0
        b[c]["losses"] +=1 if p["result"]=="LOSS" else 0
        b[c]["profit"] +=p.get("profit",0)
    s={}
    for c,v in b.items():
        t=v["wins"]+v["losses"]
        s[c]={"wins":v["wins"],"losses":v["losses"],"total":t,
              "win_pct":round(v["wins"]/t*100,1) if t else 0,
              "roi":round(v["profit"]/(t*100)*100,1) if t else 0,
              "profit":round(v["profit"],2)}
    for lbl,days in [("7d",7),("30d",30)]:
        cut=now-timedelta(days=days)
        wp=[p for p in results["picks"] if p.get("graded") and
            datetime.fromisoformat(p.get("date","2000-01-01T00:00:00+00:00"))>=cut]
        t=len(wp); w=sum(1 for p in wp if p["result"]=="WIN")
        pr=sum(p.get("profit",0) for p in wp)
        s[f"overall_{lbl}"]={"wins":w,"losses":t-w,"total":t,
                              "win_pct":round(w/t*100,1) if t else 0,
                              "roi":round(pr/(t*100)*100,1) if t else 0,
                              "profit":round(pr,2)}
    results["summary"]=s
    results["last_graded"]=now.isoformat()

def record_picks(games,results):
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
# MASTER
# ─────────────────────────────────────────────────────────────────────────────
def build_data():
    print("[INFO] === ParlAID v4 ===")

    results=load_results()
    scores=fetch_scores_for_grading()
    if scores: grade_picks(results,scores)

    prev_odds  = load_prev_odds()
    fatigue    = build_fatigue_map()
    raw_odds   = fetch_odds()
    games      = normalise_odds(raw_odds,prev_odds,fatigue)
    injuries   = fetch_injuries()
    espn_sb    = fetch_espn_scoreboard()
    public_data= fetch_public_betting()
    all_rss    = fetch_rss()

    output=[]
    for g in games:
        home=g["home_team"]; away=g["away_team"]
        ns=game_news(home,away,all_rss) or game_news_api(home,away)
        if not game_news(home,away,all_rss): time.sleep(0.2)

        sharp=enrich_sharp_signal(g,public_data,g["line_movement"])
        briefing=build_structured_briefing(g,injuries,sharp)

        # Attach live score data if game is in progress
        live=espn_sb.get(g["game"],espn_sb.get(f"{g['away_abbr']} @ {g['home_abbr']}",{}))

        output.append({
            **g,
            "briefing":   briefing,
            "injury_notes":briefing["injury_notes"],
            "sharp":      sharp,
            "live": {
                "status":         live.get("status","pre"),
                "status_detail":  live.get("status_detail","Upcoming"),
                "period":         live.get("period",0),
                "clock":          live.get("clock",""),
                "home_score":     live.get("home_score",0),
                "away_score":     live.get("away_score",0),
                "spread_alert":   live.get("spread_alert"),
                "espn_spread":    live.get("espn_spread",""),
            },
            "news":[{"title":n["title"],"source":n["source"],
                     "link":n["link"],"published":n.get("published","")}
                    for n in ns[:3]],
        })

    record_picks(output,results)
    save_results(results)

    payload={
        "last_updated":datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC"),
        "total_games":len(output),
        "games":output,
        "track_record":results.get("summary",{}),
    }
    with open("data.json","w") as f: json.dump(payload,f,indent=2)
    print(f"[DONE] data.json — {len(output)} games.")
    for g in output:
        live_s=g["live"]["status_detail"]
        sharp_s=g["sharp"].get("sharp_label","—")
        print(f"  {g['game']:42s}  {g['pick']:22s}  {g['confidence']}  LIVE:{live_s}  {sharp_s[:40]}")

if __name__=="__main__":
    build_data()
