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
# SITE-URI ACTUALIZATE - din Google results
# ═══════════════════════════════════════════════════════════════
SITES = {
    # Site-uri cu produse Ideal Standard (din Google)
    'instalatiaz.ro': {
        'search': 'https://www.instalatiaz.ro/cautare?q={}',
        'alt_search': 'https://www.instalatiaz.ro/?s={}'
    },
    'foglia.ro': {
        'search': 'https://www.foglia.ro/catalogsearch/result/?q={}',
    },
    'bagno.ro': {
        'search': 'https://www.bagno.ro/catalogsearch/result/?q={}',
    },
    'decostores.ro': {
        'search': 'https://www.decostores.ro/catalogsearch/result/?q={}',
    },
    'vasetoaleta.ro': {
        'search': 'https://www.vasetoaleta.ro/catalogsearch/result/?q={}',
    },
    'compari.ro': {
        'search': 'https://www.compari.ro/search.html?search_query={}',
    },
    # Site-uri din sesiunea anterioară
    'sensodays.ro': {
        'search': 'https://www.sensodays.ro/catalogsearch/result/?q={}',
    },
    'germanquality.ro': {
        'search': 'https://www.germanquality.ro/catalogsearch/result/?q={}',
    },
    'absulo.ro': {
        'search': 'https://www.absulo.ro/catalogsearch/result/?q={}',
    },
    'novambient.ro': {
        'search': 'https://www.novambient.ro/catalogsearch/result/?q={}',
    },
    'neakaisa.ro': {
        'search': 'https://neakaisa.ro/index.php?route=product/search&search={}',
    },
    'sanitino.ro': {
        'search': 'https://www.sanitino.ro/cauta/?q={}',
    },
    # Magazine mari
    'dedeman.ro': {
        'search': 'https://www.dedeman.ro/ro/cautare?q={}',
    },
    'emag.ro': {
        'search': 'https://www.emag.ro/search/{}',
    },
    'romstal.ro': {
        'search': 'https://www.romstal.ro/cautare?q={}',
    },
    'hornbach.ro': {
        'search': 'https://www.hornbach.ro/s/{}',
    },
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
    """Extrage preț - JSON-LD > META > CSS"""
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
    
    # 3. CSS - mai multe selectoare
    selectors = [
        '[data-price-amount]', '[data-price]', 'span[itemprop="price"]',
        '.product-new-price', '.price-new', '.current-price', '.special-price .price',
        '.product-price', '.price-box .price', '.price-wrapper .price',
        '.price', '[class*="price"]:not([class*="old"]):not([class*="regular"])'
    ]
    for sel in selectors:
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
    """Verifică dacă SKU-ul e în pagină"""
    sku_norm = normalize(str(sku))
    if len(sku_norm) < 4:
        return True
    
    try:
        # În URL
        if sku_norm in normalize(page.url):
            return True
        
        # În body
        body = page.locator('body').inner_text()
        body_norm = normalize(body)
        
        if sku_norm in body_norm:
            return True
        if sku_norm[1:] in body_norm:  # Fără prima literă
            return True
        # Primele 5 caractere
        if len(sku_norm) >= 5 and sku_norm[:5] in body_norm:
            return True
    except:
        pass
    
    return False

def find_product_links(page, domain):
    """Găsește link-uri către produse în pagina de căutare"""
    links = []
    
    # Selectoare generice pentru link-uri produse
    selectors = [
        '.product-item a.product-item-link',
        '.product-item-info a',
        '.product a',
        '.product-layout a',
        '.product-thumb a',
        '.card-item a',
        'a[href*="/p/"]',
        'a[href*="/produs/"]', 
        'a[href*="/product/"]',
        'a[href*="-p-"]',
        '.products-grid a',
        '.product-name a',
        'h2 a', 'h3 a', 'h4 a'  # Titluri de produse
    ]
    
    for sel in selectors:
        try:
            for link in page.locator(sel).all()[:10]:
                href = link.get_attribute('href')
                if not href:
                    continue
                
                # Skip linkuri invalide
                if any(x in href.lower() for x in ['/cart', '/login', '/account', '/wishlist', 'javascript:', '#']):
                    continue
                
                # Construiește URL complet
                if not href.startswith('http'):
                    href = f"https://www.{domain}{href}" if not href.startswith('/') else f"https://www.{domain}{href}"
                
                if href not in links and domain in href:
                    links.append(href)
                    
        except:
            continue
    
    return links[:5]  # Max 5 produse

def scrape_site(context, domain, config, sku, name):
    """Caută și extrage preț de pe un site"""
    page = None
    try:
        page = context.new_page()
        
        # Încearcă URL-ul principal de căutare
        search_urls = [config['search'].format(quote_plus(sku))]
        if 'alt_search' in config:
            search_urls.append(config['alt_search'].format(quote_plus(sku)))
        
        for search_url in search_urls:
            try:
                page.goto(search_url, timeout=20000, wait_until='domcontentloaded')
                time.sleep(random.uniform(1.5, 2.5))
                
                # Accept cookies
                for btn in ['Accept', 'Acceptă', 'OK', 'Agree', 'Sunt de acord']:
                    try:
                        page.click(f'button:has-text("{btn}")', timeout=800)
                        break
                    except:
                        pass
                
                # Găsește link-uri produse
                product_links = find_product_links(page, domain)
                
                if product_links:
                    break
                    
            except:
                continue
        
        if not product_links:
            logger.info(f"   ⚪ {domain}: 0 produse")
            return None
        
        logger.info(f"   🔍 {domain}: {len(product_links)} link-uri")
        
        # Verifică fiecare produs
        for href in product_links:
            try:
                page.goto(href, timeout=15000, wait_until='domcontentloaded')
                time.sleep(1)
                
                # Validare SKU
                if not sku_in_page(sku, page):
                    logger.info(f"      ✗ SKU absent în: {href[:55]}...")
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
                    
            except:
                continue
        
    except Exception as e:
        logger.debug(f"   ❌ {domain}: {str(e)[:40]}")
    finally:
        if page:
            page.close()
    
    return None

def scan_product(sku, name, your_price=0):
    """Scanează toate site-urile"""
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
            
            time.sleep(random.uniform(0.5, 1))
        
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
    logger.info("🚀 PriceMonitor v4.2 (16 site-uri + better selectors) pe :8080")
    app.run(host='0.0.0.0', port=8080)
