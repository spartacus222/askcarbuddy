#!/usr/bin/env python3
"""
AskCarBuddy MVP v2 - AI Car Buying Intelligence
================================================
Paste any listing URL -> Get a pro-level intelligence brief.

Philosophy: You found a car you like? We help you buy it SMART.
We don't scare you away. We arm you with knowledge.

Stack: Flask + Groq AI + Auto.dev + NHTSA + Exa (scraping)
"""

import os
import json
import re
import time
import hashlib
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

# -- Logging --
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("askcarbuddy")

# -- Flask App --
app = Flask(__name__)
CORS(app)

# -- Config --
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

REPORT_PRICE      = 19  # dollars


# ==============================================================
# URL PARSER - Extract vehicle details from listing URLs
# ==============================================================

def parse_listing_url(url):
    """Extract vehicle info from a listing URL."""
    url = url.strip()
    info = {"source": "unknown", "url": url}

    if "cars.com" in url:
        info["source"] = "cars.com"
        vin_match = re.search(r'/detail/([A-HJ-NPR-Z0-9]{17})', url, re.IGNORECASE)
        if vin_match:
            info["vin"] = vin_match.group(1).upper()
        ym_match = re.search(r'/(\d{4})[-_]([a-z]+)[-_]([a-z0-9]+)', url, re.IGNORECASE)
        if ym_match:
            info["year"] = int(ym_match.group(1))
            info["make"] = ym_match.group(2).title()
            info["model"] = ym_match.group(3).title()

    elif "autotrader.com" in url:
        info["source"] = "autotrader"
        vin_match = re.search(r'/([A-HJ-NPR-Z0-9]{17})', url, re.IGNORECASE)
        if vin_match:
            info["vin"] = vin_match.group(1).upper()

    elif "cargurus.com" in url:
        info["source"] = "cargurus"
        vin_match = re.search(r'[#/]([A-HJ-NPR-Z0-9]{17})', url, re.IGNORECASE)
        if vin_match:
            info["vin"] = vin_match.group(1).upper()

    elif "facebook.com/marketplace" in url:
        info["source"] = "facebook"

    else:
        info["source"] = "dealer"
        vin_match = re.search(r'[/=]([A-HJ-NPR-Z0-9]{17})(?:[/&?.]|$)', url, re.IGNORECASE)
        if vin_match:
            info["vin"] = vin_match.group(1).upper()

    return info



# ==============================================================
# SCRAPER - Pull listing details from any URL via Exa
# ==============================================================

def scrape_listing_exa(url):
    """Use Exa API to fetch clean page content from any listing URL."""
    if not EXA_API_KEY:
        return scrape_listing_basic(url)
    try:
        resp = requests.post(EXA_URL, json={
            "urls": [url],
            "text": True,
            "extras": {"links": 3, "imageLinks": 5}
        }, headers={
            "x-api-key": EXA_API_KEY,
            "Content-Type": "application/json"
        }, timeout=15)
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                return results[0].get("text", ""), results[0].get("extras", {}).get("imageLinks", [])
    except Exception as e:
        log.warning(f"Exa scrape failed: {e}")
    return scrape_listing_basic(url), []


def scrape_listing_basic(url):
    """Basic requests scrape as fallback."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            return resp.text
    except:
        pass
    return ""


def extract_vehicle_from_text(text):
    """Parse vehicle details from raw listing text."""
    info = {}
    # Price
    price_match = re.search(r'\$(\d{1,3},?\d{3})', text)
    if price_match:
        info["price"] = int(price_match.group(1).replace(",", ""))
    # Mileage
    mile_match = re.search(r'(\d{1,3},?\d{3})\s*(?:mi(?:les)?|mileage|odometer)', text, re.IGNORECASE)
    if mile_match:
        info["mileage"] = int(mile_match.group(1).replace(",", ""))
    # VIN
    vin_match = re.search(r'VIN[:\s]*([A-HJ-NPR-Z0-9]{17})', text, re.IGNORECASE)
    if vin_match:
        info["vin"] = vin_match.group(1).upper()
    # Year/Make/Model from title-like pattern
    ymm = re.search(r'(20\d{2}|19\d{2})\s+([A-Z][a-zA-Z]+)\s+([A-Z][a-zA-Z0-9\-]+)', text)
    if ymm:
        info["year"] = int(ymm.group(1))
        info["make"] = ymm.group(2)
        info["model"] = ymm.group(3)
    # Trim
    trim_match = re.search(r'(?:trim|package)[:\s]+([A-Za-z0-9 \-]+)', text, re.IGNORECASE)
    if trim_match:
        info["trim"] = trim_match.group(1).strip()
    return info


# ==============================================================
# AUTO.DEV - VIN lookup + market comps
# ==============================================================

def lookup_vin_autodev(vin):
    """Look up a specific VIN on Auto.dev for rich listing data."""
    if not AUTODEV_API_KEY:
        return None
    try:
        resp = requests.get(f"{AUTODEV_BASE}?vin={vin}", headers={
            "Authorization": f"Bearer {AUTODEV_API_KEY}"
        }, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            records = data.get("records", [])
            if records:
                r = records[0]
                return {
                    "year": r.get("year"),
                    "make": r.get("make"),
                    "model": r.get("model"),
                    "trim": r.get("trim"),
                    "price": r.get("price"),
                    "mileage": r.get("mileage"),
                    "dealerName": r.get("dealerName"),
                    "dealerPhone": r.get("dealerPhone"),
                    "displayColor": r.get("displayColor"),
                    "photoUrls": r.get("photoUrls", []),
                    "bodyType": r.get("bodyType"),
                    "engine": r.get("engine"),
                    "transmission": r.get("transmission"),
                    "drivetrain": r.get("drivetrain"),
                    "fuelType": r.get("fuelType"),
                    "mpgCity": r.get("mpgCity"),
                    "mpgHighway": r.get("mpgHighway"),
                }
    except Exception as e:
        log.warning(f"Auto.dev VIN lookup failed: {e}")
    return None


def get_market_comps(year, make, model, trim=None, zip_code=None, listing_price=None):
    """Get comparable listings from Auto.dev to establish market position."""
    if not AUTODEV_API_KEY:
        return None
    try:
        params = {"make": make, "model": model}
        if year:
            params["year_min"] = max(year - 1, 1990)
            params["year_max"] = year + 1
        if zip_code:
            params["zip"] = zip_code
            params["radius"] = 100
        params["page_size"] = 50

        resp = requests.get(AUTODEV_BASE, params=params, headers={
            "Authorization": f"Bearer {AUTODEV_API_KEY}"
        }, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            records = data.get("records", [])
            total = data.get("totalCount", len(records))
            prices = [r["price"] for r in records if r.get("price") and r["price"] > 0]

            if not prices:
                return None

            prices.sort()
            avg_price = sum(prices) // len(prices)
            min_price = prices[0]
            max_price = prices[-1]

            percentile = None
            if listing_price:
                below = len([p for p in prices if p <= listing_price])
                percentile = round(below / len(prices) * 100)

            spread = round((max_price - min_price) / avg_price * 100) if avg_price > 0 else 0
            demand = min(10, max(1, total // 10))

            return {
                "avg_price": avg_price,
                "min_price": min_price,
                "max_price": max_price,
                "percentile": percentile,
                "comp_count": len(prices),
                "total_market": total,
                "demand_score": demand,
                "price_spread": spread,
                "prices_sample": prices[:20]
            }
    except Exception as e:
        log.warning(f"Market comp lookup failed: {e}")
    return None


# ==============================================================
# NHTSA - Recalls + complaints
# ==============================================================

def get_nhtsa_data(year, make, model):
    """Fetch recalls and complaints from NHTSA."""
    result = {"recall_count": 0, "complaint_count": 0, "recalls": [], "top_complaint_areas": []}
    try:
        # Recalls
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
        # Complaints
        resp = requests.get(NHTSA_COMPLAINTS, params={
            "make": make, "model": model, "modelYear": year
        }, timeout=10)
        if resp.status_code == 200:
            complaints = resp.json().get("results", [])
            result["complaint_count"] = len(complaints)
            # Count by component
            areas = {}
            for c in complaints:
                comp = c.get("components", "Unknown")
                areas[comp] = areas.get(comp, 0) + 1
            result["top_complaint_areas"] = sorted(areas.items(), key=lambda x: -x[1])[:5]
    except:
        pass
    return result



# ==============================================================
# AI SYSTEM PROMPT - The brain of AskCarBuddy v2
# ==============================================================

ANALYSIS_SYSTEM_PROMPT = """You are AskCarBuddy — a trusted car-expert friend with 20 years of dealership experience.

PHILOSOPHY: The user found a car they LIKE. Your job is NOT to talk them out of it.
Your job is to arm them with intelligence so they can buy it CONFIDENTLY and SMARTLY.

Think: "Great pick — here's everything you need to know to own this car and love it."

TONE: Warm, confident, knowledgeable. Like a friend who happens to be a car expert.
NOT: Salesy, fear-mongering, or generic. Never say "check the tires." Be SPECIFIC.

CRITICAL RULES:
1. Be SPECIFIC to this exact vehicle — reference the actual year, make, model, trim, generation, engine.
2. Known issues must be REAL documented issues for this specific generation/engine/transmission.
3. Frame everything as HELPFUL, not scary. "Here's what to know" not "here's what could go wrong."
4. Smart questions should help the buyer feel PREPARED, not adversarial toward the dealer.
5. Cost of ownership should be realistic and practical — what will this car actually cost to run?
6. If the car is a genuinely good find, SAY SO enthusiastically. If it has real concerns, flag them honestly but constructively.
7. The out-the-door section should prepare them for what they'll actually pay — no surprises at the desk.
8. Include SPECIFIC maintenance milestones based on the car's current mileage.

Return VALID JSON matching this exact structure:
{
  "buy_score": {
    "score": <1-10>,
    "label": "<Great Find|Solid Pick|Worth a Look|Proceed with Caution|Think Twice>",
    "one_liner": "<one warm sentence verdict — encourage if deserved>"
  },
  "at_a_glance": {
    "best_thing": "<the single best thing about this specific car>",
    "know_before_you_go": "<the single most important thing to verify before buying>"
  },
  "market_intel": {
    "summary": "<2-3 sentences on where this price sits vs market — factual, not judgmental>",
    "price_position": "<below_market|competitive|market_price|above_market>",
    "value_factors": ["<why this price makes sense OR what's driving it up/down>"]
  },
  "what_to_know": {
    "generation_overview": "<1-2 sentences about this specific generation — reputation, strengths>",
    "known_quirks": [
      {
        "item": "<specific known issue or quirk for this generation>",
        "severity": "<minor_quirk|worth_checking|important|serious>",
        "context": "<honest context — how common, how expensive IF it happens, what to look for>",
        "what_to_do": "<specific actionable step the buyer should take>"
      }
    ],
    "maintenance_upcoming": [
      {
        "service": "<specific maintenance item due based on current mileage>",
        "typical_cost": "<cost range>",
        "urgency": "<due_now|soon|within_6_months|within_a_year>"
      }
    ]
  },
  "your_game_plan": {
    "before_you_visit": ["<specific prep step 1>", "<step 2>", "<step 3>"],
    "at_the_dealer": [
      {
        "ask": "<specific question to ask>",
        "why": "<why this matters — insider knowledge>",
        "good_sign": "<what a reassuring answer sounds like>",
        "heads_up": "<what answer means you should dig deeper>"
      }
    ],
    "on_the_test_drive": ["<specific thing to check/feel/listen for on THIS car>"],
    "at_the_desk": {
      "expected_otd": "<estimated out-the-door price range>",
      "fees_to_expect": ["<legitimate fee and typical amount>"],
      "fees_to_question": ["<fee that's sometimes inflated + what it should cost>"],
      "financing_tip": "<one specific financing tip for this purchase>"
    }
  },
  "cost_to_own": {
    "monthly_fuel": "<estimated monthly fuel cost at current gas prices>",
    "annual_insurance_range": "<estimated annual insurance range>",
    "annual_maintenance": "<estimated annual maintenance cost>",
    "total_annual_estimate": "<total estimated annual running cost range>",
    "ownership_verdict": "<one sentence on whether this is cheap/average/expensive to own>"
  },
  "pro_tips": ["<genuinely useful insider tip specific to THIS car>", "<tip 2>", "<tip 3>"]
}
"""



# ==============================================================
# AI ANALYSIS GENERATOR
# ==============================================================

def generate_analysis(vehicle_info, market_data, nhtsa_data, listing_text=""):
    """Generate the full intelligence brief using Groq AI."""
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

    if market_data:
        m = market_data
        context_parts.append(f"\nMARKET DATA:")
        context_parts.append(f"  Regional average price: ${m['avg_price']:,}")
        context_parts.append(f"  Price range: ${m['min_price']:,} - ${m['max_price']:,}")
        if m.get('percentile') is not None:
            context_parts.append(f"  This listing is at the {m['percentile']}th percentile (higher = more expensive)")
        context_parts.append(f"  Comparable listings found: {m['comp_count']} (total market: {m['total_market']})")
        context_parts.append(f"  Demand score: {m['demand_score']}/10")

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
                context_parts.append(f"  Recall: {r['component']} - {r['summary'][:120]}")

    # Include raw listing text (truncated) for max context
    if listing_text:
        context_parts.append(f"\nRAW LISTING TEXT (from dealer page):")
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
            "max_tokens": 3000,
            "response_format": {"type": "json_object"}
        }, headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }, timeout=30)

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
# ORCHESTRATOR
# ==============================================================

def analyze_listing(input_data):
    """Main orchestrator. URL in -> full report out."""
    vehicle = {}
    listing_text = ""

    # Step 0: If URL, scrape it for raw content
    if input_data.get("url"):
        url_info = parse_listing_url(input_data["url"])
        vehicle.update(url_info)

        # Scrape via Exa for full page content
        scrape_result = scrape_listing_exa(input_data["url"])
        if isinstance(scrape_result, tuple):
            listing_text, images = scrape_result
            if images:
                vehicle["photos"] = images[:5]
        else:
            listing_text = scrape_result

        # Extract structured data from scraped text
        if listing_text:
            extracted = extract_vehicle_from_text(listing_text)
            for k, v in extracted.items():
                if v and not vehicle.get(k):
                    vehicle[k] = v

    # Override with user-provided fields
    for field in ["year", "make", "model", "trim", "price", "mileage", "vin", "zip", "color", "dealer_name"]:
        if input_data.get(field):
            vehicle[field] = input_data[field]

    # Validate minimum
    if not vehicle.get("make") or not vehicle.get("model"):
        return {"error": "Couldn't identify the car from that URL. Try pasting a different listing, or enter the details manually."}

    # VIN lookup for rich data
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
                vehicle["photos"] = vin_data["photoUrls"][:5]
            if vin_data.get("displayColor") and not vehicle.get("color"):
                vehicle["color"] = vin_data["displayColor"]

    # Normalize types
    if vehicle.get("year"):
        try: vehicle["year"] = int(vehicle["year"])
        except: pass
    if vehicle.get("price"):
        try: vehicle["price"] = int(str(vehicle["price"]).replace(",", "").replace("$", ""))
        except: pass
    if vehicle.get("mileage"):
        try: vehicle["mileage"] = int(str(vehicle["mileage"]).replace(",", ""))
        except: pass

    log.info(f"Analyzing: {vehicle.get('year')} {vehicle.get('make')} {vehicle.get('model')} - ${vehicle.get('price', '?')}")

    # Step 1: Market comps
    market_data = None
    if vehicle.get("make") and vehicle.get("model"):
        market_data = get_market_comps(
            vehicle.get("year"), vehicle["make"], vehicle["model"],
            vehicle.get("trim"), vehicle.get("zip") or DEFAULT_ZIP, vehicle.get("price")
        )

    # Step 2: NHTSA safety data
    nhtsa_data = None
    if vehicle.get("year") and vehicle.get("make") and vehicle.get("model"):
        nhtsa_data = get_nhtsa_data(vehicle["year"], vehicle["make"], vehicle["model"])

    # Step 3: AI analysis
    analysis = generate_analysis(vehicle, market_data, nhtsa_data, listing_text)

    if not analysis:
        return {"error": "Analysis generation failed. Please try again."}

    return {
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


# ==============================================================
# API ROUTES
# ==============================================================

@app.route("/")
def home():
    """Serve the frontend."""
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(html_path):
        with open(html_path) as f:
            return render_template_string(f.read())
    return "<h1>AskCarBuddy</h1><p>Frontend not found.</p>"

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """Main analysis endpoint."""
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
    return jsonify(info)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "AskCarBuddy", "version": "2.0.0"})


# ==============================================================
# MAIN
# ==============================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"AskCarBuddy v2 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
