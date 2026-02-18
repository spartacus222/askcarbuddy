#!/usr/bin/env python3
"""
AskCarBuddy v3 - AI Car Buying Intelligence (Enhanced)
======================================================
Paste any listing URL -> Get a pro-level intelligence brief.

Philosophy: You found a car you like? We help you buy it SMART.
No fear tactics. No "walk away" scripts. Just intelligence.

NEW in v3:
- Risk Score (0-10) — NHTSA + VIN web scan
- Deal Score + Price Distribution — visual market position
- Dealer Reputation Snapshot — scraped reviews + trust score
- Enhanced Cost to Own — fuel, insurance, maintenance estimates
- Depreciation Forecast — how well this car holds value

Stack: Flask + Groq AI + Auto.dev + NHTSA + Exa
"""

import os
import json
import re
import time
import hashlib
import logging
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
        info["price"] = int(price_match.group(1).replace(",", ""))
    mile_match = re.search(r'(\d{1,3},?\d{3})\s*(?:mi(?:les)?|mileage|odometer)', text, re.IGNORECASE)
    if mile_match:
        info["mileage"] = int(mile_match.group(1).replace(",", ""))
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
# AUTO.DEV - VIN lookup + market comps + DEAL SCORE
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
                    "trim": r.get("trim"), "price": r.get("price"), "mileage": r.get("mileage"),
                    "dealerName": r.get("dealerName"), "dealerPhone": r.get("dealerPhone"),
                    "dealerWebsite": r.get("dealerWebsite"),
                    "displayColor": r.get("displayColor"), "photoUrls": r.get("photoUrls", []),
                    "bodyType": r.get("bodyType"), "engine": r.get("engine"),
                    "transmission": r.get("transmission"), "drivetrain": r.get("drivetrain"),
                    "fuelType": r.get("fuelType"), "mpgCity": r.get("mpgCity"),
                    "mpgHighway": r.get("mpgHighway"),
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
            prices = sorted([r["price"] for r in records if r.get("price") and r["price"] > 0])

            if not prices:
                return None

            avg_price = sum(prices) // len(prices)
            median_price = int(statistics.median(prices))
            min_price = prices[0]
            max_price = prices[-1]

            # Percentile of this listing
            percentile = None
            deal_score = None
            savings = None
            if listing_price:
                below = len([p for p in prices if p <= listing_price])
                percentile = round(below / len(prices) * 100)
                # Deal score: how good is this price? Lower percentile = better deal
                # 0-20th percentile = great deal, 20-40 = good, 40-60 = fair, 60-80 = above avg, 80-100 = high
                deal_score = max(1, min(10, round(10 - (percentile / 10))))
                savings = median_price - listing_price  # positive = saving money

            # Price distribution buckets for chart
            bucket_size = max(1000, (max_price - min_price) // 8)
            buckets = []
            current = min_price
            while current < max_price:
                count = len([p for p in prices if current <= p < current + bucket_size])
                buckets.append({"min": current, "max": current + bucket_size, "count": count})
                current += bucket_size

            # Mileage-adjusted comps
            mileage_prices = []
            for r in records:
                if r.get("price") and r.get("mileage") and r["price"] > 0:
                    mileage_prices.append({"price": r["price"], "mileage": r["mileage"]})

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
# NHTSA - Recalls + complaints + RISK SCORE
# ==============================================================

def get_nhtsa_data(year, make, model):
    result = {
        "recall_count": 0, "complaint_count": 0,
        "recalls": [], "complaints_raw": [],
        "top_complaint_areas": [], "risk_score": 0, "risk_label": "Low Risk"
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
            result["top_complaint_areas"] = sorted(areas.items(), key=lambda x: -x[1])[:5]
    except:
        pass

    # Calculate risk score (0-10, lower = safer)
    recall_risk = min(5, result["recall_count"] * 0.8)
    complaint_risk = min(5, result["complaint_count"] * 0.05)
    # Check for severe recalls
    severe_keywords = ["fire", "crash", "injury", "death", "loss of control", "brake failure", "fuel leak"]
    severe_count = 0
    for r in result["recalls"]:
        text = (r.get("consequence", "") + " " + r.get("summary", "")).lower()
        if any(kw in text for kw in severe_keywords):
            severe_count += 1
    severity_bonus = min(3, severe_count * 1.5)

    raw_score = recall_risk + complaint_risk + severity_bonus
    result["risk_score"] = round(min(10, max(0, raw_score)), 1)

    if result["risk_score"] <= 2:
        result["risk_label"] = "Low Risk"
    elif result["risk_score"] <= 4:
        result["risk_label"] = "Moderate"
    elif result["risk_score"] <= 6:
        result["risk_label"] = "Elevated"
    elif result["risk_score"] <= 8:
        result["risk_label"] = "High Risk"
    else:
        result["risk_label"] = "Critical"

    return result


# ==============================================================
# DEALER REPUTATION (via Exa scraping)
# ==============================================================

def get_dealer_reputation(dealer_name, dealer_location=None):
    """Scrape dealer reviews from the web and summarize."""
    if not EXA_API_KEY or not dealer_name:
        return None
    try:
        query = f"{dealer_name} reviews"
        if dealer_location:
            query += f" {dealer_location}"

        resp = requests.post(EXA_SEARCH_URL, json={
            "query": query,
            "numResults": 5,
            "type": "keyword",
            "contents": {"text": {"maxCharacters": 2000}}
        }, headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"}, timeout=15)

        if resp.status_code == 200:
            results = resp.json().get("results", [])
            review_texts = []
            for r in results:
                text = r.get("text", "")
                if text:
                    review_texts.append(text[:500])

            if review_texts:
                return {"raw_reviews": review_texts, "source_count": len(review_texts)}
    except Exception as e:
        log.warning(f"Dealer reputation scrape failed: {e}")
    return None



# ==============================================================
# AI SYSTEM PROMPT v3 — Enhanced intelligence engine
# ==============================================================

ANALYSIS_SYSTEM_PROMPT = """You are AskCarBuddy — a trusted car-expert friend with 20 years of dealership experience.

PHILOSOPHY: The user found a car they LIKE. Your job is NOT to talk them out of it.
Your job is to arm them with intelligence so they can buy it CONFIDENTLY and SMARTLY.
Energy: "Great pick — here's everything you need to know to own this car and love it."

TONE: Warm, confident, knowledgeable. Like a friend who happens to be a car expert.
NEVER: Salesy, fear-mongering, adversarial, or generic. Be SPECIFIC to THIS car.

CRITICAL RULES:
1. Be SPECIFIC to this exact vehicle — reference the actual year, make, model, trim, generation, engine.
2. Known issues must be REAL documented issues for this specific generation/engine/transmission combo.
3. Frame everything as HELPFUL, not scary. "Here's what to know" not "here's what could go wrong."
4. If the car is genuinely good, say so enthusiastically. If it has real concerns, flag them honestly but constructively.
5. The risk score context should explain what the numbers MEAN, not just repeat them.
6. Dealer reputation should help the buyer feel prepared, not suspicious.
7. Cost estimates should be realistic ranges, not worst-case scare numbers.
8. Pro tips should be genuinely useful insider knowledge a car salesperson would know.
9. NEVER include negotiation scripts. NEVER tell them to "walk away." Help them buy THIS car smartly.
10. The depreciation section should frame value retention positively when possible.

Return VALID JSON matching this exact structure:
{
  "buy_score": {
    "score": <1-10>,
    "label": "<Great Find|Solid Pick|Worth a Look|Proceed with Caution|Think Twice>",
    "one_liner": "<warm one-sentence verdict — encourage if deserved>"
  },
  "at_a_glance": {
    "best_thing": "<the single best thing about this specific car>",
    "know_before_you_go": "<the single most important thing to verify>"
  },
  "risk_assessment": {
    "score": <0-10 float, use the NHTSA risk score provided>,
    "label": "<Low Risk|Moderate|Elevated|High Risk|Critical>",
    "summary": "<2-3 sentences explaining what the risk data means for THIS buyer — be honest but not alarming>",
    "key_flags": ["<specific flag if any — e.g. 'Airbag recall — free fix at any Toyota dealer'>"],
    "reassurances": ["<things that are GOOD about this car's safety/reliability record>"]
  },
  "deal_score": {
    "score": <1-10, use the deal score from market data>,
    "savings_vs_market": "<e.g. '$1,200 below median' or '$500 above average'>",
    "context": "<1-2 sentences on why this price is what it is — mileage, condition, location factors>",
    "verdict": "<Great Deal|Good Value|Fair Price|Slightly High|Overpriced>"
  },
  "market_intel": {
    "summary": "<2-3 sentences on where this price sits vs market — factual, helpful>",
    "price_position": "<below_market|competitive|market_price|above_market>",
    "supply_demand": "<how many similar cars are available and what that means for the buyer>",
    "value_factors": ["<factors driving this price up or down>"]
  },
  "what_to_know": {
    "generation_overview": "<1-2 sentences about this specific generation — reputation, strengths, what owners love>",
    "known_quirks": [
      {
        "item": "<specific known issue for this generation>",
        "severity": "<minor_quirk|worth_checking|important>",
        "context": "<how common, how expensive IF it happens>",
        "what_to_do": "<specific actionable step>"
      }
    ],
    "what_owners_love": ["<things real owners consistently praise about this car>"],
    "maintenance_upcoming": [
      {
        "service": "<maintenance item based on current mileage>",
        "typical_cost": "<cost range>",
        "urgency": "<due_now|soon|within_6_months|within_a_year>"
      }
    ]
  },
  "dealer_intel": {
    "trust_summary": "<1-2 sentences about the dealer based on available data — helpful framing>",
    "tips_for_visit": ["<specific tip for dealing with THIS dealer — e.g. 'ask about their return policy'>"]
  },
  "your_game_plan": {
    "before_you_visit": ["<specific prep step>"],
    "at_the_dealer": [
      {
        "ask": "<specific question>",
        "why": "<why this matters — insider knowledge>",
        "good_sign": "<reassuring answer>",
        "heads_up": "<answer that means dig deeper>"
      }
    ],
    "on_the_test_drive": ["<specific thing to check on THIS car>"],
    "at_the_desk": {
      "expected_otd": "<estimated out-the-door range>",
      "fees_to_expect": ["<legitimate fee + typical amount>"],
      "fees_to_question": ["<fee sometimes inflated + fair amount>"],
      "financing_tip": "<one specific financing tip>"
    }
  },
  "cost_to_own": {
    "monthly_fuel": "<estimated based on MPG + current gas prices>",
    "annual_insurance_range": "<estimated range for this vehicle class>",
    "annual_maintenance": "<estimated based on mileage and age>",
    "depreciation_outlook": "<how well this car holds value — frame positively when true>",
    "total_monthly_estimate": "<all-in monthly cost estimate (fuel + insurance + maintenance averaged)>",
    "ownership_verdict": "<one sentence — is this cheap/average/expensive to own for its class?>"
  },
  "pro_tips": ["<genuinely useful insider tip specific to THIS car — not generic>"]
}
"""



# ==============================================================
# AI ANALYSIS GENERATOR (Enhanced)
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

    # MARKET DATA (Enhanced with deal score)
    if market_data:
        m = market_data
        context_parts.append(f"\nMARKET DATA:")
        context_parts.append(f"  Median market price: ${m['median_price']:,}")
        context_parts.append(f"  Average market price: ${m['avg_price']:,}")
        context_parts.append(f"  Price range: ${m['min_price']:,} - ${m['max_price']:,}")
        if m.get('percentile') is not None:
            context_parts.append(f"  This listing is at the {m['percentile']}th price percentile (lower = cheaper)")
        if m.get('deal_score') is not None:
            context_parts.append(f"  DEAL SCORE: {m['deal_score']}/10 (10 = best deal)")
        if m.get('savings') is not None:
            if m['savings'] > 0:
                context_parts.append(f"  SAVINGS: ${m['savings']:,} BELOW median market price")
            elif m['savings'] < 0:
                context_parts.append(f"  PREMIUM: ${abs(m['savings']):,} ABOVE median market price")
            else:
                context_parts.append(f"  PRICE: At median market price")
        context_parts.append(f"  Comparable listings: {m['comp_count']} (total market supply: {m['total_market']})")

    # NHTSA SAFETY DATA (Enhanced with risk score)
    if nhtsa_data:
        n = nhtsa_data
        context_parts.append(f"\nSAFETY & RISK DATA (NHTSA):")
        context_parts.append(f"  RISK SCORE: {n['risk_score']}/10 ({n['risk_label']}) — lower is safer")
        context_parts.append(f"  Open recalls: {n['recall_count']}")
        context_parts.append(f"  Consumer complaints filed: {n['complaint_count']}")
        if n.get("top_complaint_areas"):
            areas = ", ".join(f"{area} ({count})" for area, count in n["top_complaint_areas"][:5])
            context_parts.append(f"  Top complaint areas: {areas}")
        if n.get("recalls"):
            for r in n["recalls"][:5]:
                context_parts.append(f"  Recall: {r['component']} - {r['summary'][:150]}")
                if r.get("remedy"):
                    context_parts.append(f"    Remedy: {r['remedy'][:100]}")

    # DEALER REPUTATION
    if dealer_rep and dealer_rep.get("raw_reviews"):
        context_parts.append(f"\nDEALER REPUTATION DATA ({dealer_rep['source_count']} sources found):")
        for i, review in enumerate(dealer_rep["raw_reviews"][:3]):
            context_parts.append(f"  Review {i+1}: {review[:300]}")

    # RAW LISTING TEXT
    if listing_text:
        context_parts.append(f"\nRAW LISTING TEXT:")
        context_parts.append(listing_text[:3000])

    context = "\n".join(context_parts)

    try:
        resp = requests.post(GROQ_URL, json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": f"Analyze this vehicle listing and generate a complete buyer intelligence brief:\n\n{context}"}
            ],
            "temperature": 0.3,
            "max_tokens": 4000,
            "response_format": {"type": "json_object"}
        }, headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }, timeout=45)

        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            analysis = json.loads(content)
            log.info(f"Analysis generated for {v.get('year')} {v.get('make')} {v.get('model')}")
            return analysis
        else:
            log.error(f"Groq API error: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        log.error(f"Analysis generation failed: {e}")
    return None


# ==============================================================
# ORCHESTRATOR (Enhanced)
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
            if vin_data.get("dealerWebsite"):
                vehicle["dealer_website"] = vin_data["dealerWebsite"]
            if vin_data.get("photoUrls") and not vehicle.get("photos"):
                vehicle["photos"] = vin_data["photoUrls"][:8]
            if vin_data.get("displayColor") and not vehicle.get("color"):
                vehicle["color"] = vin_data["displayColor"]

    # Normalize types
    for field in ["year", "price", "mileage"]:
        if vehicle.get(field):
            try: vehicle[field] = int(str(vehicle[field]).replace(",", "").replace("$", ""))
            except: pass

    log.info(f"Analyzing: {vehicle.get('year')} {vehicle.get('make')} {vehicle.get('model')} - ${vehicle.get('price', '?')}")

    # Parallel data gathering
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

    # AI analysis
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
            "prices_sample": market_data["prices_sample"] if market_data else [],
        } if market_data else None,
        "nhtsa_data": {
            "recall_count": nhtsa_data["recall_count"] if nhtsa_data else 0,
            "complaint_count": nhtsa_data["complaint_count"] if nhtsa_data else 0,
            "risk_score": nhtsa_data["risk_score"] if nhtsa_data else 0,
            "risk_label": nhtsa_data["risk_label"] if nhtsa_data else "Unknown",
        },
        "analysis": analysis,
        "generated_at": datetime.utcnow().isoformat(),
        "report_id": hashlib.md5(json.dumps(vehicle, sort_keys=True, default=str).encode()).hexdigest()[:12],
        "version": "3.0.0"
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
        "status": "ok",
        "service": "AskCarBuddy",
        "version": "3.0.0",
        "apis": {
            "groq": bool(GROQ_API_KEY),
            "autodev": bool(AUTODEV_API_KEY),
            "exa": bool(EXA_API_KEY)
        }
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"AskCarBuddy v3 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
