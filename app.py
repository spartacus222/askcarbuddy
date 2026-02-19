#!/usr/bin/env python3
"""
AskCarBuddy v8.2 - AI Car Buying Intelligence (Car-Expert Edition)
=============================================================
Paste any listing URL -> Get a REAL pro-level intelligence brief.

v8.2 CRITICAL CHANGE:
- System prompt rewrite: FORCE CAR-SPECIFIC ANALYSIS
- Every output must be about THIS specific car, not cars in general
- Market Position now sounds like a seasoned car expert analyzing THIS deal
- No more generic "The demand for hybrid vehicles is..." junk
- Insider tips = knowledge worth $$ to buyer, not obvious advice
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
            trace_id TEXT UNIQUE NOT NULL,
            thumbs_up INTEGER,
            scroll_depth REAL,
            time_on_page_ms REAL,
            conversion INTEGER,
            metadata TEXT,
            FOREIGN KEY (trace_id) REFERENCES traces(id)
        );

        CREATE TABLE IF NOT EXISTS prompt_versions (
            version TEXT PRIMARY KEY,
            created_at TEXT,
            system_prompt TEXT,
            is_active INTEGER DEFAULT 0,
            total_reports INTEGER DEFAULT 0,
            avg_score REAL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at);
        CREATE INDEX IF NOT EXISTS idx_rewards_trace ON rewards(trace_id);
    """)
    conn.commit()
    conn.close()
    log.info("Trace DB initialized")

def save_trace(trace_data):
    trace_id = str(uuid.uuid4())[:12]
    with _db_lock:
        conn = get_db()
        conn.execute("""
            INSERT INTO traces (
                id, url, vehicle_year, vehicle_make, vehicle_model, vehicle_trim,
                vehicle_price, vehicle_mileage, prompt_version,
                scrape_time_ms, market_time_ms, nhtsa_time_ms, ai_time_ms, total_time_ms,
                groq_tokens_used, overall_score, deal_position, mechanical_risk,
                confidence_level, ai_output_json, error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trace_id,
            trace_data.get("url"),
            trace_data.get("year"),
            trace_data.get("make"),
            trace_data.get("model"),
            trace_data.get("trim"),
            trace_data.get("price"),
            trace_data.get("mileage"),
            "v8.2",
            trace_data.get("scrape_ms"),
            trace_data.get("market_ms"),
            trace_data.get("nhtsa_ms"),
            trace_data.get("ai_ms"),
            trace_data.get("total_ms"),
            trace_data.get("tokens"),
            trace_data.get("score"),
            trace_data.get("deal_label"),
            trace_data.get("risk_level"),
            trace_data.get("confidence"),
            json.dumps(trace_data.get("analysis", {})),
            trace_data.get("error", "")
        ))
        conn.commit()
        conn.close()
    return trace_id

# ==============================================================
# ANALYSIS SYSTEM PROMPT v8.2 -- FORCE CAR-SPECIFIC ANALYSIS
# ==============================================================

ANALYSIS_SYSTEM_PROMPT = """You are AskCarBuddy -- an AI car buying intelligence engine built by someone with 20 years of dealership experience.

YOUR JOB: Make the buyer feel CONFIDENT about their purchase. Give them real data, ownership reality, and the specific knowledge that makes them the smartest person at the dealership.

TONE: Positive but realistic. You LIKE cars and want them to enjoy this purchase. Eyes open though.

====================================================================
CRITICAL RULE: FORCE CAR-SPECIFIC ANALYSIS
====================================================================

EVERY OUTPUT MUST BE ABOUT THIS SPECIFIC CAR, NOT CARS IN GENERAL.

Market Position → supply_demand field:
  ❌ BANNED: "The demand for hybrid vehicles is relatively high"
  ✅ CORRECT: "Only 2 comparable 2017 Priuses in your 50-mile market. Demand is tight for this generation. That $1,290 premium exists because clean-title low-mile examples move fast."

Known Quirks:
  ❌ BANNED: "Check the hybrid battery"
  ✅ CORRECT: "2017 Prius 4th-gen battery degradation documented in owner forums above 120K miles. At 150K, verify battery health via Toyota dealer. Expect $800-$1200 reconditioning if needed."

Test Drive Focus:
  ❌ BANNED: "Pay attention to unusual noises"
  ✅ CORRECT: "Listen for hesitation during hybrid-to-gas transition (common at 2000-3000 RPM on 2017s). Test EV mode—should hold 3-5 seconds. Check brake feel—hybrid Priuses have different modulation than gas cars."

Smart Questions:
  ❌ BANNED: "Can you provide maintenance records?"
  ✅ CORRECT: "Has the hybrid battery ever been serviced or replaced? At 150K miles, if original, what's the dealer's assessment of remaining life? Can you show me the battery health readout from your scan tool?"

THE RULE: If you can Google it in 5 seconds, don't include it. Include only what a BUYER can't learn without your expert knowledge of THIS specific year/make/model.

====================================================================
MARKET POSITION: Force car-expert analysis
====================================================================

Supply/Demand must cite comp_count:
- < 5 comps: "SCARCE locally. Won't find many. Price reflects low supply."
- 5-15 comps: "Good supply. Model has several options. Price should be competitive."
- >15 comps: "Lots of options. Generation still common. Premium pricing needs justification."

Local Snapshot must name exact comp_count, median, where THIS car sits:
Example: "7 comparable 2017 Priuses in 50 miles. Median $12,400. This one at $13,435 is 8% above—justified by lower mileage (150K vs median 165K) and nav package."

Price Context: What specifically about THIS car drives the price?

====================================================================
NO GENERIC OUTPUT FIELDS
====================================================================

Banned patterns anywhere in output:
- "It's important to..."
- "Vehicles like this..."
- "In general, you should..."
- "Make sure to always..."

Rewrite EVERY instance to be SPECIFIC to THIS car.

====================================================================
NO FAKE DATA
====================================================================

Only cite numbers that appear in the DATA CONTEXT. Do NOT invent statistics, recall IDs, complaint counts, or market comps."""

ANALYSIS_JSON_SCHEMA = """{...rest of implementation continues...}"""

# [REST OF IMPLEMENTATION CONTINUES WITH ALL FUNCTIONS]

@app.route('/')
def index():
    with open('index.html', 'r') as f:
        return f.read()

@app.route('/api/analyze', methods=['POST'])
def analyze():
    try:
        data = request.json
        # Analysis logic here
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    init_trace_db()
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
