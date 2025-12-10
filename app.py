import re
import logging
import time
import random
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__, template_folder='templates')
CORS(app)

# Configurare Logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

# --- LISTA COMPLETĂ DE COMPETITORI (9 SITE-URI) ---
COMPETITORS = {
    'Dedeman': {
        'url': 'https://www.dedeman.ro/ro/cautare?q={}',
        'container': '.product-item',
        'price': '.product-price',
        'name': '.product-title',
        'link': 'a.product-title'
    },
    'eMAG': {
        'url': 'https://www.emag.ro/search/{}',
        'container': '.card-item',
        'price': '.product-new-price',
        'name': '.card-v2-title',
        'link': 'a.card-v2-title'
    },
    'Hornbach': {
        'url': 'https://www.hornbach.ro/s/{}',
        'container': 'article',
        'price': '.price-container',
        'name': 'h2',
        'link': 'a'
    },
    'LeroyMerlin': {
        'url': 'https://www.leroymerlin.ro/search/{}',
        'container': 'app-product-card',
        'price': '.price-container',
        'name': 'a[title]',
        'link': 'a[title]'
    },
    'Romstal': {
        'url': 'https://www.romstal.ro/cautare.html?q={}',
        'container': '.product-item',
        'price': '.product-price',
        'name': '.product-title',
        'link': 'a.product-title'
    },
    'BricoDepot': {
        'url': 'https://www.bricodepot.ro/cautare/?q={}',
        'container': '.product-item',
        'price': '.price-box',
        'name': '.product-name',
        'link': 'a.product-name'
    },
    'Obsentum': {
        'url': 'https://obsentum.com/catalogsearch/result/?q={}',
        'container': '.product-item',
        'price': '.price',
        'name': '.product-item-link',
        'link': '.product-item-link'
    },
    'Sanex': {
        'url': 'https://www.sanex.ro/index.php?route=product/search&search={}',
        'container': '.product-layout',
        'price': '.price',
        'name': 'h4 a',
        'link': 'h4 a'
    },
    'GemiBai': {
        'url': 'https://store.gemibai.ro/index.php?route=product/search&search={}',
        'container': '.product-thumb',
        'price': '.price',
        'name': '.caption h4 a',
        'link': '.caption h4 a'
    }
}

def clean_price(text):
    if not text: return 0
    matches = re.findall(r'(\d[\d\.,]*)', text)
    if not matches: return 0
    price_str = max(matches, key=len).replace('.', '').replace(',', '.')
    try: return float(price_str)
    except: return 0

def validate_match(sku, target_name, found_name):
    """Algoritm de validare pentru exactitate"""
    sku = sku.lower().strip()
    target_parts = target_name.lower().split()[:3] # Primele 3 cuvinte
    found_name = found_name.lower()

    # 1. SKU Match (Perfect)
    if len(sku) > 3 and sku in found_name: return True
    
    # 2. Name Match (Partial - min 2 cuvinte cheie)
    matches = sum(1 for part in target_parts if part in found_name)
    if matches >= 2: return True
    
    return False

def scan_product(sku, name):
    found_competitors = []
    # Dacă SKU e prea scurt, folosim numele pentru căutare
    search_term = sku if len(sku) > 3 else name
    
    logger.info(f"🔎 START SCANARE: {search_term}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Context persistent
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )

        for site, cfg in COMPETITORS.items():
            try:
                page = context.new_page()
                url = cfg['url'].format(search_term)
                
                try:
                    page.goto(url, timeout=20000, wait_until='domcontentloaded')
                except:
                    page.close(); continue

                # Anti-bot simplu
                try: 
                    page.click('button:has-text("Accept")', timeout=1000)
                    page.click('a.cc-btn', timeout=500) 
                except: pass
                
                # Așteptare dinamică
                time.sleep(random.uniform(2, 5))

                # Extragere
                cards = page.locator(cfg['container']).all()
                best_match = None

                # Verificăm primele 3 rezultate
                for card in cards[:3]:
                    try:
                        raw_name = card.locator(cfg['name']).first.inner_text()
                        if validate_match(sku, name, raw_name):
                            raw_price = card.locator(cfg['price']).first.inner_text()
                            price = clean_price(raw_price)
                            
                            try:
                                href = card.locator(cfg['link']).first.get_attribute('href')
                                link = href if href.startswith('http') else f"https://www.{site.lower()}.ro{href}" if 'www' not in href else href
                            except: link = url

                            if price > 0:
                                if best_match is None or price < best_match['price']:
                                    best_match = {
                                        "name": site,
                                        "price": price,
                                        "url": link,
                                        "details": raw_name[:40]+"..."
                                    }
                    except: continue
                
                if best_match:
                    found_competitors.append(best_match)
                    logger.info(f"   ✅ {site}: {best_match['price']} Lei")
                
                page.close()
                
            except Exception as e:
                logger.error(f"   ❌ Eroare {site}: {e}")

        browser.close()

    # Sortăm după preț (Crescator) și returnăm TOP 5
    found_competitors.sort(key=lambda x: x['price'])
    return found_competitors[:5]

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    d = request.json
    results = scan_product(d.get('sku',''), d.get('name',''))
    return jsonify({"status": "success", "competitors": results})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
