import re
import logging
import time
import random
import unicodedata
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

# ═══════════════════════════════════════════════════════════════
# SITE-URI PENTRU PRODUSE SANITARE/INSTALAȚII (ACTUALIZAT)
# ═══════════════════════════════════════════════════════════════
COMPETITORS = {
    # MAGAZINE GENERALE
    'Dedeman': {
        'url': 'https://www.dedeman.ro/ro/cautare?q={}',
        'card': '.product-item',
        'price': '.product-price',
        'name': '.product-title',
        'link': 'a.product-title'
    },
    'eMAG': {
        'url': 'https://www.emag.ro/search/{}',
        'card': '.card-item',
        'price': '.product-new-price',
        'name': '.card-v2-title',
        'link': 'a.card-v2-title'
    },
    'Hornbach': {
        'url': 'https://www.hornbach.ro/s/{}',
        'card': 'article',
        'price': '.price-container',
        'name': 'h2',
        'link': 'a'
    },
    
    # MAGAZINE SANITARE SPECIALIZATE
    'Romstal': {
        'url': 'https://www.romstal.ro/cautare?q={}',
        'card': '.product-item-info',
        'price': '.price',
        'name': '.product-item-link',
        'link': '.product-item-link'
    },
    'Sanitino': {
        'url': 'https://www.sanitino.ro/cauta/?q={}',
        'card': '.product-box',
        'price': '.price',
        'name': '.product-title',
        'link': 'a.product-title'
    },
    'SanoTerm': {
        'url': 'https://www.sanoterm.ro/cautare?search={}',
        'card': '.product-layout',
        'price': '.price',
        'name': '.name a',
        'link': '.name a'
    },
    'Novambient': {
        'url': 'https://www.novambient.ro/catalogsearch/result/?q={}',
        'card': '.product-item',
        'price': '.price',
        'name': '.product-item-link',
        'link': '.product-item-link'
    },
    'Neakaisa': {
        'url': 'https://neakaisa.ro/cautare?search={}',
        'card': '.product-thumb',
        'price': '.price',
        'name': '.caption a',
        'link': '.caption a'
    },
    'Absulo': {
        'url': 'https://www.absulo.ro/search/{}',
        'card': '.product-item',
        'price': '.price',
        'name': '.product-title',
        'link': 'a'
    },
    'Obsentum': {
        'url': 'https://obsentum.com/catalogsearch/result/?q={}',
        'card': '.product-item',
        'price': '.price',
        'name': '.product-item-link',
        'link': '.product-item-link'
    },
    'Sanex': {
        'url': 'https://www.sanex.ro/index.php?route=product/search&search={}',
        'card': '.product-layout',
        'price': '.price',
        'name': 'h4 a',
        'link': 'h4 a'
    },
    'PicoShop': {
        'url': 'https://www.picoshop.ro/search?q={}',
        'card': '.product-item',
        'price': '.price',
        'name': '.product-name',
        'link': 'a'
    },
    'InstalShop': {
        'url': 'https://www.instalshop.ro/catalogsearch/result/?q={}',
        'card': '.product-item',
        'price': '.price',
        'name': '.product-item-link',
        'link': '.product-item-link'
    }
}

def normalize_text(text):
    if not text: return ""
    text = unicodedata.normalize('NFD', text).encode('ascii', 'ignore').decode()
    return text.lower().strip()

def clean_price(text):
    if not text: return 0
    text_lower = text.lower()
    if any(x in text_lower for x in ['luna', 'rata', 'transport', 'livrare', '/luna', 'lei/']):
        return 0
    matches = re.findall(r'(\d[\d\.\,]*)', text)
    if not matches: return 0
    prices = []
    for m in matches:
        p = m.replace('.', '').replace(',', '.')
        try:
            val = float(p)
            if val > 10:
                prices.append(val)
        except:
            pass
    return max(prices) if prices else 0

def validate_match(sku, target_name, found_name):
    sku = normalize_text(str(sku))
    found_name = normalize_text(found_name)
    target_name = normalize_text(target_name)
    
    # SKU exact match (prioritate maximă)
    if len(sku) > 3:
        # Caută SKU-ul exact
        if re.search(r'\b' + re.escape(sku) + r'\b', found_name):
            return True
        # Sau conținut parțial
        if sku in found_name:
            return True
    
    # Nume match (backup)
    stop_words = {'pentru', 'cm', 'alb', 'alba', 'negru', 'cu', 'de', 'si', 'la', 'din', 'x', 'mm', 'set', 'ideal', 'standard'}
    target_parts = [w for w in target_name.split() if w not in stop_words and len(w) > 2][:5]
    matches = sum(1 for part in target_parts if part in found_name)
    return matches >= 2

def safe_goto(page, url, retries=2):
    for i in range(retries):
        try:
            page.goto(url, timeout=25000, wait_until='domcontentloaded')
            return True
        except:
            if i < retries - 1:
                time.sleep(2)
    return False

def extract_price_fallback(page):
    """Extrage prețul folosind metode alternative"""
    # Metoda 1: JSON-LD
    try:
        import json
        scripts = page.locator('script[type="application/ld+json"]').all()
        for script in scripts:
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
                            return float(str(price).replace(',', '.'))
            except:
                continue
    except:
        pass
    
    # Metoda 2: Selectoare generice
    generic_selectors = [
        '.price', '.product-price', '.current-price', '.special-price',
        '[data-price]', 'span[itemprop="price"]', '.woocommerce-Price-amount'
    ]
    for selector in generic_selectors:
        try:
            el = page.locator(selector).first
            text = el.inner_text()
            price = clean_price(text)
            if price > 0:
                return price
            # Check data-price attribute
            data_price = el.get_attribute('data-price')
            if data_price:
                return float(data_price)
        except:
            continue
    
    return 0

def scan_direct(sku, name, your_price=0):
    found = []
    search_term = sku if len(str(sku)) > 3 else name.split()[0] if name else sku
    logger.info(f"🔎 Caut: {search_term} ({name[:30]}...)")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )

        for site, cfg in COMPETITORS.items():
            page = None
            try:
                page = context.new_page()
                url = cfg['url'].format(quote_plus(search_term))
                
                if not safe_goto(page, url):
                    logger.warning(f"   ⚠️ {site}: timeout")
                    page.close()
                    continue

                # Accept cookies
                try:
                    page.click('button:has-text("Accept")', timeout=1500)
                except:
                    try:
                        page.click('button:has-text("Acceptă")', timeout=1000)
                    except:
                        pass
                
                time.sleep(random.uniform(1.5, 2.5))

                # Găsește carduri
                cards = page.locator(cfg['card']).all()
                
                if len(cards) == 0:
                    logger.info(f"   ⚪ {site}: 0 carduri")
                    page.close()
                    continue
                
                logger.info(f"   🔍 {site}: {len(cards)} carduri")
                
                best = None
                for card in cards[:5]:
                    try:
                        # Extrage numele produsului
                        try:
                            raw_name = card.locator(cfg['name']).first.inner_text()
                        except:
                            raw_name = card.inner_text()[:100]
                        
                        # Validează match
                        if not validate_match(sku, name, raw_name):
                            continue
                        
                        # Extrage prețul
                        try:
                            raw_price = card.locator(cfg['price']).first.inner_text()
                            price = clean_price(raw_price)
                        except:
                            price = 0
                        
                        # Fallback pentru preț
                        if price <= 0:
                            try:
                                price_el = card.locator('.price, [class*="price"]').first
                                price = clean_price(price_el.inner_text())
                            except:
                                pass
                        
                        if price > 0:
                            # Extrage link
                            try:
                                href = card.locator(cfg['link']).first.get_attribute('href')
                                if href and not href.startswith('http'):
                                    # Construiește URL complet
                                    from urllib.parse import urlparse
                                    base = urlparse(url)
                                    href = f"{base.scheme}://{base.netloc}{href}"
                                link = href if href else url
                            except:
                                link = url

                            if best is None or price < best['price']:
                                best = {"name": site, "price": price, "url": link}
                                logger.info(f"      ✓ Match: {price} Lei")
                    except Exception as e:
                        continue
                
                if best:
                    # Calcul diferență %
                    if your_price > 0:
                        diff = ((best['price'] - your_price) / your_price) * 100
                        best['diff'] = round(diff, 1)
                    else:
                        best['diff'] = 0
                    
                    found.append(best)
                    logger.info(f"   ✅ {site}: {best['price']} Lei ({best['diff']:+.1f}%)")
                    
            except Exception as e:
                logger.error(f"   ❌ {site}: {str(e)[:40]}")
            finally:
                if page:
                    page.close()

        browser.close()

    found.sort(key=lambda x: x['price'])
    logger.info(f"📊 Total: {len(found)} rezultate")
    return found[:5]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    d = request.json
    your_price = float(d.get('price', 0) or 0)
    results = scan_direct(d.get('sku', ''), d.get('name', ''), your_price)
    return jsonify({"status": "success", "competitors": results})

if __name__ == '__main__':
    logger.info("🚀 PriceMonitor v2.3 (13 site-uri sanitare) pornit pe :8080")
    app.run(host='0.0.0.0', port=8080)
