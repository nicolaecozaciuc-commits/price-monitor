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
            
            # Accept cookies
            try:
                page.click('#bnp_btn_accept', timeout=3000)
                time.sleep(2)
            except:
                pass
            
            # Screenshot
            page.screenshot(path=f"{DEBUG_DIR}/bing_{sku}.png")
            
            # Salvează HTML pentru debug
            html = page.content()
            with open(f"{DEBUG_DIR}/bing_{sku}.html", 'w') as f:
                f.write(html)
            
            # METODA NOUĂ: Extrage din HTML complet
            # Caută pattern: domain.ro ... preț RON
            
            # Găsește toate linkurile .ro cu prețuri în apropiere
            blocked = ['google', 'bing', 'microsoft', 'facebook', 'youtube', 'doarbai', 
                      'termohabitat', 'kaufland', 'wikipedia', 'emag']
            
            # Pattern: URL .ro urmat de text care conține preț
            # Sau preț urmat de domain .ro
            
            body_text = page.locator('body').inner_text()
            
            # Salvează text pentru debug
            with open(f"{DEBUG_DIR}/bing_{sku}.txt", 'w') as f:
                f.write(body_text)
            
            # Găsește toate domeniile .ro valide
            domains = re.findall(r'(?:https?://)?(?:www\.)?([a-z0-9-]+\.ro)', body_text.lower())
            valid_domains = []
            for d in domains:
                if len(d) > 5 and not any(b in d for b in blocked):
                    if d not in valid_domains:
                        valid_domains.append(d)
            
            logger.info(f"   🌐 Domenii găsite: {valid_domains[:8]}")
            
            # Găsește toate prețurile
            prices = re.findall(r'([\d.,]+)\s*(?:RON|Lei|ron|lei)', body_text)
            valid_prices = [clean_price(p) for p in prices]
            valid_prices = [p for p in valid_prices if p > 0]
            
            logger.info(f"   💰 Prețuri găsite: {valid_prices[:8]}")
            
            # Asociază: pentru fiecare domain, caută prețul cel mai aproape
            for domain in valid_domains[:10]:
                # Găsește poziția domeniului în text
                pos = body_text.lower().find(domain)
                if pos == -1:
                    continue
                
                # Caută preț în zona ±300 caractere
                start = max(0, pos - 300)
                end = min(len(body_text), pos + 300)
                context_text = body_text[start:end]
                
                price_match = re.search(r'([\d.,]+)\s*(?:RON|Lei|ron|lei)', context_text)
                if price_match:
                    price = clean_price(price_match.group(1))
                    if price > 0:
                        # Verifică să nu fie duplicat
                        if not any(r['name'] == domain for r in found):
                            found.append({
                                'name': domain,
                                'price': price,
                                'url': f"https://www.{domain}",
                                'method': 'Bing'
                            })
                            logger.info(f"   ✅ {domain}: {price} Lei")
            
            logger.info(f"   📋 Total: {len(found)}")
            
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
    logger.info("🚀 PriceMonitor v8.1 (Full Text Extract) pe :8080")
    app.run(host='0.0.0.0', port=8080)
