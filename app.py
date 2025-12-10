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

# Site-uri blocate
BLOCKED = ['google', 'bing', 'microsoft', 'facebook', 'youtube', 'doarbai', 'termohabitat', 
           'wikipedia', 'amazon', 'ebay', 'olx', 'anre.ro', 'kaufland']

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
    """Verifică dacă domain-ul e valid"""
    if not domain or len(domain) < 5:
        return False
    if any(b in domain for b in BLOCKED):
        return False
    # Trebuie să aibă cel puțin 2 caractere înainte de .ro
    match = re.match(r'^([a-z0-9-]+)\.ro$', domain)
    if match and len(match.group(1)) >= 3:
        return True
    return False

def extract_from_bing(page, sku):
    """Extrage prețuri și site-uri din Bing"""
    results = []
    
    try:
        # Ia toate rezultatele de căutare
        search_results = page.locator('.b_algo').all()
        
        for result in search_results[:10]:
            try:
                # Extrage URL
                link = result.locator('a').first
                href = link.get_attribute('href') or ''
                
                # Extrage domain
                domain_match = re.search(r'https?://(?:www\.)?([a-z0-9-]+\.ro)', href.lower())
                if not domain_match:
                    continue
                domain = domain_match.group(1)
                
                if not is_valid_domain(domain):
                    continue
                
                # Extrage textul rezultatului
                text = result.inner_text()
                
                # Caută preț în text
                price_match = re.search(r'([\d.,]+)\s*(?:RON|Lei|lei|Ron)', text)
                if price_match:
                    price = clean_price(price_match.group(1))
                    if price > 0:
                        results.append({
                            'name': domain,
                            'price': price,
                            'url': href,
                            'method': 'Bing'
                        })
                        logger.info(f"      ✓ {domain}: {price} Lei")
                        
            except:
                continue
        
        # Dacă nu găsim prețuri în rezultate, caută în tot body-ul
        if not results:
            body_text = page.locator('body').inner_text()
            
            # Găsește toate aparițiile: preț + domeniu în apropiere
            # Pattern: domeniu.ro urmat de preț sau invers
            
            # Extrage toate domeniile valide
            domains = re.findall(r'(?:https?://)?(?:www\.)?([a-z0-9-]+\.ro)', body_text.lower())
            valid_domains = [d for d in set(domains) if is_valid_domain(d)]
            
            # Extrage toate prețurile
            prices = re.findall(r'([\d.,]+)\s*(?:RON|Lei)', body_text)
            valid_prices = [clean_price(p) for p in prices if clean_price(p) > 0]
            
            logger.info(f"      Domenii: {valid_domains[:5]}")
            logger.info(f"      Prețuri: {valid_prices[:5]}")
            
            # Asociază primul preț cu fiecare domain găsit
            if valid_prices and valid_domains:
                main_price = valid_prices[0]  # Presupunem că primul preț e cel relevant
                for domain in valid_domains[:5]:
                    results.append({
                        'name': domain,
                        'price': main_price,
                        'url': f"https://www.{domain}",
                        'method': 'Bing-Text'
                    })
        
    except Exception as e:
        logger.debug(f"Bing extract error: {e}")
    
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
            query = f"{sku} pret"
            url = f"https://www.bing.com/search?q={quote_plus(query)}"
            
            logger.info(f"   🔍 Bing: {query}")
            
            page.goto(url, timeout=20000, wait_until='domcontentloaded')
            time.sleep(2)
            
            # Click Accept cookies dacă apare
            try:
                page.click('#bnp_btn_accept', timeout=2000)
            except:
                pass
            
            time.sleep(1)
            
            # Extrage rezultate
            found = extract_from_bing(page, sku)
            
            logger.info(f"   📋 Bing: {len(found)} rezultate")
            
        except Exception as e:
            logger.info(f"   ❌ Error: {str(e)[:50]}")
        finally:
            page.close()
        
        browser.close()
    
    # Deduplicate
    seen = {}
    unique = []
    for r in found:
        if r['name'] not in seen:
            seen[r['name']] = r
            unique.append(r)
    
    # Calculează diferență
    for r in unique:
        if your_price > 0:
            r['diff'] = round(((r['price'] - your_price) / your_price) * 100, 1)
        else:
            r['diff'] = 0
    
    unique.sort(key=lambda x: x['price'])
    logger.info(f"📊 Total: {len(unique)}")
    return unique[:5]

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
    logger.info("🚀 PriceMonitor v7.2 (Bing Optimized) pe :8080")
    app.run(host='0.0.0.0', port=8080)
