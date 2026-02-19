#!/usr/bin/env python3
"""
AskCarBuddy v7.0 - AI Car Buying Intelligence (Smart Engine)
=============================================================
Paste any listing URL -> Get a REAL pro-level intelligence brief.

v7 architecture:
- Exa API for web research (model-specific owner issues)
- Auto.dev for local market search (50-mile radius)
- NHTSA VIN decode + recalls/complaints
- Groq LLM for analysis (llama-3.3-70b, temperature 0.15)
- Trace DB for self-improvement tracking
"""

import os
import json
import re
import time
import requests
import logging
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from bs4 import BeautifulSoup
import sqlite3
import threading
import uuid

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("askcarbuddy")

app = Flask(__name__)
CORS(app)

# ==================== CONFIG ====================
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
GROQ_MODEL = 'llama-3.3-70b-versatile'
GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'

EXA_API_KEY = os.getenv('EXA_API_KEY')
EXA_URL = 'https://api.exa.ai/search'

AUTODEV_API_KEY = os.getenv('AUTODEV_API_KEY')
AUTODEV_BASE = 'https://auto.dev/api/listings'

NHTSA_VIN_URL = 'https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/{{vin}}?format=json'
NHTSA_RECALLS_URL = 'https://api.nhtsa.gov/recalls/recallsByVehicle'
NHTSA_COMPLAINTS_URL = 'https://api.nhtsa.gov/complaints/complaintsByVehicle'

DB_PATH = os.getenv('TRACE_DB', 'askcarbuddy_traces.db')
_db_lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS traces (
            id TEXT PRIMARY KEY,
            created_at TEXT DEFAULT (datetime('now')),
            url TEXT,
            vehicle_year TEXT,
            vehicle_make TEXT,
            vehicle_model TEXT,
            vehicle_price REAL,
            vehicle_mileage REAL,
            scrape_time_ms REAL,
            market_time_ms REAL,
            nhtsa_time_ms REAL,
            ai_time_ms REAL,
            total_time_ms REAL,
            groq_tokens_used INTEGER,
            overall_score REAL,
            ai_output_json TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at);
    """)
    conn.commit()
    conn.close()
    log.info('Trace DB initialized')

def save_trace(trace_data):
    trace_id = str(uuid.uuid4())[:12]
    with _db_lock:
        conn = get_db()
        conn.execute("""
            INSERT INTO traces (id, url, vehicle_year, vehicle_make, vehicle_model,
                vehicle_price, vehicle_mileage, scrape_time_ms, market_time_ms, nhtsa_time_ms,
                ai_time_ms, total_time_ms, groq_tokens_used, overall_score, ai_output_json, error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trace_id,
            trace_data.get('url'),
            trace_data.get('year'),
            trace_data.get('make'),
            trace_data.get('model'),
            trace_data.get('price'),
            trace_data.get('mileage'),
            trace_data.get('scrape_ms'),
            trace_data.get('market_ms'),
            trace_data.get('nhtsa_ms'),
            trace_data.get('ai_ms'),
            trace_data.get('total_ms'),
            trace_data.get('tokens'),
            trace_data.get('score'),
            json.dumps(trace_data.get('analysis', {})),
            trace_data.get('error', '')
        ))
        conn.commit()
        conn.close()
    return trace_id

# ==================== SCRAPER ====================

def scrape_listing(url):
    """Extract vehicle data from listing URL."""
    try:
        response = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(response.text, 'html.parser')
        text = soup.get_text()
        
        # Extract year/make/model from URL path
        import re
        url_match = re.search(r'/(\d{4})-(.+?)-(\d+)/', url)
        if url_match:
            year, model_text, _ = url_match.groups()
            # Parse make/model from URL
            parts = model_text.split('-')
            make = parts[0] if len(parts) > 0 else ''
            model = ' '.join(parts[1:]) if len(parts) > 1 else ''
        else:
            year = make = model = ''
        
        # Extract price
        price_match = re.search(r'\$(\d+,?\d+)', text)
        price = int(price_match.group(1).replace(',', '')) if price_match else 0
        
        # Extract mileage
        mileage_match = re.search(r'(\d+,?\d+)\s*(?:miles|mi)', text)
        mileage = int(mileage_match.group(1).replace(',', '')) if mileage_match else 0
        
        return {
            'year': year,
            'make': make,
            'model': model,
            'price': price,
            'mileage': mileage,
            'listing_text': text[:3000]
        }
    except Exception as e:
        log.error(f'Scrape error: {e}')
        return {}

# ==================== MARKET SEARCH ====================

def search_market(year, make, model, zip_code, radius=50):
    """Search for comparable vehicles within radius miles."""
    if not AUTODEV_API_KEY:
        return {'comp_count': 0, 'avg_price': 0, 'median_price': 0, 'prices': []}
    
    try:
        params = {
            'query': f'{year} {make} {model}',
            'location': zip_code,
            'radius': radius,
            'limit': 20
        }
        headers = {'Authorization': f'Bearer {AUTODEV_API_KEY}'}
        
        response = requests.get(AUTODEV_BASE, params=params, headers=headers, timeout=10)
        data = response.json() if response.status_code == 200 else {}
        
        listings = data.get('listings', [])
        prices = [int(l.get('price', 0)) for l in listings if l.get('price')]
        
        if prices:
            return {
                'comp_count': len(prices),
                'avg_price': sum(prices) // len(prices),
                'median_price': sorted(prices)[len(prices)//2],
                'min_price': min(prices),
                'max_price': max(prices),
                'prices': prices
            }
        return {'comp_count': 0, 'avg_price': 0, 'median_price': 0, 'prices': []}
    except Exception as e:
        log.error(f'Market search error: {e}')
        return {}

# ==================== NHTSA ====================

def get_nhtsa_data(year, make, model):
    """Get recalls and complaints from NHTSA."""
    try:
        # Get recalls
        recalls_url = NHTSA_RECALLS_URL
        recalls_params = {'make': make, 'model': model, 'modelYear': year}
        recalls_response = requests.get(recalls_url, params=recalls_params, timeout=10)
        recalls_data = recalls_response.json() if recalls_response.status_code == 200 else {}
        
        # Get complaints
        complaints_url = NHTSA_COMPLAINTS_URL
        complaints_params = {'make': make, 'model': model, 'modelYear': year, 'pageSize': 50}
        complaints_response = requests.get(complaints_url, params=complaints_params, timeout=10)
        complaints_data = complaints_response.json() if complaints_response.status_code == 200 else {}
        
        return {
            'recalls': recalls_data.get('results', []),
            'complaints': complaints_data.get('results', [])
        }
    except Exception as e:
        log.error(f'NHTSA error: {e}')
        return {'recalls': [], 'complaints': []}

# ==================== AI ANALYSIS ====================

def generate_analysis(vehicle, market_data, nhtsa_data):
    """Generate buyer intelligence report via Groq."""
    if not GROQ_API_KEY:
        return {}
    
    context = f"""
Vehicle: {vehicle.get('year')} {vehicle.get('make')} {vehicle.get('model')}
Price: ${vehicle.get('price'):,}
Mileage: {vehicle.get('mileage'):,} miles

Market Data ({market_data.get('comp_count')} comps in 50 miles):
Median: ${market_data.get('median_price', 0):,}
Range: ${market_data.get('min_price', 0):,} - ${market_data.get('max_price', 0):,}

NHTSA Data:
Recalls: {len(nhtsa_data.get('recalls', []))}
Complaints: {len(nhtsa_data.get('complaints', []))}
"""
    
    user_msg = f"""Generate a comprehensive buyer intelligence brief for this {vehicle.get('year')} {vehicle.get('make')} {vehicle.get('model')} listed at ${vehicle.get('price'):,} with {vehicle.get('mileage'):,} miles.

Provide:
1. Overall score (0-10) and recommendation
2. Market position analysis
3. Key mechanical risks
4. Ownership cost estimates
5. Insider tips specific to this model

Be direct, specific, and actionable. Return as JSON.

{context}"""
    
    try:
        response = requests.post(
            GROQ_URL,
            json={
                'model': GROQ_MODEL,
                'messages': [{'role': 'user', 'content': user_msg}],
                'temperature': 0.15,
                'max_tokens': 2000,
                'response_format': {'type': 'json_object'}
            },
            headers={'Authorization': f'Bearer {GROQ_API_KEY}', 'Content-Type': 'application/json'},
            timeout=60
        )
        
        if response.status_code == 200:
            content = response.json()['choices'][0]['message']['content']
            return json.loads(content)
        else:
            log.error(f'Groq error: {response.status_code}')
            return {}
    except Exception as e:
        log.error(f'Analysis error: {e}')
        return {}

# ==================== ROUTES ====================

@app.route('/')
def index():
    try:
        with open('index.html', 'r') as f:
            return f.read()
    except:
        return '''<html><body><h1>AskCarBuddy</h1><p>Paste a car listing URL to analyze.</p></body></html>'''

@app.route('/api/analyze', methods=['POST'])
def analyze():
    start_time = time.time()
    data = request.json or {}
    url = data.get('listing_url')
    
    if not url:
        return jsonify({'error': 'Missing listing_url'}), 400
    
    try:
        # Scrape
        scrape_start = time.time()
        vehicle = scrape_listing(url)
        scrape_time = (time.time() - scrape_start) * 1000
        
        if not vehicle.get('year') or not vehicle.get('make') or not vehicle.get('model'):
            return jsonify({'error': 'Couldn\'t identify the car. Try a different listing URL.'}), 400
        
        # Market search
        market_start = time.time()
        zip_code = data.get('zip', '48309')
        market_data = search_market(vehicle['year'], vehicle['make'], vehicle['model'], zip_code, radius=50)
        market_time = (time.time() - market_start) * 1000
        
        # NHTSA
        nhtsa_start = time.time()
        nhtsa_data = get_nhtsa_data(vehicle['year'], vehicle['make'], vehicle['model'])
        nhtsa_time = (time.time() - nhtsa_start) * 1000
        
        # Analysis
        ai_start = time.time()
        analysis = generate_analysis(vehicle, market_data, nhtsa_data)
        ai_time = (time.time() - ai_start) * 1000
        
        total_time = (time.time() - start_time) * 1000
        
        response = {
            'vehicle': vehicle,
            'market_data': market_data,
            'nhtsa_data': {'recall_count': len(nhtsa_data.get('recalls', [])), 'complaint_count': len(nhtsa_data.get('complaints', []))},
            'analysis': analysis,
            'generated_at': datetime.now().isoformat(),
            'version': '7.0.0'
        }
        
        # Save trace
        save_trace({
            'url': url,
            'year': vehicle.get('year'),
            'make': vehicle.get('make'),
            'model': vehicle.get('model'),
            'price': vehicle.get('price'),
            'mileage': vehicle.get('mileage'),
            'scrape_ms': scrape_time,
            'market_ms': market_time,
            'nhtsa_ms': nhtsa_time,
            'ai_ms': ai_time,
            'total_ms': total_time,
            'analysis': analysis
        })
        
        return jsonify(response)
    except Exception as e:
        log.error(f'Error: {e}')
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    init_db()
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
