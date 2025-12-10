import re
import logging
import time
from urllib.parse import quote_plus
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__, template_folder='templates')
CORS(app)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

BLOCKED = ['google', 'bing', 'microsoft', 'facebook', 'youtube', 'doarbai', 'termohabitat', 
           'wikipedia', 'amazon', 'ebay', 'olx', 'kaufland', 'anre']

def clean_price(value):
    if not value: return 0
    text = re.sub(r'[^\d,.]', '', str(value))
    if not text: return 0
    if ',' in text and '.' in text:
        text = text.replace('.', '').replace(',', '.') if text.rindex(',') > text.rindex('.') else text.replace(',', '')
    elif ',' in text:
        text = text.replace(',', '.')
    try:
        price = float(text)
        return price if 50 < price < 500000 else 0
    except:
        return 0

def is_valid_domain(domain):
    if not domain or len(domain) < 5:
        return False
    if any(b in domain for b in BLOCKED):
        return False
    match = re.match(r'^([a-z0-9-]+)\.ro$', domain)
    return match and len(match.group(1)) >= 3

def extract_from_bing_serp(page, sku):
    """Extrage prețuri și site-uri DIRECT din pagina Bing"""
    results = []
    sku_lower = sku.lower()
    
    try:
        # Parsează fiecare rezultat Bing
        for result in page.locator('.b_algo').all()[:15]:
            try:
                # Extrage URL
                link_el = result.locator('a').first
                href = link_el.get_attribute('href') or ''
                
                # Extrage domain
                domain_match = re.search(r'https?://(?:www\.)?([a-z0-9-]+\.ro)', href.lower())
                if not domain_match:
                    continue
                domain = domain_match.group(1)
                
                if not is_valid_domain(domain):
                    continue
                
                # Extrage tot textul din rezultat
                result_text = result.inner_text()
                
                # Verifică dacă SKU apare în rezultat
                if sku_lower not in result_text.lower():
                    continue
                
                # Caută preț în text
                price_matches = re.findall(r'([\d.,]+)\s*(?:RON|Lei|lei|Ron)', result_text)
                
                for price_str in price_matches:
                    price = clean_price(price_str)
                    if price > 0:
                        results.append({
                            'name': domain,
                            'price': price,
                            'url': href,
                            'method': 'Bing'
                        })
                        logger.info(f"      ✓ {domain}: {price} Lei")
                        break  # Un preț per rezultat
                        
            except:
                continue
                
    except Exception as e:
        logger.debug(f"Extract error: {e}")
    
    return results

def scan_product(sku, name, your_price=0):
    found = []
    sku = str(sku).strip()
    
    logger.info(f"🔎 {sku} - {name[:30]}...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale='ro-RO'
        )
        
        page = context.new_page()
        
        try:
            # Bing search
            query = f"{sku} pret RON"
            url = f"https://www.bing.com/search?q={quote_plus(query)}"
            
            logger.info(f"   🔍 Bing: {query}")
            
            page.goto(url, timeout=20000, wait_until='domcontentloaded')
            time.sleep(2)
            
            # Accept cookies
            try:
                page.click('#bnp_btn_accept', timeout=2000)
                time.sleep(0.5)
            except:
                pass
            
            # Extrage din SERP
            found = extract_from_bing_serp(page, sku)
            
            # Deduplicate by domain
            seen = {}
            unique = []
            for r in found:
                if r['name'] not in seen:
                    seen[r['name']] = r
                    unique.append(r)
            found = unique
            
            logger.info(f"   📋 Găsite: {len(found)} rezultate")
            
        except Exception as e:
            logger.info(f"   ❌ Error: {str(e)[:50]}")
        finally:
            page.close()
        
        browser.close()
    
    # Calculează diferență
    for r in found:
        r['diff'] = round(((r['price'] - your_price) / your_price) * 100, 1) if your_price > 0 else 0
    
    found.sort(key=lambda x: x['price'])
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
    logger.info("🚀 PriceMonitor v7.7 (Bing SERP Direct) pe :8080")
    app.run(host='0.0.0.0', port=8080)
