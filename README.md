# 🚗 AskCarBuddy

**AI-powered car buying intelligence.** Paste any car listing URL, get an instant pro-level brief.

## What You Get
- **Buy Score** (1-10) with honest verdict
- **Market Intel** — where the price sits vs comparable listings
- **What to Know** — real issues for this specific car, not generic advice
- **Your Game Plan** — what to check, what to ask, what to expect at the desk
- **Cost to Own** — fuel, insurance, maintenance estimates
- **Pro Tips** — insider knowledge specific to this car

## Philosophy
You found a car you like? **We help you buy it smart.** We don't scare you away.

## Stack
- Python/Flask backend
- Groq AI (Llama 3.3 70B)
- Auto.dev API (market comps + VIN lookup)
- NHTSA API (recalls + complaints)
- Exa API (listing scraping)

## Deploy on Railway
1. Fork this repo
2. Connect to Railway
3. Set environment variables: `AUTODEV_API_KEY`, `GROQ_API_KEY`, `EXA_API_KEY`
4. Deploy

## Run Locally
```bash
pip install -r requirements.txt
cp .env.example .env  # fill in your keys
python app.py
```

## Cost
- Groq: Free tier (30 req/min)
- Auto.dev: Free Starter plan
- NHTSA: Free (government API)
- Exa: Free tier available
- Railway: ~$5/mo

---
Built by AskCarBuddy © 2026
