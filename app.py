import re
import logging
import time
import random
import unicodedata
import json
from urllib.parse import quote_plus, urljoin
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__, template_folder='templates')
CORS(app)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

SITES = {
    'foglia.ro': 'https://www.foglia.ro/catalogsearch/result/?q={}',
    'bagno.ro': 'https://www.bagno.ro/catalogsearch/result/?q={}',
    'absulo.ro': 'https://www.absulo.ro/catalogsearch/result/?q={}',
    'sensodays.ro': 'https://www.sensodays.ro/catalogsearch/result/?q={}',
    'germanquality.ro': 'https://www.germanquality.ro/catalogsearch/result/?q={}',
    'romstal.ro': 'https://www.romstal.ro/cautare?q={}',
    'dedeman.ro': 'https://www.dedeman.ro/ro/cautare?q={}',
    'emag.ro': 'https://www.emag.ro/search/{}',
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

# ═══════════════════════════════════════════════════════════════
# AI-POWERED PRICE EXTRACTION (Euristică)
# ═══════════════════════════════════════════════════════════════

def extract_price_ai(page):
    """Extragere preț cu multiple metode + euristică"""
    prices_found = []
    
    # METODA 1: JSON-LD (cea mai sigură)
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
                                prices_found.append(('JSON-LD', p, 100))  # confidence 100%
            except:
                continue
    except:
        pass
    
    # METODA 2: META tags
    for sel in ['meta[property="product:price:amount"]', 'meta[property="og:price:amount"]']:
        try:
            p = clean_price(page.locator(sel).first.get_attribute('content'))
            if p > 0:
                prices_found.append(('META', p, 95))
        except:
            pass
    
    # METODA 3: Data attributes
    for sel in ['[data-price-amount]', '[data-price]', '[itemprop="price"]']:
        try:
            el = page.locator(sel).first
            p = clean_price(el.get_attribute('data-price-amount') or el.get_attribute('data-price') or el.get_attribute('content'))
            if p > 0:
                prices_found.append(('DATA-ATTR', p, 90))
        except:
            pass
    
    # METODA 4: Euristică - caută pattern de preț în zonele tipice
    try:
        # Caută elemente cu "price" în clasă/id
        for el in page.locator('[class*="price"], [id*="price"]').all()[:5]:
            text = el.inner_text()
            # Pattern: număr urmat de Lei/RON/lei
            matches = re.findall(r'([\d.,]+)\s*(?:Lei|RON|lei)', text)
            for match in matches:
                p = clean_price(match)
                if p > 0:
                    prices_found.append(('EURISTIC', p, 70))
    except:
        pass
    
    # Cross-validation: dacă avem multiple metode cu același preț, crește confidence
    if prices_found:
        # Sortează după confidence
        prices_found.sort(key=lambda x: x[2], reverse=True)
        best = prices_found[0]
        
        # Verifică dacă alte metode confirmă
        same_price_count = sum(1 for m, p, c in prices_found if abs(p - best[1]) < 1)
        if same_price_count >= 2:
            return best[1], f"{best[0]}✓"  # Verificat
        return best[1], best[0]
    
    return 0, None

# ═══════════════════════════════════════════════════════════════
# SMART LINK FINDER (Găsește produse în pagină)
# ═══════════════════════════════════════════════════════════════

def find_product_links_smart(page, domain, sku, name):
    """Găsește link-uri produse folosind multiple strategii"""
    sku_lower = sku.lower()
    sku_norm = normalize(sku)
    name_words = [w.lower() for w in name.split()[:3] if len(w) > 2]
    
    candidates = []
    base_url = f"https://www.{domain}"
    
    try:
        all_links = page.locator('a[href]').all()
        
        for link in all_links[:100]:
            try:
                href = link.get_attribute('href') or ''
                if not href or any(x in href.lower() for x in ['cart', 'login', 'account', 'mailto', 'javascript', '#', '.pdf', '.jpg']):
                    continue
                
                # Construiește URL complet
                if href.startswith('/'):
                    href = base_url + href
                elif not href.startswith('http'):
                    continue
                
                if domain not in href:
                    continue
                
                href_lower = href.lower()
                href_norm = normalize(href)
                
                score = 0
                
                # SCORING:
                # SKU în URL (cel mai important)
                if sku_lower in href_lower or sku_norm in href_norm:
                    score += 100
                
                # Cuvinte din nume în URL
                for word in name_words:
                    if word in href_lower:
                        score += 20
                
                # Link text conține SKU
                try:
                    link_text = link.inner_text().lower()
                    if sku_lower in link_text:
                        score += 50
                    for word in name_words:
                        if word in link_text:
                            score += 10
                except:
                    pass
                
                # URL pare a fi produs
                if any(x in href_lower for x in ['/p/', '/produs/', '/product/', '-p-', '.html']):
                    score += 5
                
                if score > 0:
                    candidates.append((score, href))
                    
            except:
                continue
        
        # Sortează după scor și returnează top 5
        candidates.sort(key=lambda x: x[0], reverse=True)
        return [url for score, url in candidates[:5]]
        
    except:
        pass
    
    return []

# ═══════════════════════════════════════════════════════════════
# ADAPTIVE SCRAPING
# ═══════════════════════════════════════════════════════════════

class AdaptiveScraper:
    def __init__(self):
        self.success_rate = {}
        self.delays = {}
    
    def get_delay(self, domain):
        return self.delays.get(domain, 1.5)
    
    def record_result(self, domain, success):
        if domain not in self.success_rate:
            self.success_rate[domain] = []
        self.success_rate[domain].append(success)
        
        # Păstrează ultimele 10
        self.success_rate[domain] = self.success_rate[domain][-10:]
        
        # Ajustează delay bazat pe success rate
        rate = sum(self.success_rate[domain]) / len(self.success_rate[domain])
        if rate < 0.3:
            self.delays[domain] = min(5, self.delays.get(domain, 1.5) + 0.5)
        elif rate > 0.7:
            self.delays[domain] = max(1, self.delays.get(domain, 1.5) - 0.2)

scraper = AdaptiveScraper()

def scrape_site(context, domain, search_url, sku, name):
    page = None
    
    try:
        page = context.new_page()
        url = search_url.format(quote_plus(sku))
        
        # Human-like behavior
        delay = scraper.get_delay(domain)
        
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        time.sleep(delay)
        
        # Random mouse movement (anti-detect)
        try:
            page.mouse.move(random.randint(100, 500), random.randint(100, 400))
        except:
            pass
        
        # Accept cookies
        try:
            page.click('button:has-text("Accept")', timeout=500)
        except:
            pass
        
        # Smart link finding
        product_links = find_product_links_smart(page, domain, sku, name)
        
        if not product_links:
            # Fallback: încearcă căutare cu nume
            name_short = ' '.join(name.split()[:3])
            url2 = search_url.format(quote_plus(name_short))
            page.goto(url2, timeout=15000, wait_until='domcontentloaded')
            time.sleep(1)
            product_links = find_product_links_smart(page, domain, sku, name)
        
        if not product_links:
            logger.info(f"   ⚪ {domain}: 0 produse")
            scraper.record_result(domain, False)
            return None
        
        logger.info(f"   🔍 {domain}: {len(product_links)} candidați")
        
        # Verifică fiecare link
        for href in product_links:
            try:
                page.goto(href, timeout=12000, wait_until='domcontentloaded')
                time.sleep(0.8)
                
                # Verifică SKU în pagină
                body = page.locator('body').inner_text()
                if normalize(sku) not in normalize(body) and normalize(sku)[1:] not in normalize(body):
                    continue
                
                # Extrage preț
                price, method = extract_price_ai(page)
                
                if price > 0:
                    scraper.record_result(domain, True)
                    return {'name': domain, 'price': price, 'url': href, 'method': method}
                    
            except:
                continue
        
        scraper.record_result(domain, False)
        logger.info(f"   ⚪ {domain}: SKU negăsit în candidați")
        
    except Exception as e:
        logger.info(f"   ❌ {domain}: {str(e)[:30]}")
        scraper.record_result(domain, False)
    finally:
        if page:
            page.close()
    
    return None

def scan_product(sku, name, your_price=0):
    found = []
    sku = str(sku).strip()
    name = str(name).strip()
    
    logger.info(f"🔎 {sku} - {name[:35]}...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale='ro-RO'
        )
        
        for domain, search_url in SITES.items():
            result = scrape_site(context, domain, search_url, sku, name)
            
            if result:
                if your_price > 0:
                    result['diff'] = round(((result['price'] - your_price) / your_price) * 100, 1)
                else:
                    result['diff'] = 0
                found.append(result)
                logger.info(f"   ✅ {domain}: {result['price']} Lei [{result['method']}]")
            
            time.sleep(0.3)
        
        browser.close()
    
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
    logger.info("🚀 PriceMonitor v6.0 (AI-Powered + Adaptive) pe :8080")
    app.run(host='0.0.0.0', port=8080)
