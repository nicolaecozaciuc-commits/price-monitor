import re
import logging
import time
import random
import unicodedata
import json
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

def normalize(text):
    if not text: return ""
    text = unicodedata.normalize('NFD', text).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]', '', text.lower())

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
        return price if 10 < price < 500000 else 0
    except:
        return 0

def extract_prices_from_search_page(page, sku):
    """Extrage prețuri DIRECT din pagina de căutare (Google/Bing)"""
    results = []
    sku_norm = normalize(sku)
    
    try:
        # Ia tot textul paginii
        body_text = page.content()
        
        # Pattern: preț + RON/Lei în apropierea unui link .ro
        # Caută blocuri care conțin și preț și domain .ro
        
        # Metoda 1: Caută elemente cu preț vizibil
        price_elements = page.locator('//*[contains(text(), "RON") or contains(text(), "Lei") or contains(text(), "lei")]').all()
        
        for el in price_elements[:30]:
            try:
                text = el.inner_text()
                
                # Extrage preț din text (ex: "567,00 RON", "539.62 Lei")
                price_match = re.search(r'([\d.,]+)\s*(?:RON|Lei|lei)', text)
                if not price_match:
                    continue
                
                price = clean_price(price_match.group(1))
                if price <= 0:
                    continue
                
                # Găsește domain-ul asociat - caută link în parent
                parent = el
                domain = None
                url = None
                
                for _ in range(5):  # Urcă max 5 nivele
                    try:
                        parent = parent.locator('..').first
                        # Caută link
                        link = parent.locator('a[href*=".ro"]').first
                        href = link.get_attribute('href')
                        if href and '.ro' in href:
                            # Extrage domain
                            match = re.search(r'https?://(?:www\.)?([^/]+\.ro)', href)
                            if match:
                                domain = match.group(1)
                                url = href
                                break
                    except:
                        break
                
                if domain and price > 0:
                    # Verifică să nu fie propriul site
                    if 'doarbai' not in domain and 'termohabitat' not in domain:
                        results.append({
                            'name': domain,
                            'price': price,
                            'url': url or f"https://{domain}",
                            'method': 'SERP'
                        })
                        
            except:
                continue
        
        # Metoda 2: Pattern regex pe tot HTML-ul pentru Google Shopping
        # Format: "599,00 RON" urmat de domain
        shopping_pattern = r'([\d.,]+)\s*(?:RON|Lei)[^<]*?([a-z0-9-]+\.ro)'
        for match in re.finditer(shopping_pattern, body_text, re.IGNORECASE):
            price = clean_price(match.group(1))
            domain = match.group(2)
            if price > 0 and 'doarbai' not in domain and 'google' not in domain:
                results.append({
                    'name': domain,
                    'price': price,
                    'url': f"https://www.{domain}",
                    'method': 'SERP-REGEX'
                })
        
    except Exception as e:
        logger.debug(f"Extract error: {e}")
    
    # Deduplicate - păstrează primul (cel mai relevant)
    seen = {}
    unique = []
    for r in results:
        if r['name'] not in seen:
            seen[r['name']] = True
            unique.append(r)
    
    return unique

def search_bing(context, sku):
    """Caută pe Bing (nu are CAPTCHA ca Google)"""
    page = None
    results = []
    
    try:
        page = context.new_page()
        
        query = f"{sku} pret"
        url = f"https://www.bing.com/search?q={quote_plus(query)}&setlang=ro"
        
        logger.info(f"   🔍 Bing: {query}")
        
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        time.sleep(2)
        
        results = extract_prices_from_search_page(page, sku)
        logger.info(f"   📋 Bing: {len(results)} prețuri găsite")
        
    except Exception as e:
        logger.info(f"   ❌ Bing error: {str(e)[:40]}")
    finally:
        if page:
            page.close()
    
    return results

def search_google(context, sku):
    """Caută pe Google - extrage prețuri din rich snippets"""
    page = None
    results = []
    
    try:
        page = context.new_page()
        
        query = f"{sku} pret"
        url = f"https://www.google.ro/search?q={quote_plus(query)}&hl=ro&gl=ro"
        
        logger.info(f"   🔍 Google: {query}")
        
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        time.sleep(2)
        
        # Verifică CAPTCHA
        if 'unusual traffic' in page.content().lower() or 'captcha' in page.content().lower():
            logger.info(f"   ⚠️ Google CAPTCHA - skip")
            return []
        
        results = extract_prices_from_search_page(page, sku)
        logger.info(f"   📋 Google: {len(results)} prețuri găsite")
        
    except Exception as e:
        logger.info(f"   ❌ Google error: {str(e)[:40]}")
    finally:
        if page:
            page.close()
    
    return results

def search_duckduckgo(context, sku):
    """Caută pe DuckDuckGo"""
    page = None
    results = []
    
    try:
        page = context.new_page()
        
        query = f"{sku} pret site:.ro"
        url = f"https://duckduckgo.com/?q={quote_plus(query)}&kl=ro-ro"
        
        logger.info(f"   🦆 DuckDuckGo: {query}")
        
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        time.sleep(3)
        
        results = extract_prices_from_search_page(page, sku)
        logger.info(f"   📋 DuckDuckGo: {len(results)} prețuri găsite")
        
    except Exception as e:
        logger.info(f"   ❌ DuckDuckGo error: {str(e)[:40]}")
    finally:
        if page:
            page.close()
    
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
        
        # Încearcă toate motoarele de căutare
        all_results = []
        
        # 1. Bing (cel mai fiabil, fără CAPTCHA)
        all_results.extend(search_bing(context, sku))
        
        # 2. Google (poate da CAPTCHA)
        if len(all_results) < 3:
            all_results.extend(search_google(context, sku))
        
        # 3. DuckDuckGo (backup)
        if len(all_results) < 3:
            all_results.extend(search_duckduckgo(context, sku))
        
        browser.close()
    
    # Deduplicate și sortează
    seen = {}
    for r in all_results:
        if r['name'] not in seen:
            seen[r['name']] = r
    
    found = list(seen.values())
    
    # Calculează diferență
    for r in found:
        if your_price > 0:
            r['diff'] = round(((r['price'] - your_price) / your_price) * 100, 1)
        else:
            r['diff'] = 0
    
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
    logger.info("🚀 PriceMonitor v7.0 (SERP Price Extraction) pe :8080")
    app.run(host='0.0.0.0', port=8080)
