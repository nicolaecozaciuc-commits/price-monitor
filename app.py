import re
import logging
import time
import random
import unicodedata
import json
from urllib.parse import urlparse, quote_plus
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
# CONFIGURARE SITE-URI DIRECTE (Fallback)
# ═══════════════════════════════════════════════════════════════
DIRECT_SITES = {
    'Dedeman': {
        'search_url': 'https://www.dedeman.ro/ro/cautare?q={}',
        'card': '.product-item',
        'name': '.product-title',
        'price': '.product-price',
        'link': 'a.product-title'
    },
    'eMAG': {
        'search_url': 'https://www.emag.ro/search/{}',
        'card': '.card-item',
        'name': '.card-v2-title',
        'price': '.product-new-price',
        'link': 'a.card-v2-title'
    },
    'Hornbach': {
        'search_url': 'https://www.hornbach.ro/s/{}',
        'card': 'article.product',
        'name': '.product-title',
        'price': '.price',
        'link': 'a'
    },
    'LeroyMerlin': {
        'search_url': 'https://www.leroymerlin.ro/cautare?query={}',
        'card': '.product-card',
        'name': '.product-title',
        'price': '.price',
        'link': 'a'
    },
    'Romstal': {
        'search_url': 'https://www.romstal.ro/cautare?q={}',
        'card': '.product-item',
        'name': '.product-title',
        'price': '.product-price',
        'link': 'a'
    },
    'Obsentum': {
        'search_url': 'https://obsentum.com/catalogsearch/result/?q={}',
        'card': '.product-item',
        'name': '.product-item-link',
        'price': '.price',
        'link': '.product-item-link'
    },
    'Sanex': {
        'search_url': 'https://www.sanex.ro/index.php?route=product/search&search={}',
        'card': '.product-layout',
        'name': 'h4 a',
        'price': '.price',
        'link': 'h4 a'
    }
}

BLOCKED_DOMAINS = ['facebook.com', 'youtube.com', 'instagram.com', 'twitter.com', 
                   'linkedin.com', 'pinterest.com', 'olx.ro', 'publi24.ro', 
                   'wikipedia.org', 'google.', 'bing.', 'duckduckgo.']

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
]

def normalize_text(text):
    if not text: return ""
    text = unicodedata.normalize('NFD', text).encode('ascii', 'ignore').decode()
    return text.lower().strip()

def extract_domain(url):
    try:
        parsed = urlparse(url)
        return parsed.netloc.replace('www.', '')
    except:
        return ""

def is_blocked_domain(url):
    domain = extract_domain(url).lower()
    return any(blocked in domain for blocked in BLOCKED_DOMAINS)

def clean_price(text):
    if not text: return 0
    text_lower = str(text).lower()
    if any(x in text_lower for x in ['luna', 'rata', 'transport', 'livrare', '/luna', 'lei/']):
        return 0
    matches = re.findall(r'(\d[\d\.\,\s]*)', str(text))
    if not matches: return 0
    prices = []
    for m in matches:
        p = m.replace(' ', '').replace('.', '').replace(',', '.')
        try:
            val = float(p)
            if 10 < val < 500000:
                prices.append(val)
        except:
            pass
    return min(prices) if prices else 0

def validate_sku_match(sku, page_text):
    if not sku or len(sku) < 3: return True
    sku_norm = normalize_text(str(sku))
    page_norm = normalize_text(str(page_text)[:10000])
    if re.search(r'\b' + re.escape(sku_norm) + r'\b', page_norm): return True
    if sku_norm in page_norm: return True
    return False

def validate_name_match(name, page_text):
    if not name: return True
    name_norm = normalize_text(name)
    page_norm = normalize_text(str(page_text)[:10000])
    stop_words = {'pentru', 'cm', 'alb', 'alba', 'negru', 'cu', 'de', 'si', 'la', 'din', 'x', 'mm', 'set', 'tip'}
    keywords = [w for w in name_norm.split() if w not in stop_words and len(w) > 2][:5]
    if not keywords: return True
    matches = sum(1 for kw in keywords if kw in page_norm)
    return matches >= min(2, len(keywords))

# ═══════════════════════════════════════════════════════════════
# EXTRAGERE PREȚ (JSON-LD > META > CSS)
# ═══════════════════════════════════════════════════════════════

def extract_price_jsonld(page):
    try:
        scripts = page.locator('script[type="application/ld+json"]').all()
        for script in scripts:
            try:
                data = json.loads(script.inner_text())
                items = data if isinstance(data, list) else [data]
                for item in items:
                    products = []
                    if item.get('@type') == 'Product':
                        products.append(item)
                    elif '@graph' in item:
                        products.extend([x for x in item['@graph'] if x.get('@type') == 'Product'])
                    for product in products:
                        offers = product.get('offers', {})
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        price = offers.get('price') or offers.get('lowPrice') or offers.get('highPrice')
                        if price:
                            return float(str(price).replace(',', '.'))
            except:
                continue
    except:
        pass
    return 0

def extract_price_meta(page):
    selectors = ['meta[property="product:price:amount"]', 'meta[property="og:price:amount"]', 
                 'meta[name="price"]', 'meta[itemprop="price"]']
    for selector in selectors:
        try:
            content = page.locator(selector).first.get_attribute('content')
            if content:
                price = clean_price(content)
                if price > 0: return price
        except:
            continue
    return 0

def extract_price_css(page):
    selectors = ['[data-price]', 'span[itemprop="price"]', '.product-new-price', '.price-new',
                 '.current-price', '.product-price', '.woocommerce-Price-amount', '.price .amount',
                 '.special-price .price', '.price-box .price', '.pret-produs', '.price-value', '.main-price']
    for selector in selectors:
        try:
            elements = page.locator(selector).all()
            for el in elements[:3]:
                data_price = el.get_attribute('data-price')
                if data_price:
                    try: return float(data_price)
                    except: pass
                content = el.get_attribute('content')
                if content:
                    price = clean_price(content)
                    if price > 0: return price
                text = el.inner_text()
                price = clean_price(text)
                if price > 0: return price
        except:
            continue
    return 0

def extract_price_from_page(page):
    price = extract_price_jsonld(page)
    if price > 0: return price
    price = extract_price_meta(page)
    if price > 0: return price
    price = extract_price_css(page)
    if price > 0: return price
    return 0

# ═══════════════════════════════════════════════════════════════
# DUCKDUCKGO SEARCH
# ═══════════════════════════════════════════════════════════════

def duckduckgo_search(context, query, max_results=8):
    urls = []
    page = None
    try:
        page = context.new_page()
        search_url = f"https://duckduckgo.com/?q={quote_plus(query)}&kl=ro-ro"
        logger.info(f"   🦆 DuckDuckGo: {query[:50]}...")
        page.goto(search_url, timeout=25000, wait_until='domcontentloaded')
        time.sleep(random.uniform(2, 3))
        
        results = page.locator('a[data-testid="result-title-a"]').all()
        if not results:
            results = page.locator('article a[href^="http"]').all()
        
        seen_domains = set()
        for result in results:
            try:
                href = result.get_attribute('href')
                if not href or is_blocked_domain(href): continue
                domain = extract_domain(href)
                if domain not in seen_domains:
                    seen_domains.add(domain)
                    urls.append(href)
                    if len(urls) >= max_results: break
            except:
                continue
        logger.info(f"   📋 DuckDuckGo: {len(urls)} URL-uri")
    except Exception as e:
        logger.warning(f"   ⚠️ DuckDuckGo error: {str(e)[:40]}")
    finally:
        if page: page.close()
    return urls

# ═══════════════════════════════════════════════════════════════
# DIRECT SCRAPING
# ═══════════════════════════════════════════════════════════════

def scrape_direct_site(context, site_name, config, sku, name):
    page = None
    try:
        page = context.new_page()
        search_term = sku if len(str(sku)) > 3 else name.split()[0]
        url = config['search_url'].format(quote_plus(search_term))
        page.goto(url, timeout=20000, wait_until='domcontentloaded')
        time.sleep(random.uniform(1.5, 2.5))
        try: page.click('button:has-text("Accept")', timeout=1500)
        except: pass
        
        cards = page.locator(config['card']).all()
        for card in cards[:3]:
            try:
                card_name = card.locator(config['name']).first.inner_text()
                if not validate_sku_match(sku, card_name) and not validate_name_match(name, card_name):
                    continue
                price_text = card.locator(config['price']).first.inner_text()
                price = clean_price(price_text)
                if price <= 0: continue
                try:
                    link = card.locator(config['link']).first.get_attribute('href')
                    if link and not link.startswith('http'):
                        link = f"https://{extract_domain(url)}{link}"
                except:
                    link = url
                return {'name': site_name, 'price': price, 'url': link or url}
            except:
                continue
    except Exception as e:
        logger.debug(f"   {site_name}: {str(e)[:30]}")
    finally:
        if page: page.close()
    return None

def scrape_url_for_price(context, url, sku, name):
    page = None
    try:
        page = context.new_page()
        page.goto(url, timeout=20000, wait_until='domcontentloaded')
        time.sleep(random.uniform(1, 2))
        try: page.click('button:has-text("Accept")', timeout=1500)
        except: pass
        
        page_text = page.content()
        if not validate_sku_match(sku, page_text):
            if not validate_name_match(name, page_text):
                return None
        
        price = extract_price_from_page(page)
        if price > 0:
            return {'name': extract_domain(url), 'price': price, 'url': url}
    except Exception as e:
        logger.debug(f"   Error {url[:30]}: {str(e)[:30]}")
    finally:
        if page: page.close()
    return None

# ═══════════════════════════════════════════════════════════════
# SCANARE PRINCIPALĂ
# ═══════════════════════════════════════════════════════════════

def scan_product(sku, name, your_price=0):
    found = []
    sku = str(sku).strip()
    name = str(name).strip()
    logger.info(f"🔎 Scanare: {sku} - {name[:40]}...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={'width': 1920, 'height': 1080},
            locale='ro-RO'
        )
        
        # ETAPA 1: DuckDuckGo Discovery
        discovery_urls = []
        if len(sku) > 3:
            urls = duckduckgo_search(context, f'{sku} pret site:.ro', max_results=5)
            discovery_urls.extend(urls)
            time.sleep(random.uniform(2, 3))
        
        if len(discovery_urls) < 3:
            name_short = ' '.join(name.split()[:4])
            urls = duckduckgo_search(context, f'{name_short} pret', max_results=5)
            for u in urls:
                if extract_domain(u) not in [extract_domain(x) for x in discovery_urls]:
                    discovery_urls.append(u)
        
        logger.info(f"   🌐 Discovery: {len(discovery_urls)} URL-uri")
        
        for url in discovery_urls[:6]:
            result = scrape_url_for_price(context, url, sku, name)
            if result:
                if not any(r['name'] == result['name'] for r in found):
                    found.append(result)
                    logger.info(f"   ✅ {result['name']}: {result['price']} Lei")
            time.sleep(random.uniform(0.5, 1))
        
        # ETAPA 2: Direct Scraping Fallback
        if len(found) < 3:
            logger.info(f"   🔄 Fallback: Direct scraping...")
            for site_name, config in DIRECT_SITES.items():
                if any(site_name.lower() in r['name'].lower() for r in found):
                    continue
                result = scrape_direct_site(context, site_name, config, sku, name)
                if result:
                    found.append(result)
                    logger.info(f"   ✅ {result['name']}: {result['price']} Lei")
                time.sleep(random.uniform(1, 2))
                if len(found) >= 5: break
        
        browser.close()
    
    # Calcul diferență %
    for item in found:
        if your_price > 0:
            diff = ((item['price'] - your_price) / your_price) * 100
            item['diff'] = round(diff, 1)
        else:
            item['diff'] = 0
    
    found.sort(key=lambda x: x['price'])
    logger.info(f"   📊 Total: {len(found)} rezultate")
    return found[:5]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    data = request.json
    sku = data.get('sku', '')
    name = data.get('name', '')
    your_price = float(data.get('price', 0) or 0)
    results = scan_product(sku, name, your_price)
    return jsonify({"status": "success", "competitors": results})

if __name__ == '__main__':
    logger.info("🚀 PriceMonitor v2.1 (DuckDuckGo + Direct + JSON-LD) pornit pe :8080")
    app.run(host='0.0.0.0', port=8080)
