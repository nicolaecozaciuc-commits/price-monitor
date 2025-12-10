import re
import logging
import time
import random
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__, template_folder='templates')
CORS(app)

# Logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

# --- CONFIGURARE COMPETITORI (Direct Scraping) ---
COMPETITORS = {
    'Dedeman': {
        'search_url': 'https://www.dedeman.ro/ro/cautare?q={}',
        'selector_card': '.product-item',
        'selector_price': '.product-price',
        'selector_name': '.product-title'
    },
    'eMAG': {
        'search_url': 'https://www.emag.ro/search/{}',
        'selector_card': '.card-item',
        'selector_price': '.product-new-price',
        'selector_name': '.card-v2-title'
    },
    'LeroyMerlin': {
        'search_url': 'https://www.leroymerlin.ro/search/{}',
        'selector_card': 'app-product-card',
        'selector_price': '.price-container',
        'selector_name': 'a[title]'
    },
    'Hornbach': {
        'search_url': 'https://www.hornbach.ro/s/{}',
        'selector_card': 'article',
        'selector_price': '.price-container',
        'selector_name': 'h2'
    },
    'BricoDepot': { # Fostul Bricostore/Praktiker
        'search_url': 'https://www.bricodepot.ro/cautare/?q={}',
        'selector_card': '.product-item',
        'selector_price': '.price-box',
        'selector_name': '.product-name'
    },
    'Obsentum': {
        'search_url': 'https://obsentum.com/catalogsearch/result/?q={}',
        'selector_card': '.product-item',
        'selector_price': '.price',
        'selector_name': '.product-item-link'
    },
    'Sanex': {
        'search_url': 'https://www.sanex.ro/index.php?route=product/search&search={}',
        'selector_card': '.product-layout',
        'selector_price': '.price',
        'selector_name': 'h4 a'
    },
    'GemiBai': {
        'search_url': 'https://store.gemibai.ro/index.php?route=product/search&search={}',
        'selector_card': '.product-thumb',
        'selector_price': '.price',
        'selector_name': '.caption h4 a'
    }
}

def clean_price(price_text):
    """Curăță prețul: '1.200,99 Lei' -> 1200.99"""
    if not price_text: return 0
    matches = re.findall(r'(\d[\d\.,]*)', price_text)
    if not matches: return 0
    
    # Luăm cea mai lungă secvență numerică găsită
    price_str = max(matches, key=len)
    price_str = price_str.replace('.', '').replace(',', '.')
    
    try:
        return float(price_str)
    except:
        return 0

def scrape_direct(sku, product_name=""):
    """
    Navighează direct pe site-urile competitorilor și caută produsul.
    """
    results = []
    
    # Termenul de căutare: Preferăm SKU-ul, dacă nu, Numele
    search_term = sku if len(sku) > 3 else product_name
    if not search_term: return []

    logger.info(f"🔍 Încep scanarea directă pentru: {search_term}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        for site_name, config in COMPETITORS.items():
            try:
                # Context nou pentru fiecare site (ca un tab incognito curat)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
                    viewport={'width': 1920, 'height': 1080}
                )
                page = context.new_page()
                
                url = config['search_url'].format(search_term)
                logger.info(f"   🌐 {site_name}: Accesez {url}...")
                
                try:
                    page.goto(url, timeout=20000, wait_until='domcontentloaded')
                except:
                    logger.warning(f"   ⚠️ {site_name}: Timeout la încărcare.")
                    context.close()
                    continue

                # Gestionare cookies
                try: 
                    page.click('button:has-text("Accept")', timeout=1500)
                    page.click('button:has-text("Sunt de acord")', timeout=1000)
                except: pass

                # Așteptare strategică (10-15s cum ai cerut, pentru siguranță)
                delay = random.uniform(10, 15)
                time.sleep(delay)

                # 1. Căutare Selector Specific
                found = False
                try:
                    if page.locator(config['selector_card']).count() > 0:
                        first_product = page.locator(config['selector_card']).first
                        raw_price = first_product.locator(config['selector_price']).first.inner_text()
                        
                        price = clean_price(raw_price)
                        if price > 0:
                            results.append({
                                "id": abs(hash(site_name + search_term)),
                                "name": site_name,
                                "price": price,
                                "url": page.url
                            })
                            logger.info(f"   ✅ {site_name}: {price} Lei")
                            found = True
                except Exception as e:
                    pass

                # 2. Fallback Generic (Dacă selectorul nu a mers, cautăm în textul paginii)
                if not found:
                    body_text = page.inner_text("body")[:3000]
                    prices = re.findall(r'(\d+[\.,]\d{2})\s*(?:lei|ron)', body_text, re.IGNORECASE)
                    if prices:
                        price = clean_price(prices[0])
                        if price > 0:
                            results.append({
                                "id": abs(hash(site_name)),
                                "name": site_name,
                                "price": price,
                                "url": page.url
                            })
                            logger.info(f"   ✅ {site_name}: {price} Lei (Generic)")

                context.close()
                
            except Exception as e:
                logger.error(f"   ❌ Eroare {site_name}: {e}")

        browser.close()

    return results

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    data = request.json
    sku = data.get('sku', '')
    name = data.get('name', '')
    
    # Apelăm funcția de scraping direct
    competitors = scrape_direct(sku, name)
    
    # Sortăm după preț
    competitors.sort(key=lambda x: x['price'])
    
    return jsonify({
        "status": "success",
        "sku": sku,
        "competitors": competitors
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
