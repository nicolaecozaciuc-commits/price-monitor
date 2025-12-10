import re
import logging
import time
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

BLOCKED = ['google', 'bing', 'microsoft', 'facebook', 'youtube', 'doarbai', 'termohabitat', 
           'wikipedia', 'amazon', 'ebay', 'olx']

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

def is_valid_domain(domain):
    if not domain or len(domain) < 5:
        return False
    if any(b in domain for b in BLOCKED):
        return False
    match = re.match(r'^([a-z0-9-]+)\.ro$', domain)
    if match and len(match.group(1)) >= 3:
        return True
    return False

def extract_price_from_page(page):
    """Extrage preț din pagina produsului"""
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
    for sel in ['meta[property="product:price:amount"]']:
        try:
            p = clean_price(page.locator(sel).first.get_attribute('content'))
            if p > 0:
                return p
        except:
            pass
    
    # CSS
    for sel in ['[data-price-amount]', '.price', '[class*="price"]']:
        try:
            el = page.locator(sel).first
            p = clean_price(el.get_attribute('data-price-amount') or el.inner_text())
            if p > 0:
                return p
        except:
            pass
    
    return 0

def verify_product_on_page(page, url, sku):
    """Accesează URL-ul și verifică dacă produsul există"""
    try:
        page.goto(url, timeout=12000, wait_until='domcontentloaded')
        time.sleep(1.5)
        
        body_text = page.locator('body').inner_text().lower()
        
        # Verifică dacă e pagină de eroare
        error_phrases = ['not found', 'nothing found', 'nu a fost gasit', 'no results', 
                        'nu exista', 'pagina nu exista', '404', 'sorry']
        if any(phrase in body_text for phrase in error_phrases):
            return None
        
        # Verifică dacă SKU există în pagină
        sku_norm = normalize(sku)
        if sku_norm not in normalize(body_text) and sku_norm[1:] not in normalize(body_text):
            return None
        
        # Extrage prețul
        price = extract_price_from_page(page)
        if price > 0:
            return price
            
    except:
        pass
    
    return None

def get_urls_from_bing(page, sku):
    """Extrage URL-uri din rezultatele Bing"""
    urls = []
    
    try:
        # Caută în rezultatele Bing
        results = page.locator('.b_algo').all()
        
        for result in results[:15]:
            try:
                link = result.locator('a').first
                href = link.get_attribute('href') or ''
                
                # Extrage domain
                domain_match = re.search(r'https?://(?:www\.)?([a-z0-9-]+\.ro)', href.lower())
                if not domain_match:
                    continue
                    
                domain = domain_match.group(1)
                if not is_valid_domain(domain):
                    continue
                
                # Extrage preț din snippet (pentru referință)
                text = result.inner_text()
                price_match = re.search(r'([\d.,]+)\s*(?:RON|Lei)', text)
                hint_price = clean_price(price_match.group(1)) if price_match else 0
                
                urls.append({
                    'url': href,
                    'domain': domain,
                    'hint_price': hint_price
                })
                
            except:
                continue
                
    except:
        pass
    
    # Deduplicate by domain
    seen = set()
    unique = []
    for u in urls:
        if u['domain'] not in seen:
            seen.add(u['domain'])
            unique.append(u)
    
    return unique

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
            # ETAPA 1: Bing search
            query = f"{sku} pret"
            url = f"https://www.bing.com/search?q={quote_plus(query)}"
            
            logger.info(f"   🔍 Bing: {query}")
            
            page.goto(url, timeout=20000, wait_until='domcontentloaded')
            time.sleep(2)
            
            # Accept cookies
            try:
                page.click('#bnp_btn_accept', timeout=2000)
                time.sleep(0.5)
            except:
                pass
            
            # Extrage URL-uri
            urls = get_urls_from_bing(page, sku)
            logger.info(f"   📋 Găsite {len(urls)} URL-uri de verificat")
            
            # ETAPA 2: Verifică fiecare URL
            for item in urls:
                logger.info(f"      🔗 Verific {item['domain']}...")
                
                price = verify_product_on_page(page, item['url'], sku)
                
                if price:
                    found.append({
                        'name': item['domain'],
                        'price': price,
                        'url': item['url'],
                        'method': 'Bing+Verify'
                    })
                    logger.info(f"      ✅ {item['domain']}: {price} Lei")
                else:
                    logger.info(f"      ❌ {item['domain']}: produs negăsit")
                
                time.sleep(0.3)
                
                if len(found) >= 5:
                    break
            
        except Exception as e:
            logger.info(f"   ❌ Error: {str(e)[:50]}")
        finally:
            page.close()
        
        browser.close()
    
    # Calculează diferență
    for r in found:
        if your_price > 0:
            r['diff'] = round(((r['price'] - your_price) / your_price) * 100, 1)
        else:
            r['diff'] = 0
    
    found.sort(key=lambda x: x['price'])
    logger.info(f"📊 Total verificate: {len(found)}")
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
    logger.info("🚀 PriceMonitor v7.3 (Bing + Verificare) pe :8080")
    app.run(host='0.0.0.0', port=8080)
