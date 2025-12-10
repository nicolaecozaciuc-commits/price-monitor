import re
import logging
import time
import os
from urllib.parse import quote_plus
from flask import Flask, request, jsonify, render_template, send_file
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__, template_folder='templates')
CORS(app)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

DEBUG_DIR = '/root/monitor/debug'
os.makedirs(DEBUG_DIR, exist_ok=True)

BLOCKED = ['google', 'bing', 'microsoft', 'facebook', 'youtube', 'doarbai', 'termohabitat', 
           'wikipedia', 'amazon', 'ebay', 'olx', 'kaufland', 'anre']

def clean_price(value):
    if not value: return 0
    text = re.sub(r'[^\d,.]', '', str(value))
    if not text: return 0
    if ',' in text and '.' in text:
        text = text.replace('.', '').replace(',', '.') if text.rindex(',') > text.rindex('.') else text.replace(',', '')
    elif ',' in text:
        text = text.replace(',', '.')
    try:
        price = float(text)
        return price if 50 < price < 500000 else 0
    except:
        return 0

def is_valid_domain(domain):
    if not domain or len(domain) < 5:
        return False
    if any(b in domain for b in BLOCKED):
        return False
    match = re.match(r'^([a-z0-9-]+)\.ro$', domain)
    return match and len(match.group(1)) >= 3

def extract_from_bing(page):
    results = []
    
    try:
        algo_count = len(page.locator('.b_algo').all())
        logger.info(f"      .b_algo count: {algo_count}")
        
        for result in page.locator('.b_algo').all()[:15]:
            try:
                link = result.locator('a').first
                href = link.get_attribute('href') or ''
                
                domain_match = re.search(r'https?://(?:www\.)?([a-z0-9-]+\.ro)', href.lower())
                if not domain_match:
                    continue
                domain = domain_match.group(1)
                
                if not is_valid_domain(domain):
                    continue
                
                text = result.inner_text()
                price_match = re.search(r'([\d.,]+)\s*(?:RON|Lei|lei|Ron)', text)
                if price_match:
                    price = clean_price(price_match.group(1))
                    if price > 0:
                        results.append({
                            'name': domain,
                            'price': price,
                            'url': href,
                            'method': 'Bing'
                        })
                        logger.info(f"      ✓ {domain}: {price} Lei")
                        
            except:
                continue
                
    except Exception as e:
        logger.info(f"      Extract error: {e}")
    
    return results

def scan_product(sku, name, your_price=0):
    found = []
    sku = str(sku).strip()
    
    logger.info(f"🔎 {sku} - {name[:30]}...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale='ro-RO'
        )
        
        page = context.new_page()
        
        try:
            query = f"{sku} pret"
            url = f"https://www.bing.com/search?q={quote_plus(query)}"
            
            logger.info(f"   🔍 Bing: {query}")
            
            page.goto(url, timeout=20000, wait_until='domcontentloaded')
            time.sleep(3)
            
            # Screenshot pentru debug
            screenshot_path = f"{DEBUG_DIR}/bing_{sku}.png"
            page.screenshot(path=screenshot_path)
            logger.info(f"   📸 Screenshot salvat: {screenshot_path}")
            
            # Accept cookies
            try:
                page.click('#bnp_btn_accept', timeout=2000)
                time.sleep(1)
                page.screenshot(path=f"{DEBUG_DIR}/bing_{sku}_after_cookies.png")
            except:
                pass
            
            found = extract_from_bing(page)
            
            seen = {}
            unique = []
            for r in found:
                if r['name'] not in seen:
                    seen[r['name']] = r
                    unique.append(r)
            found = unique
            
            logger.info(f"   📋 Găsite: {len(found)}")
            
        except Exception as e:
            logger.info(f"   ❌ Error: {str(e)[:50]}")
        finally:
            page.close()
        
        browser.close()
    
    for r in found:
        r['diff'] = round(((r['price'] - your_price) / your_price) * 100, 1) if your_price > 0 else 0
    
    found.sort(key=lambda x: x['price'])
    return found[:5]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    data = request.json
    your_price = float(data.get('price', 0) or 0)
    results = scan_product(data.get('sku', ''), data.get('name', ''), your_price)
    return jsonify({"status": "success", "competitors": results})

@app.route('/debug/<filename>')
def get_debug(filename):
    filepath = f"{DEBUG_DIR}/{filename}"
    if os.path.exists(filepath):
        return send_file(filepath)
    return "Not found", 404

if __name__ == '__main__':
    logger.info("🚀 PriceMonitor v7.8 (Debug) pe :8080")
    app.run(host='0.0.0.0', port=8080)
