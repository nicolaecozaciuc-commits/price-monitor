import re
import logging
import time
import random
import unicodedata
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__, template_folder='templates')
CORS(app)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

COMPETITORS = {
    'Dedeman': {'url': 'https://www.dedeman.ro/ro/cautare?q={}', 'card': '.product-item', 'price': '.product-price', 'name': '.product-title', 'link': 'a.product-title'},
    'eMAG': {'url': 'https://www.emag.ro/search/{}', 'card': '.card-item', 'price': '.product-new-price', 'name': '.card-v2-title', 'link': 'a.card-v2-title'},
    'Hornbach': {'url': 'https://www.hornbach.ro/s/{}', 'card': 'article', 'price': '.price-container', 'name': 'h2', 'link': 'a'},
    'LeroyMerlin': {'url': 'https://www.leroymerlin.ro/search/{}', 'card': 'app-product-card', 'price': '.price-container', 'name': 'a[title]', 'link': 'a[title]'},
    'Romstal': {'url': 'https://www.romstal.ro/cautare.html?q={}', 'card': '.product-item', 'price': '.product-price', 'name': '.product-title', 'link': 'a.product-title'},
    'BricoDepot': {'url': 'https://www.bricodepot.ro/cautare/?q={}', 'card': '.product-item', 'price': '.price-box', 'name': '.product-name', 'link': 'a.product-name'},
    'Obsentum': {'url': 'https://obsentum.com/catalogsearch/result/?q={}', 'card': '.product-item', 'price': '.price', 'name': '.product-item-link', 'link': '.product-item-link'},
    'Sanex': {'url': 'https://www.sanex.ro/index.php?route=product/search&search={}', 'card': '.product-layout', 'price': '.price', 'name': 'h4 a', 'link': 'h4 a'},
    'GemiBai': {'url': 'https://store.gemibai.ro/index.php?route=product/search&search={}', 'card': '.product-thumb', 'price': '.price', 'name': '.caption h4 a', 'link': '.caption h4 a'}
}

def normalize_text(text):
    if not text:
        return ""
    text = unicodedata.normalize('NFD', text).encode('ascii', 'ignore').decode()
    return text.lower().strip()

def clean_price(text):
    if not text:
        return 0
    text_lower = text.lower()
    if any(x in text_lower for x in ['luna', 'rata', 'transport', 'livrare', '/luna', 'lei/']):
        return 0
    matches = re.findall(r'(\d[\d\.,]*)', text)
    if not matches:
        return 0
    prices = []
    for m in matches:
        p = m.replace('.', '').replace(',', '.')
        try:
            prices.append(float(p))
        except:
            pass
    prices = [p for p in prices if p > 10]
    return max(prices) if prices else 0

def validate_match(sku, target_name, found_name):
    sku = normalize_text(str(sku))
    found_name = normalize_text(found_name)
    target_name = normalize_text(target_name)
    
    if len(sku) > 3:
        if re.search(r'\b' + re.escape(sku) + r'\b', found_name):
            return True
        if found_name.startswith(sku) or found_name.endswith(sku):
            return True
    
    stop_words = {'pentru', 'cm', 'alb', 'alba', 'negru', 'cu', 'de', 'si', 'la', 'din', 'x', 'mm'}
    target_parts = [w for w in target_name.split() if w not in stop_words and len(w) > 2][:4]
    
    matches = sum(1 for part in target_parts if part in found_name)
    return matches >= 2

def safe_goto(page, url, retries=2):
    for i in range(retries):
        try:
            page.goto(url, timeout=20000, wait_until='domcontentloaded')
            return True
        except:
            if i < retries - 1:
                time.sleep(2)
    return False

def scan_direct(sku, name):
    found = []
    search_term = sku if len(str(sku)) > 3 else name
    logger.info(f"🔎 Caut: {search_term}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )

        for site, cfg in COMPETITORS.items():
            page = None
            try:
                page = context.new_page()
                url = cfg['url'].format(search_term)
                
                if not safe_goto(page, url):
                    logger.warning(f"   ⚠️ {site}: timeout")
                    page.close()
                    continue

                try:
                    page.click('button:has-text("Accept")', timeout=1500)
                except:
                    pass
                
                time.sleep(random.uniform(1.5, 3))

                cards = page.locator(cfg['card']).all()
                best = None

                for card in cards[:3]:
                    try:
                        raw_name = card.locator(cfg['name']).first.inner_text()
                        if validate_match(sku, name, raw_name):
                            raw_price = card.locator(cfg['price']).first.inner_text()
                            price = clean_price(raw_price)
                            
                            if price > 0:
                                try:
                                    href = card.locator(cfg['link']).first.get_attribute('href')
                                    link = href if href and href.startswith('http') else url
                                except:
                                    link = url

                                if best is None or price < best['price']:
                                    best = {"name": site, "price": price, "url": link}
                    except:
                        continue
                
                if best:
                    found.append(best)
                    logger.info(f"   ✅ {site}: {best['price']} Lei")
                    
            except Exception as e:
                logger.error(f"   ❌ {site}: {str(e)[:40]}")
            finally:
                if page:
                    page.close()

        browser.close()

    found.sort(key=lambda x: x['price'])
    return found[:5]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    d = request.json
    results = scan_direct(d.get('sku', ''), d.get('name', ''))
    return jsonify({"status": "success", "competitors": results})

if __name__ == '__main__':
    logger.info("🚀 PriceMonitor v1.5 pornit pe :8080")
    app.run(host='0.0.0.0', port=8080)
    
