import re
import logging
import time
import random
import unicodedata
import json
from urllib.parse import quote_plus, urlparse
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
    'Dedeman': {
        'url': 'https://www.dedeman.ro/ro/cautare?q={}',
        'card': '.product-item',
    },
    'eMAG': {
        'url': 'https://www.emag.ro/search/{}',
        'card': '.card-item',
    },
    'Romstal': {
        'url': 'https://www.romstal.ro/cautare?q={}',
        'card': '.product-item-info',
    },
    'Obsentum': {
        'url': 'https://obsentum.com/catalogsearch/result/?q={}',
        'card': '.product-item-info',
    },
    'Sanex': {
        'url': 'https://www.sanex.ro/index.php?route=product/search&search={}',
        'card': '.product-layout',
    },
    'Absulo': {
        'url': 'https://www.absulo.ro/catalogsearch/result/?q={}',
        'card': '.product-item-info',
    },
    'Hornbach': {
        'url': 'https://www.hornbach.ro/s/{}',
        'card': 'article',
    },
    'MatHaus': {
        'url': 'https://www.mathaus.ro/cautare/{}',
        'card': '.product-item-info',
    }
}

def normalize_text(text):
    if not text: return ""
    text = unicodedata.normalize('NFD', text).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9\s]', '', text.lower()).strip()

def clean_price(value):
    if not value: return 0
    if isinstance(value, (int, float)):
        return float(value) if value > 10 else 0
    text = str(value).lower()
    if any(x in text for x in ['luna', 'rata', 'transport', '/luna']):
        return 0
    text = re.sub(r'[^\d,.]', '', str(value))
    if not text: return 0
    if ',' in text and '.' in text:
        if text.rindex(',') > text.rindex('.'):
            text = text.replace('.', '').replace(',', '.')
        else:
            text = text.replace(',', '')
    elif ',' in text:
        text = text.replace(',', '.')
    try:
        price = float(text)
        return price if price > 10 else 0
    except:
        return 0

def human_delay():
    time.sleep(random.uniform(1.5, 3))

def extract_from_jsonld(page, target_sku=None):
    try:
        scripts = page.locator('script[type="application/ld+json"]').all()
        for script in scripts:
            try:
                data = json.loads(script.inner_text())
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if '@graph' in item:
                        items.extend(item['@graph'])
                for item in items:
                    if item.get('@type') != 'Product':
                        continue
                    offers = item.get('offers', {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get('price') or offers.get('lowPrice')
                    if price:
                        price_val = clean_price(price)
                        if price_val > 0:
                            return {'price': price_val, 'method': 'JSON-LD'}
            except:
                continue
    except:
        pass
    return None

def extract_price_css(element):
    """Extrage preț din CSS"""
    selectors = [
        '.price', '[class*="price"]', '.product-price',
        '.special-price', '.regular-price', '.price-new',
        'span[data-price-amount]', '[data-price]'
    ]
    for sel in selectors:
        try:
            el = element.locator(sel).first
            # Data attribute
            for attr in ['data-price-amount', 'data-price', 'content']:
                val = el.get_attribute(attr)
                if val:
                    price = clean_price(val)
                    if price > 0:
                        return price
            # Text
            price = clean_price(el.inner_text())
            if price > 0:
                return price
        except:
            continue
    return 0

def validate_match(sku, name, card_text):
    """Validare FOARTE permisivă"""
    sku_norm = normalize_text(str(sku))
    card_norm = normalize_text(card_text)
    name_norm = normalize_text(name)
    
    # 1. SKU în card (orice parte)
    if len(sku_norm) >= 4:
        # Exact
        if sku_norm in card_norm:
            return True, "SKU_EXACT"
        # Primele 5 caractere
        if sku_norm[:5] in card_norm:
            return True, "SKU_PARTIAL"
        # Fără prima literă (uneori E306601 -> 306601)
        if sku_norm[1:] in card_norm:
            return True, "SKU_NO_PREFIX"
    
    # 2. Cuvinte cheie din denumire (min 2)
    stop = {'pentru', 'cm', 'cu', 'de', 'si', 'la', 'din', 'x', 'mm', 'set', 'tip', 'model'}
    keywords = [w for w in name_norm.split() if w not in stop and len(w) > 2]
    matches = sum(1 for kw in keywords if kw in card_norm)
    if matches >= 2:
        return True, f"KEYWORDS_{matches}"
    
    # 3. Brand + tip produs (ex: "ideal standard" + "cada")
    brands = ['ideal', 'grohe', 'roca', 'geberit', 'tece', 'hansgrohe', 'duravit']
    products = ['cada', 'baterie', 'lavoar', 'vas', 'rezervor', 'cadru', 'rama']
    
    brand_match = any(b in card_norm for b in brands if b in name_norm)
    product_match = any(p in card_norm for p in products if p in name_norm)
    
    if brand_match and product_match:
        return True, "BRAND_PRODUCT"
    
    return False, "NO_MATCH"

def scrape_site(context, site_name, config, sku, name):
    page = None
    try:
        page = context.new_page()
        search_term = sku if len(str(sku)) >= 4 else name
        url = config['url'].format(quote_plus(search_term))
        
        try:
            page.goto(url, timeout=25000, wait_until='domcontentloaded')
        except:
            return None
        
        human_delay()
        
        # Accept cookies
        for btn in ['Accept', 'Acceptă', 'OK', 'Agree']:
            try:
                page.click(f'button:has-text("{btn}")', timeout=1000)
                break
            except:
                pass
        
        # Găsește carduri
        cards = page.locator(config['card']).all()
        
        if not cards:
            # Încearcă selectoare alternative
            for alt_sel in ['.product-item', '.product', '.item', '[class*="product"]']:
                cards = page.locator(alt_sel).all()
                if cards:
                    break
        
        if not cards:
            logger.info(f"   ⚪ {site_name}: 0 carduri")
            return None
        
        logger.info(f"   🔍 {site_name}: {len(cards)} carduri")
        
        # DEBUG: Afișează primele 2 carduri
        for i, card in enumerate(cards[:2]):
            try:
                txt = card.inner_text()[:100].replace('\n', ' ')
                logger.info(f"      Card {i}: {txt}...")
            except:
                pass
        
        # Parcurge cardurile
        for idx, card in enumerate(cards[:5]):
            try:
                card_text = card.inner_text()
                
                # Validare
                is_match, match_type = validate_match(sku, name, card_text)
                
                if not is_match:
                    continue
                
                logger.info(f"      ✓ Match [{match_type}]")
                
                # Extrage link și navighează
                try:
                    link_el = card.locator('a').first
                    href = link_el.get_attribute('href')
                    
                    if href:
                        if not href.startswith('http'):
                            parsed = urlparse(url)
                            href = f"{parsed.scheme}://{parsed.netloc}{href}"
                        
                        # Mergi la pagina produsului
                        page.goto(href, timeout=20000, wait_until='domcontentloaded')
                        human_delay()
                        
                        # Extrage preț din JSON-LD
                        result = extract_from_jsonld(page)
                        if result:
                            logger.info(f"      ✓ JSON-LD: {result['price']} Lei")
                            return {
                                'name': site_name,
                                'price': result['price'],
                                'url': href,
                                'method': 'JSON-LD'
                            }
                        
                        # Fallback: CSS pe pagina produsului
                        price = extract_price_css(page.locator('body'))
                        if price > 0:
                            logger.info(f"      ✓ CSS: {price} Lei")
                            return {
                                'name': site_name,
                                'price': price,
                                'url': href,
                                'method': 'CSS'
                            }
                except:
                    pass
                
                # Fallback: preț din card
                price = extract_price_css(card)
                if price > 0:
                    try:
                        href = card.locator('a').first.get_attribute('href') or url
                        if not href.startswith('http'):
                            parsed = urlparse(url)
                            href = f"{parsed.scheme}://{parsed.netloc}{href}"
                    except:
                        href = url
                    
                    return {
                        'name': site_name,
                        'price': price,
                        'url': href,
                        'method': 'CSS-CARD'
                    }
                    
            except:
                continue
        
    except Exception as e:
        logger.error(f"   ❌ {site_name}: {str(e)[:50]}")
    finally:
        if page:
            page.close()
    
    return None

def scan_product(sku, name, your_price=0):
    found = []
    sku = str(sku).strip()
    name = str(name).strip()
    
    logger.info(f"🔎 Caut: {sku} - {name[:40]}...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale='ro-RO'
        )
        
        for site_name, config in COMPETITORS.items():
            result = scrape_site(context, site_name, config, sku, name)
            
            if result:
                if your_price > 0:
                    diff = ((result['price'] - your_price) / your_price) * 100
                    result['diff'] = round(diff, 1)
                else:
                    result['diff'] = 0
                
                found.append(result)
                logger.info(f"   ✅ {site_name}: {result['price']} Lei ({result['diff']:+.1f}%)")
            
            time.sleep(random.uniform(0.5, 1.5))
        
        browser.close()
    
    found.sort(key=lambda x: x['price'])
    logger.info(f"📊 Total: {len(found)} rezultate")
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
    logger.info("🚀 PriceMonitor v3.1 (DEBUG MODE) pe :8080")
    app.run(host='0.0.0.0', port=8080)
