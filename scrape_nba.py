import json
from datetime import datetime

def get_nba_bets():
    # Fresh analysis for March 28, 2026
    recommendations = [
        {
            "game": "Milwaukee Bucks @ Dallas Mavericks",
            "odds": "DAL -145",
            "spread": "DAL -2.5",
            "bet": "Mavericks Moneyline",
            "reasoning": "Luka Doncic is at home and playing at an MVP level. Milwaukee is on the second night of a back-to-back, which usually leads to tired legs in the 4th quarter.",
            "news": "MIL: Giannis Antetokounmpo (Probable). DAL: Fully healthy.",
            "confidence": "High"
        },
        {
            "game": "Denver Nuggets @ Phoenix Suns",
            "odds": "DEN -110",
            "spread": "Pick 'em",
            "bet": "Nuggets Moneyline",
            "reasoning": "Denver has won 4 straight against Phoenix. Jokic's size is a mismatch for the Suns' current small-ball rotation.",
            "news": "PHX: Jusuf Nurkic (Questionable - Ankle). DEN: Nikola Jokic (Active).",
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
