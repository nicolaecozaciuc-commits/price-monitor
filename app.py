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

def accept_cookies(page):
    """Încearcă să accepte cookie-uri"""
    selectors = [
        'button:has-text("Permite toate")',
        'button:has-text("Accept")',
        'button:has-text("Acceptă")',
        '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
    ]
    
    for selector in selectors:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=1000):
                btn.click(force=True)
                logger.info(f"         🍪 Cookie: {selector[:30]}")
                return True
        except:
            continue
    return False

def extract_prices_from_text(text):
    prices = []
    matches = re.findall(r'([\d.,]+)\s*Lei', text, re.IGNORECASE)
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
    url = search_url.format(quote_plus(sku))
    
    try:
        # Prima încărcare
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        time.sleep(2)
        
        # Accept cookies
        cookie_accepted = accept_cookies(page)
        
        if cookie_accepted:
            # RELOAD pagina după accept cookies!
            time.sleep(1)
            page.reload(wait_until='domcontentloaded')
            time.sleep(3)
            logger.info(f"         🔄 Reload după cookies")
        
        # Scroll
        page.evaluate("window.scrollTo(0, 500)")
        time.sleep(1)
        
        # Salvează debug
        if save_debug:
            page.screenshot(path=f"{DEBUG_DIR}/{domain}_{sku}.png")
        
        body_text = page.locator('body').inner_text()
        body_lower = body_text.lower()
        
        # Check erori
        error_phrases = ['0 produse', 'nu s-au gasit', 'nu am gasit', 'niciun rezultat']
        for phrase in error_phrases:
            if phrase in body_lower and 'produse)' not in body_lower:
                logger.info(f"         ⚠️ {phrase}")
                return None
        
        # Check SKU
        has_sku = sku_lower in body_lower or sku_norm in normalize(body_text)
        logger.info(f"         SKU: {has_sku}")
        
        if not has_sku:
            return None
        
        # Extrage prețuri
        prices = extract_prices_from_text(body_text)
        logger.info(f"         💰 {prices[:5]}")
        
        if prices:
            return {'price': prices[0], 'url': url}
        
        return None
        
    except Exception as e:
        logger.info(f"         ❌ {str(e)[:30]}")
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
            except:
                pass
            
            domains = get_domains_from_bing(page)
            
            for important in ['absulo.ro', 'germanquality.ro', 'sensodays.ro']:
                if important not in domains:
                    domains.append(important)
            
            logger.info(f"   🌐 {domains[:8]}")
            
            # ETAPA 2: Verifică
            for i, domain in enumerate(domains[:8]):
                logger.info(f"      🔗 {domain}...")
                
                result = find_price_on_search_page(page, domain, sku, save_debug=(i<3))
                
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
            logger.info(f"   ❌ {str(e)[:50]}")
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
    logger.info("🚀 PriceMonitor v9.5 (Reload After Cookies) pe :8080")
    app.run(host='0.0.0.0', port=8080)
