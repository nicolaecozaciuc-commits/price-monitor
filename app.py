import re, logging
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__, template_folder='templates')
CORS(app)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

def extract_price(text):
    matches = re.findall(r'(\d[\d\.,]*)\s*(?:lei|ron)', text, re.IGNORECASE)
    if not matches: return None
    price_str = matches[-1].replace('.', '').replace(',', '.')
    try: return float(price_str)
    except: return None

def search_google_real(query):
    results = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            page = context.new_page()
            logger.info(f"Caut: {query}")
            page.goto(f"https://www.google.com/search?q={query}", timeout=30000)
            try: page.click('button:has-text("Accept")', timeout=2000)
            except: pass
            page.wait_for_selector('#search', timeout=8000)
            for el in page.query_selector_all('.g')[:6]:
                try:
                    title_el, link_el, snippet_el = el.query_selector('h3'), el.query_selector('a'), el.query_selector('.VwiC3b')
                    if not title_el or not link_el: continue
                    link = link_el.get_attribute('href')
                    if ".ro" not in link: continue
                    price = extract_price(f"{title_el.inner_text()} {snippet_el.inner_text() if snippet_el else ''}")
                    if price and price > 0:
                        results.append({"id": abs(hash(link)), "name": link.split('/')[2].replace('www.','').split('.')[0].capitalize(), "price": price, "url": link})
                except: continue
            browser.close()
        except Exception as e:
            logger.error(f"Eroare: {e}")
    unique = {}
    for r in results:
        if r['name'] not in unique or r['price'] < unique[r['name']]['price']: unique[r['name']] = r
    return list(unique.values())

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    data = request.json
    competitors = search_google_real(f"{data.get('sku','')} {data.get('name','')} pret".strip())
    competitors.sort(key=lambda x: x['price'])
    return jsonify({"status": "success", "sku": data.get('sku',''), "competitors": competitors})

if __name__ == '__main__':
    print("\nServer pornit pe :8080\n")
    app.run(host='0.0.0.0', port=8080)
