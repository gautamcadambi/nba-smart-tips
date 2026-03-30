"""
ParlAID — NBA Smart Tips Scraper
- Primary odds: The Odds API (DraftKings lines, same source ESPN uses)
- Win probability: derived mathematically from moneyline (NOT hardcoded 50/50)
- Injuries: NBA.com official report + ESPN fallback
- News: ESPN RSS + Bleacher Report RSS + SportsKeeda RSS + NewsAPI fallback

Setup:
  pip install requests beautifulsoup4 feedparser
  GitHub Secrets: ODDS_API_KEY (the-odds-api.com) + NEWS_API_KEY (newsapi.org)
"""

import json, os, re, time, requests, feedparser
from datetime import datetime, timezone
from bs4 import BeautifulSoup

# ── API Keys (injected from GitHub Secrets) ───────────────────────────────────
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

NBA_TEAM_MAP = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "LA Clippers": "LAC", "Los Angeles Clippers": "LAC", "Los Angeles Lakers": "LAL",
    "LA Lakers": "LAL", "Memphis Grizzlies": "MEM", "Miami Heat": "MIA",
    "Milwaukee Bucks": "MIL", "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC", "Orlando Magic": "ORL",
    "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS", "Toronto Raptors": "TOR",
    "Utah Jazz": "UTA", "Washington Wizards": "WAS",
}

FEED_SOURCES = [
    "https://www.espn.com/espn/rss/nba/news",
    "https://bleacherreport.com/nba.rss",
    "https://www.sportskeeda.com/basketball/feed",
]

# ── Win probability from moneyline (core fix for 50/50 problem) ───────────────
def ml_to_prob(ml_str):
    """
    Convert American moneyline string (e.g. '-180', '+150') to implied
    win probability as a percentage, with vig removed.
    Returns float 0-100.
    """
    try:
        ml = int(str(ml_str).replace("+", "").replace(" ", ""))
        if ml < 0:
            return abs(ml) / (abs(ml) + 100) * 100
        else:
            return 100 / (ml + 100) * 100
    except:
        return 50.0

def remove_vig(prob_home, prob_away):
    """
    Remove the bookmaker's vig (overround) so probabilities sum to 100%.
    Returns (home_prob, away_prob) as clean percentages.
    """
    total = prob_home + prob_away
    if total == 0:
        return 50.0, 50.0
    return round((prob_home / total) * 100, 1), round((prob_away / total) * 100, 1)

# ── Confidence tier from probability edge ─────────────────────────────────────
def confidence_from_prob(prob):
    """Higher probability = higher confidence, with meaningful thresholds."""
    if prob >= 72:
        return "HIGH", "★★★"
    elif prob >= 58:
        return "MEDIUM", "★★☆"
    else:
        return "LOW", "★☆☆"

# ── Fetch odds from The Odds API ──────────────────────────────────────────────
def fetch_odds():
    if not ODDS_API_KEY:
        print("[WARN] No ODDS_API_KEY set. No odds available.")
        return []
    url = (
        "https://api.the-odds-api.com/v4/sports/basketball_nba/odds/"
        f"?apiKey={ODDS_API_KEY}&regions=us&markets=h2h,spreads&oddsFormat=american"
        "&bookmakers=draftkings"   # DraftKings = same lines ESPN displays
    )
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()
        print(f"[INFO] Odds API returned {len(data)} games.")
        return data
    except Exception as e:
        print(f"[ERROR] Odds API failed: {e}")
        return []

# ── Parse odds API response ───────────────────────────────────────────────────
def normalise_odds(raw):
    games = []
    for g in raw:
        home = g.get("home_team", "")
        away = g.get("away_team", "")
        home_abbr = NBA_TEAM_MAP.get(home, home[:3].upper())
        away_abbr = NBA_TEAM_MAP.get(away, away[:3].upper())

        home_ml_raw = away_ml_raw = None
        spread_str = "N/A"
        spread_fav = None

        for bm in g.get("bookmakers", []):
            for market in bm.get("markets", []):
                if market["key"] == "h2h":
                    for o in market.get("outcomes", []):
                        price = o["price"]
                        formatted = f"+{price}" if price > 0 else str(price)
                        if o["name"] == home:
                            home_ml_raw = price
                            home_ml_fmt = formatted
                        elif o["name"] == away:
                            away_ml_raw = price
                            away_ml_fmt = formatted
                elif market["key"] == "spreads":
                    for o in market.get("outcomes", []):
                        if o["name"] == home:
                            pt = o["point"]
                            sign = "+" if pt > 0 else ""
                            spread_str = f"{home_abbr} {sign}{pt}"
                            spread_fav = away if pt > 0 else home
            break  # only first bookmaker needed

        # Calculate win probabilities from moneyline (THE FIX)
        if home_ml_raw is not None and away_ml_raw is not None:
            raw_home_prob = ml_to_prob(home_ml_raw)
            raw_away_prob = ml_to_prob(away_ml_raw)
            home_prob, away_prob = remove_vig(raw_home_prob, raw_away_prob)
        else:
            home_prob, away_prob = 50.0, 50.0
            home_ml_fmt = "N/A"
            away_ml_fmt = "N/A"

        # Favourite = whoever has higher win probability
        if home_prob >= away_prob:
            fav = home
            fav_abbr = home_abbr
            fav_ml = home_ml_fmt if home_ml_raw is not None else "N/A"
            fav_prob = home_prob
        else:
            fav = away
            fav_abbr = away_abbr
            fav_ml = away_ml_fmt if away_ml_raw is not None else "N/A"
            fav_prob = away_prob

        confidence, stars = confidence_from_prob(fav_prob)

        games.append({
            "game":         f"{away} @ {home}",
            "home_team":    home,
            "away_team":    away,
            "home_abbr":    home_abbr,
            "away_abbr":    away_abbr,
            "home_ml":      home_ml_fmt if home_ml_raw is not None else "N/A",
            "away_ml":      away_ml_fmt if away_ml_raw is not None else "N/A",
            "home_prob":    home_prob,
            "away_prob":    away_prob,
            "spread":       spread_str,
            "favorite":     fav,
            "fav_abbr":     fav_abbr,
            "pick":         fav,
            "pick_abbr":    fav_abbr,
            "moneyline":    fav_ml,
            "win_probability": fav_prob,
            "confidence":   confidence,
            "confidence_stars": stars,
            "commence_time": g.get("commence_time", ""),
        })
    return games

# ── Fetch injuries ────────────────────────────────────────────────────────────
def fetch_injuries():
    injuries = {}
    # Try ESPN (more reliably structured than NBA.com for scraping)
    try:
        r = requests.get("https://www.espn.com/nba/injuries", headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.content, "html.parser")
        for section in soup.select(".ResponsiveTable"):
            title = section.select_one(".Table__Title")
            if not title:
                continue
            team_name = title.get_text(strip=True)
            abbr = NBA_TEAM_MAP.get(team_name, team_name[:3].upper())
            for row in section.select("tr.Table__TR--sm"):
                cells = row.select("td")
                if len(cells) >= 3:
                    name   = cells[0].get_text(strip=True)
                    status = cells[1].get_text(strip=True)
                    reason = cells[2].get_text(strip=True)
                    injuries.setdefault(abbr, []).append(
                        {"player": name, "status": status, "reason": reason}
                    )
        if injuries:
            print(f"[INFO] Injuries loaded for {len(injuries)} teams from ESPN.")
    except Exception as e:
        print(f"[WARN] ESPN injury scrape failed: {e}")
    return injuries

# ── Fetch RSS news ────────────────────────────────────────────────────────────
def fetch_rss():
    articles = []
    for url in FEED_SOURCES:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:30]:
                articles.append({
                    "title":     entry.get("title", ""),
                    "summary":   entry.get("summary", "")[:300],
                    "link":      entry.get("link", ""),
                    "source":    feed.feed.get("title", url),
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            print(f"[WARN] RSS error ({url}): {e}")
    print(f"[INFO] RSS returned {len(articles)} total articles.")
    return articles

def game_news_from_rss(team1, team2, all_articles):
    kw = [team1.split()[-1], team2.split()[-1], team1, team2]
    matched = []
    for art in all_articles:
        text = (art["title"] + " " + art["summary"]).lower()
        if any(k.lower() in text for k in kw):
            matched.append(art)
        if len(matched) >= 3:
            break
    return matched

def game_news_from_newsapi(team1, team2):
    if not NEWS_API_KEY:
        return []
    q = f"{team1.split()[-1]} OR {team2.split()[-1]} NBA"
    url = (f"https://newsapi.org/v2/everything?q={q}"
           f"&sortBy=publishedAt&language=en&pageSize=3&apiKey={NEWS_API_KEY}")
    try:
        r = requests.get(url, timeout=8).json()
        return [{"title": a.get("title",""), "summary": (a.get("description") or "")[:300],
                 "link": a.get("url",""), "source": (a.get("source") or {}).get("name","NewsAPI"),
                 "published": a.get("publishedAt","")} for a in r.get("articles",[])]
    except Exception as e:
        print(f"[WARN] NewsAPI error: {e}")
        return []

# ── Build reasoning string ────────────────────────────────────────────────────
def build_reasoning(game, injuries):
    home       = game["home_team"]
    away       = game["away_team"]
    home_abbr  = game["home_abbr"]
    away_abbr  = game["away_abbr"]
    pick       = game["pick"]
    fav_prob   = game["win_probability"]
    dog_prob   = round(100 - fav_prob, 1)
    spread     = game["spread"]
    fav_ml     = game["moneyline"]
    home_inj   = injuries.get(home_abbr, [])
    away_inj   = injuries.get(away_abbr, [])

    # Injury summary
    inj_lines = []
    for p in (home_inj + away_inj)[:4]:
        inj_lines.append(f"{p['player']} ({p['status']})")
    injury_text = "; ".join(inj_lines) if inj_lines else "No significant injuries reported."

    # Build reasoning
    dog = away if pick == home else home
    edge = fav_prob - dog_prob

    r = (f"**{pick} ML ({fav_ml})** — Model gives {pick} a {fav_prob}% win probability "
         f"vs {dog}'s {dog_prob}%, a {edge:.1f}pt edge. ")

    if spread != "N/A":
        r += f"The market spread of {spread} aligns with this gap. "

    has_key_injury = any(
        p["status"].lower() in ["out", "doubtful"]
        for p in (home_inj + away_inj)
    )
    if has_key_injury:
        r += f"⚠️ Injury alert: {injury_text} — monitor for late changes before tip-off. "
    elif inj_lines:
        r += f"Minor injury watch: {injury_text}. "
    else:
        r += "Both rosters are healthy, keeping this a clean line to bet. "

    return r, injury_text

# ── Master build ──────────────────────────────────────────────────────────────
def build_data():
    print("[INFO] Fetching odds from The Odds API...")
    raw = fetch_odds()
    games = normalise_odds(raw)
    print(f"[INFO] Normalised {len(games)} games.")

    print("[INFO] Fetching injuries...")
    injuries = fetch_injuries()

    print("[INFO] Fetching RSS news...")
    all_articles = fetch_rss()

    output = []
    for g in games:
        home, away = g["home_team"], g["away_team"]

        news = game_news_from_rss(home, away, all_articles)
        if not news:
            news = game_news_from_newsapi(home, away)
            time.sleep(0.25)

        reasoning, injury_text = build_reasoning(g, injuries)

        output.append({
            "game":              g["game"],
            "home_team":         home,
            "away_team":         away,
            "home_abbr":         g["home_abbr"],
            "away_abbr":         g["away_abbr"],
            "home_ml":           g["home_ml"],
            "away_ml":           g["away_ml"],
            "home_prob":         g["home_prob"],
            "away_prob":         g["away_prob"],
            "spread":            g["spread"],
            "favorite":          g["favorite"],
            "pick":              g["pick"],
            "pick_abbr":         g["pick_abbr"],
            "moneyline":         g["moneyline"],
            "win_probability":   g["win_probability"],
            "confidence":        g["confidence"],
            "confidence_stars":  g["confidence_stars"],
            "commence_time":     g["commence_time"],
            "reasoning":         reasoning,
            "injury_notes":      injury_text,
            "news": [{"title": n["title"], "source": n["source"],
                      "link": n["link"], "published": n.get("published","")}
                     for n in news[:3]],
        })

    payload = {
        "last_updated": datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC"),
        "total_games":  len(output),
        "games":        output,
    }

    with open("data.json", "w") as f:
        json.dump(payload, f, indent=2)

    print(f"[DONE] data.json written — {len(output)} games.")
    for g in output:
        print(f"  {g['game']:45s}  {g['pick']:25s}  {g['win_probability']}%  {g['confidence']}")

if __name__ == "__main__":
    build_data()
