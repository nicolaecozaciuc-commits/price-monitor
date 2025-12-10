import re
import logging
import time
import random
import json
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__, template_folder='templates')
CORS(app)

# Configurare Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# --- CONFIGURARE DIRECT SEARCH FALLBACK ---
# Folosit dacă Google/Discovery eșuează complet
DIRECT_TARGETS = {
    'dedeman.ro': 'https://www.dedeman.ro/ro/cautare?q={}',
    'emag.ro': 'https://www.emag.ro/search/{}',
    'hornbach.ro': 'https://www.hornbach.ro/s/{}',
    'leroymerlin.ro': 'https://www.leroymerlin.ro/search/{}',
    'romstal.ro': 'https://www.romstal.ro/cautare.html?q={}',
    'bricodepot.ro': 'https://www.bricodepot.ro/cautare/?q={}',
    'mathaus.ro': 'https://mathaus.ro/search?text={}',
    'sanex.ro': 'https://www.sanex.ro/index.php?route=product/search&search={}',
    'gemibai.ro': 'https://store.gemibai.ro/index.php?route=product/search&search={}'
}

def clean_price(text):
    """Curăță prețul din orice format text"""
    if not text: return 0
    text = str(text)
    # Găsește secvențe numerice
    matches = re.findall(r'(\d[\d\.,]*)', text)
    if not matches: return 0
    
    price_str = max(matches, key=len)
    
    # Logică românească: 1.200,50 -> 1200.50
    if ',' in price_str and '.' in price_str:
        if price_str.find('.') < price_str.find(','): 
            price_str = price_str.replace('.', '').replace(',', '.')
        else:
            price_str = price_str.replace(',', '')
    elif ',' in price_str:
        price_str = price_str.replace(',', '.')
        
    try: return float(price_str)
    except: return 0

# --- MODULE DE EXTRACȚIE ---

def extract_json_ld(page):
    """
    NIVEL 1: Extragere structurată (Cea mai precisă)
    Caută schema.org/Product în sursa paginii
    """
    try:
        scripts = page.locator('script[type="application/ld+json"]').all()
        for script in scripts:
            try:
                content = script.inner_text()
                data = json.loads(content)
                
                # Normalizare date (unele sunt liste, altele dict)
                items = data if isinstance(data, list) else [data]
                if '@graph' in data: items.extend(data['@graph'])

                for item in items:
                    if item.get('@type') == 'Product':
                        offers = item.get('offers')
                        if isinstance(offers, list): offers = offers[0]
                        if not offers: continue
                        
                        price = offers.get('price')
                        currency = offers.get('priceCurrency', 'RON')
                        
                        if price:
                            logger.info("   ✨ Preț găsit via JSON-LD (Schema.org)")
                            return float(price), currency
            except: continue
    except Exception as e:
        pass
    return None, None

def extract_meta_tags(page):
    """NIVEL 2: OpenGraph și Meta Tags"""
    try:
        price = page.locator('meta[property="product:price:amount"]').get_attribute('content')
        if price: 
            logger.info("   ✨ Preț găsit via Meta Tags")
            return float(price), "RON"
    except: pass
    return None, None

def extract_visual(page):
    """NIVEL 3: Selectoare CSS (Fallback vizual)"""
    selectors = [
        '.product-price', '.price', '.product-new-price', 
        '.current-price', '.price-container', '.special-price'
    ]
    for sel in selectors:
        if page.locator(sel).count() > 0:
            text = page.locator(sel).first.inner_text()
            price = clean_price(text)
            if price > 0:
                logger.info(f"   👁️ Preț găsit via CSS ({sel})")
                return price, "RON"
    return 0, None

# --- MOTOR DE CĂUTARE ---

def discovery_phase(page, query):
    """
    ETAPA 1: Descoperire URL-uri
    Folosește Google cu comportament uman
    """
    links = []
    try:
        logger.info(f"🕵️ Discovery: {query}")
        page.goto(f"https://www.google.com/search?q={query}", timeout=25000)
        
        # Human behavior: Click random pentru a părea om
        try: page.mouse.move(random.randint(100, 500), random.randint(100, 500))
        except: pass
        
        # Accept cookies
        try: page.click('div:has-text("Acceptă tot")', timeout=2000)
        except: pass

        # Așteptăm rezultate
        page.wait_for_selector('#search', timeout=8000)
        
        results = page.locator('.g a').all()
        for res in results[:6]:
            url = res.get_attribute('href')
            if url and '.ro' in url and 'google' not in url:
                links.append(url)
                
    except Exception as e:
        logger.warning(f"⚠️ Discovery Error: {e}")
        
    return list(set(links)) # Elimină duplicate

def analyze_page(page, url):
    """
    ETAPA 2: Analiză și Extragere Hibridă
    """
    try:
        domain = url.split('/')[2].replace('www.', '').split('.')[0].capitalize()
        logger.info(f"   >> Analizez: {domain}")
        
        page.goto(url, timeout=25000, wait_until='domcontentloaded')
        
        # Nivel 1: JSON-LD
        price, _ = extract_json_ld(page)
        method = "JSON-LD"
        
        # Nivel 2: Meta Tags
        if not price:
            price, _ = extract_meta_tags(page)
            method = "Meta"
            
        # Nivel 3: Visual
        if not price:
            price, _ = extract_visual(page)
            method = "Visual"

        if price and price > 0:
            return {
                "name": domain,
                "price": price,
                "url": url,
                "method": method
            }
    except: pass
    return None

def scan_hybrid(sku, name):
    found = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        page = context.new_page()

        # 1. Discovery
        query = f"{sku} {name} pret site:.ro"
        urls = discovery_phase(page, query)
        
        # Fallback: Dacă Google nu dă nimic, încercăm direct pe site-uri mari
        if not urls:
            logger.info("⚠️ Fallback la Direct Search...")
            for domain, search_url in DIRECT_TARGETS.items():
                if len(found) >= 3: break # Ne oprim dacă avem deja rezultate
                try:
                    direct_url = search_url.format(sku)
                    page.goto(direct_url, timeout=15000)
                    # Simplificat: luăm primul link din rezultate
                    first_link = page.locator('a[href*="/product/"], a[href*="/p/"]').first
                    if first_link.count() > 0:
                        href = first_link.get_attribute('href')
                        if href:
                            if 'http' not in href: href = 'https://' + domain + href
                            urls.append(href)
                except: continue

        # 2. Extraction
        for url in urls[:7]: # Analizăm maxim 7 link-uri pentru viteză
            data = analyze_page(page, url)
            if data:
                found.append(data)
            time.sleep(random.uniform(1, 2)) # Pauză umană

        browser.close()

    # Sortare și Top 5
    found.sort(key=lambda x: x['price'])
    return found[:5]

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    d = request.json
    results = scan_hybrid(d.get('sku',''), d.get('name',''))
    return jsonify({"status": "success", "competitors": results})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
