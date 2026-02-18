#!/usr/bin/env python3
"""
AskCarBuddy MVP — AI Car Buying Intelligence
=============================================
Paste any listing URL → Get a pro-level acquisition brief.

Core Output:
  - Buy Score (1-10) with clear label
  - Market Position (price percentile, regional comparison)
  - Reliability Risk Profile (known issues, repair costs)
  - Smart Questions to Ask (specific, with why + good/red flag answers)
  - Negotiation Strategy (offer range, leverage points, fee checklist)
  - Shareable PDF Report

Stack: Flask + Groq AI + Auto.dev + NHTSA + BeautifulSoup
"""

import os
import json
import re
import time
import hashlib
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("askcarbuddy")

# ── Flask App ────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

# ── Config ───────────────────────────────────────────────────
AUTODEV_API_KEY   = os.getenv("AUTODEV_API_KEY", "")
GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")
DEFAULT_ZIP       = os.getenv("DEFAULT_ZIP", "48309")

AUTODEV_BASE      = "https://auto.dev/api/listings"
NHTSA_RECALLS_URL = "https://api.nhtsa.gov/recalls/recallsByVehicle"
NHTSA_COMPLAINTS  = "https://api.nhtsa.gov/complaints/complaintsByVehicle"
GROQ_URL          = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL        = "llama-3.3-70b-versatile"

REPORT_PRICE      = 19  # dollars


# ══════════════════════════════════════════════════════════════
# URL PARSER — Extract vehicle details from listing URLs
# ══════════════════════════════════════════════════════════════

def parse_listing_url(url):
    """
    Extract vehicle info from a listing URL.
    Supports: Cars.com, AutoTrader, CarGurus, Facebook Marketplace, dealer sites.
    Returns dict with: year, make, model, trim, price, mileage, vin, zip, source.
    """
    url = url.strip()
    info = {"source": "unknown", "url": url}

    # Cars.com: /vehicledetail/detail/xyz/ or new URL patterns
    if "cars.com" in url:
        info["source"] = "cars.com"
        # Try to extract VIN from URL
        vin_match = re.search(r'/detail/([A-HJ-NPR-Z0-9]{17})', url, re.IGNORECASE)
        if vin_match:
            info["vin"] = vin_match.group(1).upper()
        # Try year/make/model from URL path
        ym_match = re.search(r'/(\d{4})[-_]([a-z]+)[-_]([a-z0-9]+)', url, re.IGNORECASE)
        if ym_match:
            info["year"] = int(ym_match.group(1))
            info["make"] = ym_match.group(2).title()
            info["model"] = ym_match.group(3).title()

    # AutoTrader
    elif "autotrader.com" in url:
        info["source"] = "autotrader"
        vin_match = re.search(r'/([A-HJ-NPR-Z0-9]{17})', url, re.IGNORECASE)
        if vin_match:
            info["vin"] = vin_match.group(1).upper()
        ym_match = re.search(r'/(\d{4})[-_]([a-z]+)[-_]([a-z0-9]+)', url, re.IGNORECASE)
        if ym_match:
            info["year"] = int(ym_match.group(1))
            info["make"] = ym_match.group(2).title()
            info["model"] = ym_match.group(3).title()

    # CarGurus
    elif "cargurus.com" in url:
        info["source"] = "cargurus"
        vin_match = re.search(r'#listing=([A-HJ-NPR-Z0-9]{17})', url, re.IGNORECASE)
        if not vin_match:
            vin_match = re.search(r'/([A-HJ-NPR-Z0-9]{17})', url, re.IGNORECASE)
        if vin_match:
            info["vin"] = vin_match.group(1).upper()

    # Facebook Marketplace
    elif "facebook.com/marketplace" in url:
        info["source"] = "facebook"

    # Generic — try to find VIN anywhere in URL
    else:
        info["source"] = "dealer"
        vin_match = re.search(r'[/=]([A-HJ-NPR-Z0-9]{17})(?:[/&?]|$)', url, re.IGNORECASE)
        if vin_match:
            info["vin"] = vin_match.group(1).upper()

    return info


def scrape_listing(url):
    """
    Attempt to scrape basic info from a listing page.
    Falls back gracefully if blocked.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            text = resp.text
            info = {}
            # Try to find price
            price_match = re.search(r'\$(\d{1,3},?\d{3})', text)
            if price_match:
                info["price"] = int(price_match.group(1).replace(",", ""))
            # Try to find mileage
            mile_match = re.search(r'(\d{1,3},?\d{3})\s*(?:mi|miles|mileage)', text, re.IGNORECASE)
            if mile_match:
                info["mileage"] = int(mile_match.group(1).replace(",", ""))
            # Try to find VIN
            vin_match = re.search(r'([A-HJ-NPR-Z0-9]{17})', text)
            if vin_match:
                info["vin"] = vin_match.group(1).upper()
            # Try year/make/model from title
            title_match = re.search(r'(20\d{2}|19\d{2})\s+([A-Z][a-zA-Z]+)\s+([A-Z][a-zA-Z0-9\- ]+)', text)
            if title_match:
                info["year"] = int(title_match.group(1))
                info["make"] = title_match.group(2)
                info["model"] = title_match.group(3).strip()
            return info
    except Exception as e:
        log.warning(f"Scrape failed for {url}: {e}")
    return {}



# ══════════════════════════════════════════════════════════════
# MARKET ANALYSIS ENGINE
# ══════════════════════════════════════════════════════════════

def get_market_comps(year, make, model, trim=None, zip_code=None, price=None):
    """
    Pull comparable listings from Auto.dev to build market context.
    Returns: avg_price, price_percentile, comp_count, price_range, demand_score.
    """
    zc = zip_code or DEFAULT_ZIP
    params = {
        "zip": zc, "distance": "150", "page_size": "50",
        "sort_by": "price", "sort_order": "asc",
        "make": make, "model": model
    }
    if year:
        params["year_min"] = str(max(int(year) - 1, 2000))
        params["year_max"] = str(int(year) + 1)
    if trim:
        params["trim"] = trim

    try:
        resp = requests.get(AUTODEV_BASE, params=params,
                           headers={"Authorization": f"Bearer {AUTODEV_API_KEY}"}, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            records = data.get("records", [])
            total = data.get("totalCount", 0)

            # Extract valid prices
            prices = []
            for r in records:
                p = r.get("price")
                if isinstance(p, (int, float)) and p > 2000:
                    prices.append(p)

            if not prices:
                return None

            avg_price = sum(prices) / len(prices)
            min_price = min(prices)
            max_price = max(prices)

            # Calculate percentile of the listed price
            percentile = None
            if price and isinstance(price, (int, float)):
                below = sum(1 for p in prices if p <= price)
                percentile = round((below / len(prices)) * 100)

            # Demand score (0-10): based on how many comps exist and price spread
            if total >= 50:
                demand = 4  # Lots of inventory = low demand pressure
            elif total >= 20:
                demand = 6
            elif total >= 10:
                demand = 7
            elif total >= 5:
                demand = 8
            else:
                demand = 9  # Very few = high demand / rare

            # Adjust demand by price spread
            spread = (max_price - min_price) / avg_price if avg_price > 0 else 0
            if spread < 0.1:
                demand = min(demand + 1, 10)  # Tight pricing = strong market

            return {
                "avg_price": round(avg_price),
                "min_price": round(min_price),
                "max_price": round(max_price),
                "percentile": percentile,
                "comp_count": len(prices),
                "total_market": total,
                "demand_score": demand,
                "price_spread": round(spread * 100, 1)
            }
    except Exception as e:
        log.error(f"Market comp lookup failed: {e}")

    return None


def get_nhtsa_data(year, make, model):
    """
    Pull recall and complaint data from NHTSA.
    Returns: recall_count, complaint_count, top_issues.
    """
    result = {"recalls": [], "recall_count": 0, "complaints": [], "complaint_count": 0}

    try:
        # Recalls
        resp = requests.get(NHTSA_RECALLS_URL, params={
            "make": make, "model": model, "modelYear": str(year)
        }, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            recalls = data.get("results", [])
            result["recall_count"] = len(recalls)
            for r in recalls[:10]:
                result["recalls"].append({
                    "component": r.get("Component", "Unknown"),
                    "summary": r.get("Summary", "")[:200],
                    "consequence": r.get("Consequence", "")[:150],
                    "remedy": r.get("Remedy", "")[:150],
                    "date": r.get("ReportReceivedDate", "")
                })
    except Exception as e:
        log.warning(f"NHTSA recalls failed: {e}")

    try:
        # Complaints
        resp = requests.get(NHTSA_COMPLAINTS, params={
            "make": make, "model": model, "modelYear": str(year)
        }, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            complaints = data.get("results", [])
            result["complaint_count"] = len(complaints)
            # Group by component
            components = {}
            for c in complaints:
                comp = c.get("components", "Unknown")
                if comp not in components:
                    components[comp] = 0
                components[comp] += 1
            result["top_complaint_areas"] = sorted(components.items(), key=lambda x: -x[1])[:5]
    except Exception as e:
        log.warning(f"NHTSA complaints failed: {e}")

    return result


def lookup_vin_autodev(vin):
    """Look up a specific VIN on Auto.dev for detailed listing data."""
    try:
        resp = requests.get(f"{AUTODEV_BASE}/{vin}",
                           headers={"Authorization": f"Bearer {AUTODEV_API_KEY}"}, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        log.warning(f"VIN lookup failed: {e}")
    return None



# ══════════════════════════════════════════════════════════════
# AI ANALYSIS ENGINE — The Pro Buyer Brain
# ══════════════════════════════════════════════════════════════

ANALYSIS_SYSTEM_PROMPT = """You are an expert car buying advisor with 20 years of dealership experience.
You think like a seasoned buyer who has seen every trick, knows every weak point, and spots what normal people miss.

You are analyzing a specific vehicle listing for a consumer. Your job is to give them the EXACT intelligence
a professional buyer would have — specific, actionable, no generic fluff.

CRITICAL RULES:
1. Be SPECIFIC to this exact vehicle. Reference the actual year, make, model, trim, generation.
2. Known issues must be REAL issues for this specific generation/engine/transmission combo.
3. Questions to ask must be things a dealer would NOT want you to ask.
4. Negotiation strategy must account for the actual market position of this listing.
5. Never be generic. Never say "check the tires." Say something a pro would actually say.
6. Think like you're sitting in the dealer's chair — what would make YOU uncomfortable?
7. Include insider knowledge: dealer holdback, factory incentives, auction values, seasonal trends.
8. Factor in days on lot, market velocity, competing inventory.

You must return VALID JSON matching this exact structure:
{
  "buy_score": {
    "score": <1-10>,
    "label": "<Strong Buy|Fair Buy|Leverage Opportunity|High Risk|Walk Away>",
    "one_liner": "<one sentence verdict>"
  },
  "market_position": {
    "summary": "<2-3 sentence market analysis>",
    "price_verdict": "<overpriced|market_priced|below_market|aggressive_deal>",
    "days_estimate": "<estimated days on market if available>",
    "seasonal_factor": "<any seasonal pricing advantage or disadvantage>"
  },
  "reliability_profile": {
    "risk_tier": "<Low|Moderate|Elevated|High>",
    "summary": "<2-3 sentence reliability overview for this generation>",
    "known_issues": [
      {
        "issue": "<specific problem>",
        "severity": "<minor|moderate|major|critical>",
        "typical_cost": "<repair cost range>",
        "frequency": "<common|occasional|rare>",
        "check": "<how to verify this specific issue>"
      }
    ],
    "maintenance_watch": "<upcoming major maintenance items based on mileage>"
  },
  "smart_questions": [
    {
      "question": "<specific question to ask>",
      "why": "<why this matters — insider reason>",
      "good_answer": "<what a good answer sounds like>",
      "red_flag": "<what answer should worry you>"
    }
  ],
  "negotiation_strategy": {
    "leverage_points": ["<specific leverage point 1>", "<leverage point 2>"],
    "opening_offer": "<dollar amount or range>",
    "target_price": "<realistic target>",
    "walk_away_price": "<maximum you should pay>",
    "tactics": ["<specific tactic 1>", "<tactic 2>", "<tactic 3>"],
    "fee_watchlist": ["<specific fee to challenge 1>", "<fee 2>", "<fee 3>"],
    "timing_advice": "<when to buy for best leverage>"
  },
  "pro_tips": ["<insider tip 1>", "<insider tip 2>", "<insider tip 3>"]
}
"""

def generate_analysis(vehicle_info, market_data, nhtsa_data):
    """
    Generate the full pro buyer analysis using Groq AI.
    Combines vehicle details, market comps, and safety data into one prompt.
    """
    # Build context for the AI
    context_parts = []

    # Vehicle details
    v = vehicle_info
    context_parts.append(f"VEHICLE: {v.get('year', '?')} {v.get('make', '?')} {v.get('model', '?')} {v.get('trim', '')}")
    if v.get("price"): context_parts.append(f"LISTED PRICE: ${v['price']:,}")
    if v.get("mileage"): context_parts.append(f"MILEAGE: {v['mileage']:,} miles")
    if v.get("vin"): context_parts.append(f"VIN: {v['vin']}")
    if v.get("color"): context_parts.append(f"COLOR: {v['color']}")
    if v.get("zip"): context_parts.append(f"LOCATION ZIP: {v['zip']}")
    if v.get("dealer_name"): context_parts.append(f"DEALER: {v['dealer_name']}")

    # Market data
    if market_data:
        m = market_data
        context_parts.append(f"\nMARKET DATA:")
        context_parts.append(f"  Regional average price: ${m['avg_price']:,}")
        context_parts.append(f"  Price range: ${m['min_price']:,} - ${m['max_price']:,}")
        if m.get('percentile') is not None:
            context_parts.append(f"  This listing is at the {m['percentile']}th percentile (higher = more expensive)")
        context_parts.append(f"  Comparable listings found: {m['comp_count']} (total market: {m['total_market']})")
        context_parts.append(f"  Demand score: {m['demand_score']}/10")
        context_parts.append(f"  Price spread: {m['price_spread']}%")

    # NHTSA data
    if nhtsa_data:
        n = nhtsa_data
        context_parts.append(f"\nSAFETY DATA (NHTSA):")
        context_parts.append(f"  Recalls: {n['recall_count']}")
        context_parts.append(f"  Consumer complaints: {n['complaint_count']}")
        if n.get("top_complaint_areas"):
            areas = ", ".join(f"{area} ({count})" for area, count in n["top_complaint_areas"][:5])
            context_parts.append(f"  Top complaint areas: {areas}")
        if n.get("recalls"):
            for r in n["recalls"][:3]:
                context_parts.append(f"  Recall: {r['component']} — {r['summary'][:100]}")

    context = "\n".join(context_parts)

    # Call Groq
    try:
        resp = requests.post(GROQ_URL, json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": f"Analyze this vehicle listing and generate a complete acquisition brief:\n\n{context}"}
            ],
            "temperature": 0.3,
            "max_tokens": 3000,
            "response_format": {"type": "json_object"}
        }, headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }, timeout=30)

        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            analysis = json.loads(content)
            log.info(f"Analysis generated successfully for {v.get('year')} {v.get('make')} {v.get('model')}")
            return analysis
        else:
            log.error(f"Groq API error: {resp.status_code} — {resp.text[:200]}")
    except Exception as e:
        log.error(f"Analysis generation failed: {e}")

    return None



# ══════════════════════════════════════════════════════════════
# ORCHESTRATOR — Ties everything together
# ══════════════════════════════════════════════════════════════

def analyze_listing(input_data):
    """
    Main orchestrator. Takes user input and produces full analysis.
    Input can be: { url: "..." } or { year, make, model, trim, price, mileage, vin, zip }
    """
    vehicle = {}

    # If URL provided, try to parse it
    if input_data.get("url"):
        url_info = parse_listing_url(input_data["url"])
        vehicle.update(url_info)
        # Try to scrape for more details
        scraped = scrape_listing(input_data["url"])
        for k, v in scraped.items():
            if v and not vehicle.get(k):
                vehicle[k] = v

    # Override/supplement with user-provided fields
    for field in ["year", "make", "model", "trim", "price", "mileage", "vin", "zip", "color", "dealer_name"]:
        if input_data.get(field):
            vehicle[field] = input_data[field]

    # Validate minimum required fields
    if not vehicle.get("make") or not vehicle.get("model"):
        return {"error": "Need at least make and model. Paste a URL or enter details manually."}

    # If we have a VIN, try Auto.dev lookup for rich data
    if vehicle.get("vin") and AUTODEV_API_KEY:
        vin_data = lookup_vin_autodev(vehicle["vin"])
        if vin_data:
            for k in ["year", "make", "model", "trim", "price", "mileage"]:
                if vin_data.get(k) and not vehicle.get(k):
                    vehicle[k] = vin_data[k]
            if vin_data.get("dealerName"):
                vehicle["dealer_name"] = vin_data["dealerName"]
            if vin_data.get("dealerPhone"):
                vehicle["dealer_phone"] = vin_data["dealerPhone"]
            if vin_data.get("photoUrls"):
                vehicle["photos"] = vin_data["photoUrls"][:5]
            if vin_data.get("displayColor"):
                vehicle["color"] = vin_data["displayColor"]

    # Normalize types
    if vehicle.get("year"): vehicle["year"] = int(vehicle["year"])
    if vehicle.get("price"):
        try: vehicle["price"] = int(str(vehicle["price"]).replace(",", "").replace("$", ""))
        except: pass
    if vehicle.get("mileage"):
        try: vehicle["mileage"] = int(str(vehicle["mileage"]).replace(",", ""))
        except: pass

    log.info(f"Analyzing: {vehicle}")

    # Step 1: Market comps
    market_data = None
    if vehicle.get("make") and vehicle.get("model"):
        market_data = get_market_comps(
            vehicle.get("year"), vehicle["make"], vehicle["model"],
            vehicle.get("trim"), vehicle.get("zip"), vehicle.get("price")
        )

    # Step 2: NHTSA safety data
    nhtsa_data = None
    if vehicle.get("year") and vehicle.get("make") and vehicle.get("model"):
        nhtsa_data = get_nhtsa_data(vehicle["year"], vehicle["make"], vehicle["model"])

    # Step 3: AI analysis
    analysis = generate_analysis(vehicle, market_data, nhtsa_data)

    if not analysis:
        return {"error": "Analysis generation failed. Please try again."}

    # Assemble the full report
    report = {
        "vehicle": vehicle,
        "market_data": market_data,
        "nhtsa_data": {
            "recall_count": nhtsa_data["recall_count"] if nhtsa_data else 0,
            "complaint_count": nhtsa_data["complaint_count"] if nhtsa_data else 0,
        },
        "analysis": analysis,
        "generated_at": datetime.utcnow().isoformat(),
        "report_id": hashlib.md5(json.dumps(vehicle, sort_keys=True, default=str).encode()).hexdigest()[:12]
    }

    return report


# ══════════════════════════════════════════════════════════════
# API ROUTES
# ══════════════════════════════════════════════════════════════

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """
    Main analysis endpoint.
    Accepts JSON: { url: "...", year, make, model, trim, price, mileage, vin, zip }
    Returns full analysis report.
    """
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
    """Parse a listing URL and return extracted vehicle info."""
    data = request.get_json()
    url = data.get("url", "")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    info = parse_listing_url(url)
    scraped = scrape_listing(url)
    for k, v in scraped.items():
        if v and not info.get(k):
            info[k] = v

    return jsonify(info)


@app.route("/health", methods=["GET"])
def health():
    """Health check."""
    return jsonify({
        "status": "ok",
        "service": "AskCarBuddy",
        "version": "1.0.0"
    })


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"🚗 AskCarBuddy starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
