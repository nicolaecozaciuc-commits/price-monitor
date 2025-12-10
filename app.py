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
           'wikipedia', 'amazon', 'ebay', 'olx', 'kaufland']

# Site-uri directe ca fallback
DIRECT_SITES = {
    'foglia.ro': 'https://www.foglia.ro/catalogsearch/result/?q={}',
    'bagno.ro': 'https://www.bagno.ro/catalogsearch/result/?q={}',
    'instalatiiaz.ro': 'https://www.instalatiiaz.ro/?s={}',
    'sensodays.ro': 'https://www.sensodays.ro/catalogsearch/result/?q={}',
    'absulo.ro': 'https://www.absulo.ro/catalogsearch/result/?q={}',
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

def is_valid_domain(domain):
    if not domain or len(domain) < 5:
        return False
    if any(b in domain for b in BLOCKED):
        return False
    match = re.match(r'^([a-z0-9-]+)\.ro$', domain)
    return match and len(match.group(1)) >= 3

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
    try:
        p = clean_price(page.locator('meta[property="product:price:amount"]').first.get_attribute('content'))
        if p > 0:
            return p
    except:
        pass
    
    # CSS
    for sel in ['[data-price-amount]', '.price-new', '.special-price .price', '.price']:
        try:
            el = page.locator(sel).first
            p = clean_price(el.get_attribute('data-price-amount') or el.inner_text())
            if p > 0:
                return p
        except:
            pass
    
    return 0

def get_urls_from_bing(page, sku):
    """Extrage URL-uri din Bing - mai multe metode"""
    urls = []
    
    try:
        # Metodă 1: Rezultate standard .b_algo
        for result in page.locator('.b_algo').all()[:15]:
            try:
                href = result.locator('a').first.get_attribute('href') or ''
                domain_match = re.search(r'https?://(?:www\.)?([a-z0-9-]+\.ro)', href.lower())
                if domain_match and is_valid_domain(domain_match.group(1)):
                    urls.append({'url': href, 'domain': domain_match.group(1)})
            except:
                continue
        
        # Metodă 2: Toate linkurile .ro din pagină
        for link in page.locator('a[href*=".ro"]').all()[:30]:
            try:
                href = link.get_attribute('href') or ''
                domain_match = re.search(r'https?://(?:www\.)?([a-z0-9-]+\.ro)', href.lower())
                if domain_match and is_valid_domain(domain_match.group(1)):
                    domain = domain_match.group(1)
                    if not any(u['domain'] == domain for u in urls):
                        urls.append({'url': href, 'domain': domain})
            except:
                continue
                
    except:
        pass
    
    return urls[:10]

def verify_and_get_price(page, url, sku):
    """Verifică URL și extrage preț"""
    try:
        page.goto(url, timeout=12000, wait_until='domcontentloaded')
        time.sleep(1.5)
        
        body_text = page.locator('body').inner_text().lower()
        
        # Skip pagini de eroare
        if any(x in body_text for x in ['not found', 'nothing found', 'nu a fost gasit', '404']):
            return None
        
        # Verifică SKU (mai permisiv)
        sku_norm = normalize(sku)
        body_norm = normalize(body_text)
        
        # SKU complet sau parțial
        if sku_norm in body_norm or sku_norm[1:] in body_norm or sku_norm[:5] in body_norm:
            price = extract_price_from_page(page)
            if price > 0:
                return price
                
    except:
        pass
    
    return None

def search_direct_site(page, domain, search_url, sku):
    """Caută direct pe un site"""
    try:
        url = search_url.format(quote_plus(sku))
        page.goto(url, timeout=12000, wait_until='domcontentloaded')
        time.sleep(1.5)
        
        sku_lower = sku.lower()
        sku_norm = normalize(sku)
        
        # Caută link cu SKU
        for link in page.locator('a[href]').all()[:30]:
            try:
                href = link.get_attribute('href') or ''
                if sku_lower in href.lower() or sku_norm in normalize(href):
                    if href.startswith('/'):
                        href = f"https://www.{domain}{href}"
                    if domain in href:
                        page.goto(href, timeout=10000, wait_until='domcontentloaded')
                        time.sleep(1)
                        
                        price = extract_price_from_page(page)
                        if price > 0:
                            return {'price': price, 'url': href}
            except:
                continue
                
    except:
        pass
    
    return None

def scan_product(sku, name, your_price=0):
    found = []
    found_domains = set()
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
            # ═══════════════════════════════════════════════════
            # ETAPA 1: Bing Discovery
            # ═══════════════════════════════════════════════════
            query = f"{sku} pret"
            url = f"https://www.bing.com/search?q={quote_plus(query)}"
            
            logger.info(f"   🔍 Bing: {query}")
            
            page.goto(url, timeout=20000, wait_until='domcontentloaded')
            time.sleep(2)
            
            try:
                page.click('#bnp_btn_accept', timeout=2000)
                time.sleep(0.5)
            except:
                pass
            
            urls = get_urls_from_bing(page, sku)
            logger.info(f"   📋 Bing: {len(urls)} URL-uri")
            
            for item in urls:
                if item['domain'] in found_domains:
                    continue
                    
                logger.info(f"      🔗 {item['domain']}...")
                price = verify_and_get_price(page, item['url'], sku)
                
                if price:
                    found.append({
                        'name': item['domain'],
                        'price': price,
                        'url': item['url'],
                        'method': 'Bing'
                    })
                    found_domains.add(item['domain'])
                    logger.info(f"      ✅ {price} Lei")
                
                time.sleep(0.3)
                if len(found) >= 5:
                    break
            
            # ═══════════════════════════════════════════════════
            # ETAPA 2: Fallback - Site-uri directe
            # ═══════════════════════════════════════════════════
            if len(found) < 3:
                logger.info(f"   🔄 Fallback: căutare directă...")
                
                for domain, search_url in DIRECT_SITES.items():
                    if domain in found_domains:
                        continue
                    
                    logger.info(f"      🔗 {domain}...")
                    result = search_direct_site(page, domain, search_url, sku)
                    
                    if result:
                        found.append({
                            'name': domain,
                            'price': result['price'],
                            'url': result['url'],
                            'method': 'Direct'
                        })
                        found_domains.add(domain)
                        logger.info(f"      ✅ {result['price']} Lei")
                    
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

if __name__ == '__main__':
    logger.info("🚀 PriceMonitor v7.4 (Bing + Direct Fallback) pe :8080")
    app.run(host='0.0.0.0', port=8080)
