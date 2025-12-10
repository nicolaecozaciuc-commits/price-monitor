import re
import logging
import time
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

# --- CONFIGURARE ---
app = Flask(__name__, template_folder='templates')
CORS(app)

# Logging curat
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

def extract_price(text):
    """
    Extrage prețul dintr-un text, gestionând formatul românesc.
    Ex: "1.200,50 Lei" -> 1200.50
    """
    # Caută numere urmate de lei/ron
    matches = re.findall(r'(\d[\d\.,]*)\s*(?:lei|ron)', text, re.IGNORECASE)
    if not matches:
        return None
    
    # Luăm ultimul preț găsit (de obicei cel mai vizibil)
    price_str = matches[-1]
    
    # Curățăm formatul: eliminăm punctele de mii, înlocuim virgula cu punct
    price_str = price_str.replace('.', '').replace(',', '.')
    
    try:
        return float(price_str)
    except:
        return None

def search_google_real(query):
    """
    Caută pe Google folosind un browser real (Playwright)
    """
    results = []
    with sync_playwright() as p:
        try:
            # Lansăm browserul în mod headless (invizibil)
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            logger.info(f"🔎 Căutare: {query}")
            
            # Navigare Google
            page.goto(f"https://www.google.com/search?q={query}", timeout=25000)
            
            # Gestionare pop-up cookies
            try: 
                page.click('button:has-text("Accept")', timeout=1500)
                page.click('div:has-text("Acceptă tot")', timeout=500)
            except: 
                pass

            # Așteptare rezultate
            page.wait_for_selector('#search', timeout=6000)
            
            # Extragere elemente
            elements = page.query_selector_all('.g')
            
            # Analizăm primele 6 rezultate organice
            for el in elements[:6]:
                try:
                    title_el = el.query_selector('h3')
                    link_el = el.query_selector('a')
                    snippet_el = el.query_selector('.VwiC3b') # Clasa pentru text descriptiv
                    
                    if not title_el or not link_el: continue
                    
                    title = title_el.inner_text()
                    link = link_el.get_attribute('href')
                    snippet = snippet_el.inner_text() if snippet_el else ""
                    
                    # Filtru: Doar site-uri .ro
                    if ".ro" not in link: continue
                    
                    # Extrage preț
                    full_text = f"{title} {snippet}"
                    price = extract_price(full_text)
                    
                    if price and price > 0:
                        domain = link.split('/')[2].replace('www.', '').split('.')[0].capitalize()
                        results.append({
                            "id": abs(hash(link)),
                            "name": domain,
                            "price": price,
                            "url": link
                        })
                except: continue
                
            browser.close()
        except Exception as e:
            logger.error(f"Eroare Playwright: {e}")
            if 'browser' in locals(): browser.close()
            
    # Eliminăm duplicatele (păstrăm cel mai mic preț per domeniu)
    unique_results = {}
    for r in results:
        if r['name'] not in unique_results or r['price'] < unique_results[r['name']]['price']:
            unique_results[r['name']] = r
            
    return list(unique_results.values())

# --- RUTE ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    data = request.json
    sku = data.get('sku', '')
    name = data.get('name', '')
    
    if not sku:
        return jsonify({"status": "error", "message": "Lipsă SKU"}), 400

    search_term = f"{sku} {name} pret".strip()
    competitors = search_google_real(search_term)
    
    # Sortăm concurenții crescător după preț
    competitors.sort(key=lambda x: x['price'])
    
    return jsonify({
        "status": "success", 
        "sku": sku, 
        "competitors": competitors
    })

if __name__ == '__main__':
    # Pornire server pe portul 8080
    app.run(host='0.0.0.0', port=8080)
