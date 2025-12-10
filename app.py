import re
import logging
import time
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

# Configurare aplicație Flask
app = Flask(__name__, template_folder='templates')
CORS(app)

# Configurare logging (ascundem logurile interne inutile)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

def extract_price(text):
    """
    Extrage un preț numeric dintr-un text (ex: '1.200,50 Lei' -> 1200.50).
    Gestionează formatele românești (punct la mii, virgulă la zecimale).
    """
    # Caută modele de preț urmate de lei/ron
    matches = re.findall(r'(\d[\d\.,]*)\s*(?:lei|ron)', text, re.IGNORECASE)
    if not matches:
        return None
    
    # Luăm ultimul preț găsit (de obicei cel mai relevant din snippet)
    price_str = matches[-1]
    
    # Standardizăm formatul: eliminăm punctele de mii, înlocuim virgula cu punct
    price_str = price_str.replace('.', '').replace(',', '.')
    
    try:
        return float(price_str)
    except:
        return None

def search_google_real(query):
    """
    Motorul principal de căutare folosind Playwright (Browser Real Headless).
    """
    results = []
    with sync_playwright() as p:
        try:
            # Lansăm un browser Chromium izolat
            browser = p.chromium.launch(headless=True)
            
            # Folosim un User-Agent real pentru a evita blocarea
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            logger.info(f"🔎 Caut pe Google: {query}")
            
            # Navigăm pe Google
            page.goto(f"https://www.google.com/search?q={query}", timeout=30000)
            
            # Tentativă de a închide pop-up-ul de consimțământ Google (dacă apare)
            try: 
                page.click('button:has-text("Accept")', timeout=2000)
                page.click('div:has-text("Acceptă tot")', timeout=500)
            except: 
                pass

            # Așteptăm încărcarea rezultatelor
            page.wait_for_selector('#search', timeout=8000)
            
            # Selectăm elementele de rezultat organic
            elements = page.query_selector_all('.g')
            
            # Procesăm primele 6 rezultate
            for el in elements[:6]:
                try:
                    title_el = el.query_selector('h3')
                    link_el = el.query_selector('a')
                    snippet_el = el.query_selector('.VwiC3b') # Clasa pentru textul descriptiv
                    
                    if not title_el or not link_el:
                        continue
                    
                    title = title_el.inner_text()
                    link = link_el.get_attribute('href')
                    snippet = snippet_el.inner_text() if snippet_el else ""
                    
                    # Filtru: Ignorăm site-urile care nu sunt din România
                    if ".ro" not in link:
                        continue
                    
                    # Extragem prețul din textul vizibil în Google (Titlu + Snippet)
                    full_text = f"{title} {snippet}"
                    price = extract_price(full_text)
                    
                    if price and price > 0:
                        # Extragem numele domeniului (ex: dedeman.ro -> Dedeman)
                        domain = link.split('/')[2].replace('www.', '').split('.')[0].capitalize()
                        
                        results.append({
                            "id": abs(hash(link)),
                            "name": domain,
                            "price": price,
                            "url": link
                        })
                except:
                    continue
                
            browser.close()
        except Exception as e:
            logger.error(f"Eroare căutare: {e}")
            if 'browser' in locals():
                browser.close()
            
    # Deduplicare: Păstrăm cel mai mic preț găsit per domeniu
    unique_results = {}
    for r in results:
        if r['name'] not in unique_results or r['price'] < unique_results[r['name']]['price']:
            unique_results[r['name']] = r
            
    return list(unique_results.values())

@app.route('/')
def index():
    """Servește pagina principală"""
    return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    """API Endpoint pentru verificarea unui singur produs"""
    data = request.json
    sku = data.get('sku', '')
    name = data.get('name', '')
    
    # Construim query-ul de căutare
    search_term = f"{sku} {name} pret".strip()
    
    # Executăm căutarea
    competitors = search_google_real(search_term)
    
    # Sortăm rezultatele crescător după preț
    competitors.sort(key=lambda x: x['price'])
    
    return jsonify({
        "status": "success", 
        "sku": sku, 
        "competitors": competitors
    })

if __name__ == '__main__':
    print(f"\n🚀 Serverul PriceMonitor este pornit pe portul 8080...\n")
    app.run(host='0.0.0.0', port=8080)
