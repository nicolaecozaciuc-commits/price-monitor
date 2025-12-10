import re
import logging
import time
import random
import unicodedata
import json
import os
from urllib.parse import quote_plus, urlparse
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
# SITE-URI DE BAZĂ (known good)
# ═══════════════════════════════════════════════════════════════
BASE_SITES = [
    'dedeman.ro', 'emag.ro', 'hornbach.ro', 'romstal.ro', 'obsentum.com',
    'sanex.ro', 'absulo.ro', 'mathaus.ro', 'bricodepot.ro',
    # Site-uri descoperite din Google
    'neakaisa.ro', 'sanitino.ro', 'sensodays.ro', 'euro-instal.ro',
    'foglia.ro', 'germanquality.ro', 'bagno.ro', 'conrep.ro',
    'novambient.ro', 'hvbtermice.ro', 'hvbklimatik.ro', 'shopmania.ro'
]

# Fișier pentru site-uri învățate
LEARNED_SITES_FILE = '/root/monitor/learned_sites.json'

# Domenii blocate
BLOCKED_DOMAINS = ['facebook.com', 'youtube.com', 'instagram.com', 'linkedin.com', 
                   'pinterest.com', 'olx.ro', 'wikipedia.org', 'google.', 'bing.']

def load_learned_sites():
    """Încarcă site-uri învățate din fișier"""
    try:
        if os.path.exists(LEARNED_SITES_FILE):
            with open(LEARNED_SITES_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {'sites': [], 'search_urls': {}}

def save_learned_sites(data):
    """Salvează site-uri noi învățate"""
    try:
        with open(LEARNED_SITES_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except:
        pass

def normalize_text(text):
    if not text: return ""
    text = unicodedata.normalize('NFD', text).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9\s]', '', text.lower()).strip()

def clean_price(value):
    if not value: return 0
    if isinstance(value, (int, float)):
        return float(value) if value > 10 else 0
    text = str(value).lower()
    if any(x in text for x in ['luna', 'rata', 'transport', '/luna']):
        return 0
    text = re.sub(r'[^\d,.]', '', str(value))
    if not text: return 0
    if ',' in text and '.' in text:
        if text.rindex(',') > text.rindex('.'):
            text = text.replace('.', '').replace(',', '.')
        else:
            text = text.replace(',', '')
    elif ',' in text:
        text = text.replace(',', '.')
    try:
        price = float(text)
        return price if 10 < price < 500000 else 0
    except:
        return 0

def extract_domain(url):
    try:
        parsed = urlparse(url)
        return parsed.netloc.replace('www.', '')
    except:
        return ""

def is_valid_shop(url):
    domain = extract_domain(url).lower()
    if not domain or not domain.endswith('.ro') and not domain.endswith('.com'):
        return False
    return not any(b in domain for b in BLOCKED_DOMAINS)

# ═══════════════════════════════════════════════════════════════
# GOOGLE DISCOVERY - Găsește site-uri noi
# ═══════════════════════════════════════════════════════════════

def google_discover_sites(context, query):
    """
    Caută pe Google și extrage URL-urile site-urilor.
    Returnează lista de URL-uri de produse găsite.
    """
    discovered = []
    page = None
    
    try:
        page = context.new_page()
        search_url = f"https://www.google.ro/search?q={quote_plus(query + ' pret')}&hl=ro&gl=ro&num=20"
        
        logger.info(f"   🔍 Google Discovery: {query[:30]}...")
        
        page.goto(search_url, timeout=20000, wait_until='domcontentloaded')
        time.sleep(random.uniform(2, 3))
        
        # Verifică CAPTCHA
        content = page.content().lower()
        if 'unusual traffic' in content or 'captcha' in content:
            logger.warning(f"   ⚠️ Google CAPTCHA - skip discovery")
            return []
        
        # Extrage link-uri din rezultate organice
        links = page.locator('div.g a[href^="http"]').all()
        
        seen_domains = set()
        for link in links:
            try:
                href = link.get_attribute('href')
                if not href or not is_valid_shop(href):
                    continue
                
                domain = extract_domain(href)
                if domain not in seen_domains:
                    seen_domains.add(domain)
                    discovered.append({
                        'url': href,
                        'domain': domain
                    })
                    
                    if len(discovered) >= 10:
                        break
            except:
                continue
        
        logger.info(f"   📋 Descoperite: {[d['domain'] for d in discovered]}")
        
    except Exception as e:
        logger.debug(f"   Google error: {str(e)[:40]}")
    finally:
        if page:
            page.close()
    
    return discovered

# ═══════════════════════════════════════════════════════════════
# EXTRAGERE PREȚ DIN PAGINĂ
# ═══════════════════════════════════════════════════════════════

def extract_price_jsonld(page):
    """Extrage preț din JSON-LD (cea mai precisă metodă)"""
    try:
        scripts = page.locator('script[type="application/ld+json"]').all()
        for script in scripts:
            try:
                data = json.loads(script.inner_text())
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if '@graph' in item:
                        items.extend(item['@graph'])
                for item in items:
                    if item.get('@type') != 'Product':
                        continue
                    offers = item.get('offers', {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get('price') or offers.get('lowPrice')
                    if price:
                        return clean_price(price)
            except:
                continue
    except:
        pass
    return 0

def extract_price_meta(page):
    """Extrage preț din meta tags"""
    for sel in ['meta[property="product:price:amount"]', 'meta[property="og:price:amount"]']:
        try:
            content = page.locator(sel).first.get_attribute('content')
            price = clean_price(content)
            if price > 0:
                return price
        except:
            continue
    return 0

def extract_price_css(page):
    """Extrage preț din CSS selectoare"""
    selectors = [
        '[data-price-amount]', '[data-price]', 'span[itemprop="price"]',
        '.product-new-price', '.price-new', '.current-price', '.special-price .price',
        '.product-price', '.price', '[class*="price"]'
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            # Atribute
            for attr in ['data-price-amount', 'data-price', 'content']:
                val = el.get_attribute(attr)
                if val:
                    price = clean_price(val)
                    if price > 0:
                        return price
            # Text
            price = clean_price(el.inner_text())
            if price > 0:
                return price
        except:
            continue
    return 0

def extract_price(page):
    """Extrage preț folosind toate metodele"""
    price = extract_price_jsonld(page)
    if price > 0:
        return price, 'JSON-LD'
    
    price = extract_price_meta(page)
    if price > 0:
        return price, 'META'
    
    price = extract_price_css(page)
    if price > 0:
        return price, 'CSS'
    
    return 0, None

# ═══════════════════════════════════════════════════════════════
# VALIDARE PRODUS
# ═══════════════════════════════════════════════════════════════

def validate_product(sku, name, page_text):
    """Verifică dacă pagina conține produsul căutat"""
    sku_norm = normalize_text(str(sku))
    page_norm = normalize_text(page_text[:5000])
    name_norm = normalize_text(name)
    
    # SKU match
    if len(sku_norm) >= 4:
        if sku_norm in page_norm:
            return True
        if sku_norm[1:] in page_norm:  # Fără prima literă
            return True
    
    # Keywords match (min 3)
    stop = {'pentru', 'cm', 'cu', 'de', 'si', 'la', 'din', 'x', 'mm', 'set', 'alba', 'alb'}
    keywords = [w for w in name_norm.split() if w not in stop and len(w) > 2]
    matches = sum(1 for kw in keywords if kw in page_norm)
    
    return matches >= 3

# ═══════════════════════════════════════════════════════════════
# SCRAPE UN URL SPECIFIC
# ═══════════════════════════════════════════════════════════════

def scrape_product_url(context, url, sku, name):
    """Accesează un URL și extrage prețul dacă e produsul corect"""
    page = None
    try:
        page = context.new_page()
        page.goto(url, timeout=20000, wait_until='domcontentloaded')
        time.sleep(random.uniform(1.5, 2.5))
        
        # Accept cookies
        for btn in ['Accept', 'Acceptă', 'OK']:
            try:
                page.click(f'button:has-text("{btn}")', timeout=1000)
                break
            except:
                pass
        
        # Verifică dacă e produsul corect
        page_text = page.locator('body').inner_text()
        
        if not validate_product(sku, name, page_text):
            return None
        
        # Extrage prețul
        price, method = extract_price(page)
        
        if price > 0:
            domain = extract_domain(url)
            return {
                'name': domain,
                'price': price,
                'url': url,
                'method': method
            }
        
    except Exception as e:
        logger.debug(f"   Error {url[:30]}: {str(e)[:30]}")
    finally:
        if page:
            page.close()
    
    return None

# ═══════════════════════════════════════════════════════════════
# CĂUTARE PE SITE CUNOSCUT
# ═══════════════════════════════════════════════════════════════

def search_on_site(context, domain, sku, name):
    """Caută produsul pe un site specific"""
    
    # Patterns de căutare pentru diferite site-uri
    search_patterns = {
        'dedeman.ro': 'https://www.dedeman.ro/ro/cautare?q={}',
        'emag.ro': 'https://www.emag.ro/search/{}',
        'hornbach.ro': 'https://www.hornbach.ro/s/{}',
        'romstal.ro': 'https://www.romstal.ro/cautare?q={}',
        'obsentum.com': 'https://obsentum.com/catalogsearch/result/?q={}',
        'absulo.ro': 'https://www.absulo.ro/catalogsearch/result/?q={}',
        'neakaisa.ro': 'https://neakaisa.ro/cautare?search={}',
        'sanitino.ro': 'https://www.sanitino.ro/cauta/?q={}',
        'sensodays.ro': 'https://www.sensodays.ro/cautare?q={}',
        'foglia.ro': 'https://www.foglia.ro/cautare?q={}',
        'bagno.ro': 'https://www.bagno.ro/cautare?q={}',
        'mathaus.ro': 'https://www.mathaus.ro/cautare/{}',
        'bricodepot.ro': 'https://www.bricodepot.ro/search/?q={}',
    }
    
    # Găsește pattern-ul sau construiește unul generic
    search_url = None
    for pattern_domain, pattern in search_patterns.items():
        if pattern_domain in domain:
            search_url = pattern.format(quote_plus(sku))
            break
    
    if not search_url:
        # Pattern generic
        search_url = f"https://www.{domain}/cautare?q={quote_plus(sku)}"
    
    page = None
    try:
        page = context.new_page()
        page.goto(search_url, timeout=20000, wait_until='domcontentloaded')
        time.sleep(random.uniform(1.5, 2.5))
        
        # Accept cookies
        for btn in ['Accept', 'Acceptă', 'OK']:
            try:
                page.click(f'button:has-text("{btn}")', timeout=1000)
                break
            except:
                pass
        
        # Găsește primul produs valid
        # Caută link-uri către produse
        product_links = page.locator('a[href*="/p/"], a[href*="/produs/"], a[href*="/product/"]').all()
        
        if not product_links:
            # Fallback: orice link care pare a fi produs
            product_links = page.locator('.product a, .product-item a, [class*="product"] a').all()
        
        for link in product_links[:5]:
            try:
                href = link.get_attribute('href')
                if not href:
                    continue
                
                if not href.startswith('http'):
                    href = f"https://www.{domain}{href}"
                
                # Verifică dacă URL-ul pare a fi pagină de produs
                if '/cart' in href or '/login' in href or '/account' in href:
                    continue
                
                # Navighează și verifică
                page.goto(href, timeout=15000, wait_until='domcontentloaded')
                time.sleep(1)
                
                page_text = page.locator('body').inner_text()
                
                if validate_product(sku, name, page_text):
                    price, method = extract_price(page)
                    if price > 0:
                        return {
                            'name': domain,
                            'price': price,
                            'url': href,
                            'method': method
                        }
            except:
                continue
        
    except Exception as e:
        logger.debug(f"   {domain}: {str(e)[:30]}")
    finally:
        if page:
            page.close()
    
    return None

# ═══════════════════════════════════════════════════════════════
# FUNCȚIA PRINCIPALĂ
# ═══════════════════════════════════════════════════════════════

def scan_product(sku, name, your_price=0):
    """
    Strategie:
    1. Google Discovery - găsește site-uri care au produsul
    2. Accesează URL-urile directe găsite
    3. Fallback: caută pe site-uri cunoscute
    """
    found = []
    sku = str(sku).strip()
    name = str(name).strip()
    
    logger.info(f"🔎 Scanare: {sku} - {name[:40]}...")
    
    # Încarcă site-uri învățate
    learned = load_learned_sites()
    all_known_sites = set(BASE_SITES + learned.get('sites', []))
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale='ro-RO'
        )
        
        # ═══════════════════════════════════════════════════════
        # ETAPA 1: Google Discovery
        # ═══════════════════════════════════════════════════════
        discovered = google_discover_sites(context, sku)
        
        if not discovered and name:
            # Încearcă cu numele
            name_short = ' '.join(name.split()[:4])
            time.sleep(random.uniform(2, 3))
            discovered = google_discover_sites(context, name_short)
        
        # Salvează site-uri noi
        new_sites = []
        for d in discovered:
            if d['domain'] not in all_known_sites:
                new_sites.append(d['domain'])
                all_known_sites.add(d['domain'])
        
        if new_sites:
            learned['sites'] = list(set(learned.get('sites', []) + new_sites))
            save_learned_sites(learned)
            logger.info(f"   💾 Site-uri noi salvate: {new_sites}")
        
        # ═══════════════════════════════════════════════════════
        # ETAPA 2: Accesează URL-urile directe din Google
        # ═══════════════════════════════════════════════════════
        for d in discovered:
            result = scrape_product_url(context, d['url'], sku, name)
            if result:
                if not any(r['name'] == result['name'] for r in found):
                    found.append(result)
                    logger.info(f"   ✅ {result['name']}: {result['price']} Lei [{result['method']}]")
            time.sleep(random.uniform(0.5, 1))
            
            if len(found) >= 5:
                break
        
        # ═══════════════════════════════════════════════════════
        # ETAPA 3: Fallback - Caută pe site-uri cunoscute
        # ═══════════════════════════════════════════════════════
        if len(found) < 3:
            logger.info(f"   🔄 Fallback: căutare pe site-uri cunoscute...")
            
            priority_sites = ['dedeman.ro', 'emag.ro', 'romstal.ro', 'absulo.ro', 
                             'neakaisa.ro', 'sanitino.ro', 'sensodays.ro', 'bagno.ro']
            
            for domain in priority_sites:
                if any(domain in r['name'] for r in found):
                    continue
                
                result = search_on_site(context, domain, sku, name)
                if result:
                    found.append(result)
                    logger.info(f"   ✅ {result['name']}: {result['price']} Lei [{result['method']}]")
                
                time.sleep(random.uniform(1, 2))
                
                if len(found) >= 5:
                    break
        
        browser.close()
    
    # Calcul diferență %
    for item in found:
        if your_price > 0:
            diff = ((item['price'] - your_price) / your_price) * 100
            item['diff'] = round(diff, 1)
        else:
            item['diff'] = 0
    
    # Sortează după preț
    found.sort(key=lambda x: x['price'])
    logger.info(f"📊 Total: {len(found)} rezultate")
    
    return found[:5]

# ═══════════════════════════════════════════════════════════════
# API PENTRU ADMINISTRARE SITE-URI
# ═══════════════════════════════════════════════════════════════

@app.route('/api/sites', methods=['GET'])
def get_sites():
    """Returnează toate site-urile cunoscute"""
    learned = load_learned_sites()
    return jsonify({
        'base_sites': BASE_SITES,
        'learned_sites': learned.get('sites', [])
    })

@app.route('/api/sites/add', methods=['POST'])
def add_site():
    """Adaugă un site manual"""
    data = request.json
    domain = data.get('domain', '').strip()
    if domain:
        learned = load_learned_sites()
        if domain not in learned.get('sites', []):
            learned['sites'] = learned.get('sites', []) + [domain]
            save_learned_sites(learned)
        return jsonify({'status': 'ok', 'domain': domain})
    return jsonify({'status': 'error'})

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
    logger.info("🚀 PriceMonitor v4.0 (Auto-Discovery + Learning) pe :8080")
    app.run(host='0.0.0.0', port=8080)
