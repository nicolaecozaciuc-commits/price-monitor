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

# DOAR site-urile tale și motoare de căutare
BLOCKED = ['google', 'bing', 'microsoft', 'facebook', 'youtube', 'doarbai', 
           'termohabitat', 'wikipedia', 'amazon.com', 'ebay']

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
    for sel in ['[data-price-amount]', '.price-new', '.special-price .price', '.product-price', '.price']:
        try:
            el = page.locator(sel).first
            p = clean_price(el.get_attribute('data-price-amount') or el.inner_text())
            if p > 0:
                return p
        except:
            pass
    
    return 0

def get_urls_from_bing(page):
    """Extrage URL-uri din Bing - TOATE linkurile .ro"""
    urls = []
    
    try:
        # Metodă 1: .b_algo
        for result in page.locator('.b_algo').all()[:15]:
            try:
                for link in result.locator('a').all()[:3]:
                    href = link.get_attribute('href') or ''
                    if '.ro' in href and 'bing' not in href.lower():
                        domain_match = re.search(r'https?://(?:www\.)?([a-z0-9-]+\.ro)', href.lower())
                        if domain_match:
                            domain = domain_match.group(1)
                            if not any(b in domain for b in BLOCKED):
                                if not any(u['domain'] == domain for u in urls):
                                    urls.append({'url': href, 'domain': domain})
            except:
                continue
        
        # Metodă 2: Toate linkurile cu .ro
        if len(urls) < 5:
            for link in page.locator('a[href*=".ro"]').all()[:30]:
                try:
                    href = link.get_attribute('href') or ''
                    if 'bing' in href.lower():
                        continue
                    domain_match = re.search(r'https?://(?:www\.)?([a-z0-9-]+\.ro)', href.lower())
                    if domain_match:
                        domain = domain_match.group(1)
                        if not any(b in domain for b in BLOCKED):
                            if not any(u['domain'] == domain for u in urls):
                                urls.append({'url': href, 'domain': domain})
                except:
                    continue
                    
    except:
        pass
    
    logger.info(f"   🌐 Domenii: {[u['domain'] for u in urls[:8]]}")
    return urls[:10]

def verify_product(page, url, sku):
    """Verifică produs - mai permisiv"""
    try:
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        time.sleep(2)
        
        body_text = page.locator('body').inner_text().lower()
        
        # Verifică să nu fie pagină de eroare
        error_phrases = ['nu am gasit', 'nu a fost gasit', 'nothing found', 'no results', 
                        '0 rezultate', 'pagina nu exista', '404 not found']
        for phrase in error_phrases:
            if phrase in body_text:
                return None
        
        # Verifică SKU - MAI PERMISIV
        sku_norm = normalize(sku)
        body_norm = normalize(body_text)
        
        # Încearcă mai multe variante
        found_sku = False
        if sku_norm in body_norm:
            found_sku = True
        elif sku_norm[1:] in body_norm:  # fără prima literă
            found_sku = True
        elif len(sku_norm) > 4 and sku_norm[:5] in body_norm:  # primele 5 caractere
            found_sku = True
        
        if not found_sku:
            return None
        
        # Extrage preț
        price = extract_price_from_page(page)
        return price if price > 0 else None
        
    except Exception as e:
        logger.debug(f"      Verify error: {str(e)[:30]}")
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
            query = f"{sku} pret"
            url = f"https://www.bing.com/search?q={quote_plus(query)}"
            
            logger.info(f"   🔍 Bing: {query}")
            
            page.goto(url, timeout=20000, wait_until='domcontentloaded')
            time.sleep(2)
            
            # Accept cookies
            try:
                page.click('#bnp_btn_accept', timeout=3000)
                time.sleep(1)
            except:
                pass
            
            # Extrage URL-uri
            urls = get_urls_from_bing(page)
            logger.info(f"   📋 URL-uri: {len(urls)}")
            
            # Verifică fiecare
            for item in urls:
                logger.info(f"      🔗 {item['domain']}...")
                
                price = verify_product(page, item['url'], sku)
                
                if price:
                    found.append({
                        'name': item['domain'],
                        'price': price,
                        'url': item['url'],
                        'method': 'Bing'
                    })
                    logger.info(f"      ✅ {price} Lei")
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
    logger.info("🚀 PriceMonitor v8.3 (Fixed) pe :8080")
    app.run(host='0.0.0.0', port=8080)
