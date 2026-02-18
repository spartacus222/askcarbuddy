#!/usr/bin/env python3
"""
AskCarBuddy v5.0 - AI Car Buying Intelligence (Smart Engine)
=============================================================
Paste any listing URL -> Get a REAL pro-level intelligence brief.

v4 changes:
- Completely rewritten AI prompt with identity anchoring
- Two-pass generation: research pass + analysis pass
- Temperature dropped to 0.2 for factual precision
- Vehicle identity block forces model to anchor every answer
- Quality: every question, tip, and checklist item MUST reference the specific car
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
NHTSA_VIN_DECODE  = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues"
GROQ_URL          = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL        = "llama-3.3-70b-versatile"
EXA_URL           = "https://api.exa.ai/contents"
EXA_SEARCH_URL    = "https://api.exa.ai/search"


# ==============================================================
# HELPERS
# ==============================================================

def parse_price(val):
    if val is None: return None
    if isinstance(val, (int, float)): return int(val) if val > 0 else None
    s = re.sub(r'[^\d.]', '', str(val).strip())
    try:
        p = int(float(s))
        return p if p > 0 else None
    except: return None

def parse_mileage(val):
    if val is None: return None
    if isinstance(val, (int, float)): return int(val) if val > 0 else None
    s = re.sub(r'[^\d]', '', str(val).strip())
    try:
        m = int(s)
        return m if m > 0 else None
    except: return None


# ==============================================================
# URL PARSER
# ==============================================================

def parse_listing_url(url):
    url = url.strip()
    info = {"source": "unknown", "url": url}
    if "cars.com" in url: info["source"] = "cars.com"
    elif "autotrader.com" in url: info["source"] = "autotrader"
    elif "cargurus.com" in url: info["source"] = "cargurus"
    elif "facebook.com/marketplace" in url: info["source"] = "facebook"
    else: info["source"] = "dealer"
    vin_match = re.search(r'[/=]([A-HJ-NPR-Z0-9]{17})(?:[/&?.]|$)', url, re.IGNORECASE)
    if vin_match: info["vin"] = vin_match.group(1).upper()
    return info


# ==============================================================
# SCRAPER
# ==============================================================

def extract_vin_from_url(url):
    """Extract VIN from URL path or query params."""
    vin_match = re.search(r'[A-HJ-NPR-Z0-9]{17}', url, re.IGNORECASE)
    if vin_match:
        candidate = vin_match.group(0).upper()
        if re.match(r'^[A-HJ-NPR-Z0-9]{17}$', candidate):
            return candidate
    return None

def extract_ymm_from_url(url):
    """Extract year/make/model from URL path (common dealer URL format)."""
    path = url.lower().split('?')[0]
    ymm = re.search(r'(20\d{2}|19\d{2})[-/_]([a-z]+)[-/_]([a-z0-9]+)', path)
    if ymm:
        return {"year": int(ymm.group(1)), "make": ymm.group(2).title(), "model": ymm.group(3).title()}
    return {}

def nhtsa_vin_decode(vin):
    """Decode VIN via NHTSA — FREE, reliable, gives year/make/model/trim/specs."""
    try:
        resp = requests.get(f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json", timeout=10)
        if resp.status_code == 200:
            r = resp.json().get("Results", [{}])[0]
            info = {}
            if r.get("ModelYear"): info["year"] = int(r["ModelYear"])
            if r.get("Make"): info["make"] = r["Make"].title()
            if r.get("Model"): info["model"] = r["Model"]
            if r.get("Trim") and "/" not in r["Trim"]: info["trim"] = r["Trim"]
            if r.get("BodyClass"): info["body"] = r["BodyClass"]
            if r.get("DriveType"): info["drive_type"] = r["DriveType"]
            if r.get("FuelTypePrimary"): info["fuel_type"] = r["FuelTypePrimary"]
            if r.get("EngineCylinders"): info["engine_cylinders"] = r["EngineCylinders"]
            if r.get("DisplacementL"): info["engine_size"] = f"{r['DisplacementL']}L"
            if r.get("TransmissionStyle"): info["transmission"] = r["TransmissionStyle"]
            info["vin"] = vin
            log.info(f"NHTSA decode: {info.get('year')} {info.get('make')} {info.get('model')}")
            return info
    except Exception as e:
        log.warning(f"NHTSA decode failed: {e}")
    return {}

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
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}, timeout=12, allow_redirects=True)
        if resp.status_code == 200: return resp.text
    except: pass
    return ""

def extract_vehicle_from_text(text):
    """Extract vehicle info from HTML/text — price, mileage, VIN, and title-based YMM."""
    info = {}
    # Price
    price_match = re.search(r'\$(\d{1,3},?\d{3})', text)
    if price_match: info["price"] = parse_price(price_match.group(0))
    # Mileage
    mile_match = re.search(r'(\d{1,3},?\d{3})\s*(?:mi(?:les)?|mileage|odometer)', text, re.IGNORECASE)
    if mile_match: info["mileage"] = parse_mileage(mile_match.group(1))
    # VIN from text
    vin_match = re.search(r'(?:VIN|Stock)[:\s#]*([A-HJ-NPR-Z0-9]{17})', text, re.IGNORECASE)
    if vin_match: info["vin"] = vin_match.group(1).upper()
    # Dealer name from structured data
    dealer_match = re.search(r'"dealer(?:Name|_name)"\s*:\s*"([^"]+)"', text)
    if dealer_match: info["dealer_name"] = dealer_match.group(1)
    # Title-based extraction (most reliable for YMM from HTML)
    title = re.search(r'<title[^>]*>(.*?)</title>', text, re.IGNORECASE | re.DOTALL)
    og = re.search(r'<meta[^>]*property=["\'"]og:title["\'"][^>]*content=["\'"]([^"\'"]*)', text, re.IGNORECASE)
    title_text = (og.group(1) if og else title.group(1) if title else "").strip()
    if title_text:
        ymm = re.search(r'(20\d{2}|19\d{2})\s+([A-Za-z]+)\s+([A-Za-z0-9][A-Za-z0-9\- ]+?)(?:\s+[-|·•]|\s+for\s|\s+in\s|$)', title_text)
        if ymm:
            info["year"] = int(ymm.group(1))
            info["make"] = ymm.group(2).strip()
            info["model"] = ymm.group(3).strip()
    # JSON-LD structured data (best source)
    jsonld_matches = re.findall(r'<script[^>]*type=["\'"]application/ld\+json["\'"][^>]*>(.*?)</script>', text, re.DOTALL | re.IGNORECASE)
    for jtext in jsonld_matches[:3]:
        try:
            import json as jlib
            jd = jlib.loads(jtext)
            if isinstance(jd, list): jd = jd[0]
            if jd.get("@type") in ["Vehicle", "Car", "Product", "Auto"]:
                if jd.get("vehicleIdentificationNumber"): info["vin"] = jd["vehicleIdentificationNumber"].upper()
                if jd.get("name"):
                    name_ymm = re.search(r'(20\d{2}|19\d{2})\s+([A-Za-z]+)\s+(.*)', jd["name"])
                    if name_ymm:
                        info["year"] = int(name_ymm.group(1))
                        info["make"] = name_ymm.group(2)
                        info["model"] = name_ymm.group(3).split(" - ")[0].strip()
                if jd.get("mileageFromOdometer"):
                    m = jd["mileageFromOdometer"]
                    if isinstance(m, dict): m = m.get("value", m.get("name", ""))
                    mile_val = re.search(r'([\d,]+)', str(m))
                    if mile_val: info["mileage"] = parse_mileage(mile_val.group(1))
                if jd.get("offers"):
                    offers = jd["offers"]
                    if isinstance(offers, list): offers = offers[0]
                    if isinstance(offers, dict) and offers.get("price"):
                        info["price"] = parse_price(str(offers["price"]))
        except: pass
    return info


# ==============================================================
# NHTSA VIN DECODE Ã¢ÂÂ get exact specs
# ==============================================================

def decode_vin_nhtsa(vin):
    """Decode VIN via NHTSA to get exact engine, displacement, drivetrain, etc."""
    try:
        resp = requests.get(f"{NHTSA_VIN_DECODE}/{vin}", params={"format": "json", "modelYear": ""}, timeout=10)
        if resp.status_code == 200:
            results = resp.json().get("Results", [])
            if results:
                r = results[0]
                return {
                    "engine_displacement": r.get("DisplacementL", ""),
                    "engine_cylinders": r.get("EngineCylinders", ""),
                    "engine_model": r.get("EngineModel", ""),
                    "fuel_type": r.get("FuelTypePrimary", ""),
                    "drive_type": r.get("DriveType", ""),
                    "transmission": r.get("TransmissionStyle", ""),
                    "body_class": r.get("BodyClass", ""),
                    "plant_city": r.get("PlantCity", ""),
                    "plant_country": r.get("PlantCountry", ""),
                    "series": r.get("Series", ""),
                    "trim": r.get("Trim", ""),
                    "gvwr": r.get("GVWR", ""),
                    "electrification": r.get("ElectrificationLevel", ""),
                    "battery_type": r.get("BatteryType", ""),
                    "ev_range": r.get("EVDriveUnit", ""),
                }
    except Exception as e:
        log.warning(f"NHTSA VIN decode failed: {e}")
    return None


# ==============================================================
# AUTO.DEV Ã¢ÂÂ VIN lookup + market comps
# ==============================================================

def lookup_vin_autodev(vin):
    if not AUTODEV_API_KEY: return None
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
    if not AUTODEV_API_KEY: return None
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
            prices = []
            mileage_prices = []
            for r in records:
                p = parse_price(r.get("price"))
                m = parse_mileage(r.get("mileage"))
                if p:
                    prices.append(p)
                    if m: mileage_prices.append({"price": p, "mileage": m})
            if not prices: return None
            prices.sort()
            avg_price = sum(prices) // len(prices)
            median_price = int(statistics.median(prices))
            min_price = prices[0]
            max_price = prices[-1]
            percentile = None; deal_score = None; savings = None
            if listing_price:
                below = len([p for p in prices if p <= listing_price])
                percentile = round(below / len(prices) * 100)
                deal_score = max(1, min(10, round(10 - (percentile / 10))))
                savings = median_price - listing_price
            num_buckets = min(10, max(4, len(prices) // 2))
            bucket_size = max(500, (max_price - min_price) // num_buckets)
            if bucket_size == 0: bucket_size = 1000
            buckets = []
            current = min_price
            while current < max_price + bucket_size:
                count = len([p for p in prices if current <= p < current + bucket_size])
                buckets.append({"min": current, "max": current + bucket_size, "count": count})
                current += bucket_size
                if len(buckets) > 15: break
            return {
                "avg_price": avg_price, "median_price": median_price,
                "min_price": min_price, "max_price": max_price,
                "percentile": percentile, "deal_score": deal_score, "savings": savings,
                "comp_count": len(prices), "total_market": total,
                "price_buckets": buckets, "prices_sample": prices[:30],
                "mileage_prices": mileage_prices[:30]
            }
    except Exception as e:
        log.warning(f"Market comp lookup failed: {e}")
    return None


# ==============================================================
# NHTSA Ã¢ÂÂ recalls + complaints
# ==============================================================

def get_nhtsa_data(year, make, model):
    result = {
        "recall_count": 0, "complaint_count": 0,
        "recalls": [], "complaints_raw": [],
        "top_complaint_areas": [],
        "risk_score": 0, "risk_label": "Low Risk",
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
    except: pass
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
    except: pass
    # Risk score Ã¢ÂÂ realistic calibration
    cc = result["complaint_count"]
    if cc <= 20: complaint_pts = 0
    elif cc <= 50: complaint_pts = 0.5
    elif cc <= 100: complaint_pts = 1.0
    elif cc <= 200: complaint_pts = 1.5
    elif cc <= 500: complaint_pts = 2.5
    else: complaint_pts = 3.5
    rc = result["recall_count"]
    if rc <= 2: recall_pts = 0
    elif rc <= 4: recall_pts = 0.5
    elif rc <= 6: recall_pts = 1.5
    else: recall_pts = 2.5
    severe_keywords = ["death", "fatality", "unintended acceleration", "loss of steering"]
    severe_count = 0
    for c in result.get("complaints_raw", []):
        text = str(c.get("summary", "")).lower()
        if any(kw in text for kw in severe_keywords): severe_count += 1
    severity_pts = min(2, severe_count * 0.5)
    raw = complaint_pts + recall_pts + severity_pts
    result["risk_score"] = round(min(10, max(0, raw)), 1)
    if result["risk_score"] <= 1.5: result["risk_label"] = "Low Risk"
    elif result["risk_score"] <= 3: result["risk_label"] = "Below Average Risk"
    elif result["risk_score"] <= 5: result["risk_label"] = "Average"
    elif result["risk_score"] <= 7: result["risk_label"] = "Above Average Risk"
    else: result["risk_label"] = "High Risk"
    return result


# ==============================================================
# DEALER REPUTATION
# ==============================================================

def get_dealer_reputation(dealer_name, dealer_location=None):
    if not EXA_API_KEY or not dealer_name: return None
    try:
        query = f'"{dealer_name}" reviews rating'
        if dealer_location: query += f" {dealer_location}"
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
# WEB RESEARCH Ã¢ÂÂ Exa search for model-specific intelligence
# ==============================================================

def research_vehicle_web(year, make, model, trim=None):
    """Search the web for known issues, owner reviews, and buying guides for this specific vehicle."""
    if not EXA_API_KEY: return None
    vehicle_str = f"{year} {make} {model}"
    if trim: vehicle_str += f" {trim}"
    try:
        queries = [
            f"{vehicle_str} common problems known issues owner complaints",
            f"{vehicle_str} long term reliability review what owners say",
        ]
        all_text = []
        for q in queries:
            resp = requests.post(EXA_SEARCH_URL, json={
                "query": q, "numResults": 3, "type": "auto",
                "contents": {"text": {"maxCharacters": 1500}}
            }, headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"}, timeout=12)
            if resp.status_code == 200:
                for r in resp.json().get("results", []):
                    txt = r.get("text", "")
                    if txt: all_text.append(txt[:1500])
        if all_text:
            return "\n---\n".join(all_text[:6])
    except Exception as e:
        log.warning(f"Web research failed: {e}")
    return None


# ==============================================================
# AI SYSTEM PROMPT v4 Ã¢ÂÂ IDENTITY-ANCHORED INTELLIGENCE
# ==============================================================
# The key insight: instead of one massive prompt that says "be specific",
# we build a VEHICLE IDENTITY CARD that the model must reference in every answer.
# Then we use a two-pass approach: research context first, then generate.

ANALYSIS_SYSTEM_PROMPT = """You are AskCarBuddy Ã¢ÂÂ a car buying intelligence engine. You have 20 years of dealership experience selling every make and model.

YOUR JOB: The buyer found a car they WANT. Help them buy it SMART. Not scare them. Not talk them out of it. Arm them with knowledge.

Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
ABSOLUTE RULES Ã¢ÂÂ VIOLATIONS = FAILURE
Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

RULE 1: EVERY answer must name the specific car.
  Ã¢ÂÂ BAD: "Check for unusual noises during the test drive"
  Ã¢ÂÂ GOOD: "On the 2017 Prius Three with the 1.8L 2ZR-FXE, listen for a rattling heat shield Ã¢ÂÂ it's the #1 minor complaint on Gen 4 Priuses over 80K miles"

RULE 2: Questions must be things a BUYER couldn't Google.
  Ã¢ÂÂ BAD: "Ask about the vehicle history"
  Ã¢ÂÂ GOOD: "Ask them to pull up the hybrid battery health report on the Techstream Ã¢ÂÂ any Toyota dealer can run this in 10 minutes. You want cycle count under 400 and SOH above 70%"

RULE 3: Known quirks must be DOCUMENTED, REAL issues for THIS generation.
  Ã¢ÂÂ BAD: "Some owners report transmission issues"
  Ã¢ÂÂ GOOD: "The 2017 Prius uses Toyota's eCVT (technically a power-split device, not a traditional CVT). It's virtually bulletproof Ã¢ÂÂ there are almost zero transmission failures reported. The inverter coolant pump is the component to watch, with a handful of failures around 120-150K miles ($400-600 to replace)"

RULE 4: Use REAL numbers. Cost estimates, percentages, mileage intervals.
  Ã¢ÂÂ BAD: "Budget for regular maintenance"
  Ã¢ÂÂ GOOD: "At 104K miles, you're due for the 105K service: transmission fluid change ($150-180), spark plugs ($180-220 for iridium), coolant flush ($120-150). Total: ~$450-550"

RULE 5: Frame recalls as GOOD NEWS (free manufacturer fix).
  Ã¢ÂÂ BAD: "This car has 3 recalls which is concerning"
  Ã¢ÂÂ GOOD: "3 recalls on file Ã¢ÂÂ all have free fixes at any Toyota dealer. The fuel pump relay one is quick (30 min). Confirm all 3 are completed by running the VIN at toyota.com/recall"

RULE 6: Complaint context is MANDATORY.
  Ã¢ÂÂ BAD: "115 complaints filed with NHTSA"
  Ã¢ÂÂ GOOD: "115 NHTSA complaints across ~150,000 units sold = 0.077% complaint rate. That's one of the lowest in the compact hybrid class. For comparison, the 2017 Honda CR-V has 900+ complaints"

RULE 7: Pro tips must be INSIDER knowledge only.
  Ã¢ÂÂ BAD: "Consider getting a pre-purchase inspection"
  Ã¢ÂÂ GOOD: "Toyota's hybrid battery warranty was extended to 10 years/150K miles for 2020+ models, but some dealers will goodwill the repair on 2017s if the battery fails near the 8yr mark Ã¢ÂÂ ask the service manager, not the salesperson"

RULE 8: Test drive checklist items must test THIS car's known characteristics.
  Ã¢ÂÂ BAD: "Test the brakes"
  Ã¢ÂÂ GOOD: "Brake feel on the Prius is weird by design Ã¢ÂÂ the first inch of pedal travel is regenerative braking (no friction). Press harder to feel the mechanical brakes engage. If there's a grinding or pulsation when the mechanical brakes kick in, the rotors need resurfacing (~$250)"

Return VALID JSON. Every string value must reference the specific vehicle by name, year, or component."""


ANALYSIS_JSON_SCHEMA = """{
  "buy_score": {
    "score": <1-10>,
    "label": "<Great Find|Solid Pick|Worth a Look|Proceed with Caution|Think Twice>",
    "one_liner": "<confident verdict naming the car Ã¢ÂÂ e.g., 'This 2017 Prius Three at $13K with 104K miles is a no-brainer for anyone wanting a reliable 50+ MPG daily driver'>"
  },
  "at_a_glance": {
    "best_thing": "<the single best thing about THIS specific car Ã¢ÂÂ name it>",
    "know_before_you_go": "<the ONE most important thing to check Ã¢ÂÂ specific and actionable>"
  },
  "risk_assessment": {
    "score_context": "<explain what the NHTSA numbers MEAN with fleet-size context and class comparison>",
    "key_reassurances": ["<specific safety/reliability positives Ã¢ÂÂ reference the car>"],
    "items_to_verify": ["<framed as due diligence, not red flags Ã¢ÂÂ e.g., 'Confirm recall XX-XXX (fuel pump relay) is completed via toyota.com/recall Ã¢ÂÂ free 30-min fix if not'>"]
  },
  "deal_analysis": {
    "price_vs_market": "<specific: 'At $13,435, this 2017 Prius Three sits $X below the $Y median across Z listings within 150 miles'>",
    "why_this_price": "<what's driving the price Ã¢ÂÂ mileage, trim, color, market supply>",
    "value_verdict": "<direct: is this a good deal or not>",
    "deal_label": "<Steal|Great Deal|Good Value|Fair Price|Slightly High|Overpriced>"
  },
  "what_to_know": {
    "generation_overview": "<2-3 sentences about THIS generation Ã¢ÂÂ number it, name the platform, what changed from previous gen. e.g., 'This is a 4th-gen Prius (XW50, 2016-2022) built on Toyota's TNGA platform...'>",
    "what_owners_love": ["<things REAL owners of this generation praise Ã¢ÂÂ be specific to this model>"],
    "known_quirks": [
      {
        "item": "<specific documented issue for this generation/engine Ã¢ÂÂ name the component>",
        "severity": "<minor_quirk|worth_checking|important>",
        "reality_check": "<how common? what % of owners? what does it cost?>",
        "what_to_do": "<exactly what to check and how Ã¢ÂÂ not 'have it inspected'>"
      }
    ],
    "big_ticket_watch": "<the ONE expensive component at THIS mileage Ã¢ÂÂ with cost and expected remaining life>",
    "maintenance_now": [
      {
        "service": "<specific service due at this mileage per manufacturer schedule>",
        "cost": "<specific cost range for THIS car>",
        "urgency": "<due_now|soon|within_6_months|within_a_year>",
        "why": "<why this matters for THIS car's engine/transmission specifically>"
      }
    ]
  },
  "your_game_plan": {
    "before_you_go": ["<specific prep Ã¢ÂÂ name the tools, websites, VIN portals for THIS make>"],
    "smart_questions": [
      {
        "ask": "<insider question Ã¢ÂÂ what to literally say to the salesperson>",
        "why_this_matters": "<what the answer reveals that you can't find online>",
        "good_answer": "<what a trustworthy dealer would say>",
        "dig_deeper_if": "<what answer should concern you>"
      }
    ],
    "test_drive_checklist": ["<specific to THIS car's drivetrain, known behaviors, and common failure points>"],
    "at_the_desk": {
      "expected_otd_range": "<specific OTD estimate including state tax + fees>",
      "standard_fees": ["<fee: $amount Ã¢ÂÂ expected>"],
      "fees_worth_asking_about": ["<fee that's sometimes inflated, what fair amount is>"],
      "financing_intel": "<specific to this price point and vehicle type>"
    }
  },
  "cost_to_own": {
    "monthly_fuel": "<calculated from THIS car's actual MPG>",
    "annual_insurance_estimate": "<realistic for this vehicle class>",
    "annual_maintenance": "<based on THIS car's schedule at THIS mileage>",
    "first_year_budget": "<itemized year 1 maintenance for THIS car at THIS mileage>",
    "depreciation_outlook": "<how THIS model holds value specifically>",
    "total_monthly_cost": "<all-in monthly estimate>",
    "verdict": "<one sentence Ã¢ÂÂ cheap, average, or expensive to own?>"
  },
  "pro_tips": ["<genuine insider knowledge about THIS specific car that only a veteran would know>"]
}"""


# ==============================================================
# AI ANALYSIS GENERATOR v4 Ã¢ÂÂ Identity-anchored, two-context
# ==============================================================

def build_vehicle_identity(vehicle_info, vin_decode=None):
    """Build a structured identity card that forces the AI to reference this specific car."""
    v = vehicle_info
    lines = []
    lines.append("=" * 50)
    lines.append("VEHICLE IDENTITY CARD Ã¢ÂÂ Reference this in EVERY answer")
    lines.append("=" * 50)

    year = v.get('year', '?')
    make = v.get('make', '?')
    model = v.get('model', '?')
    trim = v.get('trim', '')

    lines.append(f"VEHICLE: {year} {make} {model} {trim}".strip())
    if v.get("vin"): lines.append(f"VIN: {v['vin']}")
    if v.get("price"): lines.append(f"LISTED PRICE: ${v['price']:,}")
    if v.get("mileage"): lines.append(f"MILEAGE: {v['mileage']:,} miles")
    if v.get("color"): lines.append(f"COLOR: {v['color']}")
    if v.get("dealer_name"): lines.append(f"DEALER: {v['dealer_name']}")
    if v.get("dealer_phone"): lines.append(f"PHONE: {v['dealer_phone']}")
    if v.get("zip"): lines.append(f"LOCATION: ZIP {v['zip']}")

    lines.append("")
    lines.append("POWERTRAIN SPECS:")
    if v.get("engine"): lines.append(f"  Engine: {v['engine']}")
    if vin_decode:
        vd = vin_decode
        if vd.get("engine_displacement"): lines.append(f"  Displacement: {vd['engine_displacement']}L")
        if vd.get("engine_cylinders"): lines.append(f"  Cylinders: {vd['engine_cylinders']}")
        if vd.get("engine_model"): lines.append(f"  Engine Code: {vd['engine_model']}")
        if vd.get("fuel_type"): lines.append(f"  Fuel: {vd['fuel_type']}")
        if vd.get("electrification"): lines.append(f"  Electrification: {vd['electrification']}")
        if vd.get("battery_type"): lines.append(f"  Battery: {vd['battery_type']}")
    if v.get("transmission"): lines.append(f"  Transmission: {v['transmission']}")
    if v.get("drivetrain"): lines.append(f"  Drivetrain: {v['drivetrain']}")
    if v.get("fuelType"): lines.append(f"  Fuel Type: {v['fuelType']}")
    if v.get("mpgCity") and v.get("mpgHighway"):
        lines.append(f"  MPG: {v['mpgCity']} city / {v['mpgHighway']} hwy")
    if v.get("bodyType"): lines.append(f"  Body: {v['bodyType']}")

    if vin_decode:
        vd = vin_decode
        if vd.get("plant_country"): lines.append(f"  Built in: {vd.get('plant_city', '')} {vd['plant_country']}")

    lines.append("=" * 50)
    return "\n".join(lines)


def generate_analysis(vehicle_info, market_data, nhtsa_data, dealer_rep, listing_text="", vin_decode=None, web_research=None):
    # Build the identity card
    identity = build_vehicle_identity(vehicle_info, vin_decode)

    v = vehicle_info
    context_parts = [identity]

    # MARKET DATA
    if market_data:
        m = market_data
        context_parts.append(f"\nMARKET DATA ({m['comp_count']} comparable listings within 150 miles):")
        context_parts.append(f"  Median: ${m['median_price']:,}  |  Average: ${m['avg_price']:,}")
        context_parts.append(f"  Range: ${m['min_price']:,} - ${m['max_price']:,}")
        if m.get('percentile') is not None:
            context_parts.append(f"  This car's percentile: {m['percentile']}th (lower = cheaper)")
        if m.get('savings') is not None:
            if m['savings'] > 0:
                context_parts.append(f"  >>> ${m['savings']:,} BELOW median <<<")
            elif m['savings'] < 0:
                context_parts.append(f"  >>> ${abs(m['savings']):,} ABOVE median <<<")
        if m.get('deal_score'):
            context_parts.append(f"  Deal score: {m['deal_score']}/10")
        context_parts.append(f"  Total supply: {m['total_market']} similar vehicles on market")
        if m.get('mileage_prices') and v.get('mileage'):
            similar = [x for x in m['mileage_prices'] if abs(x['mileage'] - v['mileage']) < 20000]
            if similar:
                sp = [x['price'] for x in similar]
                context_parts.append(f"  Similar-mileage comps: avg ${sum(sp)//len(sp):,} ({len(sp)} listings)")

    # NHTSA DATA
    if nhtsa_data:
        n = nhtsa_data
        context_parts.append(f"\nNHTSA SAFETY DATA:")
        context_parts.append(f"  Risk score: {n['risk_score']}/10 ({n['risk_label']})")
        context_parts.append(f"  Recalls: {n['recall_count']} (recalls = FREE manufacturer fixes = GOOD)")
        context_parts.append(f"  Complaints: {n['complaint_count']} total filed")
        if n.get("top_complaint_areas"):
            areas = ", ".join(f"{a} ({c})" for a, c in n["top_complaint_areas"][:8])
            context_parts.append(f"  Breakdown: {areas}")
        for r in n.get("recalls", [])[:5]:
            context_parts.append(f"  RECALL [{r['component']}]: {r['summary'][:200]}")
            if r.get("remedy"): context_parts.append(f"    FIX: {r['remedy'][:150]}")
        # Include actual complaint descriptions for the AI to reference
        for c in n.get("complaints_raw", [])[:8]:
            summary = str(c.get("summary", ""))[:200]
            comp = c.get("components", "")
            if summary:
                context_parts.append(f"  COMPLAINT [{comp}]: {summary}")

    # DEALER REVIEWS
    if dealer_rep and dealer_rep.get("raw_reviews"):
        context_parts.append(f"\nDEALER REVIEWS ({dealer_rep['source_count']} sources):")
        for i, review in enumerate(dealer_rep["raw_reviews"][:3]):
            context_parts.append(f"  Review {i+1}: {review[:400]}")

    # WEB RESEARCH Ã¢ÂÂ model-specific intelligence from the internet
    if web_research:
        context_parts.append(f"\nWEB RESEARCH Ã¢ÂÂ Known issues and owner feedback for this vehicle:")
        context_parts.append(web_research[:4000])

    # RAW LISTING
    if listing_text:
        context_parts.append(f"\nLISTING PAGE CONTENT:")
        context_parts.append(listing_text[:3000])

    context = "\n".join(context_parts)

    user_msg = f"""Generate a complete buyer intelligence brief for this vehicle.

IMPORTANT: 
- Every answer must name the specific car ({v.get('year', '?')} {v.get('make', '?')} {v.get('model', '?')}) or its specific components
- Every question must be something a buyer couldn't find by Googling
- Every test drive item must test THIS car's known characteristics
- Use the web research data to identify REAL documented issues for this generation
- Zero generic advice allowed

{context}

Return the JSON analysis matching this schema:
{ANALYSIS_JSON_SCHEMA}"""

    for attempt, max_tok in enumerate([8192, 16384], 1):
        try:
            log.info(f"Groq attempt {attempt} with max_tokens={max_tok}")
            resp = requests.post(GROQ_URL, json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg}
                ],
                "temperature": 0.15,
                "max_tokens": max_tok,
                "response_format": {"type": "json_object"}
            }, headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            }, timeout=90)

            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                analysis = json.loads(content)
                log.info(f"Analysis generated (attempt {attempt}): {v.get('year')} {v.get('make')} {v.get('model')}")
                return analysis
            elif resp.status_code == 400 and "json_validate_failed" in resp.text:
                log.warning(f"JSON truncated at {max_tok} tokens, retrying with more...")
                continue
            else:
                log.error(f"Groq error: {resp.status_code} - {resp.text[:300]}")
                break
        except json.JSONDecodeError as e:
            log.error(f"JSON parse error: {e}")
            continue
        except Exception as e:
            log.error(f"Analysis generation failed: {e}")
            break
    return None


# ==============================================================
# ORCHESTRATOR Ã¢ÂÂ now with VIN decode + web research
# ==============================================================

def analyze_listing(input_data):
    vehicle = {}
    listing_text = ""

    if input_data.get("url"):
        url = input_data["url"]
        # Step 1: Extract VIN from URL (instant, no network)
        url_vin = extract_vin_from_url(url)
        if url_vin:
            vehicle["vin"] = url_vin
            log.info(f"VIN from URL: {url_vin}")

        # Step 2: Extract year/make/model from URL path
        url_ymm = extract_ymm_from_url(url)
        for k, v in url_ymm.items():
            if v and not vehicle.get(k): vehicle[k] = v

        # Step 3: If we have a VIN, decode via NHTSA (FREE, authoritative)
        if vehicle.get("vin"):
            nhtsa_info = nhtsa_vin_decode(vehicle["vin"])
            for k, v in nhtsa_info.items():
                if v and not vehicle.get(k): vehicle[k] = v

        # Step 4: Scrape for price, mileage, photos, dealer info
        scrape_result = scrape_listing_exa(url)
        if isinstance(scrape_result, tuple):
            listing_text, images = scrape_result
            if images: vehicle["photos"] = images[:5]
        else:
            listing_text = scrape_result
        if listing_text:
            extracted = extract_vehicle_from_text(listing_text)
            for k, val in extracted.items():
                if val and not vehicle.get(k): vehicle[k] = val

        # Step 5: If found VIN in HTML but not from URL, decode that too
        if vehicle.get("vin") and not vehicle.get("make"):
            nhtsa_info2 = nhtsa_vin_decode(vehicle["vin"])
            for k, v in nhtsa_info2.items():
                if v and not vehicle.get(k): vehicle[k] = v

        # Step 6: Also try parse_listing_url as fallback
        url_info = parse_listing_url(url)
        for k, v in url_info.items():
            if v and not vehicle.get(k): vehicle[k] = v

    for field in ["year", "make", "model", "trim", "price", "mileage", "vin", "zip", "color", "dealer_name"]:
        if input_data.get(field): vehicle[field] = input_data[field]

    if not vehicle.get("make") or not vehicle.get("model"):
        return {"error": "Couldn't identify the car. Try a different listing URL or enter details manually."}

    # VIN enrichment via Auto.dev
    if vehicle.get("vin") and AUTODEV_API_KEY:
        vin_data = lookup_vin_autodev(vehicle["vin"])
        if vin_data:
            for k in ["year", "make", "model", "trim", "price", "mileage", "engine",
                       "transmission", "drivetrain", "fuelType", "mpgCity", "mpgHighway", "bodyType"]:
                if vin_data.get(k) and not vehicle.get(k): vehicle[k] = vin_data[k]
            if vin_data.get("dealerName") and not vehicle.get("dealer_name"):
                vehicle["dealer_name"] = vin_data["dealerName"]
            if vin_data.get("dealerPhone"): vehicle["dealer_phone"] = vin_data["dealerPhone"]
            if vin_data.get("photoUrls") and not vehicle.get("photos"):
                vehicle["photos"] = vin_data["photoUrls"][:8]
            if vin_data.get("displayColor") and not vehicle.get("color"):
                vehicle["color"] = vin_data["displayColor"]

    # Normalize types
    if vehicle.get("price"): vehicle["price"] = parse_price(vehicle["price"]) or vehicle["price"]
    if vehicle.get("mileage"): vehicle["mileage"] = parse_mileage(vehicle["mileage"]) or vehicle["mileage"]
    if vehicle.get("year"):
        try: vehicle["year"] = int(vehicle["year"])
        except: pass

    log.info(f"Analyzing: {vehicle.get('year')} {vehicle.get('make')} {vehicle.get('model')} - ${vehicle.get('price', '?')}")

    # === STEP 1: VIN decode via NHTSA for exact specs ===
    vin_decode = None
    if vehicle.get("vin"):
        vin_decode = decode_vin_nhtsa(vehicle["vin"])
        if vin_decode:
            # Enrich vehicle with decoded data
            if vin_decode.get("trim") and not vehicle.get("trim"):
                vehicle["trim"] = vin_decode["trim"]
            if vin_decode.get("drive_type") and not vehicle.get("drivetrain"):
                vehicle["drivetrain"] = vin_decode["drive_type"]
            if vin_decode.get("transmission") and not vehicle.get("transmission"):
                vehicle["transmission"] = vin_decode["transmission"]

    # === STEP 2: Market comps ===
    market_data = None
    if vehicle.get("make") and vehicle.get("model"):
        market_data = get_market_comps(
            vehicle.get("year"), vehicle["make"], vehicle["model"],
            vehicle.get("trim"), vehicle.get("zip") or DEFAULT_ZIP, vehicle.get("price")
        )

    # === STEP 3: NHTSA recalls + complaints ===
    nhtsa_data = None
    if vehicle.get("year") and vehicle.get("make") and vehicle.get("model"):
        nhtsa_data = get_nhtsa_data(vehicle["year"], vehicle["make"], vehicle["model"])

    # === STEP 4: Dealer reputation ===
    dealer_rep = None
    if vehicle.get("dealer_name"):
        dealer_rep = get_dealer_reputation(vehicle["dealer_name"], vehicle.get("zip"))

    # === STEP 5: Web research for model-specific intelligence ===
    web_research = None
    if vehicle.get("year") and vehicle.get("make") and vehicle.get("model"):
        web_research = research_vehicle_web(
            vehicle["year"], vehicle["make"], vehicle["model"], vehicle.get("trim")
        )

    # === STEP 6: Generate AI analysis ===
    analysis = generate_analysis(vehicle, market_data, nhtsa_data, dealer_rep, listing_text, vin_decode, web_research)

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
        "version": "5.0.0"
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
        "status": "ok", "service": "AskCarBuddy", "version": "5.0.0",
        "apis": {"groq": bool(GROQ_API_KEY), "autodev": bool(AUTODEV_API_KEY), "exa": bool(EXA_API_KEY)}
    })

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"AskCarBuddy v5.0 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
