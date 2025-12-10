import re
import logging
import time
import json
import os
from urllib.parse import quote_plus
from flask import Flask, request, jsonify, render_template, send_file
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__, template_folder='templates')
CORS(app)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

DEBUG_DIR = '/root/monitor/debug'
os.makedirs(DEBUG_DIR, exist_ok=True)

BLOCKED = ['google', 'bing', 'microsoft', 'facebook', 'youtube', 'doarbai', 'termohabitat', 'wikipedia', 'amazon', 'ebay']

SEARCH_URLS = {
    'emag.ro': 'https://www.emag.ro/search/{}',
    'absulo.ro': 'https://www.absulo.ro/catalogsearch/result/?q={}',
    'germanquality.ro': 'https://www.germanquality.ro/catalogsearch/result/?q={}',
    'sensodays.ro': 'https://www.sensodays.ro/catalogsearch/result/?q={}',
    'foglia.ro': 'https://www.foglia.ro/catalogsearch/result/?q={}',
    'bagno.ro': 'https://www.bagno.ro/c?query={}',
    'romstal.ro': 'https://www.romstal.ro/cautare?q={}',
    'compari.ro': 'https://www.compari.ro/search/?q={}',
    'ideal-standard.ro': 'https://www.ideal-standard.ro/ro/search?text={}',
    'instalatiiaz.ro': 'https://www.instalatiiaz.ro/?s={}',
    'dedeman.ro': 'https://www.dedeman.ro/ro/cautare?query={}',
    'baterii-lux.ro': 'https://www.baterii-lux.ro/cautare?controller=search&s={}',
    'badehaus.ro': 'https://www.badehaus.ro/cautare?search={}',
    'vasetoaleta.ro': 'https://www.vasetoaleta.ro/search?q={}',
    'decostores.ro': 'https://www.decostores.ro/search?q={}',
}

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

def normalize(text):
    import unicodedata
    if not text: return ""
    text = unicodedata.normalize('NFD', text).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]', '', text.lower())

def accept_cookies(page):
    selectors = [
        'button:has-text("Permite toate")',
        'button:has-text("Accept")',
        'button:has-text("Acceptă")',
        '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
    ]
    for selector in selectors:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=1000):
                btn.click(force=True)
                return True
        except:
            continue
    return False

def extract_prices_from_text(text):
    prices = []
    matches = re.findall(r'([\d.,]+)\s*Lei', text, re.IGNORECASE)
    for m in matches:
        p = clean_price(m)
        if p > 0 and p not in prices:
            prices.append(p)
    return prices[:10]


# ============ GOOGLE STEALTH - PASUL 1 ============
def google_stealth_search(page, query, sku_for_match=None):
    """
    Google caută în tăcere, face 'poză' la prima pagină.
    Returnează lista de {domain, price} găsite în snippets.
    query = ce căutăm (SKU sau denumire)
    sku_for_match = SKU-ul pentru salvarea fișierelor debug (opțional)
    """
    results = []
    search_query = f"{query} pret RON"
    url = f"https://www.google.com/search?q={quote_plus(search_query)}&hl=ro&gl=ro"
    file_suffix = sku_for_match or query.replace(' ', '_')[:20]
    
    try:
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        time.sleep(2)
        
        # Accept cookies Google
        try:
            page.click('button:has-text("Accept all")', timeout=2000)
        except:
            try:
                page.click('button:has-text("Acceptă tot")', timeout=1000)
            except:
                pass
        
        time.sleep(1)
        
        # Salvează "poza"
        page.screenshot(path=f"{DEBUG_DIR}/google_{file_suffix}.png")
        
        # Extragem textul întregii pagini
        body_text = page.locator('body').inner_text()
        
        # Salvează și textul
        with open(f"{DEBUG_DIR}/google_{file_suffix}.txt", 'w', encoding='utf-8') as f:
            f.write(body_text)
        
        # Căutăm blocuri cu preț
        lines = body_text.split('\n')
        current_domain = None
        
        for i, line in enumerate(lines):
            line_lower = line.lower()
            
            # Detectează domain .ro
            domain_match = re.search(r'(?:https?://)?(?:www\.)?([a-z0-9-]+\.ro)', line_lower)
            if domain_match:
                d = domain_match.group(1)
                if len(d) > 4 and not any(b in d for b in BLOCKED):
                    current_domain = d
            
            # Dacă linia conține query (sau parte din el) și avem domain
            query_lower = query.lower()
            # Căutăm fie query-ul complet, fie SKU-ul dacă e prezent
            has_match = query_lower in line_lower
            if not has_match and len(query.split()) > 1:
                # Pentru query-uri lungi, verificăm dacă măcar 2 cuvinte se potrivesc
                words = query_lower.split()
                matches = sum(1 for w in words if w in line_lower and len(w) > 3)
                has_match = matches >= 2
            
            if has_match and current_domain:
                # Caută TOATE prețurile în context
                context = ' '.join(lines[max(0,i-2):min(len(lines),i+3)])
                price_matches = re.findall(r'([\d.,]+)\s*(?:RON|Lei|lei)', context, re.IGNORECASE)
                
                # Extrage toate prețurile valide
                valid_prices = []
                for pm in price_matches:
                    p = clean_price(pm)
                    if p > 0:
                        valid_prices.append(p)
                
                # Ia cel mai MIC preț (prețul real, nu PRP/preț vechi)
                if valid_prices:
                    price = min(valid_prices)
                    # Verifică să nu fie duplicat
                    if not any(r['domain'] == current_domain for r in results):
                        results.append({
                            'domain': current_domain,
                            'price': price,
                            'source': 'Google SERP'
                        })
                        logger.info(f"      🟢 {current_domain}: {price} Lei")
        
        logger.info(f"   📸 Google: {len(results)} cu preț")
        
    except Exception as e:
        logger.info(f"   ⚠️ Google: {str(e)[:40]}")
    
    return results


# ============ BING FALLBACK - PASUL 2 ============
def get_domains_from_bing(page, sku):
    """Bing ca fallback - extrage domenii și prețuri"""
    results = []
    
    try:
        for block in page.locator('.b_algo').all()[:15]:
            try:
                text = block.inner_text()
                text_lower = text.lower()
                
                # Extrage domain
                domain = None
                for line in text.split('\n')[:3]:
                    match = re.search(r'(?:https?://)?(?:www\.)?([a-z0-9-]+\.ro)', line.lower())
                    if match:
                        d = match.group(1)
                        if len(d) > 4 and not any(b in d for b in BLOCKED):
                            domain = d
                            break
                
                if not domain:
                    continue
                
                # Verifică duplicat
                if any(r['domain'] == domain for r in results):
                    continue
                
                # Verifică dacă SKU apare
                has_sku = sku.lower() in text_lower
                
                # Extrage preț
                price = 0
                price_match = re.search(r'([\d.,]+)\s*(?:RON|Lei|lei)', text)
                if price_match:
                    price = clean_price(price_match.group(1))
                
                results.append({
                    'domain': domain,
                    'price': price,
                    'has_sku': has_sku,
                    'source': 'Bing SERP'
                })
                
                if price > 0 and has_sku:
                    logger.info(f"      🔵 {domain}: {price} Lei")
                elif has_sku:
                    logger.info(f"      🔵 {domain}: (pe site)")
                    
            except:
                continue
    except:
        pass
    
    return results


# ============ VIZITĂ SITE (doar dacă trebuie) ============
def find_price_on_site(page, domain, sku, save_debug=False):
    """Vizitează site-ul doar dacă nu avem preț din SERP"""
    
    search_url = SEARCH_URLS.get(domain, f'https://www.{domain}/search?q={{}}')
    sku_norm = normalize(sku)
    sku_lower = sku.lower()
    url = search_url.format(quote_plus(sku))
    
    try:
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        time.sleep(3)
        
        if accept_cookies(page):
            time.sleep(2)
            page.reload(wait_until='domcontentloaded')
            time.sleep(3)
        
        page.evaluate("window.scrollTo(0, 500)")
        time.sleep(1)
        
        if save_debug:
            page.screenshot(path=f"{DEBUG_DIR}/{domain}_{sku}.png")
        
        body_text = page.locator('body').inner_text()
        body_lower = body_text.lower()
        
        # Check erori
        error_phrases = ['0 produse', 'nu s-au gasit', 'nu am gasit', 'niciun rezultat', '0 rezultate']
        for phrase in error_phrases:
            if phrase in body_lower and 'produse)' not in body_lower:
                logger.info(f"         ⚠️ {phrase}")
                return None
        
        # Check SKU
        has_sku = sku_lower in body_lower or sku_norm in normalize(body_text)
        if not has_sku:
            return None
        
        prices = extract_prices_from_text(body_text)
        if prices:
            return {'price': prices[0], 'url': url}
        
        return None
        
    except Exception as e:
        logger.info(f"         ❌ {str(e)[:30]}")
        return None


def scan_product(sku, name, your_price=0):
    found = []
    sku = str(sku).strip()
    
    logger.info(f"🔎 {sku} - {name[:30]}...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale='ro-RO',
            timezone_id='Europe/Bucharest',
        )
        
        # Stealth
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        
        page = context.new_page()
        
        try:
            # ========== PASUL 1: GOOGLE SEARCH BY SKU ==========
            logger.info(f"   🔍 Google #1: SKU...")
            google_results = google_stealth_search(page, sku, sku)
            
            # Adaugă rezultatele cu preț direct
            for r in google_results:
                if r['price'] > 0:
                    found.append({
                        'name': r['domain'],
                        'price': r['price'],
                        'url': f"https://www.{r['domain']}",
                        'method': 'Google SKU'
                    })
            
            # ========== PASUL 2: GOOGLE SEARCH BY NAME (dacă avem < 5) ==========
            if len(found) < 5 and name and len(name) > 10:
                logger.info(f"   🔍 Google #2: Denumire...")
                # Construiește query din denumire (primele 5-6 cuvinte + SKU)
                name_words = name.split()[:6]
                name_query = ' '.join(name_words)
                if sku.upper() not in name_query.upper():
                    name_query += f" {sku}"
                
                google_results_name = google_stealth_search(page, name_query, f"{sku}_name")
                
                # Adaugă doar site-uri noi
                for r in google_results_name:
                    if r['price'] > 0 and not any(f['name'] == r['domain'] for f in found):
                        found.append({
                            'name': r['domain'],
                            'price': r['price'],
                            'url': f"https://www.{r['domain']}",
                            'method': 'Google Name'
                        })
                        logger.info(f"      🟡 {r['domain']}: {r['price']} Lei (din denumire)")
            
            # ========== PASUL 2: BING (fallback) ==========
            if len(found) < 3:
                logger.info(f"   🔍 Bing completează...")
                query = f"{sku} pret"
                url = f"https://www.bing.com/search?q={quote_plus(query)}"
                
                page.goto(url, timeout=20000, wait_until='domcontentloaded')
                time.sleep(3)
                
                try:
                    page.click('#bnp_btn_accept', timeout=3000)
                except:
                    pass
                
                page.screenshot(path=f"{DEBUG_DIR}/bing_{sku}.png")
                
                bing_results = get_domains_from_bing(page, sku)
                
                for r in bing_results:
                    # Nu adăuga duplicate
                    if any(f['name'] == r['domain'] for f in found):
                        continue
                    
                    if r['price'] > 0 and r.get('has_sku'):
                        found.append({
                            'name': r['domain'],
                            'price': r['price'],
                            'url': f"https://www.{r['domain']}",
                            'method': 'Bing SERP'
                        })
            
            # ========== PASUL 3: VIZITĂ SITE (doar dacă nu avem preț) ==========
            # Colectăm site-urile care au SKU dar nu au preț
            sites_to_visit = []
            
            for r in google_results:
                if r['price'] == 0 and r['domain'] not in [f['name'] for f in found]:
                    sites_to_visit.append(r['domain'])
            
            if len(found) < 3 and sites_to_visit:
                logger.info(f"   🌐 Verificăm {len(sites_to_visit)} site-uri...")
                
                for domain in sites_to_visit[:3]:
                    logger.info(f"      🔗 {domain}...")
                    result = find_price_on_site(page, domain, sku, save_debug=True)
                    
                    if result:
                        found.append({
                            'name': domain,
                            'price': result['price'],
                            'url': result['url'],
                            'method': 'Site Visit'
                        })
                        logger.info(f"      ✅ {result['price']} Lei")
                    
                    if len(found) >= 5:
                        break
            
            logger.info(f"   📊 Total: {len(found)}")
            
        except Exception as e:
            logger.info(f"   ❌ {str(e)[:50]}")
        finally:
            page.close()
        
        browser.close()
    
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

@app.route('/debug/<filename>')
def get_debug(filename):
    filepath = f"{DEBUG_DIR}/{filename}"
    if os.path.exists(filepath):
        return send_file(filepath)
    return "Not found", 404

if __name__ == '__main__':
    logger.info("🚀 PriceMonitor v9.9 (Google SKU + Denumire) pe :8080")
    app.run(host='0.0.0.0', port=8080)
