import re
import logging
import time
import random
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__, template_folder='templates')
CORS(app)

# Logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

# --- CONFIGURARE COMPETITORI ---
# Aici definim cum căutăm pe fiecare site
COMPETITORS = {
    'Dedeman': {
        'search_url': 'https://www.dedeman.ro/ro/cautare?q={}',
        'selector_card': '.product-item', # Container produs
        'selector_price': '.product-price', # Unde e prețul
        'selector_name': '.product-title'   # Unde e numele
    },
    'eMAG': {
        'search_url': 'https://www.emag.ro/search/{}',
        'selector_card': '.card-item',
        'selector_price': '.product-new-price',
        'selector_name': '.card-v2-title'
    },
    'LeroyMerlin': {
        'search_url': 'https://www.leroymerlin.ro/search/{}',
        'selector_card': 'app-product-card', # Uneori variază
        'selector_price': '.price-container',
        'selector_name': '.product-title'
    },
    'Hornbach': {
        'search_url': 'https://www.hornbach.ro/s/{}',
        'selector_card': 'article',
        'selector_price': '.price-container',
        'selector_name': 'h2'
    }
}

def clean_price(price_text):
    """Curăță prețul: '1.200,99 Lei' -> 1200.99"""
    if not price_text: return 0
    # Păstrează doar cifre, punct și virgulă
    matches = re.findall(r'(\d[\d\.,]*)', price_text)
    if not matches: return 0
    
    # Luăm cea mai lungă secvență numerică găsită (de obicei prețul întreg)
    price_str = max(matches, key=len)
    
    # Format românesc: punct la mii, virgulă la zecimale
    price_str = price_str.replace('.', '').replace(',', '.')
    
    try:
        return float(price_str)
    except:
        return 0

def scrape_direct(sku, product_name=""):
    """
    Navighează direct pe site-urile competitorilor și caută produsul.
    """
    results = []
    
    # Termenul de căutare: Preferăm SKU-ul, dacă nu, Numele
    search_term = sku if len(sku) > 3 else product_name
    if not search_term: return []

    logger.info(f"🔍 Încep scanarea directă pentru: {search_term}")

    with sync_playwright() as p:
        # Lansăm browserul (Headless = False uneori ajută la evitarea bot detection, dar pe server folosim True)
        browser = p.chromium.launch(headless=True)
        
        # Iterăm prin competitori
        for site_name, config in COMPETITORS.items():
            try:
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
                    viewport={'width': 1366, 'height': 768}
                )
                page = context.new_page()
                
                # Construim URL-ul de căutare
                url = config['search_url'].format(search_term)
                logger.info(f"   🌐 {site_name}: Accesez {url}...")
                
                # Navigăm (timeout scurt să nu blocăm tot procesul)
                try:
                    page.goto(url, timeout=15000, wait_until='domcontentloaded')
                except:
                    logger.warning(f"   ⚠️ {site_name}: Timeout la încărcare.")
                    context.close()
                    continue

                # Gestionare rapidă cookies (generic)
                try: 
                    page.click('button:has-text("Accept")', timeout=1000)
                    page.click('button:has-text("Sunt de acord")', timeout=500)
                except: pass

                # Așteptăm puțin să se încarce JS-ul
                page.wait_for_timeout(1500)

                # Încercăm să găsim prețul direct în textul paginii (mai robust decât selectorii specifici care se schimbă)
                # Strategie: Luăm conținutul vizibil și căutăm primul preț asociat cu un element de produs
                
                # Varianta 1: Selector specific (dacă e definit bine)
                found = False
                try:
                    # Căutăm containerul produsului
                    if page.locator(config['selector_card']).count() > 0:
                        first_product = page.locator(config['selector_card']).first
                        
                        raw_price = first_product.locator(config['selector_price']).first.inner_text()
                        raw_name = first_product.locator(config['selector_name']).first.inner_text()
                        
                        price = clean_price(raw_price)
                        
                        if price > 0:
                            results.append({
                                "id": abs(hash(site_name + search_term)),
                                "name": site_name,
                                "price": price,
                                "url": url,
                                "details": raw_name[:50] + "..."
                            })
                            logger.info(f"   ✅ {site_name}: Găsit {price} Lei")
                            found = True
                except Exception as e:
                    pass

                # Varianta 2: Fallback - Căutăm "Lei" în pagină dacă selectorul a eșuat
                if not found:
                    body_text = page.inner_text("body")
                    # Căutăm un preț în primii 2000 de caractere (zona de sus a rezultatelor)
                    snippet = body_text[:3000]
                    # Regex simplu pentru preț
                    prices = re.findall(r'(\d+[\.,]\d{2})\s*(?:lei|ron)', snippet, re.IGNORECASE)
                    if prices:
                        price = clean_price(prices[0])
                        if price > 0:
                            results.append({
                                "id": abs(hash(site_name)),
                                "name": site_name,
                                "price": price,
                                "url": url,
                                "details": "Detectat generic"
                            })
                            logger.info(f"   ✅ {site_name}: Găsit {price} Lei (Generic)")

                context.close()
                
            except Exception as e:
                logger.error(f"   ❌ Eroare {site_name}: {e}")

        browser.close()

    return results

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    data = request.json
    sku = data.get('sku', '')
    name = data.get('name', '')
    
    # Apelăm funcția de scraping direct
    competitors = scrape_direct(sku, name)
    
    # Sortăm după preț
    competitors.sort(key=lambda x: x['price'])
    
    return jsonify({
        "status": "success",
        "sku": sku,
        "competitors": competitors
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
