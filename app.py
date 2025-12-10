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

BLOCKED = ['google', 'bing', 'microsoft', 'facebook', 'youtube', 'doarbai', 'termohabitat', 'wikipedia', 'r.ro', 'f.ro', 'n.ro', 'math.ro', 'slider.ro']

# Site-uri cu pattern căutare cunoscut
SEARCH_PATTERNS = {
    'emag.ro': 'https://www.emag.ro/search/{}',
    'absulo.ro': 'https://www.absulo.ro/catalogsearch/result/?q={}',
    'germanquality.ro': 'https://www.germanquality.ro/catalogsearch/result/?q={}',
    'compari.ro': 'https://www.compari.ro/search/?q={}',
    'conrep.ro': 'https://www.conrep.ro/cautare?search={}',
    'ideal-standard.ro': 'https://www.ideal-standard.ro/search?q={}',
    'sensodays.ro': 'https://www.sensodays.ro/catalogsearch/result/?q={}',
    'foglia.ro': 'https://www.foglia.ro/catalogsearch/result/?q={}',
    'bagno.ro': 'https://www.bagno.ro/catalogsearch/result/?q={}',
    'romstal.ro': 'https://www.romstal.ro/cautare?q={}',
}

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
        for script in page.locator('script[type="application/ld+json"]').all()[:5]:
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
    for sel in ['[data-price-amount]', '.price-new', '.price']:
        try:
            el = page.locator(sel).first
            p = clean_price(el.get_attribute('data-price-amount') or el.inner_text())
            if p > 0:
                return p
        except:
            pass
    
    return 0

def find_product_on_site(page, domain, sku):
    """Caută produs pe un site și returnează URL + preț"""
    
    # Obține pattern-ul de căutare
    search_url = SEARCH_PATTERNS.get(domain)
    if not search_url:
        search_url = f"https://www.{domain}/search?q={{}}"
    
    try:
        url = search_url.format(quote_plus(sku))
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        time.sleep(2)
        
        sku_lower = sku.lower()
        sku_norm = normalize(sku)
        
        # Caută link cu SKU în URL sau text
        for link in page.locator('a[href]').all()[:50]:
            try:
                href = link.get_attribute('href') or ''
                href_lower = href.lower()
                
                # Skip non-product
                if any(x in href_lower for x in ['cart', 'login', 'account', '#', 'mailto']):
                    continue
                
                # Verifică SKU în URL
                if sku_lower in href_lower or sku_norm in normalize(href):
                    # Construiește URL complet
                    if href.startswith('/'):
                        href = f"https://www.{domain}{href}"
                    
                    if domain in href:
                        # Accesează pagina produsului
                        page.goto(href, timeout=12000, wait_until='domcontentloaded')
                        time.sleep(1.5)
                        
                        # Verifică SKU în pagină
                        body = page.locator('body').inner_text()
                        if sku_norm in normalize(body) or sku_norm[1:] in normalize(body):
                            price = extract_price_from_page(page)
                            if price > 0:
                                return {'url': href, 'price': price}
            except:
                continue
        
        return None
        
    except:
        return None

def scan_product(sku, name, your_price=0):
    found = []
    sku = str(sku).strip()
    
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
            # ETAPA 1: Bing pentru a vedea ce site-uri au produsul
            query = f"{sku} pret"
            url = f"https://www.bing.com/search?q={quote_plus(query)}"
            
            logger.info(f"   🔍 Bing: {query}")
            
            page.goto(url, timeout=20000, wait_until='domcontentloaded')
            time.sleep(3)
            
            try:
                page.click('#bnp_btn_accept', timeout=3000)
                time.sleep(1)
            except:
                pass
            
            # Extrage domenii din HTML
            html = page.content()
            all_domains = re.findall(r'([a-z0-9-]+\.ro)', html.lower())
            unique_domains = []
            for d in all_domains:
                if len(d) > 5 and d not in unique_domains and not any(b in d for b in BLOCKED):
                    unique_domains.append(d)
            
            logger.info(f"   🌐 Site-uri Bing: {unique_domains[:8]}")
            
            # ETAPA 2: Caută direct pe site-urile găsite
            sites_to_check = unique_domains[:8]
            
            # Adaugă și site-uri importante care nu sunt în Bing
            for important in ['emag.ro', 'germanquality.ro', 'sensodays.ro']:
                if important not in sites_to_check:
                    sites_to_check.append(important)
            
            for domain in sites_to_check[:10]:
                if any(f['name'] == domain for f in found):
                    continue
                
                logger.info(f"      🔗 {domain}...")
                
                result = find_product_on_site(page, domain, sku)
                
                if result:
                    found.append({
                        'name': domain,
                        'price': result['price'],
                        'url': result['url'],
                        'method': 'Direct'
                    })
                    logger.info(f"      ✅ {result['price']} Lei")
                else:
                    logger.info(f"      ❌ negăsit")
                
                time.sleep(0.3)
                if len(found) >= 5:
                    break
            
            logger.info(f"   📊 Total: {len(found)}")
            
        except Exception as e:
            logger.info(f"   ❌ Error: {str(e)[:50]}")
        finally:
            page.close()
        
        browser.close()
    
    for r in found:
        r['diff'] = round(((r['price'] - your_price) / your_price) * 100, 1) if your_price > 0 else 0
    
    found.sort(key=lambda x: x['price'])
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
    logger.info("🚀 PriceMonitor v8.6 (Hybrid: Bing Discovery + Direct Search) pe :8080")
    app.run(host='0.0.0.0', port=8080)
