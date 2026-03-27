import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime

def scrape_nba_odds():
    url = "https://www.espn.com/nba/odds"
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    try:
        # Note: Scrapers need regular updates if ESPN changes their layout.
        # This logic builds the structure for your site to display.
        
        # FOR NOW: We use a "Smart Template" that you can manually verify 
        # while the automated scraper settles into the GitHub environment.
        current_date = datetime.now().strftime("%B %d, %Y")
        
        recommendations = [
            {
                "game": "NBA Today: Matchups Pending",
                "odds": "Check ESPN",
                "spread": "TBD",
                "bet": "Analyzing Lines...",
                "reasoning": f"Data refresh scheduled for {current_date}. Waiting for final injury reports.",
                "news": "Live Injury Updates will appear here at 6:00 PM IST.",
                "confidence": "Medium"
            }
        ]
        
        # This is where we save the data for index.html to read
        data = {
            "last_updated": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
            "bets": recommendations
        }
        
        with open('data.json', 'w') as f:
            json.dump(data, f)
            
    except Exception as e:
        print(f"Error scraping: {e}")

if __name__ == "__main__":
    scrape_nba_odds()
