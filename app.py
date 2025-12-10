import re
import logging
import time
import json
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

# Site-uri țintă pentru căutare
TARGET_SITES = [
    'foglia.ro',
    'bagno.ro', 
    'instalatiiaz.ro',
    'sensodays.ro',
    'absulo.ro',
    'sanitino.ro',
    'romstal.ro',
    'compari.ro',
]

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
        return price if 50 < price < 500000 else 0
    except:
        return 0

def normalize(text):
    import unicodedata
    if not text: return ""
    text = unicodedata.normalize('NFD', text).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]', '', text.lower())

def extract_price_from_page(page):
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
                                return p
            except:
                continue
    except:
        pass
    
    # META
    try:
        p = clean_price(page.locator('meta[property="product:price:amount"]').first.get_attribute('content'))
        if p > 0:
            return p
    except:
        pass
    
    # CSS
    for sel in ['[data-price-amount]', '.price-new', '.special-price .price', '.price', '[class*="price"]']:
        try:
            el = page.locator(sel).first
            p = clean_price(el.get_attribute('data-price-amount') or el.inner_text())
            if p > 0:
                return p
        except:
            pass
    
    return 0

def search_bing_for_site(page, sku, site):
    """Caută pe Bing: SKU site:example.ro"""
    try:
        query = f"{sku} site:{site}"
        url = f"https://www.bing.com/search?q={quote_plus(query)}"
        
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        time.sleep(1.5)
        
        # Extrage primul URL de pe site
        for result in page.locator('.b_algo').all()[:5]:
            try:
                href = result.locator('a').first.get_attribute('href') or ''
                if site in href.lower():
                    return href
            except:
                continue
                
    except:
        pass
    
    return None

def verify_product_page(page, url, sku):
    """Verifică pagina produsului și extrage prețul"""
    try:
        page.goto(url, timeout=12000, wait_until='domcontentloaded')
        time.sleep(1.5)
        
        body_text = page.locator('body').inner_text().lower()
        
        # Skip pagini de eroare
        if any(x in body_text for x in ['not found', 'nothing found', 'nu a fost', '404', 'no results']):
            return None
        
        # Verifică SKU
        sku_norm = normalize(sku)
        body_norm = normalize(body_text)
        
        if sku_norm in body_norm or sku_norm[1:] in body_norm:
            price = extract_price_from_page(page)
            if price > 0:
                return price
                
    except:
        pass
    
    return None

def scan_product(sku, name, your_price=0):
    found = []
    found_domains = set()
    sku = str(sku).strip()
    name = str(name).strip()
    
    logger.info(f"🔎 {sku} - {name[:30]}...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale='ro-RO'
        )
        
        page = context.new_page()
        
        try:
            # Accept Bing cookies o singură dată
            page.goto("https://www.bing.com/search?q=test", timeout=15000)
            time.sleep(1)
            try:
                page.click('#bnp_btn_accept', timeout=2000)
            except:
                pass
            
            # Caută pe Bing pentru fiecare site țintă
            logger.info(f"   🔍 Căutare Bing per site...")
            
            for site in TARGET_SITES:
                if site in found_domains:
                    continue
                
                logger.info(f"      🌐 {site}...")
                
                # Caută URL-ul de pe Bing
                product_url = search_bing_for_site(page, sku, site)
                
                if product_url:
                    logger.info(f"         📄 URL găsit")
                    
                    # Verifică pagina și extrage prețul
                    price = verify_product_page(page, product_url, sku)
                    
                    if price:
                        found.append({
                            'name': site,
                            'price': price,
                            'url': product_url,
                            'method': 'Bing'
                        })
                        found_domains.add(site)
                        logger.info(f"         ✅ {price} Lei")
                    else:
                        logger.info(f"         ❌ SKU/preț negăsit")
                else:
                    logger.info(f"         ⚪ niciun rezultat")
                
                time.sleep(0.5)
                
                if len(found) >= 5:
                    break
            
        except Exception as e:
            logger.info(f"   ❌ Error: {str(e)[:50]}")
        finally:
            page.close()
        
        browser.close()
    
    # Calculează diferență
    for r in found:
        r['diff'] = round(((r['price'] - your_price) / your_price) * 100, 1) if your_price > 0 else 0
    
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
    logger.info("🚀 PriceMonitor v7.6 (Bing Site-Targeted) pe :8080")
    app.run(host='0.0.0.0', port=8080)
