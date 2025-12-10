import re
import logging
import time
import random
import unicodedata
import json
from urllib.parse import quote_plus
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__, template_folder='templates')
CORS(app)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

SITES = {
    'foglia.ro': 'https://www.foglia.ro/catalogsearch/result/?q={}',
    'bagno.ro': 'https://www.bagno.ro/catalogsearch/result/?q={}',
    'absulo.ro': 'https://www.absulo.ro/catalogsearch/result/?q={}',
    'sensodays.ro': 'https://www.sensodays.ro/catalogsearch/result/?q={}',
    'germanquality.ro': 'https://www.germanquality.ro/catalogsearch/result/?q={}',
    'romstal.ro': 'https://www.romstal.ro/cautare?q={}',
    'dedeman.ro': 'https://www.dedeman.ro/ro/cautare?q={}',
    'hornbach.ro': 'https://www.hornbach.ro/s/{}',
}

def normalize(text):
    if not text: return ""
    text = unicodedata.normalize('NFD', text).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]', '', text.lower())

def clean_price(value):
    if not value: return 0
    if isinstance(value, (int, float)):
        return float(value) if value > 10 else 0
    text = str(value).lower()
    if any(x in text for x in ['luna', 'rata', 'transport']):
        return 0
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

def extract_price(page):
    # JSON-LD
    try:
        for script in page.locator('script[type="application/ld+json"]').all()[:3]:
            try:
                data = json.loads(script.inner_text())
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get('@type') == 'Product':
                        offers = item.get('offers', {})
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        price = offers.get('price') or offers.get('lowPrice')
                        if price:
                            p = clean_price(price)
                            if p > 0:
                                return p, 'JSON-LD'
            except:
                continue
    except:
        pass
    
    # META
    for sel in ['meta[property="product:price:amount"]', 'meta[property="og:price:amount"]']:
        try:
            p = clean_price(page.locator(sel).first.get_attribute('content'))
            if p > 0:
                return p, 'META'
        except:
            pass
    
    # CSS
    for sel in ['[data-price-amount]', '.price-new', '.current-price', '.price']:
        try:
            el = page.locator(sel).first
            p = clean_price(el.get_attribute('data-price-amount') or el.inner_text())
            if p > 0:
                return p, 'CSS'
        except:
            pass
    
    return 0, None

def scrape_site(context, domain, search_url, sku):
    page = None
    sku_lower = sku.lower()
    sku_norm = normalize(sku)
    
    try:
        page = context.new_page()
        url = search_url.format(quote_plus(sku))
        
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        time.sleep(2)
        
        # Accept cookies rapid
        try:
            page.click('button:has-text("Accept")', timeout=500)
        except:
            pass
        
        # Caută link cu SKU în URL
        product_url = None
        try:
            for link in page.locator('a[href]').all()[:50]:
                href = link.get_attribute('href') or ''
                if sku_lower in href.lower() or sku_norm in normalize(href):
                    if not any(x in href for x in ['cart', 'login', 'mailto']):
                        if href.startswith('/'):
                            href = f"https://www.{domain}{href}"
                        if domain in href:
                            product_url = href
                            break
        except:
            pass
        
        if product_url:
            logger.info(f"   🔗 {domain}: link găsit")
            page.goto(product_url, timeout=15000, wait_until='domcontentloaded')
            time.sleep(1)
            
            price, method = extract_price(page)
            if price > 0:
                return {'name': domain, 'price': price, 'url': product_url, 'method': method}
        
        logger.info(f"   ⚪ {domain}: negăsit")
        
    except Exception as e:
        logger.info(f"   ❌ {domain}: timeout/error")
    finally:
        if page:
            page.close()
    
    return None

def scan_product(sku, name, your_price=0):
    found = []
    sku = str(sku).strip()
    
    logger.info(f"🔎 {sku} - {name[:35]}...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        
        for domain, search_url in SITES.items():
            result = scrape_site(context, domain, search_url, sku)
            
            if result:
                if your_price > 0:
                    result['diff'] = round(((result['price'] - your_price) / your_price) * 100, 1)
                else:
                    result['diff'] = 0
                found.append(result)
                logger.info(f"   ✅ {domain}: {result['price']} Lei")
            
            time.sleep(0.3)
        
        browser.close()
    
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

if __name__ == '__main__':
    logger.info("🚀 PriceMonitor v5.1 (Fast) pe :8080")
    app.run(host='0.0.0.0', port=8080)
