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

BLOCKED = ['google', 'bing', 'microsoft', 'facebook', 'youtube', 'doarbai', 'termohabitat', 'wikipedia', 'amazon', 'ebay']

SEARCH_URLS = {
    'emag.ro': 'https://www.emag.ro/search/{}',
    'absulo.ro': 'https://www.absulo.ro/catalogsearch/result/?q={}',
    'germanquality.ro': 'https://www.germanquality.ro/catalogsearch/result/?q={}',
    'sensodays.ro': 'https://www.sensodays.ro/catalogsearch/result/?q={}',
    'foglia.ro': 'https://www.foglia.ro/catalogsearch/result/?q={}',
    'bagno.ro': 'https://www.bagno.ro/catalogsearch/result/?q={}',
    'romstal.ro': 'https://www.romstal.ro/cautare?q={}',
    'compari.ro': 'https://www.compari.ro/search/?q={}',
    'ideal-standard.ro': 'https://www.ideal-standard.ro/ro/search?text={}',
    'instalatiiaz.ro': 'https://www.instalatiiaz.ro/?s={}',
    'dedeman.ro': 'https://www.dedeman.ro/ro/cautare?query={}',
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
    for sel in ['[data-price-amount]', '.price-new', '.special-price .price', '.product-price', '.price']:
        try:
            el = page.locator(sel).first
            p = clean_price(el.get_attribute('data-price-amount') or el.inner_text())
            if p > 0:
                return p
        except:
            pass
    
    return 0

def extract_prices_from_text(text):
    """Extrage toate prețurile dintr-un text"""
    prices = []
    
    # Pattern-uri multiple
    patterns = [
        r'([\d.,]+)\s*Lei',           # 828,57 Lei
        r'([\d.,]+)\s*lei',           # 828,57 lei
        r'([\d.,]+)\s*RON',           # 828.57 RON
        r'([\d.,]+)\s*ron',           # 828.57 ron
        r'Lei\s*([\d.,]+)',           # Lei 828,57
        r'RON\s*([\d.,]+)',           # RON 828.57
        r'Pret[:\s]*([\d.,]+)',       # Pret: 828,57
        r'price[:\s]*([\d.,]+)',      # price: 828.57
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            p = clean_price(m)
            if p > 0 and p not in prices:
                prices.append(p)
    
    return prices[:10]

def get_domains_from_bing(page):
    domains = []
    try:
        for block in page.locator('.b_algo').all()[:15]:
            try:
                text = block.inner_text()
                for line in text.split('\n')[:3]:
                    match = re.search(r'(?:https?://)?(?:www\.)?([a-z0-9-]+\.ro)', line.lower())
                    if match:
                        d = match.group(1)
                        if len(d) > 4 and d not in domains and not any(b in d for b in BLOCKED):
                            domains.append(d)
                            break
            except:
                continue
    except:
        pass
    return domains[:10]

def find_price_on_search_page(page, domain, sku, save_debug=False):
    """Caută preț pe pagina de rezultate"""
    
    search_url = SEARCH_URLS.get(domain, f'https://www.{domain}/search?q={{}}')
    sku_norm = normalize(sku)
    sku_lower = sku.lower()
    
    try:
        url = search_url.format(quote_plus(sku))
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        time.sleep(4)
        
        # Scroll
        page.evaluate("window.scrollTo(0, 500)")
        time.sleep(1)
        
        # Salvează debug
        if save_debug:
            page.screenshot(path=f"{DEBUG_DIR}/{domain}_{sku}.png")
            with open(f"{DEBUG_DIR}/{domain}_{sku}.txt", 'w', encoding='utf-8') as f:
                f.write(page.locator('body').inner_text())
        
        body_text = page.locator('body').inner_text()
        body_lower = body_text.lower()
        body_norm = normalize(body_text)
        
        # Check SKU
        has_sku = sku_norm in body_norm or sku_lower in body_lower or sku_norm[1:] in body_norm
        logger.info(f"         SKU în pagină: {has_sku}")
        
        # Check erori
        if '0 produse' in body_lower and 'produse)' not in body_lower:
            logger.info(f"         ⚠️ 0 produse")
            return None
        
        if not has_sku:
            return None
        
        # Extrage prețuri
        prices = extract_prices_from_text(body_text)
        logger.info(f"         💰 Prețuri găsite: {prices[:5]}")
        
        if prices:
            # Returnează primul preț valid
            return {'price': prices[0], 'url': url}
        
        # Încearcă JSON-LD de pe pagină
        price = extract_price_from_page(page)
        if price > 0:
            logger.info(f"         💰 Preț JSON-LD: {price}")
            return {'price': price, 'url': url}
        
        return None
        
    except Exception as e:
        logger.info(f"         ❌ Error: {str(e)[:30]}")
        return None

def scan_product(sku, name, your_price=0):
    found = []
    sku = str(sku).strip()
    
    logger.info(f"🔎 {sku} - {name[:30]}...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale='ro-RO',
            timezone_id='Europe/Bucharest',
            extra_http_headers={
                'Accept-Language': 'ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            }
        )
        
        page = context.new_page()
        
        try:
            # ETAPA 1: Bing
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
            
            domains = get_domains_from_bing(page)
            
            # Adaugă site-uri importante
            for important in ['germanquality.ro', 'sensodays.ro', 'absulo.ro']:
                if important not in domains:
                    domains.append(important)
            
            logger.info(f"   🌐 Site-uri: {domains[:8]}")
            
            # ETAPA 2: Verifică pe fiecare site
            for i, domain in enumerate(domains[:8]):
                logger.info(f"      🔗 {domain}...")
                
                save_debug = (i < 2)  # Primele 2
                result = find_price_on_search_page(page, domain, sku, save_debug)
                
                if result:
                    found.append({
                        'name': domain,
                        'price': result['price'],
                        'url': result['url'],
                        'method': 'Verified'
                    })
                    logger.info(f"      ✅ {result['price']} Lei")
                else:
                    logger.info(f"      ⚪ negăsit")
                
                time.sleep(0.5)
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
    logger.info("🚀 PriceMonitor v9.2 (Multi-Price Pattern) pe :8080")
    app.run(host='0.0.0.0', port=8080)
