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
        return price if 10 < price < 500000 else 0
    except:
        return 0

def extract_prices_from_html(html_content, page_text):
    """Extrage prețuri din HTML"""
    results = []
    
    # Pattern: preț urmat de RON/Lei
    patterns = [
        r'([\d\s.,]+)\s*(?:RON|Lei|lei)',
        r'(?:RON|Lei|lei)\s*([\d\s.,]+)',
        r'(\d{2,6}[.,]\d{2})\s*(?:RON|Lei|lei|Ron)',
    ]
    
    for pattern in patterns:
        for match in re.finditer(pattern, page_text):
            price = clean_price(match.group(1))
            if price > 50:  # Minimum plauzibil
                results.append(price)
    
    # Găsește domenii .ro
    domains = re.findall(r'(?:https?://)?(?:www\.)?([a-z0-9-]+\.ro)', html_content.lower())
    domains = [d for d in domains if 'google' not in d and 'doarbai' not in d]
    
    return list(set(results)), list(set(domains))

def search_and_debug(context, engine, url, sku):
    """Caută și salvează debug info"""
    page = None
    results = []
    
    try:
        page = context.new_page()
        
        logger.info(f"   🔍 {engine}: căutare...")
        
        page.goto(url, timeout=20000, wait_until='domcontentloaded')
        time.sleep(3)
        
        # Salvează screenshot
        screenshot_path = f"{DEBUG_DIR}/{engine.lower()}_{sku}.png"
        page.screenshot(path=screenshot_path, full_page=False)
        logger.info(f"   📸 Screenshot: {screenshot_path}")
        
        # Salvează HTML
        html_content = page.content()
        html_path = f"{DEBUG_DIR}/{engine.lower()}_{sku}.html"
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        # Salvează text vizibil
        page_text = page.locator('body').inner_text()
        text_path = f"{DEBUG_DIR}/{engine.lower()}_{sku}.txt"
        with open(text_path, 'w', encoding='utf-8') as f:
            f.write(page_text)
        
        # Verifică CAPTCHA
        if 'unusual traffic' in html_content.lower() or 'captcha' in html_content.lower():
            logger.info(f"   ⚠️ {engine} CAPTCHA detectat")
            return []
        
        # Extrage prețuri și domenii
        prices, domains = extract_prices_from_html(html_content, page_text)
        
        logger.info(f"   💰 Prețuri găsite: {prices[:10]}")
        logger.info(f"   🌐 Domenii găsite: {domains[:10]}")
        
        # Încearcă să asocieze prețuri cu domenii
        # Caută în text pattern: "preț ... domain" sau "domain ... preț"
        for domain in domains[:10]:
            # Caută prețul cel mai aproape de domain în text
            domain_pos = page_text.lower().find(domain)
            if domain_pos == -1:
                continue
            
            # Caută preț în jur de ±500 caractere
            context_start = max(0, domain_pos - 500)
            context_end = min(len(page_text), domain_pos + 500)
            context_text = page_text[context_start:context_end]
            
            price_match = re.search(r'([\d.,]+)\s*(?:RON|Lei)', context_text)
            if price_match:
                price = clean_price(price_match.group(1))
                if price > 50:
                    results.append({
                        'name': domain,
                        'price': price,
                        'url': f"https://www.{domain}",
                        'method': engine
                    })
        
        logger.info(f"   📋 {engine}: {len(results)} rezultate asociate")
        
    except Exception as e:
        logger.info(f"   ❌ {engine} error: {str(e)[:50]}")
    finally:
        if page:
            page.close()
    
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
        
        # Bing
        bing_url = f"https://www.bing.com/search?q={quote_plus(sku + ' pret')}"
        found.extend(search_and_debug(context, 'Bing', bing_url, sku))
        
        # Google (poate da CAPTCHA)
        if len(found) < 3:
            google_url = f"https://www.google.ro/search?q={quote_plus(sku + ' pret')}&hl=ro"
            found.extend(search_and_debug(context, 'Google', google_url, sku))
        
        browser.close()
    
    # Deduplicate
    seen = {}
    for r in found:
        if r['name'] not in seen:
            seen[r['name']] = r
    found = list(seen.values())
    
    # Calculează diferență
    for r in found:
        if your_price > 0:
            r['diff'] = round(((r['price'] - your_price) / your_price) * 100, 1)
        else:
            r['diff'] = 0
    
    found.sort(key=lambda x: x['price'])
    logger.info(f"📊 Total: {len(found)}")
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
    logger.info("🚀 PriceMonitor v7.1 (Debug Mode) pe :8080")
    app.run(host='0.0.0.0', port=8080)
