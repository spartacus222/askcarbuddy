#!/usr/bin/env python3
"""
AskCarBuddy v3.1 - AI Car Buying Intelligence (Fixed)
=====================================================
Paste any listing URL -> Get a REAL pro-level intelligence brief.

Philosophy: You found a car you like? We help you buy it SMART.
Quality bar: Every report should read like advice from a 20-year car sales veteran.
"""

import os
import json
import re
import time
import hashlib
import logging
import math
import requests
import statistics
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("askcarbuddy")

app = Flask(__name__)
CORS(app)

AUTODEV_API_KEY   = os.getenv("AUTODEV_API_KEY", "")
GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")
EXA_API_KEY       = os.getenv("EXA_API_KEY", "")
DEFAULT_ZIP       = os.getenv("DEFAULT_ZIP", "48309")

AUTODEV_BASE      = "https://auto.dev/api/listings"
NHTSA_RECALLS_URL = "https://api.nhtsa.gov/recalls/recallsByVehicle"
NHTSA_COMPLAINTS  = "https://api.nhtsa.gov/complaints/complaintsByVehicle"
GROQ_URL          = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL        = "llama-3.3-70b-versatile"
EXA_URL           = "https://api.exa.ai/contents"
EXA_SEARCH_URL    = "https://api.exa.ai/search"


# ==============================================================
# HELPERS — robust price/mileage parsing
# ==============================================================

def parse_price(val):
    """Parse price from any format: '$18,167', '18167', 18167, etc."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val) if val > 0 else None
    s = str(val).strip()
    s = re.sub(r'[^\d.]', '', s)  # strip everything except digits and dot
    try:
        p = int(float(s))
        return p if p > 0 else None
    except:
        return None


def parse_mileage(val):
    """Parse mileage from any format: '45,317 Miles', '45317', 45317, etc."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val) if val > 0 else None
    s = str(val).strip()
    s = re.sub(r'[^\d]', '', s)
    try:
        m = int(s)
        return m if m > 0 else None
    except:
        return None


# ==============================================================
# URL PARSER
# ==============================================================

def parse_listing_url(url):
    url = url.strip()
    info = {"source": "unknown", "url": url}
    if "cars.com" in url:
        info["source"] = "cars.com"
    elif "autotrader.com" in url:
        info["source"] = "autotrader"
    elif "cargurus.com" in url:
        info["source"] = "cargurus"
    elif "facebook.com/marketplace" in url:
        info["source"] = "facebook"
    else:
        info["source"] = "dealer"
    vin_match = re.search(r'[/=]([A-HJ-NPR-Z0-9]{17})(?:[/&?.]|$)', url, re.IGNORECASE)
    if vin_match:
        info["vin"] = vin_match.group(1).upper()
    return info


# ==============================================================
# SCRAPER
# ==============================================================

def scrape_listing_exa(url):
    if not EXA_API_KEY:
        return scrape_listing_basic(url), []
    try:
        resp = requests.post(EXA_URL, json={
            "urls": [url], "text": True,
            "extras": {"links": 3, "imageLinks": 5}
        }, headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"}, timeout=15)
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                return results[0].get("text", ""), results[0].get("extras", {}).get("imageLinks", [])
    except Exception as e:
        log.warning(f"Exa scrape failed: {e}")
    return scrape_listing_basic(url), []


def scrape_listing_basic(url):
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            return resp.text
    except:
        pass
    return ""


def extract_vehicle_from_text(text):
    info = {}
    price_match = re.search(r'\$(\d{1,3},?\d{3})', text)
    if price_match:
        info["price"] = parse_price(price_match.group(0))
    mile_match = re.search(r'(\d{1,3},?\d{3})\s*(?:mi(?:les)?|mileage|odometer)', text, re.IGNORECASE)
    if mile_match:
        info["mileage"] = parse_mileage(mile_match.group(1))
    vin_match = re.search(r'VIN[:\s]*([A-HJ-NPR-Z0-9]{17})', text, re.IGNORECASE)
    if vin_match:
        info["vin"] = vin_match.group(1).upper()
    ymm = re.search(r'(20\d{2}|19\d{2})\s+([A-Z][a-zA-Z]+)\s+([A-Z][a-zA-Z0-9\-]+)', text)
    if ymm:
        info["year"] = int(ymm.group(1))
        info["make"] = ymm.group(2)
        info["model"] = ymm.group(3)
    return info



# ==============================================================
# AUTO.DEV — VIN lookup + market comps (FIXED price parsing)
# ==============================================================

def lookup_vin_autodev(vin):
    if not AUTODEV_API_KEY:
        return None
    try:
        resp = requests.get(f"{AUTODEV_BASE}?vin={vin}", headers={
            "Authorization": f"Bearer {AUTODEV_API_KEY}"
        }, timeout=10)
        if resp.status_code == 200:
            records = resp.json().get("records", [])
            if records:
                r = records[0]
                return {
                    "year": r.get("year"), "make": r.get("make"), "model": r.get("model"),
                    "trim": r.get("trim"), "price": parse_price(r.get("price")),
                    "mileage": parse_mileage(r.get("mileage")),
                    "dealerName": r.get("dealerName"), "dealerPhone": r.get("dealerPhone"),
                    "dealerWebsite": r.get("dealerWebsite"),
                    "displayColor": r.get("displayColor"), "photoUrls": r.get("photoUrls", []),
                    "bodyType": r.get("bodyType"), "engine": r.get("engine"),
                    "transmission": r.get("transmission"), "drivetrain": r.get("drivetrain"),
                    "fuelType": r.get("fuelType"),
                    "mpgCity": r.get("mpgCity"), "mpgHighway": r.get("mpgHighway"),
                }
    except Exception as e:
        log.warning(f"Auto.dev VIN lookup failed: {e}")
    return None


def get_market_comps(year, make, model, trim=None, zip_code=None, listing_price=None):
    if not AUTODEV_API_KEY:
        return None
    try:
        params = {"make": make, "model": model, "page_size": 50}
        if year:
            params["year_min"] = max(year - 1, 1990)
            params["year_max"] = year + 1
        if zip_code:
            params["zip"] = zip_code
            params["radius"] = 150

        resp = requests.get(AUTODEV_BASE, params=params, headers={
            "Authorization": f"Bearer {AUTODEV_API_KEY}"
        }, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            records = data.get("records", [])
            total = data.get("totalCount", len(records))

            # FIXED: Parse prices from strings like "$18,167"
            prices = []
            mileage_prices = []
            for r in records:
                p = parse_price(r.get("price"))
                m = parse_mileage(r.get("mileage"))
                if p:
                    prices.append(p)
                    if m:
                        mileage_prices.append({"price": p, "mileage": m})

            if not prices:
                return None

            prices.sort()
            avg_price = sum(prices) // len(prices)
            median_price = int(statistics.median(prices))
            min_price = prices[0]
            max_price = prices[-1]

            percentile = None
            deal_score = None
            savings = None
            if listing_price:
                below = len([p for p in prices if p <= listing_price])
                percentile = round(below / len(prices) * 100)
                deal_score = max(1, min(10, round(10 - (percentile / 10))))
                savings = median_price - listing_price

            # Price distribution buckets for chart
            num_buckets = min(10, max(4, len(prices) // 2))
            bucket_size = max(500, (max_price - min_price) // num_buckets)
            if bucket_size == 0:
                bucket_size = 1000
            buckets = []
            current = min_price
            while current < max_price + bucket_size:
                count = len([p for p in prices if current <= p < current + bucket_size])
                buckets.append({"min": current, "max": current + bucket_size, "count": count})
                current += bucket_size
                if len(buckets) > 15:
                    break

            return {
                "avg_price": avg_price,
                "median_price": median_price,
                "min_price": min_price,
                "max_price": max_price,
                "percentile": percentile,
                "deal_score": deal_score,
                "savings": savings,
                "comp_count": len(prices),
                "total_market": total,
                "price_buckets": buckets,
                "prices_sample": prices[:30],
                "mileage_prices": mileage_prices[:30]
            }
    except Exception as e:
        log.warning(f"Market comp lookup failed: {e}")
    return None


# ==============================================================
# NHTSA — FIXED risk score (realistic, not fear-mongering)
# ==============================================================

def get_nhtsa_data(year, make, model):
    result = {
        "recall_count": 0, "complaint_count": 0,
        "recalls": [], "complaints_raw": [],
        "top_complaint_areas": [],
        "risk_score": 0, "risk_label": "Low Risk",
        "recall_details": [], "complaint_summary": ""
    }
    try:
        resp = requests.get(NHTSA_RECALLS_URL, params={
            "make": make, "model": model, "modelYear": year
        }, timeout=10)
        if resp.status_code == 200:
            recalls = resp.json().get("results", [])
            result["recall_count"] = len(recalls)
            result["recalls"] = [{
                "component": r.get("Component", "Unknown"),
                "summary": r.get("Summary", ""),
                "consequence": r.get("Consequence", ""),
                "remedy": r.get("Remedy", "")
            } for r in recalls[:10]]
    except:
        pass
    try:
        resp = requests.get(NHTSA_COMPLAINTS, params={
            "make": make, "model": model, "modelYear": year
        }, timeout=10)
        if resp.status_code == 200:
            complaints = resp.json().get("results", [])
            result["complaint_count"] = len(complaints)
            result["complaints_raw"] = complaints[:20]
            areas = {}
            for c in complaints:
                comp = c.get("components", "Unknown")
                areas[comp] = areas.get(comp, 0) + 1
            result["top_complaint_areas"] = sorted(areas.items(), key=lambda x: -x[1])[:8]
    except:
        pass

    # FIXED RISK SCORE — realistic calibration
    # Context: Popular cars (Camry, Civic, Prius) routinely have 50-200 complaints.
    # That's normal for vehicles sold in the hundreds of thousands.
    # A Prius with 100 complaints out of ~150K sold = 0.07% = very low.
    # Recalls are GOOD — they mean the manufacturer acknowledged and fixed an issue.

    # Complaint scoring: logarithmic, not linear
    # 0-20 complaints = 0 pts, 20-50 = 0.5, 50-100 = 1, 100-200 = 1.5, 200-500 = 2.5, 500+ = 3.5
    cc = result["complaint_count"]
    if cc <= 20:
        complaint_pts = 0
    elif cc <= 50:
        complaint_pts = 0.5
    elif cc <= 100:
        complaint_pts = 1.0
    elif cc <= 200:
        complaint_pts = 1.5
    elif cc <= 500:
        complaint_pts = 2.5
    else:
        complaint_pts = 3.5

    # Recall scoring: most cars have 1-5 recalls. That's normal.
    # Only penalize heavily for 6+ recalls (unusual)
    rc = result["recall_count"]
    if rc <= 2:
        recall_pts = 0
    elif rc <= 4:
        recall_pts = 0.5
    elif rc <= 6:
        recall_pts = 1.5
    else:
        recall_pts = 2.5

    # Severity check: only for truly dangerous unresolved patterns
    # Note: having a recall for fire risk is BETTER than NOT having one
    # (means manufacturer caught it and offered a fix)
    severe_keywords = ["death", "fatality", "unintended acceleration", "loss of steering"]
    severe_count = 0
    for c in result.get("complaints_raw", []):
        text = str(c.get("summary", "")).lower()
        if any(kw in text for kw in severe_keywords):
            severe_count += 1
    severity_pts = min(2, severe_count * 0.5)

    raw = complaint_pts + recall_pts + severity_pts
    result["risk_score"] = round(min(10, max(0, raw)), 1)

    if result["risk_score"] <= 1.5:
        result["risk_label"] = "Low Risk"
    elif result["risk_score"] <= 3:
        result["risk_label"] = "Below Average Risk"
    elif result["risk_score"] <= 5:
        result["risk_label"] = "Average"
    elif result["risk_score"] <= 7:
        result["risk_label"] = "Above Average Risk"
    else:
        result["risk_label"] = "High Risk"

    return result


# ==============================================================
# DEALER REPUTATION (via Exa scraping)
# ==============================================================

def get_dealer_reputation(dealer_name, dealer_location=None):
    if not EXA_API_KEY or not dealer_name:
        return None
    try:
        query = f'"{dealer_name}" reviews rating'
        if dealer_location:
            query += f" {dealer_location}"
        resp = requests.post(EXA_SEARCH_URL, json={
            "query": query, "numResults": 5, "type": "keyword",
            "contents": {"text": {"maxCharacters": 2000}}
        }, headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"}, timeout=15)
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            review_texts = [r.get("text", "")[:500] for r in results if r.get("text")]
            if review_texts:
                return {"raw_reviews": review_texts, "source_count": len(review_texts)}
    except Exception as e:
        log.warning(f"Dealer reputation scrape failed: {e}")
    return None



# ==============================================================
# AI SYSTEM PROMPT v3.1 — Expert-quality intelligence
# ==============================================================

ANALYSIS_SYSTEM_PROMPT = """You are AskCarBuddy — a car-buying intelligence engine powered by a veteran with 20 years of dealership experience.

PHILOSOPHY: The user found a car they LIKE. You help them buy it SMART. Not scare them away.
Energy: "Great pick — here me arm you with everything you need."

QUALITY BAR: Every section must read like it came from someone who has personally sold hundreds of this exact model. Be SPECIFIC to the generation, engine, transmission, and known real-world behavior of THIS car. Generic advice like "check for unusual noises" or "research the dealer" is BANNED.

CRITICAL RULES:
1. BE GENERATION-SPECIFIC. Reference the exact generation (e.g., "4th-gen Prius" or "10th-gen Civic"). Know what engine/transmission combo this car has and its specific reputation.
2. KNOWN QUIRKS must be REAL, DOCUMENTED issues for this exact generation/engine/transmission. Not generic car problems. Cite the actual component and failure pattern.
3. COMPLAINT CONTEXT IS MANDATORY. If there are 100 NHTSA complaints on a car that sold 150K+ units, that's a 0.07% rate — SAY THAT. Never present raw complaint counts without fleet-size context. Popular reliable cars get more complaints simply because more exist.
4. RECALLS ARE GOOD NEWS. A recall means the manufacturer caught an issue and offers a FREE fix. Frame recalls as "Toyota identified this and will fix it for free at any dealer" — not as scary red flags.
5. MAINTENANCE must be MILEAGE-SPECIFIC. At 104K miles, what SPECIFIC services are due based on the manufacturer's maintenance schedule for this engine? Include costs.
6. TEST DRIVE items must be SPECIFIC to this car's known characteristics. For a Prius: "Switch to EV mode — you should get 1-2 miles of pure electric range at low speed. If the battery can't sustain EV mode, it may be degrading." NOT "check for unusual noises."
7. DEALER QUESTIONS must reveal insider information. "Ask to see the hybrid battery health scan — any Toyota dealer can run this in 10 minutes with Techstream" NOT "ask about the vehicle history."
8. COST ESTIMATES must be realistic for this specific vehicle, not generic ranges.
9. OTD ESTIMATE must include specific state taxes and typical dealer fees for the region.
10. NEVER include negotiation scripts or "walk away" advice. Help them buy THIS car intelligently.
11. If the car is genuinely a good deal, say so with confidence. Don't hedge everything.
12. PRO TIPS must be insider knowledge that can't be easily Googled. Things a car salesperson would know from experience.

Return VALID JSON matching this structure:
{
  "buy_score": {
    "score": <1-10>,
    "label": "<Great Find|Solid Pick|Worth a Look|Proceed with Caution|Think Twice>",
    "one_liner": "<confident one-sentence verdict — be direct, not wishy-washy>"
  },
  "at_a_glance": {
    "best_thing": "<the single best thing about THIS specific car — not generic>",
    "know_before_you_go": "<the ONE most important thing to verify — specific and actionable>"
  },
  "risk_assessment": {
    "score_context": "<explain what the risk numbers actually MEAN. e.g., '100 complaints across ~150K vehicles sold = 0.07% rate, which is extremely low for any vehicle.' Frame recalls as addressed issues.>",
    "key_reassurances": ["<specific good things about this car's safety/reliability record>"],
    "items_to_verify": ["<specific things to confirm — framed as due diligence, not red flags. e.g., 'Confirm the electrical wiring recall (NHTSA XX-XXX) has been completed — free fix at any Toyota dealer'>"]
  },
  "deal_analysis": {
    "price_vs_market": "<specific comparison: 'At $13,435, this is $X below/above the median of $Y across Z comparable listings within 150 miles'>",
    "why_this_price": "<explain what's driving the price — mileage, trim, condition, market supply>",
    "value_verdict": "<direct assessment: is this a good deal or not, and why>",
    "deal_label": "<Steal|Great Deal|Good Value|Fair Price|Slightly High|Overpriced>"
  },
  "what_to_know": {
    "generation_overview": "<2-3 sentences about this SPECIFIC generation — its reputation, what owners love, what it's known for. Reference the generation number.>",
    "what_owners_love": ["<specific things real owners of THIS generation consistently praise>"],
    "known_quirks": [
      {
        "item": "<specific documented issue for this generation/engine>",
        "severity": "<minor_quirk|worth_checking|important>",
        "reality_check": "<how common is this REALLY? what percentage of owners experience it? what does it cost IF it happens?>",
        "what_to_do": "<specific, actionable step — not 'have it checked' but exactly what to check and how>"
      }
    ],
    "big_ticket_watch": "<the ONE expensive component to be aware of for this car at this mileage, with specific cost and timeline. e.g., 'Hybrid battery: rated for 150-200K miles, replacement costs $2,000-$3,500 if needed. At 104K you likely have 50-100K miles left.'>",
    "maintenance_now": [
      {
        "service": "<specific service due at this mileage per manufacturer schedule>",
        "cost": "<specific cost range>",
        "urgency": "<due_now|next_3_months|next_6_months|next_year>",
        "why": "<why this matters for THIS car specifically>"
      }
    ]
  },
  "your_game_plan": {
    "before_you_go": ["<specific prep step — not 'research the dealer' but actionable items like 'Run the VIN on Toyota's recall portal at toyota.com/recall to verify all 3 recalls are completed'>"],
    "smart_questions": [
      {
        "ask": "<specific insider question that reveals real information>",
        "why_this_matters": "<what this tells you that you can't find online>",
        "good_answer": "<what a trustworthy dealer would say>",
        "dig_deeper_if": "<what answer should prompt follow-up>"
      }
    ],
    "test_drive_checklist": ["<specific to THIS car — what to feel, listen for, test based on its known characteristics and drivetrain>"],
    "at_the_desk": {
      "expected_otd_range": "<specific out-the-door estimate including tax + fees for the region>",
      "standard_fees": ["<fee name: $amount — normal/expected>"],
      "fees_worth_asking_about": ["<fee that's sometimes inflated, what the fair amount is>"],
      "financing_intel": "<specific financing insight for this price point and vehicle type>"
    }
  },
  "cost_to_own": {
    "monthly_fuel": "<calculated from actual MPG and current gas prices>",
    "annual_insurance_estimate": "<realistic range for this vehicle class and price>",
    "annual_maintenance": "<based on this car's specific maintenance schedule at this mileage>",
    "first_year_maintenance_budget": "<total expected maintenance spend in year 1 of ownership, itemized>",
    "depreciation_outlook": "<how this model holds value — with specific data if possible>",
    "total_monthly_running_cost": "<all-in estimate: fuel + insurance/12 + maintenance/12>",
    "verdict": "<one confident sentence — is this cheap, average, or expensive to own?>"
  },
  "pro_tips": ["<genuinely insider knowledge specific to THIS car that can't be easily Googled — things only someone who's sold dozens of these would know>"]
}
"""



# ==============================================================
# AI ANALYSIS GENERATOR (Enhanced context feeding)
# ==============================================================

def generate_analysis(vehicle_info, market_data, nhtsa_data, dealer_rep, listing_text=""):
    context_parts = []

    v = vehicle_info
    context_parts.append(f"VEHICLE: {v.get('year', '?')} {v.get('make', '?')} {v.get('model', '?')} {v.get('trim', '')}")
    if v.get("price"): context_parts.append(f"LISTED PRICE: ${v['price']:,}")
    if v.get("mileage"): context_parts.append(f"MILEAGE: {v['mileage']:,} miles")
    if v.get("vin"): context_parts.append(f"VIN: {v['vin']}")
    if v.get("color"): context_parts.append(f"COLOR: {v['color']}")
    if v.get("zip"): context_parts.append(f"LOCATION ZIP: {v['zip']}")
    if v.get("dealer_name"): context_parts.append(f"DEALER: {v['dealer_name']}")
    if v.get("dealer_phone"): context_parts.append(f"DEALER PHONE: {v['dealer_phone']}")
    if v.get("engine"): context_parts.append(f"ENGINE: {v['engine']}")
    if v.get("transmission"): context_parts.append(f"TRANSMISSION: {v['transmission']}")
    if v.get("drivetrain"): context_parts.append(f"DRIVETRAIN: {v['drivetrain']}")
    if v.get("fuelType"): context_parts.append(f"FUEL: {v['fuelType']}")
    if v.get("mpgCity") and v.get("mpgHighway"):
        context_parts.append(f"MPG: {v['mpgCity']} city / {v['mpgHighway']} hwy")
    if v.get("bodyType"): context_parts.append(f"BODY: {v['bodyType']}")

    # MARKET DATA — give AI full context
    if market_data:
        m = market_data
        context_parts.append(f"\nMARKET COMPARISON DATA ({m['comp_count']} comparable listings within 150 miles):")
        context_parts.append(f"  Median market price: ${m['median_price']:,}")
        context_parts.append(f"  Average market price: ${m['avg_price']:,}")
        context_parts.append(f"  Price range: ${m['min_price']:,} - ${m['max_price']:,}")
        if m.get('percentile') is not None:
            context_parts.append(f"  This car's price percentile: {m['percentile']}th (lower = cheaper relative to market)")
        if m.get('savings') is not None:
            if m['savings'] > 0:
                context_parts.append(f"  PRICE ADVANTAGE: This car is ${m['savings']:,} BELOW the median market price")
            elif m['savings'] < 0:
                context_parts.append(f"  PRICE PREMIUM: This car is ${abs(m['savings']):,} ABOVE the median market price")
            else:
                context_parts.append(f"  This car is priced AT the median market price")
        if m.get('deal_score'):
            context_parts.append(f"  Calculated deal score: {m['deal_score']}/10")
        context_parts.append(f"  Total market supply: {m['total_market']} similar vehicles")
        # Mileage context
        if m.get('mileage_prices'):
            mp = m['mileage_prices']
            similar_mile = [x for x in mp if v.get('mileage') and abs(x['mileage'] - v['mileage']) < 20000]
            if similar_mile:
                sim_prices = [x['price'] for x in similar_mile]
                context_parts.append(f"  Cars with similar mileage ({v['mileage']-20000:,}-{v['mileage']+20000:,} mi): avg ${sum(sim_prices)//len(sim_prices):,} ({len(sim_prices)} listings)")

    # NHTSA DATA — give AI context about what the numbers mean
    if nhtsa_data:
        n = nhtsa_data
        context_parts.append(f"\nSAFETY DATA (NHTSA):")
        context_parts.append(f"  Calculated risk score: {n['risk_score']}/10 ({n['risk_label']})")
        context_parts.append(f"  Recalls: {n['recall_count']} (NOTE: recalls mean the manufacturer identified an issue and offers a FREE fix)")
        context_parts.append(f"  Consumer complaints: {n['complaint_count']} (NOTE: popular vehicles that sell 100K+ units routinely have 50-200 complaints; raw count alone is NOT a risk indicator)")
        if n.get("top_complaint_areas"):
            areas = ", ".join(f"{area} ({count})" for area, count in n["top_complaint_areas"][:8])
            context_parts.append(f"  Complaint breakdown: {areas}")
        for r in n.get("recalls", [])[:5]:
            context_parts.append(f"  RECALL: {r['component']}")
            context_parts.append(f"    Issue: {r['summary'][:200]}")
            if r.get("remedy"):
                context_parts.append(f"    Fix: {r['remedy'][:150]}")

    # DEALER REPUTATION
    if dealer_rep and dealer_rep.get("raw_reviews"):
        context_parts.append(f"\nDEALER REVIEW DATA ({dealer_rep['source_count']} web sources scraped):")
        for i, review in enumerate(dealer_rep["raw_reviews"][:3]):
            context_parts.append(f"  Source {i+1}: {review[:400]}")

    # RAW LISTING TEXT
    if listing_text:
        context_parts.append(f"\nRAW LISTING PAGE CONTENT (scraped from dealer website):")
        context_parts.append(listing_text[:4000])

    context = "\n".join(context_parts)

    try:
        resp = requests.post(GROQ_URL, json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": f"Generate a complete, expert-quality buyer intelligence brief for this vehicle. Be specific to this exact car — no generic advice.\n\n{context}"}
            ],
            "temperature": 0.4,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"}
        }, headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }, timeout=60)

        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            analysis = json.loads(content)
            log.info(f"Analysis generated: {v.get('year')} {v.get('make')} {v.get('model')}")
            return analysis
        else:
            log.error(f"Groq error: {resp.status_code} - {resp.text[:300]}")
    except Exception as e:
        log.error(f"Analysis generation failed: {e}")
    return None


# ==============================================================
# ORCHESTRATOR
# ==============================================================

def analyze_listing(input_data):
    vehicle = {}
    listing_text = ""

    if input_data.get("url"):
        url_info = parse_listing_url(input_data["url"])
        vehicle.update(url_info)
        scrape_result = scrape_listing_exa(input_data["url"])
        if isinstance(scrape_result, tuple):
            listing_text, images = scrape_result
            if images:
                vehicle["photos"] = images[:5]
        else:
            listing_text = scrape_result
        if listing_text:
            extracted = extract_vehicle_from_text(listing_text)
            for k, val in extracted.items():
                if val and not vehicle.get(k):
                    vehicle[k] = val

    for field in ["year", "make", "model", "trim", "price", "mileage", "vin", "zip", "color", "dealer_name"]:
        if input_data.get(field):
            vehicle[field] = input_data[field]

    if not vehicle.get("make") or not vehicle.get("model"):
        return {"error": "Couldn't identify the car. Try a different listing URL or enter details manually."}

    # VIN enrichment
    if vehicle.get("vin") and AUTODEV_API_KEY:
        vin_data = lookup_vin_autodev(vehicle["vin"])
        if vin_data:
            for k in ["year", "make", "model", "trim", "price", "mileage", "engine",
                       "transmission", "drivetrain", "fuelType", "mpgCity", "mpgHighway", "bodyType"]:
                if vin_data.get(k) and not vehicle.get(k):
                    vehicle[k] = vin_data[k]
            if vin_data.get("dealerName") and not vehicle.get("dealer_name"):
                vehicle["dealer_name"] = vin_data["dealerName"]
            if vin_data.get("dealerPhone"):
                vehicle["dealer_phone"] = vin_data["dealerPhone"]
            if vin_data.get("photoUrls") and not vehicle.get("photos"):
                vehicle["photos"] = vin_data["photoUrls"][:8]
            if vin_data.get("displayColor") and not vehicle.get("color"):
                vehicle["color"] = vin_data["displayColor"]

    # Normalize types
    if vehicle.get("price"):
        vehicle["price"] = parse_price(vehicle["price"]) or vehicle["price"]
    if vehicle.get("mileage"):
        vehicle["mileage"] = parse_mileage(vehicle["mileage"]) or vehicle["mileage"]
    if vehicle.get("year"):
        try: vehicle["year"] = int(vehicle["year"])
        except: pass

    log.info(f"Analyzing: {vehicle.get('year')} {vehicle.get('make')} {vehicle.get('model')} - ${vehicle.get('price', '?')}")

    market_data = None
    if vehicle.get("make") and vehicle.get("model"):
        market_data = get_market_comps(
            vehicle.get("year"), vehicle["make"], vehicle["model"],
            vehicle.get("trim"), vehicle.get("zip") or DEFAULT_ZIP, vehicle.get("price")
        )

    nhtsa_data = None
    if vehicle.get("year") and vehicle.get("make") and vehicle.get("model"):
        nhtsa_data = get_nhtsa_data(vehicle["year"], vehicle["make"], vehicle["model"])

    dealer_rep = None
    if vehicle.get("dealer_name"):
        dealer_rep = get_dealer_reputation(vehicle["dealer_name"], vehicle.get("zip"))

    analysis = generate_analysis(vehicle, market_data, nhtsa_data, dealer_rep, listing_text)

    if not analysis:
        return {"error": "Analysis generation failed. Please try again."}

    return {
        "vehicle": vehicle,
        "market_data": {
            "avg_price": market_data["avg_price"] if market_data else None,
            "median_price": market_data["median_price"] if market_data else None,
            "min_price": market_data["min_price"] if market_data else None,
            "max_price": market_data["max_price"] if market_data else None,
            "percentile": market_data["percentile"] if market_data else None,
            "deal_score": market_data["deal_score"] if market_data else None,
            "savings": market_data["savings"] if market_data else None,
            "comp_count": market_data["comp_count"] if market_data else 0,
            "total_market": market_data["total_market"] if market_data else 0,
            "price_buckets": market_data["price_buckets"] if market_data else [],
        } if market_data else None,
        "nhtsa_data": {
            "recall_count": nhtsa_data["recall_count"] if nhtsa_data else 0,
            "complaint_count": nhtsa_data["complaint_count"] if nhtsa_data else 0,
            "risk_score": nhtsa_data["risk_score"] if nhtsa_data else 0,
            "risk_label": nhtsa_data["risk_label"] if nhtsa_data else "Unknown",
            "top_complaint_areas": nhtsa_data["top_complaint_areas"][:5] if nhtsa_data else [],
        },
        "analysis": analysis,
        "generated_at": datetime.utcnow().isoformat(),
        "report_id": hashlib.md5(json.dumps(vehicle, sort_keys=True, default=str).encode()).hexdigest()[:12],
        "version": "3.1.0"
    }



# ==============================================================
# API ROUTES
# ==============================================================

@app.route("/")
def home():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(html_path):
        with open(html_path) as f:
            return render_template_string(f.read())
    return "<h1>AskCarBuddy</h1><p>Frontend not found.</p>"

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    try:
        report = analyze_listing(data)
        if "error" in report:
            return jsonify(report), 400
        return jsonify(report)
    except Exception as e:
        log.error(f"Analysis error: {e}")
        return jsonify({"error": "Something went wrong. Please try again."}), 500

@app.route("/api/parse-url", methods=["POST"])
def api_parse_url():
    data = request.get_json()
    url = data.get("url", "")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    return jsonify(parse_listing_url(url))

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok", "service": "AskCarBuddy", "version": "3.1.0",
        "apis": {"groq": bool(GROQ_API_KEY), "autodev": bool(AUTODEV_API_KEY), "exa": bool(EXA_API_KEY)}
    })

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"AskCarBuddy v3.1 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
