import re
import logging
import time
import random
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

# --- CONFIGURARE ---
app = Flask(__name__, template_folder='templates')
CORS(app)

# Configurare Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

# Setări Anti-Blocare
MIN_DELAY = 10
MAX_DELAY = 15
MAX_RETRIES = 3

# Configurare Selectori specifici
SITE_SELECTORS = {
    'dedeman.ro': '.product-price',
    'emag.ro': '.product-new-price',
    'hornbach.ro': '.price-container',
    'leroymerlin.ro': '.price-container',
    'matlaus.ro': '.price',
    'romstal.ro': '.product-price',
    'germanyquality.ro': '.price',
    'jollycluj.ro': '.price',
    'neakaisa.ro': '.product-price'
}

def human_delay(min_s=2, max_s=5):
    sleep_time = random.uniform(min_s, max_s)
    logger.info(f"💤 Aștept {sleep_time:.1f} secunde...")
    time.sleep(sleep_time)

def extract_price_from_page(page, url):
    domain = url.split('/')[2].replace('www.', '')
    price = 0
    
    for site, selector in SITE_SELECTORS.items():
        if site in domain:
            try:
                if page.locator(selector).count() > 0:
                    text = page.locator(selector).first.inner_text()
                    price = clean_price(text)
                    if price > 0: return price
            except: pass

    try:
        body_text = page.inner_text('body')[:5000]
        matches = re.findall(r'(\d[\d\.,]*)\s*(?:lei|ron)', body_text, re.IGNORECASE)
        if matches:
            candidates = [clean_price(m) for m in matches]
            candidates = [c for c in candidates if c > 10]
            if candidates:
                price = candidates[0]
    except: pass
    
    return price

def clean_price(text):
    if not text: return 0
    matches = re.findall(r'(\d[\d\.,]*)', text)
    if not matches: return 0
    price_str = max(matches, key=len)
    price_str = price_str.replace('.', '').replace(',', '.')
    try: return float(price_str)
    except: return 0

def search_google_discovery(query):
    links = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        page = context.new_page()
        
        try:
            logger.info(f"🕵️ [Google] Caut: {query}")
            human_delay(MIN_DELAY, MAX_DELAY)
            
            page.goto(f"https://www.google.com/search?q={query}", timeout=30000)
            
            if "consent" in page.url or "sorry" in page.url:
                logger.warning("⚠️ Posibil blocaj Google sau Consent Screen.")
                try:
                    page.click('button:has-text("Accept")', timeout=2000)
                    page.click('div:has-text("Acceptă tot")', timeout=2000)
                    human_delay(2, 4)
                except: pass

            page.wait_for_selector('#search', timeout=15000)
            
            results = page.query_selector_all('.g a')
            for res in results:
                link = res.get_attribute('href')
                if link and ".ro" in link and "google" not in link:
                    if link not in links:
                        links.append(link)
                        
            links = links[:5]
            logger.info(f"✅ [Google] Am găsit {len(links)} link-uri.")

        except Exception as e:
            logger.error(f"❌ Eroare Google: {e}")
        
        browser.close()
        
    return links

def analyze_competitor_page(url):
    data = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        
        try:
            domain = url.split('/')[2].replace('www.', '').split('.')[0].capitalize()
            logger.info(f"   🚀 Vizitez: {domain}")
            
            page.goto(url, timeout=20000, wait_until='domcontentloaded')
            human_delay(1, 3)
            
            price = extract_price_from_page(page, url)
            
            if price > 0:
                data = {
                    "id": abs(hash(url)),
                    "name": domain,
                    "price": price,
                    "url": url
                }
                logger.info(f"      💰 Preț: {price} Lei")
            else:
                logger.warning(f"      ⚠️ Preț negăsit pe {domain}")
                
        except Exception as e:
            logger.error(f"   ❌ Eroare: {e}")
            
        browser.close()
    return data

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    data = request.json
    sku = data.get('sku', '')
    name = data.get('name', '')
    
    query = f"{sku} {name} pret".strip()
    
    links = search_google_discovery(query)
    
    if not links:
        return jsonify({
            "status": "error",
            "message": "Nu am găsit rezultate.",
            "competitors": []
        })

    competitors = []
    for link in links:
        comp_data = analyze_competitor_page(link)
        if comp_data:
            competitors.append(comp_data)
            
    competitors.sort(key=lambda x: x['price'])
    
    return jsonify({
        "status": "success",
        "sku": sku,
        "competitors": competitors
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
