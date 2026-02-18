# 🚗 AskCarBuddy — AI Car Buying Intelligence

Paste any car listing URL → Get a pro-level acquisition brief.

## What You Get
- **Buy Score** (1-10) with clear verdict
- **Market Position** — price percentile, regional comparison, demand score
- **Reliability Intel** — known issues for this exact generation/engine
- **Smart Questions** — what to ask, why, good vs red flag answers
- **Negotiation Strategy** — opening offer, target, walk-away, tactics, fee watchlist
- **Shareable Report** — screenshot-worthy, bring to the dealership

## Stack
- **Backend**: Python/Flask + Groq AI
- **Data**: Auto.dev API + NHTSA (both free)
- **Frontend**: Pure HTML/CSS/JS (GitHub Pages)
- **Deploy**: Railway (~$5/mo)
- **Payments**: Stripe ($19/report)

## Quick Start
```bash
pip install -r requirements.txt
cp .env.example .env  # fill in your keys
python app.py
```

## Deploy to Railway
1. Fork this repo
2. Connect to Railway
3. Set env vars: `AUTODEV_API_KEY`, `GROQ_API_KEY`
4. Deploy!

## Business Model
- Free: Buy Score + Market Position + 2 Smart Questions
- $19: Full report (all questions, negotiation scripts, tactics, PDF)

## API
- `POST /api/analyze` — Full analysis (accepts URL or manual input)
- `POST /api/parse-url` — Parse listing URL
- `GET /health` — Health check

---
Built by AskCarBuddy Team
