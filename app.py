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

# ═══════════════════════════════════════════════════════════════
# SITE-URI DIRECTE - din Google results pentru produse sanitare
# ═══════════════════════════════════════════════════════════════
SITES = {
    # Site-uri specializate (din Google screenshot)
    'neakaisa.ro': {
        'search': 'https://neakaisa.ro/cautare?search={}',
        'product_link': '.product-thumb a, .product-layout a, a[href*="/produs"]'
    },
    'sanitino.ro': {
        'search': 'https://www.sanitino.ro/cauta/?q={}',
        'product_link': '.product-box a, .product a, a[href*="/p/"]'
    },
    'sensodays.ro': {
        'search': 'https://www.sensodays.ro/catalogsearch/result/?q={}',
        'product_link': '.product-item a, a.product-item-link'
    },
    'foglia.ro': {
        'search': 'https://www.foglia.ro/cauta?q={}',
        'product_link': '.product a, a[href*="/produs"]'
    },
    'bagno.ro': {
        'search': 'https://www.bagno.ro/catalogsearch/result/?q={}',
        'product_link': '.product-item a, a.product-item-link'
    },
    'absulo.ro': {
        'search': 'https://www.absulo.ro/catalogsearch/result/?q={}',
        'product_link': '.product-item a, a.product-item-link'
    },
    'euro-instal.ro': {
        'search': 'https://euro-instal.ro/?s={}&post_type=product',
        'product_link': '.product a, .woocommerce-loop-product__link'
    },
    'germanquality.ro': {
        'search': 'https://www.germanquality.ro/catalogsearch/result/?q={}',
        'product_link': '.product-item a, a.product-item-link'
    },
    'novambient.ro': {
        'search': 'https://www.novambient.ro/catalogsearch/result/?q={}',
        'product_link': '.product-item a, a.product-item-link'
    },
    'hvbtermice.ro': {
        'search': 'https://hvbtermice.ro/?s={}&post_type=product',
        'product_link': '.product a, .woocommerce-loop-product__link'
    },
    # Magazine mari
    'dedeman.ro': {
        'search': 'https://www.dedeman.ro/ro/cautare?q={}',
        'product_link': '.product-item a.product-title'
    },
    'emag.ro': {
        'search': 'https://www.emag.ro/search/{}',
        'product_link': '.card-item a.card-v2-title'
    },
    'romstal.ro': {
        'search': 'https://www.romstal.ro/cautare?q={}',
        'product_link': '.product-item a.product-item-link'
    },
    'hornbach.ro': {
        'search': 'https://www.hornbach.ro/s/{}',
        'product_link': 'article a'
    }
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
    """Extrage preț din pagina produsului"""
    # 1. JSON-LD
    try:
        for script in page.locator('script[type="application/ld+json"]').all():
            try:
                data = json.loads(script.inner_text())
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if '@graph' in item:
                        items.extend(item['@graph'])
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
    
    # 2. Meta tags
    for sel in ['meta[property="product:price:amount"]', 'meta[property="og:price:amount"]']:
        try:
            p = clean_price(page.locator(sel).first.get_attribute('content'))
            if p > 0:
                return p, 'META'
        except:
            pass
    
    # 3. CSS
    for sel in ['[data-price-amount]', '[data-price]', '.product-new-price', '.price-new', '.current-price', '.price']:
        try:
            el = page.locator(sel).first
            for attr in ['data-price-amount', 'data-price', 'content']:
                val = el.get_attribute(attr)
                if val:
                    p = clean_price(val)
                    if p > 0:
                        return p, 'CSS'
            p = clean_price(el.inner_text())
            if p > 0:
                return p, 'CSS'
        except:
            pass
    
    return 0, None

def sku_in_page(sku, page):
    """Verifică STRICT dacă SKU-ul e în pagină"""
    sku_norm = normalize(str(sku))
    if len(sku_norm) < 4:
        return True  # SKU prea scurt, acceptăm
    
    try:
        # Verifică în URL
        url = page.url.lower()
        if sku_norm in normalize(url):
            return True
        
        # Verifică în body text
        body_text = page.locator('body').inner_text()
        body_norm = normalize(body_text)
        
        # SKU exact
        if sku_norm in body_norm:
            return True
        
        # SKU fără prima literă (E306601 -> 306601)
        if sku_norm[1:] in body_norm:
            return True
            
    except:
        pass
    
    return False

def scrape_site(context, domain, config, sku, name):
    """Caută și extrage preț de pe un site"""
    page = None
    try:
        page = context.new_page()
        url = config['search'].format(quote_plus(sku))
        
        page.goto(url, timeout=20000, wait_until='domcontentloaded')
        time.sleep(random.uniform(1.5, 2.5))
        
        # Accept cookies
        for btn in ['Accept', 'Acceptă', 'OK', 'Agree']:
            try:
                page.click(f'button:has-text("{btn}")', timeout=800)
                break
            except:
                pass
        
        # Găsește link-uri produse
        product_links = []
        for selector in config['product_link'].split(', '):
            try:
                links = page.locator(selector).all()
                for link in links[:5]:
                    href = link.get_attribute('href')
                    if href:
                        if not href.startswith('http'):
                            href = f"https://www.{domain}{href}"
                        if href not in product_links:
                            product_links.append(href)
            except:
                pass
        
        if not product_links:
            # Fallback: orice link care pare produs
            try:
                for link in page.locator('a[href]').all()[:20]:
                    href = link.get_attribute('href')
                    if href and ('/p/' in href or '/produs' in href or '/product' in href):
                        if not href.startswith('http'):
                            href = f"https://www.{domain}{href}"
                        if href not in product_links:
                            product_links.append(href)
            except:
                pass
        
        if not product_links:
            logger.info(f"   ⚪ {domain}: 0 produse")
            return None
        
        logger.info(f"   🔍 {domain}: {len(product_links)} link-uri")
        
        # Verifică fiecare produs
        for href in product_links[:3]:
            try:
                page.goto(href, timeout=15000, wait_until='domcontentloaded')
                time.sleep(1)
                
                # VALIDARE STRICTĂ: SKU trebuie să fie în pagină!
                if not sku_in_page(sku, page):
                    logger.info(f"      ✗ SKU absent în: {href[:50]}...")
                    continue
                
                # Extrage preț
                price, method = extract_price(page)
                
                if price > 0:
                    logger.info(f"      ✓ SKU găsit! {price} Lei [{method}]")
                    return {
                        'name': domain,
                        'price': price,
                        'url': href,
                        'method': method
                    }
                    
            except Exception as e:
                continue
        
    except Exception as e:
        logger.debug(f"   ❌ {domain}: {str(e)[:40]}")
    finally:
        if page:
            page.close()
    
    return None

def scan_product(sku, name, your_price=0):
    """Scanează toate site-urile pentru un produs"""
    found = []
    sku = str(sku).strip()
    name = str(name).strip()
    
    logger.info(f"🔎 Scanare: {sku} - {name[:40]}...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale='ro-RO'
        )
        
        for domain, config in SITES.items():
            result = scrape_site(context, domain, config, sku, name)
            
            if result:
                if your_price > 0:
                    diff = ((result['price'] - your_price) / your_price) * 100
                    result['diff'] = round(diff, 1)
                else:
                    result['diff'] = 0
                
                found.append(result)
                logger.info(f"   ✅ {domain}: {result['price']} Lei ({result['diff']:+.1f}%)")
            
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
    logger.info("🚀 PriceMonitor v4.1 (Direct Sites + Strict SKU) pe :8080")
    app.run(host='0.0.0.0', port=8080)
    
