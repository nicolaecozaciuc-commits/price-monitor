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

# Site-uri blocate (nu sunt magazine)
BLOCKED_DOMAINS = ['facebook.com', 'youtube.com', 'instagram.com', 'twitter.com', 'tiktok.com', 
                   'linkedin.com', 'pinterest.com', 'olx.ro', 'publi24.ro', 'lajumate.ro',
                   'google.com', 'google.ro', 'wikipedia.org']

# User agents realiști
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"
]

def normalize_text(text):
    """Elimină diacritice și caractere speciale"""
    if not text:
        return ""
    text = unicodedata.normalize('NFD', text).encode('ascii', 'ignore').decode()
    return text.lower().strip()

def extract_domain(url):
    """Extrage domeniul din URL"""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.replace('www.', '')
        return domain
    except:
        return ""

def is_valid_shop_url(url):
    """Verifică dacă URL-ul e de la un magazin valid"""
    domain = extract_domain(url)
    if not domain:
        return False
    # Verifică dacă e blocat
    for blocked in BLOCKED_DOMAINS:
        if blocked in domain:
            return False
    # Preferă .ro dar acceptă și altele
    return True

def clean_price(text):
    """Extrage prețul din text"""
    if not text:
        return 0
    text_lower = text.lower()
    # Ignoră prețuri de transport, rate, etc.
    if any(x in text_lower for x in ['luna', 'rata', 'transport', 'livrare', '/luna', 'lei/', 'de la']):
        return 0
    # Găsește toate numerele
    matches = re.findall(r'(\d[\d\.\,\s]*)', text)
    if not matches:
        return 0
    prices = []
    for m in matches:
        # Curăță și convertește
        p = m.replace(' ', '').replace('.', '').replace(',', '.')
        try:
            val = float(p)
            if val > 10:  # Ignoră prețuri sub 10 lei
                prices.append(val)
        except:
            pass
    return min(prices) if prices else 0  # Ia cel mai mic (prețul principal)

def validate_product_match(sku, name, page_text):
    """Verifică dacă pagina conține produsul căutat"""
    page_text = normalize_text(page_text)
    sku_norm = normalize_text(str(sku))
    name_norm = normalize_text(name)
    
    # Verifică SKU (exact match)
    if len(sku_norm) > 3 and sku_norm in page_text:
        return True
    
    # Verifică cuvinte din nume (minim 2 din 4)
    stop_words = {'pentru', 'cm', 'alb', 'alba', 'negru', 'cu', 'de', 'si', 'la', 'din', 'x', 'mm', 'set'}
    name_parts = [w for w in name_norm.split() if w not in stop_words and len(w) > 2][:4]
    matches = sum(1 for part in name_parts if part in page_text)
    
    return matches >= 2

def extract_price_from_page(page):
    """Extrage prețul din pagină folosind multiple metode"""
    
    # Metoda 1: JSON-LD Schema (cea mai precisă)
    try:
        scripts = page.locator('script[type="application/ld+json"]').all()
        for script in scripts:
            try:
                data = json.loads(script.inner_text())
                # Poate fi array sau obiect
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get('@type') == 'Product':
                        offers = item.get('offers', {})
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        price = offers.get('price') or offers.get('lowPrice')
                        if price:
                            return float(price)
            except:
                continue
    except:
        pass
    
    # Metoda 2: Meta tags
    try:
        meta_price = page.locator('meta[property="product:price:amount"]').get_attribute('content')
        if meta_price:
            return float(meta_price.replace(',', '.'))
    except:
        pass
    
    # Metoda 3: Selectoare CSS comune pentru prețuri
    price_selectors = [
        '.price-new', '.product-price', '.price', '.current-price',
        '[data-price]', '.woocommerce-Price-amount', '.product-new-price',
        '.price-box .price', '.special-price .price', '.regular-price',
        'span[itemprop="price"]', '.price-value', '.product-price-value',
        '.pret', '.pret-produs', '.price-current', '.main-price'
    ]
    
    for selector in price_selectors:
        try:
            elements = page.locator(selector).all()
            for el in elements[:3]:  # Primele 3
                text = el.inner_text()
                price = clean_price(text)
                if price > 0:
                    return price
                # Verifică și atributul data-price
                data_price = el.get_attribute('data-price')
                if data_price:
                    try:
                        return float(data_price)
                    except:
                        pass
        except:
            continue
    
    # Metoda 4: Regex pe întreaga pagină (fallback)
    try:
        body_text = page.locator('body').inner_text()
        # Caută pattern-uri de preț în RON/Lei
        patterns = [
            r'(\d[\d\.\,\s]*)\s*(?:lei|ron|RON|Lei|LEI)',
            r'(?:preț|pret|price)[\s:]*(\d[\d\.\,\s]*)',
        ]
        for pattern in patterns:
            matches = re.findall(pattern, body_text, re.IGNORECASE)
            prices = []
            for m in matches:
                p = clean_price(m)
                if p > 10:
                    prices.append(p)
            if prices:
                return min(prices)
    except:
        pass
    
    return 0

def google_search(browser_context, query, max_results=10):
    """Caută pe Google și returnează URL-uri de magazine"""
    urls = []
    page = None
    
    try:
        page = browser_context.new_page()
        search_url = f"https://www.google.ro/search?q={quote_plus(query)}&hl=ro&gl=ro&num=20"
        
        logger.info(f"   🔍 Google: {query[:50]}...")
        
        page.goto(search_url, timeout=30000, wait_until='domcontentloaded')
        time.sleep(random.uniform(2, 4))
        
        # Accept cookies dacă apare
        try:
            page.click('button:has-text("Accept")', timeout=2000)
            time.sleep(1)
        except:
            pass
        
        # Verifică CAPTCHA
        if 'unusual traffic' in page.content().lower() or 'captcha' in page.content().lower():
            logger.warning("   ⚠️ Google CAPTCHA detectat")
            return []
        
        # Extrage rezultatele
        results = page.locator('div.g a[href^="http"]').all()
        
        for result in results:
            try:
                href = result.get_attribute('href')
                if href and is_valid_shop_url(href):
                    domain = extract_domain(href)
                    # Evită duplicate de pe același domeniu
                    if not any(extract_domain(u) == domain for u in urls):
                        urls.append(href)
                        if len(urls) >= max_results:
                            break
            except:
                continue
        
        logger.info(f"   📋 Găsite {len(urls)} site-uri")
        
    except Exception as e:
        logger.error(f"   ❌ Google error: {str(e)[:50]}")
    finally:
        if page:
            page.close()
    
    return urls

def scrape_price_from_url(browser_context, url, sku, name):
    """Accesează URL-ul și extrage prețul"""
    page = None
    try:
        page = browser_context.new_page()
        page.goto(url, timeout=25000, wait_until='domcontentloaded')
        time.sleep(random.uniform(1.5, 3))
        
        # Accept cookies
        try:
            page.click('button:has-text("Accept")', timeout=1500)
        except:
            pass
        
        # Verifică dacă e produsul corect
        page_text = page.locator('body').inner_text()[:5000]  # Primele 5000 caractere
        
        if not validate_product_match(sku, name, page_text):
            return None
        
        # Extrage prețul
        price = extract_price_from_page(page)
        
        if price > 0:
            domain = extract_domain(url)
            return {
                'name': domain,
                'price': price,
                'url': url
            }
        
    except Exception as e:
        logger.debug(f"   Error scraping {url[:30]}: {str(e)[:30]}")
    finally:
        if page:
            page.close()
    
    return None

def scan_product(sku, name, your_price=0):
    """Scanează un produs folosind Google + extragere directă"""
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
        
        # Strategii de căutare (în ordinea priorității)
        search_queries = []
        
        # 1. SKU exact (cel mai precis)
        if len(sku) > 3:
            search_queries.append(f'"{sku}" preț lei')
        
        # 2. SKU + parte din nume
        name_short = ' '.join(name.split()[:3])
        if len(sku) > 3:
            search_queries.append(f'{sku} {name_short} preț')
        
        # 3. Denumire completă
        search_queries.append(f'{name} preț lei site:.ro')
        
        all_urls = []
        
        # Căutări pe Google
        for query in search_queries:
            if len(all_urls) >= 8:  # Suficiente URL-uri
                break
            
            urls = google_search(context, query, max_results=5)
            
            for url in urls:
                domain = extract_domain(url)
                if not any(extract_domain(u) == domain for u in all_urls):
                    all_urls.append(url)
            
            # Pauză între căutări Google
            time.sleep(random.uniform(3, 5))
        
        logger.info(f"   🌐 Total {len(all_urls)} URL-uri de verificat")
        
        # Extrage prețuri de pe fiecare URL
        for url in all_urls[:10]:  # Max 10 site-uri
            result = scrape_price_from_url(context, url, sku, name)
            if result:
                # Calculează diferența procentuală
                if your_price > 0:
                    diff = ((result['price'] - your_price) / your_price) * 100
                    result['diff'] = round(diff, 1)
                else:
                    result['diff'] = 0
                
                found.append(result)
                logger.info(f"   ✅ {result['name']}: {result['price']} Lei ({result['diff']:+.1f}%)")
            
            # Pauză între site-uri
            time.sleep(random.uniform(1, 2))
        
        browser.close()
    
    # Sortează după preț și returnează top 5
    found.sort(key=lambda x: x['price'])
    return found[:5]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    data = request.json
    sku = data.get('sku', '')
    name = data.get('name', '')
    your_price = data.get('price', 0)
    
    results = scan_product(sku, name, your_price)
    
    return jsonify({
        "status": "success",
        "competitors": results
    })

if __name__ == '__main__':
    logger.info("🚀 PriceMonitor v2.0 (Google + Direct) pornit pe :8080")
    app.run(host='0.0.0.0', port=8080)
