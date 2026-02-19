#!/usr/bin/env python3
"""
AskCarBuddy v7.0 - AI Car Buying Intelligence (Smart Engine)
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

# Initialize trace DB on startup
try:
    init_trace_db()
except Exception as e:
    log.warning(f'Trace DB init deferred: {e}')


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
# SELF-IMPROVING AGENT — PHASE 1: TRACE STORE + LEARNING LOOP
# ==============================================================

import sqlite3
import uuid
import threading

DB_PATH = os.getenv("TRACE_DB", "askcarbuddy_traces.db")
_db_lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_trace_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS traces (
            id TEXT PRIMARY KEY,
            created_at TEXT DEFAULT (datetime('now')),
            url TEXT,
            vehicle_year TEXT,
            vehicle_make TEXT,
            vehicle_model TEXT,
            vehicle_trim TEXT,
            vehicle_price REAL,
            vehicle_mileage REAL,
            prompt_version TEXT DEFAULT 'v1',
            scrape_time_ms REAL,
            market_time_ms REAL,
            nhtsa_time_ms REAL,
            ai_time_ms REAL,
            total_time_ms REAL,
            groq_tokens_used INTEGER,
            overall_score REAL,
            deal_position TEXT,
            mechanical_risk TEXT,
            confidence_level REAL,
            ai_output_json TEXT,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS rewards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            signal_type TEXT NOT NULL,
            signal_value REAL NOT NULL,
            metadata TEXT,
            FOREIGN KEY (trace_id) REFERENCES traces(id)
        );

        CREATE TABLE IF NOT EXISTS prompt_versions (
            version TEXT PRIMARY KEY,
            created_at TEXT DEFAULT (datetime('now')),
            system_prompt TEXT NOT NULL,
            json_schema TEXT NOT NULL,
            is_active INTEGER DEFAULT 0,
            total_reports INTEGER DEFAULT 0,
            avg_score REAL DEFAULT 0,
            avg_thumbs_up_rate REAL DEFAULT 0,
            avg_time_on_page REAL DEFAULT 0,
            conversion_rate REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS page_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            event_type TEXT NOT NULL,
            section_name TEXT,
            duration_ms REAL,
            scroll_depth REAL,
            metadata TEXT,
            FOREIGN KEY (trace_id) REFERENCES traces(id)
        );

        CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at);
        CREATE INDEX IF NOT EXISTS idx_traces_prompt ON traces(prompt_version);
        CREATE INDEX IF NOT EXISTS idx_rewards_trace ON rewards(trace_id);
        CREATE INDEX IF NOT EXISTS idx_events_trace ON page_events(trace_id);
    """)
    conn.commit()
    conn.close()
    log.info("Trace DB initialized")

def save_trace(trace_data):
    trace_id = str(uuid.uuid4())[:12]
    with _db_lock:
        conn = get_db()
        conn.execute("""
            INSERT INTO traces (id, url, vehicle_year, vehicle_make, vehicle_model, vehicle_trim,
                vehicle_price, vehicle_mileage, prompt_version, scrape_time_ms, market_time_ms,
                nhtsa_time_ms, ai_time_ms, total_time_ms, groq_tokens_used, overall_score,
                deal_position, mechanical_risk, confidence_level, ai_output_json, error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trace_id,
            trace_data.get("url", ""),
            trace_data.get("year", ""),
            trace_data.get("make", ""),
            trace_data.get("model", ""),
            trace_data.get("trim", ""),
            trace_data.get("price"),
            trace_data.get("mileage"),
            trace_data.get("prompt_version", "v1"),
            trace_data.get("scrape_time_ms"),
            trace_data.get("market_time_ms"),
            trace_data.get("nhtsa_time_ms"),
            trace_data.get("ai_time_ms"),
            trace_data.get("total_time_ms"),
            trace_data.get("groq_tokens"),
            trace_data.get("overall_score"),
            trace_data.get("deal_position"),
            trace_data.get("mechanical_risk"),
            trace_data.get("confidence_level"),
            trace_data.get("ai_output_json"),
            trace_data.get("error")
        ))
        conn.commit()
        conn.close()
    log.info(f"Trace saved: {trace_id}")
    return trace_id

def save_reward(trace_id, signal_type, signal_value, metadata=None):
    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO rewards (trace_id, signal_type, signal_value, metadata) VALUES (?,?,?,?)",
            (trace_id, signal_type, signal_value, json.dumps(metadata) if metadata else None)
        )
        conn.commit()
        conn.close()
    log.info(f"Reward saved: {trace_id} | {signal_type}={signal_value}")

def save_page_event(trace_id, event_type, section_name=None, duration_ms=None, scroll_depth=None, metadata=None):
    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO page_events (trace_id, event_type, section_name, duration_ms, scroll_depth, metadata) VALUES (?,?,?,?,?,?)",
            (trace_id, event_type, section_name, duration_ms, scroll_depth, json.dumps(metadata) if metadata else None)
        )
        conn.commit()
        conn.close()

def get_learning_stats():
    conn = get_db()
    stats = {}
    stats["total_reports"] = conn.execute("SELECT COUNT(*) FROM traces WHERE error IS NULL").fetchone()[0]
    stats["total_errors"] = conn.execute("SELECT COUNT(*) FROM traces WHERE error IS NOT NULL").fetchone()[0]
    stats["total_rewards"] = conn.execute("SELECT COUNT(*) FROM rewards").fetchone()[0]
    stats["avg_overall_score"] = conn.execute("SELECT AVG(overall_score) FROM traces WHERE overall_score IS NOT NULL").fetchone()[0]
    stats["avg_total_time_ms"] = conn.execute("SELECT AVG(total_time_ms) FROM traces WHERE total_time_ms IS NOT NULL").fetchone()[0]

    thumbs = conn.execute("""
        SELECT signal_value, COUNT(*) as cnt FROM rewards 
        WHERE signal_type='thumbs' GROUP BY signal_value
    """).fetchall()
    stats["thumbs_up"] = sum(r[1] for r in thumbs if r[0] > 0)
    stats["thumbs_down"] = sum(r[1] for r in thumbs if r[0] < 0)

    by_prompt = conn.execute("""
        SELECT prompt_version, COUNT(*) as cnt, AVG(overall_score) as avg_score
        FROM traces WHERE error IS NULL GROUP BY prompt_version
    """).fetchall()
    stats["by_prompt_version"] = [{"version": r[0], "count": r[1], "avg_score": round(r[2] or 0, 2)} for r in by_prompt]

    popular = conn.execute("""
        SELECT vehicle_make, vehicle_model, COUNT(*) as cnt
        FROM traces WHERE error IS NULL
        GROUP BY vehicle_make, vehicle_model ORDER BY cnt DESC LIMIT 10
    """).fetchall()
    stats["popular_vehicles"] = [{"make": r[0], "model": r[1], "count": r[2]} for r in popular]

    recent = conn.execute("""
        SELECT section_name, AVG(duration_ms) as avg_dur, COUNT(*) as cnt
        FROM page_events WHERE event_type='section_view' AND duration_ms > 0
        GROUP BY section_name ORDER BY avg_dur DESC
    """).fetchall()
    stats["section_engagement"] = [{"section": r[0], "avg_time_ms": round(r[1] or 0), "views": r[2]} for r in recent]

    conn.close()
    return stats

BRAIN_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AskCarBuddy Brain</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0f;color:#e0e0e0;font-family:'Inter',system-ui,sans-serif;padding:24px}
h1{font-size:1.8rem;margin-bottom:24px;background:linear-gradient(135deg,#00ff88,#00d4ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-bottom:32px}
.stat{background:#12121a;border:1px solid #1e1e2e;border-radius:16px;padding:24px;text-align:center}
.stat-val{font-size:2.2rem;font-weight:800;margin:8px 0}
.stat-label{font-size:0.78rem;text-transform:uppercase;letter-spacing:2px;color:#888}
.green{color:#00ff88}.blue{color:#00d4ff}.amber{color:#ffaa00}.red{color:#ff4466}
.card{background:#12121a;border:1px solid #1e1e2e;border-radius:16px;padding:24px;margin-bottom:20px}
.card h2{font-size:1.1rem;margin-bottom:16px;color:#fff}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px;border-bottom:1px solid #1e1e2e;color:#888;font-size:0.75rem;text-transform:uppercase;letter-spacing:1px}
td{padding:10px;border-bottom:1px solid #0f0f18;font-size:0.9rem}
.bar-track{height:8px;background:#1e1e2e;border-radius:4px;overflow:hidden;margin-top:4px}
.bar-fill{height:100%;border-radius:4px;transition:width 0.8s ease}
.refresh-btn{background:linear-gradient(135deg,#00ff88,#00d4ff);color:#000;border:none;padding:10px 24px;border-radius:12px;font-weight:700;cursor:pointer;font-size:0.85rem;margin-bottom:24px}
.empty{color:#555;font-style:italic;padding:20px;text-align:center}
</style>
</head>
<body>
<h1>AskCarBuddy Brain</h1>
<button class="refresh-btn" onclick="load()">Refresh</button>
<div class="grid" id="stats"></div>
<div class="card"><h2>Prompt Version Performance</h2><div id="prompts"></div></div>
<div class="card"><h2>Section Engagement</h2><div id="sections"></div></div>
<div class="card"><h2>Popular Vehicles</h2><div id="vehicles"></div></div>
<script>
function load(){
fetch("/api/learning").then(function(r){return r.json()}).then(function(d){
var s=document.getElementById("stats");
var tu=d.thumbs_up||0,td=d.thumbs_down||0,tpct=tu+td>0?Math.round(tu/(tu+td)*100):0;
s.innerHTML='<div class="stat"><div class="stat-label">Total Reports</div><div class="stat-val blue">'+(d.total_reports||0)+'</div></div>'
+'<div class="stat"><div class="stat-label">Avg Score</div><div class="stat-val green">'+(d.avg_overall_score?d.avg_overall_score.toFixed(1):"--")+'</div></div>'
+'<div class="stat"><div class="stat-label">Avg Time</div><div class="stat-val amber">'+(d.avg_total_time_ms?Math.round(d.avg_total_time_ms/1000)+"s":"--")+'</div></div>'
+'<div class="stat"><div class="stat-label">Thumbs Up</div><div class="stat-val green">'+tu+'</div></div>'
+'<div class="stat"><div class="stat-label">Thumbs Down</div><div class="stat-val red">'+td+'</div></div>'
+'<div class="stat"><div class="stat-label">Approval Rate</div><div class="stat-val '+(tpct>=70?"green":tpct>=50?"amber":"red")+'">'+tpct+'%</div></div>'
+'<div class="stat"><div class="stat-label">Errors</div><div class="stat-val red">'+(d.total_errors||0)+'</div></div>'
+'<div class="stat"><div class="stat-label">Reward Signals</div><div class="stat-val blue">'+(d.total_rewards||0)+'</div></div>';
var pv=d.by_prompt_version||[];
var ph=document.getElementById("prompts");
if(!pv.length){ph.innerHTML='<div class="empty">No data yet. Analyze some listings first.</div>';return}
var pt='<table><tr><th>Version</th><th>Reports</th><th>Avg Score</th></tr>';
pv.forEach(function(p){pt+='<tr><td>'+p.version+'</td><td>'+p.count+'</td><td class="'+(p.avg_score>=7?"green":p.avg_score>=5?"amber":"red")+'">'+p.avg_score+'</td></tr>'});
pt+='</table>';ph.innerHTML=pt;
var se=d.section_engagement||[];
var sh=document.getElementById("sections");
if(!se.length){sh.innerHTML='<div class="empty">No engagement data yet.</div>'}else{
var mx=Math.max.apply(null,se.map(function(x){return x.avg_time_ms}));
var st='<table><tr><th>Section</th><th>Avg Time</th><th>Views</th><th></th></tr>';
se.forEach(function(x){var pct=Math.round(x.avg_time_ms/mx*100);
st+='<tr><td>'+x.section+'</td><td>'+Math.round(x.avg_time_ms/1000)+'s</td><td>'+x.views+'</td><td style="width:40%"><div class="bar-track"><div class="bar-fill" style="width:'+pct+'%;background:linear-gradient(90deg,#00ff88,#00d4ff)"></div></div></td></tr>'});
st+='</table>';sh.innerHTML=st}
var veh=d.popular_vehicles||[];
var vh=document.getElementById("vehicles");
if(!veh.length){vh.innerHTML='<div class="empty">No vehicles analyzed yet.</div>'}else{
var vt='<table><tr><th>Make</th><th>Model</th><th>Reports</th></tr>';
veh.forEach(function(v){vt+='<tr><td>'+v.make+'</td><td>'+v.model+'</td><td>'+v.count+'</td></tr>'});
vt+='</table>';vh.innerHTML=vt}
}).catch(function(e){console.error(e)})}
load();
</script>
</body>
</html>"""



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
    """Extract VIN from URL path or query params with validation."""
    # VINs are 17 chars but must start with a valid WMI (World Manufacturer Identifier)
    # Position 1: country (1-5=NA, J=Japan, K=Korea, S-W=Europe, etc.)
    # Position 9: check digit (0-9 or X)
    # Position 10: model year (A-Y excluding I,O,Q,U,Z or 1-9)
    vin_match = re.search(r'[A-HJ-NPR-Z0-9]{17}', url, re.IGNORECASE)
    if vin_match:
        candidate = vin_match.group(0).upper()
        if re.match(r'^[A-HJ-NPR-Z0-9]{17}$', candidate):
            # Basic VIN validation: position 10 must be valid model year code
            year_char = candidate[9]
            valid_year_chars = set('ABCDEFGHJKLMNPRSTVWXY123456789')
            if year_char not in valid_year_chars:
                return None
            # Position 1 must be a valid country code (not a hex-only sequence)
            # Reject if it looks like a hex hash (all chars are 0-9, A-F)
            if all(c in '0123456789ABCDEF' for c in candidate):
                return None  # Likely a hex hash, not a VIN
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
    """Decode VIN via NHTSA â FREE, reliable, gives year/make/model/trim/specs."""
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
    """Extract vehicle info from HTML/text â price, mileage, VIN, and title-based YMM."""
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
        ymm = re.search(r'(20\d{2}|19\d{2})\s+([A-Za-z]+)\s+([A-Za-z0-9][A-Za-z0-9\- ]+?)(?:\s+[-|Â·â¢]|\s+for\s|\s+in\s|$)', title_text)
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
# NHTSA VIN DECODE ÃÂ¢ÃÂÃÂ get exact specs
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
# AUTO.DEV ÃÂ¢ÃÂÃÂ VIN lookup + market comps
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
            params["radius"] = 50
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
# NHTSA ÃÂ¢ÃÂÃÂ recalls + complaints
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
    # Risk score ÃÂ¢ÃÂÃÂ realistic calibration
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
# WEB RESEARCH ÃÂ¢ÃÂÃÂ Exa search for model-specific intelligence
# ==============================================================

def research_model_year(year, make, model, trim=None):
    """Targeted research: What's special about this model year / generation?"""
    if not EXA_API_KEY: return None
    vehicle_str = f"{year} {make} {model}"
    if trim: vehicle_str += f" {trim}"
    queries = [
        f"{vehicle_str} generation changes what's new specs review",
        f"{year} {make} {model} vs previous year changes improvements",
    ]
    return _exa_multi_search(queries, max_results=3, max_chars=1500)


def research_owner_feedback(year, make, model, trim=None):
    """Targeted research: Real owner experiences from forums and Reddit."""
    if not EXA_API_KEY: return None
    vehicle_str = f"{year} {make} {model}"
    queries = [
        f"site:reddit.com {vehicle_str} owner review experience",
        f"{vehicle_str} forum owner long term review what I wish I knew",
        f"{vehicle_str} real owner complaints pros cons daily driving",
    ]
    results = _exa_multi_search(queries, max_results=3, max_chars=2000)
    if results:
        # Tag each result with its source URL for citation
        return results
    return None


def research_dealer_questions(year, make, model, trim=None):
    """Targeted research: Known issues to ask about for THIS car."""
    if not EXA_API_KEY: return None
    vehicle_str = f"{year} {make} {model}"
    queries = [
        f"{vehicle_str} buying guide what to check inspection tips",
        f"{vehicle_str} common problems to look for before buying",
    ]
    return _exa_multi_search(queries, max_results=3, max_chars=1500)


def _exa_multi_search(queries, max_results=3, max_chars=1500):
    """Execute multiple Exa searches and combine results with source URLs."""
    all_results = []
    for q in queries:
        try:
            resp = requests.post(EXA_SEARCH_URL, json={
                "query": q, "numResults": max_results, "type": "auto",
                "contents": {"text": {"maxCharacters": max_chars}}
            }, headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"}, timeout=12)
            if resp.status_code == 200:
                for r in resp.json().get("results", []):
                    txt = r.get("text", "")
                    url = r.get("url", "")
                    title = r.get("title", "")
                    if txt:
                        source_tag = f"[Source: {title} - {url}]" if url else ""
                        all_results.append(f"{source_tag}\n{txt[:max_chars]}")
        except Exception as e:
            log.warning(f"Exa search failed for '{q[:50]}': {e}")
    if all_results:
        return "\n---\n".join(all_results[:8])
    return None


# ==============================================================
# SECTION GENERATORS (v9.1 Pipeline) 
# Each section gets a focused mini-prompt with ONLY its relevant data
# ==============================================================

SECTION_PROMPTS = {
    "model_year_summary": """You are a car expert writing one section of a buyer report.

SECTION: "Know Your Car" — Model Year Summary
VEHICLE: {vehicle_str}

TASK: Write a concise summary of what makes this model year special. What generation is it? 
What changed vs previous year? Key highlights (engine, tech, safety, design). Include a fun fact.

{data_context}

OUTPUT FORMAT (JSON):
{{
  "headline": "<one punchy line about this model year>",
  "generation": "<which generation — e.g., '4th Generation (2016-2023)'>",
  "what_changed_this_year": "<specific changes for this model year>",
  "highlights": ["<highlight 1>", "<highlight 2>", "<highlight 3>"],
  "engine_and_drivetrain": "<1-2 sentences about powertrain>",
  "fun_fact": "<one interesting thing most people don't know>"
}}

RULES:
- ONLY use information from the research data above
- If research data is thin, focus on what you DO know from the data
- Name the specific car in every response
- No generic filler""",

    "vehicle_history": """You are a car expert writing one section of a buyer report.

SECTION: "The History" — Vehicle Safety & Recall Data  
VEHICLE: {vehicle_str}

TASK: Summarize the NHTSA recall and complaint data for this model year. Frame recalls as 
FREE manufacturer fixes (positive framing). Be factual and helpful, not scary.

{data_context}

OUTPUT FORMAT (JSON):
{{
  "headline": "<one line — e.g., 'Clean model year with 2 recalls — both are free dealer fixes'>",
  "recalls_for_model_year": <integer>,
  "recall_details": ["<brief description of each recall + that it's a free fix>"],
  "complaints_for_model_year": <integer>,
  "common_complaint_areas": "<factual summary of top categories if any>",
  "carfax_tip": "<specific advice about what to look for on Carfax for THIS car>",
  "nhtsa_source": "NHTSA data for {year} {make} {model} model year — check this specific VIN at nhtsa.gov/recalls"
}}

RULES:
- If 0 recalls, say that clearly as a positive
- If 0 complaints, say that clearly as a positive
- Frame recalls as free fixes, not scary defects
- ONLY cite data provided — do NOT invent recalls or complaints
- NHTSA data is for the MODEL YEAR, not this specific VIN — say that""",

    "price_analysis": """You are a car market analyst writing one section of a buyer report.

SECTION: "The Price" — Market Comparison
VEHICLE: {vehicle_str}

TASK: Analyze this car's price vs local market comps within 50 miles. Is it a good deal? 
Use the actual numbers provided. Give a verdict and explain WHY.

{data_context}

OUTPUT FORMAT (JSON):
{{
  "verdict": "<Great Deal|Good Deal|Fair Price|Slightly Above Market>",
  "vs_market": "<exact comparison — e.g., '$1,290 above the $12,145 median of 6 local comps'>",
  "comp_count": "<number of comparable listings within 50 miles>",
  "price_range": "<local price range — e.g., '$9,295 - $14,995'>",
  "fair_range": "<what you'd expect to pay — e.g., '$11,500 - $13,500'>",
  "context": "<2-3 sentences explaining WHY — trim, mileage, condition vs comps. Cite numbers.>",
  "bottom_line": "<one decisive sentence>"
}}

RULES:
- ONLY use the market data numbers provided
- If no comp data, say "No comparable listings found within 50 miles" 
- Cite exact numbers from the data — never approximate when you have real figures
- Be helpful — if it's above market, explain what might justify it""",

    "owner_feedback": """You are a car expert writing one section of a buyer report.

SECTION: "Owner Talk" — What Real Owners Say
VEHICLE: {vehicle_str}

TASK: Summarize what real owners say about this car based ONLY on the web research data below.

{data_context}

OUTPUT FORMAT (JSON):
{{
  "headline": "<one line from REAL research — cite source if possible>",
  "what_owners_love": ["<from research data>", "<from research data>"],
  "what_owners_wish_they_knew": ["<from research data — cite source>"],
  "common_experiences": "<2-3 sentences from real owner perspectives>",
  "reliability_reputation": "<one sentence on reliability reputation>"
}}

CRITICAL RULES:
- ONLY use the web research data provided below
- Cite sources: "Owners on PriusChat report..." or "Reddit users mention..."
- If the research data is empty or doesn't contain owner experiences, return:
  {{
    "headline": "Limited owner data found — check Reddit and forums for real experiences",
    "what_owners_love": ["Research data was limited for this specific model — check r/{make_lower} and model-specific forums"],
    "what_owners_wish_they_knew": ["No specific owner insights found in research"],
    "common_experiences": "Limited owner-specific feedback available in our research. For real owner experiences, check Reddit r/{make_lower}, model-specific forums, and long-term review videos on YouTube.",
    "reliability_reputation": "Check Consumer Reports and J.D. Power for official reliability ratings for the {year} {make} {model}."
  }}
- BANNED: "Some owners wish they knew about regular maintenance" — that's generic filler
- BANNED: "It's important to stay up to date on recalls" — obvious and unhelpful  
- Every bullet MUST come from the research data or honestly say "not found"
- NO FABRICATED OWNER SENTIMENT""",

    "dealer_questions": """You are a car buying expert writing one section of a buyer report.

SECTION: "Go Prepared" — Smart Questions for the Dealer
VEHICLE: {vehicle_str}

TASK: Generate 5-7 smart, specific questions a buyer should ask about THIS car. 
Use the vehicle data, NHTSA data, and research to craft questions that show homework.

{data_context}

OUTPUT FORMAT (JSON):
{{
  "questions": [
    {{
      "ask": "<the exact question — specific to THIS car>",
      "why_it_matters": "<what the answer tells you>",
      "good_answer": "<what you want to hear>"
    }}
  ],
  "bonus_tip": "<one insider tip about buying THIS type of car>"
}}

RULES:
- Questions MUST reference this specific year/make/model
- If NHTSA recalls exist, include a question about whether they've been addressed
- If mileage is high, include a question about relevant service milestones
- BANNED: "Can I see the Carfax?" — too generic
- BANNED: "Has this car been in any accidents?" — too generic
- Each question should show the buyer did their homework"""
}


# ==============================================================
# AI SYSTEM PROMPT v4 ÃÂ¢ÃÂÃÂ IDENTITY-ANCHORED INTELLIGENCE
# ==============================================================
# The key insight: instead of one massive prompt that says "be specific",
# we build a VEHICLE IDENTITY CARD that the model must reference in every answer.
# Then we use a two-pass approach: research context first, then generate.


ANALYSIS_SYSTEM_PROMPT = """You are AskCarBuddy — an AI car buying assistant built by someone with 20 years of dealership experience.

YOUR JOB: The buyer found a car they WANT. Help them walk in confident and informed. You are their knowledgeable friend who did the research for them.

TONE: Warm, positive, informative. Think "car-savvy friend texting you what they found" — not a legal document or consumer report. You LIKE cars. You want them to enjoy this purchase.

====================================================================
ABSOLUTE RULE #0 — NO FAKE DATA — OVERRIDES EVERYTHING
====================================================================

DO NOT FABRICATE DATA. If the provided context does not contain a specific number, stat, date, recall ID, complaint count — DO NOT INVENT IT.

- Data from context below: cite confidently
- General automotive knowledge: label as "generally" or "typically"
- Specific stats you're making up: BANNED. NEVER DO THIS.

====================================================================
RULE #1 — THIS CAR ONLY
====================================================================

EVERY sentence must be about THIS specific car — the {year} {make} {model}. 
If you catch yourself writing generic advice that applies to all cars, DELETE IT.

❌ BANNED phrases: "It's important to...", "In general...", "Make sure to always...", "Vehicles like this..."
✅ REQUIRED: Name this car, its generation, its engine, or its specific components in every paragraph.

====================================================================
THE 5 SECTIONS — WHAT EACH ONE DOES
====================================================================

SECTION 1: "Know Your Car" (model_year_summary)
Help the buyer understand what they're looking at. What generation is this? What changed this year? What's the engine/drivetrain story? What are the highlights that make this model year special? Think of it as the "Wikipedia summary meets enthusiast review" — condensed into something useful. If you have web research data, USE IT to provide real generation-specific info.

SECTION 2: "The History" (vehicle_history)
Use any available data: NHTSA recalls for this model year, complaint data, known TSBs. Frame recalls as "these exist for the model year — check if this VIN is affected at nhtsa.gov/recalls." Mention Carfax as something the buyer should ask for. This section should make the buyer feel informed, not scared.

SECTION 3: "The Price" (price_analysis)
Use the market comparison data provided. How many comparable cars exist within 50 miles? Where does THIS car sit vs the median? Is it a good deal, fair deal, or slightly high? CITE THE ACTUAL NUMBERS. If this car is above median, explain WHY (lower miles, better trim, etc.). If below, say what a win that is. Give them a fair price range based on actual local comps.

SECTION 4: "Owner Talk" (owner_feedback)
What do real owners of this generation say? ONLY use information from the web research data provided. If the web research contains actual owner experiences, forum posts, Reddit threads, or review quotes — use those. Be specific: "Owners on PriusChat report..." or "Reddit users mention..."

CRITICAL: Do NOT fabricate generic owner wisdom. Statements like "some owners wish they knew about regular maintenance" or "others wish they knew about recall notices" are BANNED — that's filler, not research. Every bullet point must come from an actual source in the web research data. If the web research doesn't contain owner feedback, write: "Limited owner-specific feedback found in research — check Reddit r/[make] and model-specific forums for real owner experiences."

SECTION 5: "Go Prepared" (dealer_questions)
Give them 5-7 smart, specific questions to ask the dealer about THIS car. Not generic "can I see the Carfax" stuff. Questions that show they did their homework and will get them useful information. For each question, explain what the answer tells them.

====================================================================
QUALITY RULES
====================================================================

1. If you can Google it in 5 seconds, don't include it
2. Every question must be specific to this year/make/model
3. No scare tactics — this is about helping them buy smart, not scaring them away
4. Cite actual numbers from the data provided — don't round or approximate when you have exact figures
5. Keep it concise — buyers want quick intel, not essays
6. Owner Feedback MUST come from web research data — NEVER generate generic owner wisdom from your training data. If research doesn't include owner experiences, say so honestly. "Some owners wish they knew about regular maintenance" is BANNED — that's filler, not intel.
"""



# ==============================================================
# AI ANALYSIS SCHEMA
# ==============================================================


ANALYSIS_JSON_SCHEMA = """{
  "overall_score": {
    "score": <0.0-10.0 with one decimal — buying confidence score>,
    "label": "<Strong Buy|Buy|Lean Buy|Neutral|Lean Pass>",
    "one_liner": "<one decisive sentence naming the car — e.g., 'Clean 2017 Prius with solid service history at a fair local price — smart buy for a commuter'>"
  },
  "model_year_summary": {
    "headline": "<one punchy line — e.g., 'The 4th-gen Prius brought a complete redesign with 10% better fuel economy'>",
    "generation": "<which generation this is — e.g., '4th Generation (2016-2023)'>",
    "what_changed_this_year": "<what Toyota/Honda/etc changed for this model year vs previous — be specific>",
    "highlights": [
      "<specific highlight of this model year — engine, tech, safety, design>",
      "<another highlight>",
      "<another highlight>"
    ],
    "engine_and_drivetrain": "<1-2 sentences about the powertrain — what it is, how it performs, reliability reputation>",
    "fun_fact": "<one interesting thing about this generation that most people don't know>"
  },
  "vehicle_history": {
    "headline": "<one line summary — e.g., 'Clean model year with 2 recalls on record — both are free dealer fixes'>",
    "recalls_for_model_year": <integer count from NHTSA data>,
    "recall_details": ["<if recalls exist: brief description of each — what it is + that it's a free fix>"],
    "complaints_for_model_year": <integer count from NHTSA data>,
    "common_complaint_areas": "<if complaints exist: factual summary of top categories>",
    "carfax_tip": "<specific advice about what to look for on the Carfax for THIS car — e.g., 'For a 2017 Prius at 150K, you want to see consistent hybrid system service intervals. Ask for the Carfax and look for battery health checks after 100K.'>",
    "nhtsa_source": "<Always: 'NHTSA data for [year] [make] [model] model year — check this specific VIN at nhtsa.gov/recalls'>"
  },
  "price_analysis": {
    "verdict": "<Great Deal|Good Deal|Fair Price|Slightly Above Market>",
    "vs_market": "<exact comparison — e.g., '$1,290 above the $12,145 median of 6 local comps'>",
    "comp_count": "<number of comparable listings within 50 miles>",
    "price_range": "<local price range — e.g., '$9,295 - $14,995'>",
    "fair_range": "<what you'd expect to pay — e.g., '$11,500 - $13,500'>",
    "context": "<2-3 sentences explaining WHY this car is priced where it is — trim, mileage, condition vs local comps. Use actual numbers.>",
    "bottom_line": "<one sentence final take — e.g., 'Slightly above median but justified by lower mileage and nav package. Fair price for what you're getting.'>"
  },
  "owner_feedback": {
    "headline": "<one line from REAL research — e.g., 'Reddit owners love the 50+ MPG but warn about road noise on highways'>",
    "what_owners_love": [
      "<specific thing owners rave about — from forums/reviews/web research>",
      "<another thing>",
      "<another thing>"
    ],
    "what_owners_wish_they_knew": [
      "<MUST be from actual web research — e.g., 'PriusChat users say the 12V battery dies without warning around year 5' — NO generic advice like 'regular maintenance is important'>",
      "<another specific thing from research — or omit this array if no real owner insights found>"
    ],
    "common_experiences": "<2-3 sentences about what daily ownership is actually like — from real owner perspectives>",
    "reliability_reputation": "<one sentence on how this generation is regarded for reliability>"
  },
  "dealer_questions": {
    "questions": [
      {
        "ask": "<the exact question to ask — specific to THIS car>",
        "why_it_matters": "<what the answer tells you about the car>",
        "good_answer": "<what you want to hear>"
      }
    ],
    "bonus_tip": "<one insider tip about the buying process for THIS car — e.g., 'Toyota CPO warranty on a Prius covers the hybrid battery for an extra 12 months — ask if this qualifies'>"
  }
}"""



# ==============================================================
# AI ANALYSIS GENERATOR v4 ÃÂ¢ÃÂÃÂ Identity-anchored, two-context
# ==============================================================

def build_vehicle_identity(vehicle_info, vin_decode=None):
    """Build a structured identity card that forces the AI to reference this specific car."""
    v = vehicle_info
    lines = []
    lines.append("=" * 50)
    lines.append("VEHICLE IDENTITY CARD ÃÂ¢ÃÂÃÂ Reference this in EVERY answer")
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


SECTION_PROMPTS = {
    "model_year_summary": """You are a car expert writing one section of a buyer report.

SECTION: "Know Your Car" — Model Year Summary
VEHICLE: {vehicle_str}

TASK: Write a concise summary of what makes this model year special. What generation is it? 
What changed vs previous year? Key highlights (engine, tech, safety, design). Include a fun fact.

{data_context}

OUTPUT FORMAT (JSON):
{{
  "headline": "<one punchy line about this model year>",
  "generation": "<which generation — e.g., '4th Generation (2016-2023)'>",
  "what_changed_this_year": "<specific changes for this model year>",
  "highlights": ["<highlight 1>", "<highlight 2>", "<highlight 3>"],
  "engine_and_drivetrain": "<1-2 sentences about powertrain>",
  "fun_fact": "<one interesting thing most people don't know>"
}}

RULES:
- ONLY use information from the research data above
- If research data is thin, focus on what you DO know from the data
- Name the specific car in every response
- No generic filler""",

    "vehicle_history": """You are a car expert writing one section of a buyer report.

SECTION: "The History" — Vehicle Safety & Recall Data  
VEHICLE: {vehicle_str}

TASK: Summarize the NHTSA recall and complaint data for this model year. Frame recalls as 
FREE manufacturer fixes (positive framing). Be factual and helpful, not scary.

{data_context}

OUTPUT FORMAT (JSON):
{{
  "headline": "<one line — e.g., 'Clean model year with 2 recalls — both are free dealer fixes'>",
  "recalls_for_model_year": <integer>,
  "recall_details": ["<brief description of each recall + that it's a free fix>"],
  "complaints_for_model_year": <integer>,
  "common_complaint_areas": "<factual summary of top categories if any>",
  "carfax_tip": "<specific advice about what to look for on Carfax for THIS car>",
  "nhtsa_source": "NHTSA data for {year} {make} {model} model year — check this specific VIN at nhtsa.gov/recalls"
}}

RULES:
- If 0 recalls, say that clearly as a positive
- If 0 complaints, say that clearly as a positive
- Frame recalls as free fixes, not scary defects
- ONLY cite data provided — do NOT invent recalls or complaints
- NHTSA data is for the MODEL YEAR, not this specific VIN — say that""",

    "price_analysis": """You are a car market analyst writing one section of a buyer report.

SECTION: "The Price" — Market Comparison
VEHICLE: {vehicle_str}

TASK: Analyze this car's price vs local market comps within 50 miles. Is it a good deal? 
Use the actual numbers provided. Give a verdict and explain WHY.

{data_context}

OUTPUT FORMAT (JSON):
{{
  "verdict": "<Great Deal|Good Deal|Fair Price|Slightly Above Market>",
  "vs_market": "<exact comparison — e.g., '$1,290 above the $12,145 median of 6 local comps'>",
  "comp_count": "<number of comparable listings within 50 miles>",
  "price_range": "<local price range — e.g., '$9,295 - $14,995'>",
  "fair_range": "<what you'd expect to pay — e.g., '$11,500 - $13,500'>",
  "context": "<2-3 sentences explaining WHY — trim, mileage, condition vs comps. Cite numbers.>",
  "bottom_line": "<one decisive sentence>"
}}

RULES:
- ONLY use the market data numbers provided
- If no comp data, say "No comparable listings found within 50 miles" 
- Cite exact numbers from the data — never approximate when you have real figures
- Be helpful — if it's above market, explain what might justify it""",

    "owner_feedback": """You are a car expert writing one section of a buyer report.

SECTION: "Owner Talk" — What Real Owners Say
VEHICLE: {vehicle_str}

TASK: Summarize what real owners say about this car based ONLY on the web research data below.

{data_context}

OUTPUT FORMAT (JSON):
{{
  "headline": "<one line from REAL research — cite source if possible>",
  "what_owners_love": ["<from research data>", "<from research data>"],
  "what_owners_wish_they_knew": ["<from research data — cite source>"],
  "common_experiences": "<2-3 sentences from real owner perspectives>",
  "reliability_reputation": "<one sentence on reliability reputation>"
}}

CRITICAL RULES:
- ONLY use the web research data provided below
- Cite sources: "Owners on PriusChat report..." or "Reddit users mention..."
- If the research data is empty or doesn't contain owner experiences, return:
  {{
    "headline": "Limited owner data found — check Reddit and forums for real experiences",
    "what_owners_love": ["Research data was limited for this specific model — check r/{make_lower} and model-specific forums"],
    "what_owners_wish_they_knew": ["No specific owner insights found in research"],
    "common_experiences": "Limited owner-specific feedback available in our research. For real owner experiences, check Reddit r/{make_lower}, model-specific forums, and long-term review videos on YouTube.",
    "reliability_reputation": "Check Consumer Reports and J.D. Power for official reliability ratings for the {year} {make} {model}."
  }}
- BANNED: "Some owners wish they knew about regular maintenance" — that's generic filler
- BANNED: "It's important to stay up to date on recalls" — obvious and unhelpful  
- Every bullet MUST come from the research data or honestly say "not found"
- NO FABRICATED OWNER SENTIMENT""",

    "dealer_questions": """You are a car buying expert writing one section of a buyer report.

SECTION: "Go Prepared" — Smart Questions for the Dealer
VEHICLE: {vehicle_str}

TASK: Generate 5-7 smart, specific questions a buyer should ask about THIS car. 
Use the vehicle data, NHTSA data, and research to craft questions that show homework.

{data_context}

OUTPUT FORMAT (JSON):
{{
  "questions": [
    {{
      "ask": "<the exact question — specific to THIS car>",
      "why_it_matters": "<what the answer tells you>",
      "good_answer": "<what you want to hear>"
    }}
  ],
  "bonus_tip": "<one insider tip about buying THIS type of car>"
}}

RULES:
- Questions MUST reference this specific year/make/model
- If NHTSA recalls exist, include a question about whether they've been addressed
- If mileage is high, include a question about relevant service milestones
- BANNED: "Can I see the Carfax?" — too generic
- BANNED: "Has this car been in any accidents?" — too generic
- Each question should show the buyer did their homework"""
}


def generate_section(section_name, vehicle_info, data_context_str):
    """Generate a single section using a focused mini-prompt."""
    v = vehicle_info
    vehicle_str = f"{v.get('year', '?')} {v.get('make', '?')} {v.get('model', '?')}"
    if v.get('trim'):
        vehicle_str += f" {v['trim']}"

    prompt_template = SECTION_PROMPTS.get(section_name)
    if not prompt_template:
        log.error(f"No prompt template for section: {section_name}")
        return None

    prompt = prompt_template.format(
        vehicle_str=vehicle_str,
        data_context=data_context_str,
        year=v.get('year', '?'),
        make=v.get('make', '?'),
        model=v.get('model', '?'),
        make_lower=str(v.get('make', '')).lower(),
    )

    try:
        resp = requests.post(GROQ_URL, json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": "You are a car buying expert. Return ONLY valid JSON matching the requested schema. No markdown, no explanation — just the JSON object."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.15,
            "max_tokens": 3000,
            "response_format": {"type": "json_object"}
        }, headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }, timeout=30)

        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            return json.loads(content)
        else:
            log.error(f"Section {section_name} LLM error: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        log.error(f"Section {section_name} generation failed: {e}")
    return None


def generate_overall_score(vehicle_info, sections):
    """Final pass: generate overall score based on all section outputs."""
    v = vehicle_info
    vehicle_str = f"{v.get('year', '?')} {v.get('make', '?')} {v.get('model', '?')}"

    prompt = f"""Based on these section analyses for a {vehicle_str}, generate an overall buying confidence score.

SECTIONS:
{json.dumps(sections, indent=2, default=str)[:6000]}

OUTPUT FORMAT (JSON):
{{
  "score": <0.0-10.0 with one decimal>,
  "label": "<Strong Buy|Buy|Lean Buy|Neutral|Lean Pass>",
  "one_liner": "<one decisive sentence about this specific car>"
}}

Score guide: 8+ = great buy, 6-8 = solid, 4-6 = proceed with caution, <4 = think twice"""

    try:
        resp = requests.post(GROQ_URL, json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": "Return ONLY valid JSON. No explanation."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 500,
            "response_format": {"type": "json_object"}
        }, headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }, timeout=15)
        if resp.status_code == 200:
            return json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception as e:
        log.error(f"Overall score generation failed: {e}")
    return {"score": 5.0, "label": "Neutral", "one_liner": f"Report generated for {vehicle_str}"}


def generate_analysis_pipeline(vehicle_info, market_data, nhtsa_data, dealer_rep, listing_text="", vin_decode=None):
    """
    v9.1 PIPELINE: Section-by-section report generation.
    Each section gets its own targeted research + focused LLM call.
    No more single monolithic prompt that hallucinates when data is thin.
    """
    import concurrent.futures

    v = vehicle_info
    year = v.get("year")
    make = v.get("make")
    model = v.get("model")
    trim = v.get("trim")
    vehicle_str = f"{year} {make} {model}"
    identity = build_vehicle_identity(vehicle_info, vin_decode)

    # =====================================================
    # PHASE 1: Parallel targeted research (3 Exa searches)
    # =====================================================
    log.info(f"Pipeline Phase 1: Parallel research for {vehicle_str}")

    model_year_research = None
    owner_research = None
    dealer_research = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        fut_model = ex.submit(research_model_year, year, make, model, trim)
        fut_owner = ex.submit(research_owner_feedback, year, make, model, trim)
        fut_dealer = ex.submit(research_dealer_questions, year, make, model, trim)

        model_year_research = fut_model.result()
        owner_research = fut_owner.result()
        dealer_research = fut_dealer.result()

    log.info(f"Research complete: model_year={'yes' if model_year_research else 'no'}, "
             f"owner={'yes' if owner_research else 'no'}, dealer={'yes' if dealer_research else 'no'}")

    # =====================================================
    # PHASE 2: Build section-specific data contexts
    # =====================================================

    # Section 1: Model Year Summary — gets model year research + basic identity
    s1_context = f"{identity}\n\nWEB RESEARCH — Model Year Info:\n{model_year_research or 'No web research data available for this model year.'}"

    # Section 2: Vehicle History — gets NHTSA data ONLY (no web research to hallucinate from)
    s2_parts = [identity]
    if nhtsa_data:
        n = nhtsa_data
        s2_parts.append(f"\nNHTSA DATA for {year} {make} {model} MODEL YEAR:")
        s2_parts.append(f"  Recalls: {n['recall_count']}")
        s2_parts.append(f"  Complaints: {n['complaint_count']}")
        s2_parts.append(f"  Risk Score: {n['risk_score']}/10 ({n['risk_label']})")
        for r in n.get("recalls", [])[:8]:
            s2_parts.append(f"  RECALL [{r['component']}]: {r['summary'][:300]}")
            if r.get("remedy"): s2_parts.append(f"    FIX: {r['remedy'][:200]}")
        if n.get("top_complaint_areas"):
            areas = ", ".join(f"{a} ({c})" for a, c in n["top_complaint_areas"][:8])
            s2_parts.append(f"  Complaint breakdown: {areas}")
        for c in n.get("complaints_raw", [])[:8]:
            summary = str(c.get("summary", ""))[:200]
            comp = c.get("components", "")
            if summary:
                s2_parts.append(f"  COMPLAINT [{comp}]: {summary}")
    else:
        s2_parts.append("\nNo NHTSA data available.")
    s2_context = "\n".join(s2_parts)

    # Section 3: Price Analysis — gets market data ONLY
    s3_parts = [identity]
    if market_data:
        m = market_data
        s3_parts.append(f"\nMARKET DATA ({m['comp_count']} comparable listings within 50 miles):")
        s3_parts.append(f"  This car's price: ${v.get('price', 'unknown'):,}" if isinstance(v.get('price'), (int, float)) else f"  This car's price: {v.get('price', 'unknown')}")
        s3_parts.append(f"  Median: ${m['median_price']:,}  |  Average: ${m['avg_price']:,}")
        s3_parts.append(f"  Range: ${m['min_price']:,} - ${m['max_price']:,}")
        if m.get('percentile') is not None:
            s3_parts.append(f"  Percentile: {m['percentile']}th (lower = cheaper)")
        if m.get('savings') is not None:
            if m['savings'] > 0:
                s3_parts.append(f"  >>> ${m['savings']:,} BELOW median <<<")
            elif m['savings'] < 0:
                s3_parts.append(f"  >>> ${abs(m['savings']):,} ABOVE median <<<")
        if m.get('deal_score'):
            s3_parts.append(f"  Deal score: {m['deal_score']}/10")
        s3_parts.append(f"  Total supply: {m['total_market']} similar vehicles on market")
        if m.get('mileage_prices') and v.get('mileage'):
            similar = [x for x in m['mileage_prices'] if abs(x['mileage'] - v['mileage']) < 20000]
            if similar:
                sp = [x['price'] for x in similar]
                s3_parts.append(f"  Similar-mileage comps: avg ${sum(sp)//len(sp):,} ({len(sp)} listings)")
    else:
        s3_parts.append("\nNo market comparison data available within 50 miles.")
    if v.get('mileage'):
        s3_parts.append(f"  This car's mileage: {v['mileage']:,}" if isinstance(v['mileage'], (int, float)) else f"  This car's mileage: {v['mileage']}")
    s3_context = "\n".join(s3_parts)

    # Section 4: Owner Feedback — gets ONLY owner research (the gatekeeper)
    s4_context = f"{identity}\n\nWEB RESEARCH — Owner Feedback (use ONLY this data):\n{owner_research or 'NO OWNER RESEARCH DATA AVAILABLE. You must return the limited-data fallback response.'}"

    # Section 5: Dealer Questions — gets ALL data for smart synthesis
    s5_parts = [identity]
    if nhtsa_data and nhtsa_data.get("recall_count", 0) > 0:
        s5_parts.append(f"\nRECALLS: {nhtsa_data['recall_count']} recalls for this model year")
        for r in nhtsa_data.get("recalls", [])[:5]:
            s5_parts.append(f"  - {r['component']}: {r['summary'][:150]}")
    if market_data:
        s5_parts.append(f"\nPRICE: Listed at ${v.get('price', '?')}, median is ${market_data['median_price']:,}")
    if v.get('mileage'):
        s5_parts.append(f"MILEAGE: {v['mileage']:,} miles" if isinstance(v['mileage'], (int, float)) else f"MILEAGE: {v['mileage']}")
    if dealer_research:
        s5_parts.append(f"\nWEB RESEARCH — Known issues & buying tips:\n{dealer_research}")
    s5_context = "\n".join(s5_parts)

    # =====================================================
    # PHASE 3: Parallel section generation (5 LLM calls)
    # =====================================================
    log.info(f"Pipeline Phase 3: Generating 5 sections in parallel for {vehicle_str}")

    sections = {}
    section_configs = [
        ("model_year_summary", s1_context),
        ("vehicle_history", s2_context),
        ("price_analysis", s3_context),
        ("owner_feedback", s4_context),
        ("dealer_questions", s5_context),
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {}
        for section_name, context in section_configs:
            futures[ex.submit(generate_section, section_name, vehicle_info, context)] = section_name

        for future in concurrent.futures.as_completed(futures):
            section_name = futures[future]
            try:
                result = future.result()
                if result:
                    sections[section_name] = result
                    log.info(f"Section {section_name}: generated OK")
                else:
                    log.warning(f"Section {section_name}: returned None")
                    sections[section_name] = {"error": "Section generation failed"}
            except Exception as e:
                log.error(f"Section {section_name} error: {e}")
                sections[section_name] = {"error": str(e)}

    # =====================================================
    # PHASE 4: Overall score (quick final LLM call)
    # =====================================================
    log.info(f"Pipeline Phase 4: Generating overall score for {vehicle_str}")
    overall = generate_overall_score(vehicle_info, sections)

    # =====================================================
    # PHASE 5: Assemble final report
    # =====================================================
    analysis = {
        "overall_score": overall,
        **sections
    }

    log.info(f"Pipeline complete for {vehicle_str}: {len(sections)} sections generated")
    return analysis



# ==============================================================
# ORCHESTRATOR ÃÂ¢ÃÂÃÂ now with VIN decode + web research
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

    # === STEP 5: Web research now handled inside pipeline ===

    # === STEP 6: Generate AI analysis ===
    analysis = generate_analysis_pipeline(vehicle, market_data, nhtsa_data, dealer_rep, listing_text, vin_decode)

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
            "recall_count": nhtsa_data["recall_count"] if nhtsa_data else None,
            "complaint_count": nhtsa_data["complaint_count"] if nhtsa_data else None,
            "risk_score": nhtsa_data["risk_score"] if nhtsa_data else None,
            "risk_label": nhtsa_data["risk_label"] if nhtsa_data else "No data",
            "top_complaint_areas": nhtsa_data["top_complaint_areas"][:5] if nhtsa_data else [],
            "data_source": "NHTSA model-year lookup (not VIN-specific)" if nhtsa_data else "unavailable",
        },
        "analysis": analysis,
        "generated_at": datetime.utcnow().isoformat(),
        "report_id": hashlib.md5(json.dumps(vehicle, sort_keys=True, default=str).encode()).hexdigest()[:12],
        "version": "9.1.0"
    }


# ==============================================================
# API ROUTES
# ==============================================================

@app.route("/")
def home():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(html_path):
        with open(html_path) as f:
            from flask import make_response
            resp = make_response(f.read())
            resp.headers['Content-Type'] = 'text/html; charset=utf-8'
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            resp.headers['Pragma'] = 'no-cache'
            resp.headers['Expires'] = '0'
            return resp
    return "<h1>AskCarBuddy</h1><p>Frontend not found.</p>"

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    t_start = time.time()
    try:
        report = analyze_listing(data)
        total_ms = (time.time() - t_start) * 1000
        if "error" in report:
            try:
                save_trace({"url": data.get("url",""), "error": report["error"], "total_time_ms": total_ms, "prompt_version": "v9.1"})
            except Exception:
                pass
            return jsonify(report), 400
        # === SELF-IMPROVING AGENT: Save trace ===
        try:
            v = report.get("vehicle", {})
            a = report.get("analysis", {})
            os_data = a.get("overall_score", {}) if isinstance(a, dict) else {}
            pa_data = a.get("price_analysis", {}) if isinstance(a, dict) else {}
            trace_id = save_trace({
                "url": data.get("url", ""),
                "year": v.get("year", ""),
                "make": v.get("make", ""),
                "model": v.get("model", ""),
                "trim": v.get("trim", ""),
                "price": v.get("price"),
                "mileage": v.get("mileage"),
                "prompt_version": "v9.1",
                "total_time_ms": total_ms,
                "overall_score": os_data.get("score") if isinstance(os_data, dict) else None,
                "deal_position": pa_data.get("verdict") if isinstance(pa_data, dict) else None,
                "mechanical_risk": None,
                "confidence_level": None,
                "ai_output_json": json.dumps(a) if a else None
            })
            report["trace_id"] = trace_id
        except Exception as te:
            log.warning(f"Trace save failed: {te}")
        return jsonify(report)
    except Exception as e:
        log.error(f"Analysis error: {e}")
        total_ms = (time.time() - t_start) * 1000
        try:
            save_trace({"url": data.get("url",""), "error": str(e), "total_time_ms": total_ms, "prompt_version": "v9.1"})
        except Exception:
            pass
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
        "status": "ok", "service": "AskCarBuddy", "version": "9.1.0",
        "apis": {"groq": bool(GROQ_API_KEY), "autodev": bool(AUTODEV_API_KEY), "exa": bool(EXA_API_KEY)}
    })



# ==============================================================
# SELF-IMPROVING AGENT — REWARD + EVENT ENDPOINTS
# ==============================================================

@app.route("/api/reward", methods=["POST"])
def api_reward():
    data = request.get_json()
    if not data or "trace_id" not in data or "signal_type" not in data:
        return jsonify({"error": "trace_id and signal_type required"}), 400

    allowed_signals = {"thumbs", "useful", "paid", "shared", "copy_question", "section_expand"}
    if data["signal_type"] not in allowed_signals:
        return jsonify({"error": f"signal_type must be one of: {allowed_signals}"}), 400

    save_reward(
        trace_id=data["trace_id"],
        signal_type=data["signal_type"],
        signal_value=data.get("signal_value", 1),
        metadata=data.get("metadata")
    )
    return jsonify({"ok": True})

@app.route("/api/event", methods=["POST"])
def api_event():
    data = request.get_json()
    if not data or "trace_id" not in data or "event_type" not in data:
        return jsonify({"error": "trace_id and event_type required"}), 400

    save_page_event(
        trace_id=data["trace_id"],
        event_type=data["event_type"],
        section_name=data.get("section_name"),
        duration_ms=data.get("duration_ms"),
        scroll_depth=data.get("scroll_depth"),
        metadata=data.get("metadata")
    )
    return jsonify({"ok": True})

@app.route("/api/learning")
def api_learning():
    try:
        stats = get_learning_stats()
        return jsonify(stats)
    except Exception as e:
        log.error(f"Learning stats error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/admin/brain")
def admin_brain():
    return render_template_string(BRAIN_DASHBOARD_HTML)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"AskCarBuddy v7.0 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
