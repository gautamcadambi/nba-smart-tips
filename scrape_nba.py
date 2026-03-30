"""
ParlAID — NBA Smart Tips Scraper
- Primary odds: The Odds API (DraftKings lines)
- Win probability: mathematically derived from moneyline (vig-removed)
- Injuries: ESPN + NBA.com
- News: ESPN RSS + Bleacher Report RSS + SportsKeeda RSS + NewsAPI fallback
- Standings: written into data.json for dashboard context

Setup:
  pip install requests beautifulsoup4 feedparser
  GitHub Secrets: ODDS_API_KEY (the-odds-api.com) + NEWS_API_KEY (newsapi.org)
"""

import json, os, re, time, requests, feedparser
from datetime import datetime, timezone
from bs4 import BeautifulSoup

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

FEED_SOURCES = [
    "https://www.espn.com/espn/rss/nba/news",
    "https://bleacherreport.com/nba.rss",
    "https://www.sportskeeda.com/basketball/feed",
]

# ── Moneyline → probability ───────────────────────────────────────────────────
def ml_to_prob(ml):
    try:
        ml = int(str(ml).replace("+","").replace(" ",""))
        return abs(ml)/(abs(ml)+100)*100 if ml < 0 else 100/(ml+100)*100
    except:
        return 50.0

def remove_vig(p1, p2):
    t = p1 + p2
    if t == 0: return 50.0, 50.0
    return round(p1/t*100,1), round(p2/t*100,1)

def confidence_from_prob(p):
    if p >= 72: return "HIGH",   "★★★"
    if p >= 58: return "MEDIUM", "★★☆"
    return          "LOW",    "★☆☆"

# ── Odds API ──────────────────────────────────────────────────────────────────
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

def normalise_odds(raw):
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
                if mkt["key"] == "h2h":
                    for o in mkt.get("outcomes",[]):
                        p = o["price"]; fmt = f"+{p}" if p>0 else str(p)
                        if o["name"]==home: home_ml_raw=p; home_ml_fmt=fmt
                        elif o["name"]==away: away_ml_raw=p; away_ml_fmt=fmt
                elif mkt["key"] == "spreads":
                    for o in mkt.get("outcomes",[]):
                        if o["name"]==home:
                            pt=o["point"]; sign="+" if pt>0 else ""
                            spread_str=f"{home_abbr} {sign}{pt}"
            break

        if home_ml_raw is not None and away_ml_raw is not None:
            hp, ap = remove_vig(ml_to_prob(home_ml_raw), ml_to_prob(away_ml_raw))
        else:
            hp = ap = 50.0

        if hp >= ap:
            pick=home; pick_abbr=home_abbr; pick_ml=home_ml_fmt; pick_prob=hp
        else:
            pick=away; pick_abbr=away_abbr; pick_ml=away_ml_fmt; pick_prob=ap

        conf, stars = confidence_from_prob(pick_prob)
        games.append({
            "game":f"{away} @ {home}","home_team":home,"away_team":away,
            "home_abbr":home_abbr,"away_abbr":away_abbr,
            "home_ml":home_ml_fmt,"away_ml":away_ml_fmt,
            "home_prob":hp,"away_prob":ap,
            "spread":spread_str,"favorite":pick,
            "pick":pick,"pick_abbr":pick_abbr,"moneyline":pick_ml,
            "win_probability":pick_prob,"confidence":conf,"confidence_stars":stars,
            "commence_time":g.get("commence_time",""),
        })
    return games

# ── Injuries ──────────────────────────────────────────────────────────────────
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
        if injuries: print(f"[INFO] Injuries: {len(injuries)} teams.")
    except Exception as e:
        print(f"[WARN] Injuries: {e}")
    return injuries

# ── RSS News ──────────────────────────────────────────────────────────────────
def fetch_rss():
    arts = []
    for url in FEED_SOURCES:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:30]:
                arts.append({
                    "title":e.get("title",""),"summary":e.get("summary","")[:300],
                    "link":e.get("link",""),"source":feed.feed.get("title",url),
                    "published":e.get("published",""),
                })
        except Exception as e:
            print(f"[WARN] RSS {url}: {e}")
    print(f"[INFO] RSS: {len(arts)} articles.")
    return arts

def game_news_rss(t1, t2, arts):
    kw = [t1.split()[-1], t2.split()[-1], t1, t2]
    out=[]
    for a in arts:
        if any(k.lower() in (a["title"]+" "+a["summary"]).lower() for k in kw):
            out.append(a)
        if len(out)>=3: break
    return out

def game_news_api(t1, t2):
    if not NEWS_API_KEY: return []
    q=f"{t1.split()[-1]} OR {t2.split()[-1]} NBA"
    url=(f"https://newsapi.org/v2/everything?q={q}"
         f"&sortBy=publishedAt&language=en&pageSize=3&apiKey={NEWS_API_KEY}")
    try:
        r=requests.get(url,timeout=8).json()
        return [{"title":a.get("title",""),"summary":(a.get("description")or"")[:300],
                 "link":a.get("url",""),"source":(a.get("source")or{}).get("name","NewsAPI"),
                 "published":a.get("publishedAt","")} for a in r.get("articles",[])]
    except Exception as e:
        print(f"[WARN] NewsAPI: {e}"); return []

# ── Reasoning ─────────────────────────────────────────────────────────────────
def build_reasoning(game, injuries):
    ha=game["home_abbr"]; aa=game["away_abbr"]
    hi=injuries.get(ha,[]); ai=injuries.get(aa,[])
    inj="; ".join(f"{p['player']} ({p['status']})" for p in (hi+ai)[:4]) or "No significant injuries reported."
    pick=game["pick"]; prob=game["win_probability"]; dog_prob=round(100-prob,1)
    dog=game["away_team"] if pick==game["home_team"] else game["home_team"]
    r=(f"**{pick} ML ({game['moneyline']})** — Model gives {pick} a {prob}% win probability "
       f"vs {dog}'s {dog_prob}%, a {round(prob-dog_prob,1)}pt edge. "
       f"Spread: {game['spread']}. ")
    key_out=any(p["status"].lower() in ["out","doubtful"] for p in (hi+ai))
    if key_out: r+=f"⚠️ Key injury alert: {inj}"
    elif inj!="No significant injuries reported.": r+=f"Minor injury watch: {inj}"
    else: r+="Both rosters appear healthy."
    return r, inj

# ── Master ────────────────────────────────────────────────────────────────────
def build_data():
    print("[INFO] Fetching odds...")
    games = normalise_odds(fetch_odds())
    print(f"[INFO] {len(games)} games normalised.")

    print("[INFO] Fetching injuries...")
    injuries = fetch_injuries()

    print("[INFO] Fetching RSS...")
    all_rss = fetch_rss()

    output = []
    for g in games:
        home=g["home_team"]; away=g["away_team"]
        news = game_news_rss(home,away,all_rss) or game_news_api(home,away)
        if not game_news_rss(home,away,all_rss): time.sleep(0.25)
        reasoning, inj_text = build_reasoning(g, injuries)
        output.append({
            **g,
            "reasoning":    reasoning,
            "injury_notes": inj_text,
            "news": [{"title":n["title"],"source":n["source"],
                      "link":n["link"],"published":n.get("published","")}
                     for n in news[:3]],
        })

    payload={
        "last_updated": datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC"),
        "total_games":  len(output),
        "games":        output,
    }
    with open("data.json","w") as f:
        json.dump(payload,f,indent=2)

    print(f"[DONE] data.json — {len(output)} games.")
    for g in output:
        print(f"  {g['game']:45s}  {g['pick']:25s}  {g['home_prob']}% / {g['away_prob']}%  {g['confidence']}")

if __name__=="__main__":
    build_data()
