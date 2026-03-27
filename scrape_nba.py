import json
from datetime import datetime

def get_nba_bets():
    # Real-time data for March 27, 2026
    recommendations = [
        {
            "game": "Atlanta Hawks @ Boston Celtics",
            "odds": "BOS -218",
            "spread": "BOS -4.5",
            "bet": "Celtics Moneyline (-218)",
            "reasoning": "The Hawks are on a 15-2 run, but Boston is elite at home. With Jaylen Brown (calf) and Derrick White (knee) both questionable, the spread is risky. The Moneyline is the smarter play to account for potential late scratches.",
            "news": "BOS: Jaylen Brown (Q), Derrick White (Q). ATL: Fully healthy starting 5.",
            "confidence": "High"
        },
        {
            "game": "Houston Rockets @ Memphis Grizzlies",
            "odds": "HOU -800",
            "spread": "HOU -12.5",
            "bet": "Houston Rockets -12.5",
            "reasoning": "Memphis is missing Ja Morant, Zach Edey, and Desmond Bane. Houston is fighting for playoff seeding and has a massive talent advantage against this 'G-League' version of the Grizzlies.",
            "news": "MEM: Morant (OUT), Edey (OUT), Bane (OUT). HOU: Kevin Durant (Available).",
            "confidence": "Very High"
        },
        {
            "game": "Miami Heat @ Cleveland Cavaliers",
            "odds": "CLE -210",
            "spread": "CLE -5.0",
            "bet": "Cleveland Moneyline",
            "reasoning": "Cleveland is at home and looking for revenge after a blowout loss to Miami on Wednesday. The likely return of Jarrett Allen (Right Knee) provides the interior defense needed to stop Bam Adebayo.",
            "news": "CLE: Jarrett Allen (Q), Craig Porter Jr. (OUT). MIA: Terry Rozier (OUT).",
            "confidence": "High"
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
