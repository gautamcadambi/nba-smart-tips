import json
import requests
from datetime import datetime

def get_nba_bets():
    # In production, you would use BeautifulSoup to scrape https://www.espn.com/nba/odds
    # For this example, we simulate the processed data from that source
    recommendations = [
        {
            "game": "Miami Heat @ Cleveland Cavaliers",
            "odds": "-210",
            "spread": "CLE -5.5",
            "bet": "Cleveland Moneyline",
            "reasoning": "Cleveland is looking for revenge. Jarrett Allen is expected back, which anchors their defense.",
            "news": "Jarrett Allen (CLE) - Questionable; Terry Rozier (MIA) - Out.",
            "confidence": "High"
        },
        {
            "game": "Houston Rockets @ Memphis Grizzlies",
            "odds": "-800",
            "spread": "HOU -12.5",
            "bet": "Houston -12.5",
            "reasoning": "Memphis is missing their entire starting lineup. Houston's offense is top-5 since the break.",
            "news": "Ja Morant, Zach Edey (MEM) - Out.",
            "confidence": "Very High"
        }
    ]
    
    data = {
        "last_updated": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        "bets": recommendations
    }
    
    with open('data.json', 'w') as f:
        json.dump(data, f)

if __name__ == "__main__":
    get_nba_bets()
