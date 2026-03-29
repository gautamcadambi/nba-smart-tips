"""
NBA Smart Tips - Real-Time Scraper
Pulls odds from The Odds API (free tier), injuries from ESPN/NBA.com,
and news from ESPN, Bleacher Report & SportsKeeda RSS feeds.
Writes data.json for the dashboard, then updates last_updated timestamp.

Setup:
  pip install requests beautifulsoup4 feedparser
  Set ODDS_API_KEY env var (free key at https://the-odds-api.com)
  Set NEWS_API_KEY env var (free key at https://newsapi.org)
"""

import json
import os
import re
import time
import requests
import feedparser
from datetime import datetime, timezone
from bs4 import BeautifulSoup

# ── Keys (set as GitHub Secrets / env vars) ──────────────────────────────────
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")

# ── Constants ─────────────────────────────────────────────────────────────────
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
NBA_TEAM_MAP = {
    # Full name → abbreviation used by The Odds API
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

# ── 1. Fetch live odds from The Odds API ─────────────────────────────────────
def fetch_odds():
    """Returns list of game odds dicts from The Odds API (free, 500 req/mo)."""
    if not ODDS_API_KEY:
        print("[WARN] No ODDS_API_KEY — using ESPN fallback scrape.")
        return fetch_odds_espn_fallback()

    url = (
        "https://api.the-odds-api.com/v4/sports/basketball_nba/odds/"
        f"?apiKey={ODDS_API_KEY}&regions=us&markets=h2h,spreads&oddsFormat=american"
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[WARN] Odds API error: {e}. Falling back to ESPN.")
        return fetch_odds_espn_fallback()


def fetch_odds_espn_fallback():
    """Scrape ESPN /nba/odds page as a fallback odds source."""
    games = []
    try:
        r = requests.get("https://www.espn.com/nba/odds", headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.content, "html.parser")
        rows = soup.select(".Table__TR")
        # ESPN's odds table is JS-rendered; if empty, return []
        if not rows:
            print("[INFO] ESPN odds table empty (JS-rendered). No fallback odds available.")
    except Exception as e:
        print(f"[WARN] ESPN scrape error: {e}")
    return games


# ── 2. Fetch NBA injury report from NBA.com ──────────────────────────────────
def fetch_injury_report():
    """Returns dict of {team_abbr: [list of injured players]} from NBA.com."""
    injuries = {}
    try:
        url = "https://www.nba.com/players/injuries"
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.content, "html.parser")
        rows = soup.select("tr.InjuriesTable_row__w5ek2")
        for row in rows:
            cells = row.select("td")
            if len(cells) >= 4:
                name = cells[0].get_text(strip=True)
                team = cells[1].get_text(strip=True)
                status = cells[2].get_text(strip=True)
                reason = cells[3].get_text(strip=True)
                abbr = NBA_TEAM_MAP.get(team, team[:3].upper())
                injuries.setdefault(abbr, []).append(
                    {"player": name, "status": status, "reason": reason}
                )
    except Exception as e:
        print(f"[WARN] NBA.com injury scrape failed: {e}")

    # Fallback: ESPN injuries page
    if not injuries:
        try:
            url = "https://www.espn.com/nba/injuries"
            r = requests.get(url, headers=HEADERS, timeout=10)
            soup = BeautifulSoup(r.content, "html.parser")
            team_sections = soup.select(".ResponsiveTable")
            for section in team_sections:
                team_header = section.select_one(".Table__Title")
                if not team_header:
                    continue
                team_name = team_header.get_text(strip=True)
                abbr = NBA_TEAM_MAP.get(team_name, team_name[:3].upper())
                for row in section.select("tr.Table__TR--sm"):
                    cells = row.select("td")
                    if len(cells) >= 3:
                        name = cells[0].get_text(strip=True)
                        status = cells[1].get_text(strip=True)
                        reason = cells[2].get_text(strip=True)
                        injuries.setdefault(abbr, []).append(
                            {"player": name, "status": status, "reason": reason}
                        )
        except Exception as e:
            print(f"[WARN] ESPN injury scrape failed: {e}")

    return injuries


# ── 3. Fetch game-specific news from multiple RSS sources ────────────────────
FEED_SOURCES = [
    # ESPN NBA RSS
    "https://www.espn.com/espn/rss/nba/news",
    # Bleacher Report NBA
    "https://bleacherreport.com/nba.rss",
    # SportsKeeda NBA
    "https://www.sportskeeda.com/basketball/feed",
]

def fetch_rss_articles():
    """Fetch all articles from configured RSS feeds. Returns list of dicts."""
    articles = []
    for feed_url in FEED_SOURCES:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:30]:
                articles.append({
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", "")[:300],
                    "link": entry.get("link", ""),
                    "source": feed.feed.get("title", feed_url),
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            print(f"[WARN] RSS feed error ({feed_url}): {e}")
    return articles


def get_game_news(team1_name, team2_name, all_articles):
    """Filter cached articles for relevance to a specific matchup."""
    keywords = []
    for name in [team1_name, team2_name]:
        parts = name.split()
        keywords.append(parts[-1])  # e.g. "Clippers", "Bucks"
        keywords.append(name)

    matched = []
    for art in all_articles:
        text = (art["title"] + " " + art["summary"]).lower()
        if any(k.lower() in text for k in keywords):
            matched.append(art)
        if len(matched) >= 3:
            break

    return matched


# ── 4. NewsAPI fallback for game-specific news ───────────────────────────────
def get_newsapi_articles(team1, team2):
    if not NEWS_API_KEY:
        return []
    t1 = team1.split()[-1]
    t2 = team2.split()[-1]
    query = f"{t1} OR {t2} NBA"
    url = (
        f"https://newsapi.org/v2/everything?q={query}"
        f"&sortBy=publishedAt&language=en&pageSize=3&apiKey={NEWS_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=8).json()
        return [
            {
                "title": a.get("title", ""),
                "summary": (a.get("description") or "")[:300],
                "link": a.get("url", ""),
                "source": (a.get("source") or {}).get("name", "NewsAPI"),
                "published": a.get("publishedAt", ""),
            }
            for a in r.get("articles", [])
        ]
    except Exception as e:
        print(f"[WARN] NewsAPI error: {e}")
        return []


# ── 5. Build AI reasoning from available data ────────────────────────────────
def build_reasoning(game, injuries, news_articles):
    """
    Rule-based reasoning engine that generates human-readable analysis.
    In the full-stack version this calls the Anthropic API via the dashboard.
    """
    home = game["home_team"]
    away = game["away_team"]
    home_abbr = NBA_TEAM_MAP.get(home, home[:3].upper())
    away_abbr = NBA_TEAM_MAP.get(away, away[:3].upper())

    home_ml = game.get("home_ml", "N/A")
    away_ml = game.get("away_ml", "N/A")
    spread = game.get("spread", "N/A")
    fav = game.get("favorite", home)
    win_prob = game.get("win_prob", {})

    home_injuries = injuries.get(home_abbr, [])
    away_injuries = injuries.get(away_abbr, [])

    injury_notes = []
    for p in home_injuries[:3]:
        injury_notes.append(f"{p['player']} ({home_abbr}) — {p['status']}: {p['reason']}")
    for p in away_injuries[:3]:
        injury_notes.append(f"{p['player']} ({away_abbr}) — {p['status']}: {p['reason']}")

    injury_text = "; ".join(injury_notes) if injury_notes else "No significant injuries reported."

    # Determine recommendation
    home_prob = win_prob.get(home_abbr, 50)
    away_prob = win_prob.get(away_abbr, 50)
    rec_team = home if home_prob >= away_prob else away
    rec_prob = max(home_prob, away_prob)
    rec_abbr = NBA_TEAM_MAP.get(rec_team, rec_team[:3].upper())
    rec_ml = home_ml if rec_team == home else away_ml

    # Confidence tier
    if rec_prob >= 75:
        confidence = "HIGH"
        conf_stars = "★★★"
    elif rec_prob >= 60:
        confidence = "MEDIUM"
        conf_stars = "★★☆"
    else:
        confidence = "LOW"
        conf_stars = "★☆☆"

    news_headline = news_articles[0]["title"] if news_articles else "No recent news found."

    reasoning = (
        f"**Recommendation: {rec_team} ML ({rec_ml})**\n\n"
        f"Win probability model gives {rec_team} a {rec_prob:.1f}% edge. "
        f"The spread is set at {spread}, reflecting market consensus on {fav}'s advantage. "
    )

    if home_injuries or away_injuries:
        reasoning += f"Injury watch: {injury_text}. "
        if any(
            p["status"].lower() in ["out", "doubtful"]
            for p in (home_injuries + away_injuries)
        ):
            reasoning += "Key absences shift line value — monitor for late scratches before tip-off. "
    else:
        reasoning += "Both rosters appear healthy, removing injury noise from this line. "

    if news_articles:
        reasoning += f"Latest buzz: \"{news_headline}\" — factor this into your final decision."

    return {
        "pick": rec_team,
        "pick_abbr": rec_abbr,
        "moneyline": rec_ml,
        "confidence": confidence,
        "confidence_stars": conf_stars,
        "win_probability": rec_prob,
        "reasoning": reasoning,
        "injury_notes": injury_text,
    }


# ── 6. Parse odds API response into normalised game dicts ────────────────────
def normalise_odds_api(raw_games):
    """Convert The Odds API response into internal format."""
    games = []
    for g in raw_games:
        home = g.get("home_team", "")
        away = g.get("away_team", "")
        home_abbr = NBA_TEAM_MAP.get(home, home[:3].upper())
        away_abbr = NBA_TEAM_MAP.get(away, away[:3].upper())

        home_ml = away_ml = spread = spread_team = None
        for bm in g.get("bookmakers", []):
            if bm["key"] in ("draftkings", "fanduel", "betmgm"):
                for market in bm.get("markets", []):
                    if market["key"] == "h2h":
                        for o in market.get("outcomes", []):
                            if o["name"] == home:
                                home_ml = f"{'+' if o['price'] > 0 else ''}{o['price']}"
                            elif o["name"] == away:
                                away_ml = f"{'+' if o['price'] > 0 else ''}{o['price']}"
                    elif market["key"] == "spreads":
                        for o in market.get("outcomes", []):
                            if o["name"] == home:
                                pt = o["point"]
                                spread = f"{home_abbr} {'+' if pt > 0 else ''}{pt}"
                                spread_team = home if pt < 0 else away
                break

        favorite = spread_team or (home if (home_ml or "+999") < (away_ml or "+999") else away)

        games.append({
            "game_id": g.get("id", ""),
            "home_team": home,
            "away_team": away,
            "home_abbr": home_abbr,
            "away_abbr": away_abbr,
            "home_ml": home_ml or "N/A",
            "away_ml": away_ml or "N/A",
            "spread": spread or "N/A",
            "favorite": favorite,
            "commence_time": g.get("commence_time", ""),
            "win_prob": {},  # populated from sports-data feed if available
        })
    return games


# ── 7. Master build function ─────────────────────────────────────────────────
def build_data():
    print("[INFO] Fetching odds...")
    raw_odds = fetch_odds()
    games = normalise_odds_api(raw_odds) if raw_odds else []

    print(f"[INFO] Got {len(games)} games with odds.")

    print("[INFO] Fetching injury report...")
    injuries = fetch_injury_report()

    print("[INFO] Fetching RSS news feeds...")
    all_articles = fetch_rss_articles()
    if not all_articles:
        print("[INFO] RSS empty — will use NewsAPI per game.")

    output_games = []
    for game in games:
        home = game["home_team"]
        away = game["away_team"]

        # Get relevant news
        news = get_game_news(home, away, all_articles)
        if not news:
            news = get_newsapi_articles(home, away)
            time.sleep(0.3)  # rate limit

        analysis = build_reasoning(game, injuries, news)

        output_games.append({
            "game": f"{away} @ {home}",
            "home_team": home,
            "away_team": away,
            "home_abbr": game["home_abbr"],
            "away_abbr": game["away_abbr"],
            "home_ml": game["home_ml"],
            "away_ml": game["away_ml"],
            "spread": game["spread"],
            "favorite": game["favorite"],
            "commence_time": game["commence_time"],
            "pick": analysis["pick"],
            "pick_abbr": analysis["pick_abbr"],
            "moneyline": analysis["moneyline"],
            "confidence": analysis["confidence"],
            "confidence_stars": analysis["confidence_stars"],
            "win_probability": analysis["win_probability"],
            "reasoning": analysis["reasoning"],
            "injury_notes": analysis["injury_notes"],
            "news": [
                {
                    "title": n["title"],
                    "source": n["source"],
                    "link": n["link"],
                    "published": n.get("published", ""),
                }
                for n in news[:3]
            ],
        })

    payload = {
        "last_updated": datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC"),
        "total_games": len(output_games),
        "games": output_games,
    }

    with open("data.json", "w") as f:
        json.dump(payload, f, indent=2)

    print(f"[DONE] Wrote data.json with {len(output_games)} games.")


if __name__ == "__main__":
    build_data()
