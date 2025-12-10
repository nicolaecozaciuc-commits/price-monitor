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
            time.sleep(2)
            
            # Accept cookies PRIMUL
            try:
                page.click('#bnp_btn_accept', timeout=3000)
                time.sleep(2)
            except:
                pass
            
            # Screenshot DUPĂ cookies
            page.screenshot(path=f"{DEBUG_DIR}/bing_{sku}.png")
            
            # Extrage TOATE rezultatele .b_algo
            results = page.locator('.b_algo').all()
            logger.info(f"   📊 Rezultate Bing: {len(results)}")
            
            for i, result in enumerate(results[:10]):
                try:
                    # Extrage link și text
                    text = result.inner_text()
                    
                    # Afișează primele 3
                    if i < 3:
                        logger.info(f"   [{i}] {text[:120]}...")
                    
                    # Extrage URL
                    links = result.locator('a').all()
                    href = ""
                    for link in links:
                        h = link.get_attribute('href') or ''
                        if '.ro' in h and 'bing' not in h.lower():
                            href = h
                            break
                    
                    if not href:
                        continue
                    
                    # Extrage domain
                    domain_match = re.search(r'https?://(?:www\.)?([a-z0-9-]+\.ro)', href.lower())
                    if not domain_match:
                        continue
                    domain = domain_match.group(1)
                    
                    # Skip blocked
                    blocked = ['google', 'bing', 'microsoft', 'facebook', 'youtube', 'doarbai', 'termohabitat', 'kaufland']
                    if any(b in domain for b in blocked):
                        continue
                    
                    # Caută preț în text
                    price_match = re.search(r'([\d.,]+)\s*(?:RON|Lei|ron|lei)', text)
                    if price_match:
                        price = clean_price(price_match.group(1))
                        if price > 0:
                            found.append({
                                'name': domain,
                                'price': price,
                                'url': href,
                                'method': 'Bing'
                            })
                            logger.info(f"   ✅ {domain}: {price} Lei")
                            
                except Exception as e:
                    logger.info(f"   ❌ Error result {i}: {str(e)[:50]}")
            
            logger.info(f"   📋 Total găsite: {len(found)}")
            
        except Exception as e:
            logger.info(f"   ❌ Error: {str(e)[:50]}")
        finally:
            page.close()
        
        browser.close()
    
    # Deduplicate
    seen = {}
    unique = []
    for r in found:
        if r['name'] not in seen:
            seen[r['name']] = r
            unique.append(r)
    
    for r in unique:
        r['diff'] = round(((r['price'] - your_price) / your_price) * 100, 1) if your_price > 0 else 0
    
    unique.sort(key=lambda x: x['price'])
    return unique[:5]

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
    logger.info("🚀 PriceMonitor v8.0 (Simple Debug) pe :8080")
    app.run(host='0.0.0.0', port=8080)
