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

# ═══════════════════════════════════════════════════════════════
# SITE-URI CU PATTERN-URI URL DIRECTE
# ═══════════════════════════════════════════════════════════════
SITES = {
    'foglia.ro': {
        'search': 'https://www.foglia.ro/catalogsearch/result/?q={}',
        'direct_patterns': [
            'https://www.foglia.ro/{}',
            'https://www.foglia.ro/{}.html',
        ]
    },
    'bagno.ro': {
        'search': 'https://www.bagno.ro/catalogsearch/result/?q={}',
        'direct_patterns': [
            'https://www.bagno.ro/{}.html',
            'https://www.bagno.ro/{}-ideal-standard.html',
        ]
    },
    'absulo.ro': {
        'search': 'https://www.absulo.ro/catalogsearch/result/?q={}',
        'direct_patterns': [
            'https://absulo.ro/ideal-standard-{}',
        ]
    },
    'sanitino.ro': {
        'search': 'https://www.sanitino.ro/cauta/?q={}',
        'direct_patterns': [
            'https://www.sanitino.ro/ideal-standard-oleas-{}',
        ]
    },
    'sensodays.ro': {
        'search': 'https://www.sensodays.ro/catalogsearch/result/?q={}',
        'direct_patterns': []
    },
    'germanquality.ro': {
        'search': 'https://www.germanquality.ro/catalogsearch/result/?q={}',
        'direct_patterns': []
    },
    'novambient.ro': {
        'search': 'https://www.novambient.ro/catalogsearch/result/?q={}',
        'direct_patterns': []
    },
    'romstal.ro': {
        'search': 'https://www.romstal.ro/cautare?q={}',
        'direct_patterns': []
    },
    'dedeman.ro': {
        'search': 'https://www.dedeman.ro/ro/cautare?q={}',
        'direct_patterns': []
    },
    'hornbach.ro': {
        'search': 'https://www.hornbach.ro/s/{}',
        'direct_patterns': []
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
    
    # 3. CSS
    selectors = [
        '[data-price-amount]', '[data-price]', 'span[itemprop="price"]',
        '.product-new-price', '.price-new', '.current-price', '.special-price .price',
        '.product-price', '.price-box .price', '.price'
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
    """Verifică dacă SKU e în pagină"""
    sku_norm = normalize(str(sku))
    try:
        # În URL
        if sku_norm in normalize(page.url):
            return True
        # În body
        body = page.locator('body').inner_text()
        if sku_norm in normalize(body):
            return True
        if sku_norm[1:] in normalize(body):
            return True
    except:
        pass
    return False

def wait_for_content(page):
    """Așteaptă să se încarce conținutul dinamic"""
    try:
        # Scroll pentru a declanșa lazy loading
        page.evaluate("window.scrollTo(0, 500)")
        time.sleep(0.5)
        page.evaluate("window.scrollTo(0, 1000)")
        time.sleep(0.5)
        
        # Așteaptă să nu mai fie cereri de rețea
        page.wait_for_load_state('networkidle', timeout=5000)
    except:
        pass

def find_product_url_in_search(page, domain, sku):
    """Găsește URL-ul produsului în pagina de căutare"""
    sku_lower = sku.lower()
    sku_norm = normalize(sku)
    
    try:
        # Caută toate link-urile
        all_links = page.locator('a[href]').all()
        
        for link in all_links:
            try:
                href = link.get_attribute('href')
                if not href:
                    continue
                
                href_lower = href.lower()
                
                # Skip linkuri invalide
                if any(x in href_lower for x in ['cart', 'login', 'account', 'wishlist', 'mailto:', 'javascript:', 'tel:', '#', '.pdf', '.jpg']):
                    continue
                
                # Verifică dacă SKU e în URL
                if sku_lower in href_lower or sku_norm in normalize(href):
                    # Construiește URL complet
                    if href.startswith('/'):
                        href = f"https://www.{domain}{href}"
                    elif not href.startswith('http'):
                        continue
                    
                    if domain in href:
                        return href
                        
            except:
                continue
                
    except:
        pass
    
    return None

def scrape_site(context, domain, config, sku, name):
    """Caută produs pe un site"""
    page = None
    sku_lower = sku.lower()
    
    try:
        page = context.new_page()
        
        # ═══════════════════════════════════════════════════════
        # METODA 1: Căutare pe site + găsire link cu SKU
        # ═══════════════════════════════════════════════════════
        search_url = config['search'].format(quote_plus(sku))
        
        try:
            page.goto(search_url, timeout=25000, wait_until='domcontentloaded')
            time.sleep(2)
            
            # Accept cookies
            for btn in ['Accept', 'Acceptă', 'OK', 'Agree', 'Sunt de acord']:
                try:
                    page.click(f'button:has-text("{btn}")', timeout=800)
                    break
                except:
                    pass
            
            wait_for_content(page)
            
            # Caută link cu SKU în URL
            product_url = find_product_url_in_search(page, domain, sku)
            
            if product_url:
                logger.info(f"   🔗 {domain}: găsit link cu SKU")
                page.goto(product_url, timeout=20000, wait_until='domcontentloaded')
                time.sleep(1.5)
                wait_for_content(page)
                
                if sku_in_page(sku, page):
                    price, method = extract_price(page)
                    if price > 0:
                        return {'name': domain, 'price': price, 'url': product_url, 'method': method}
            
            # Verifică dacă SKU e direct în pagina de rezultate
            if sku_in_page(sku, page):
                price, method = extract_price(page)
                if price > 0:
                    logger.info(f"   💰 {domain}: preț direct în căutare")
                    return {'name': domain, 'price': price, 'url': search_url, 'method': method}
                    
        except Exception as e:
            logger.debug(f"   Search error {domain}: {str(e)[:30]}")
        
        # ═══════════════════════════════════════════════════════
        # METODA 2: URL-uri directe cu pattern
        # ═══════════════════════════════════════════════════════
        for pattern in config.get('direct_patterns', []):
            try:
                direct_url = pattern.format(sku_lower)
                page.goto(direct_url, timeout=15000, wait_until='domcontentloaded')
                time.sleep(1)
                
                # Verifică dacă pagina există și conține SKU
                if page.url and '404' not in page.url and sku_in_page(sku, page):
                    price, method = extract_price(page)
                    if price > 0:
                        logger.info(f"   🎯 {domain}: găsit via pattern direct")
                        return {'name': domain, 'price': price, 'url': direct_url, 'method': method}
            except:
                continue
        
        logger.info(f"   ⚪ {domain}: negăsit")
        
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
                logger.info(f"   ✅ {domain}: {result['price']} Lei ({result['diff']:+.1f}%) [{result['method']}]")
            
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
    logger.info("🚀 PriceMonitor v5.0 (Direct URL Patterns + Smart Search) pe :8080")
    app.run(host='0.0.0.0', port=8080)
