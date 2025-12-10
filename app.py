import re
import logging
import time
import random
import unicodedata
import json
from urllib.parse import quote_plus, urlparse
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__, template_folder='templates')
CORS(app)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

# ═══════════════════════════════════════════════════════════════
# CONFIGURARE SITE-URI
# ═══════════════════════════════════════════════════════════════
COMPETITORS = {
    'Dedeman': {
        'url': 'https://www.dedeman.ro/ro/cautare?q={}',
        'card': '.product-item',
        'name': '.product-title'
    },
    'eMAG': {
        'url': 'https://www.emag.ro/search/{}',
        'card': '.card-item',
        'name': '.card-v2-title'
    },
    'Romstal': {
        'url': 'https://www.romstal.ro/cautare?q={}',
        'card': '.product-item-info',
        'name': '.product-item-link'
    },
    'Obsentum': {
        'url': 'https://obsentum.com/catalogsearch/result/?q={}',
        'card': '.product-item-info',
        'name': '.product-item-link'
    },
    'Sanex': {
        'url': 'https://www.sanex.ro/index.php?route=product/search&search={}',
        'card': '.product-layout',
        'name': 'h4 a'
    },
    'Absulo': {
        'url': 'https://www.absulo.ro/search/{}',
        'card': '.product-item-info, .product-item',
        'name': '.product-item-link, .product-name'
    },
    'Hornbach': {
        'url': 'https://www.hornbach.ro/s/{}',
        'card': 'article[class*="product"]',
        'name': '.product-title, h2'
    },
    'MatHaus': {
        'url': 'https://www.mathaus.ro/cautare/{}',
        'card': '.product-item-info',
        'name': '.product-item-link'
    }
}

# ═══════════════════════════════════════════════════════════════
# FUNCȚII UTILITARE
# ═══════════════════════════════════════════════════════════════

def normalize_text(text):
    if not text: return ""
    text = unicodedata.normalize('NFD', text).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9\s]', '', text.lower()).strip()

def clean_price(value):
    """Extrage preț numeric din diverse formate"""
    if not value: return 0
    if isinstance(value, (int, float)):
        return float(value) if value > 10 else 0
    
    text = str(value).lower()
    # Ignoră rate, transport
    if any(x in text for x in ['luna', 'rata', 'transport', '/luna']):
        return 0
    
    # Curăță și extrage
    text = re.sub(r'[^\d,.]', '', str(value))
    if not text: return 0
    
    # Format RO: 1.234,56 -> 1234.56
    if ',' in text and '.' in text:
        if text.rindex(',') > text.rindex('.'):
            text = text.replace('.', '').replace(',', '.')
        else:
            text = text.replace(',', '')
    elif ',' in text:
        text = text.replace(',', '.')
    
    try:
        price = float(text)
        return price if price > 10 else 0
    except:
        return 0

# ═══════════════════════════════════════════════════════════════
# HUMAN EMULATION
# ═══════════════════════════════════════════════════════════════

def human_delay():
    """Delay variabil ca un utilizator real"""
    time.sleep(random.uniform(1.5, 3.5))

def human_scroll(page):
    """Scroll natural"""
    try:
        page.mouse.wheel(0, random.randint(200, 400))
        time.sleep(random.uniform(0.3, 0.7))
    except:
        pass

def human_move_mouse(page):
    """Mișcare mouse aleatorie"""
    try:
        x = random.randint(100, 800)
        y = random.randint(100, 600)
        page.mouse.move(x, y)
    except:
        pass

# ═══════════════════════════════════════════════════════════════
# EXTRAGERE PREȚ DIN JSON-LD (PRIORITATE MAXIMĂ)
# ═══════════════════════════════════════════════════════════════

def extract_from_jsonld(page, target_sku=None):
    """
    Extrage preț și SKU din JSON-LD Schema.
    Aceasta e metoda cea mai precisă - prețul oficial declarat pentru Google.
    """
    try:
        scripts = page.locator('script[type="application/ld+json"]').all()
        
        for script in scripts:
            try:
                content = script.inner_text()
                data = json.loads(content)
                
                # Poate fi array sau obiect
                items = data if isinstance(data, list) else [data]
                
                # Caută și în @graph
                for item in items:
                    if '@graph' in item:
                        items.extend(item['@graph'])
                
                for item in items:
                    if item.get('@type') != 'Product':
                        continue
                    
                    # Extrage SKU pentru validare
                    product_sku = item.get('sku', '') or item.get('mpn', '') or item.get('productID', '')
                    product_name = item.get('name', '')
                    
                    # Validare SKU dacă avem target
                    if target_sku and len(target_sku) >= 4:
                        sku_norm = normalize_text(target_sku)
                        found_sku_norm = normalize_text(str(product_sku))
                        found_name_norm = normalize_text(str(product_name))
                        
                        # Verifică match
                        if sku_norm not in found_sku_norm and sku_norm not in found_name_norm:
                            # Verifică parțial (fără ultima cifră)
                            if sku_norm[:-1] not in found_sku_norm and sku_norm[:-1] not in found_name_norm:
                                continue
                    
                    # Extrage prețul din offers
                    offers = item.get('offers', {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    
                    price = offers.get('price') or offers.get('lowPrice') or offers.get('highPrice')
                    
                    if price:
                        price_val = clean_price(price)
                        if price_val > 0:
                            logger.info(f"      ✓ JSON-LD: {price_val} Lei (SKU: {product_sku[:20]})")
                            return {
                                'price': price_val,
                                'sku': product_sku,
                                'name': product_name,
                                'method': 'JSON-LD'
                            }
            except json.JSONDecodeError:
                continue
            except Exception as e:
                continue
    except:
        pass
    
    return None

# ═══════════════════════════════════════════════════════════════
# EXTRAGERE PREȚ DIN META TAGS
# ═══════════════════════════════════════════════════════════════

def extract_from_meta(page):
    """Extrage preț din meta tags (Open Graph, etc.)"""
    selectors = [
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
        'meta[name="price"]',
        'meta[itemprop="price"]'
    ]
    
    for selector in selectors:
        try:
            el = page.locator(selector).first
            content = el.get_attribute('content')
            price = clean_price(content)
            if price > 0:
                logger.info(f"      ✓ META: {price} Lei")
                return {'price': price, 'method': 'META'}
        except:
            continue
    
    return None

# ═══════════════════════════════════════════════════════════════
# EXTRAGERE PREȚ DIN CSS (FALLBACK)
# ═══════════════════════════════════════════════════════════════

def extract_from_css(element):
    """Extrage preț din selectoare CSS comune"""
    selectors = [
        '[data-price]',
        '.price-new', '.product-new-price', '.special-price .price',
        '.current-price', '.price-value', '.product-price',
        '.woocommerce-Price-amount', 'span[itemprop="price"]',
        '.price', '[class*="price"]'
    ]
    
    for selector in selectors:
        try:
            el = element.locator(selector).first
            
            # Verifică data-price attribute
            data_price = el.get_attribute('data-price')
            if data_price:
                price = clean_price(data_price)
                if price > 0:
                    return {'price': price, 'method': 'CSS-DATA'}
            
            # Verifică content attribute
            content = el.get_attribute('content')
            if content:
                price = clean_price(content)
                if price > 0:
                    return {'price': price, 'method': 'CSS-CONTENT'}
            
            # Verifică text
            text = el.inner_text()
            price = clean_price(text)
            if price > 0:
                return {'price': price, 'method': 'CSS-TEXT'}
        except:
            continue
    
    # Fallback: extrage din tot textul
    try:
        all_text = element.inner_text()
        matches = re.findall(r'(\d{2,6}[,\.]\d{2})\s*(?:lei|ron|RON)', all_text, re.IGNORECASE)
        if matches:
            price = clean_price(matches[0])
            if price > 0:
                return {'price': price, 'method': 'REGEX'}
    except:
        pass
    
    return None

# ═══════════════════════════════════════════════════════════════
# VALIDARE MATCH
# ═══════════════════════════════════════════════════════════════

def validate_match(sku, target_name, found_text):
    """Verifică dacă produsul găsit corespunde celui căutat"""
    sku_norm = normalize_text(str(sku))
    found_norm = normalize_text(found_text)
    target_norm = normalize_text(target_name)
    
    # 1. SKU exact sau parțial
    if len(sku_norm) >= 4:
        if sku_norm in found_norm:
            return True
        if sku_norm[:-1] in found_norm:  # Fără ultima cifră
            return True
    
    # 2. Cuvinte cheie din nume (min 2 din 4)
    stop = {'pentru', 'cm', 'alb', 'alba', 'negru', 'cu', 'de', 'si', 'la', 'din', 'x', 'mm'}
    keywords = [w for w in target_norm.split() if w not in stop and len(w) > 2][:4]
    
    if keywords:
        matches = sum(1 for kw in keywords if kw in found_norm)
        if matches >= 2:
            return True
    
    return False

# ═══════════════════════════════════════════════════════════════
# SCANARE PRODUS PE UN SITE
# ═══════════════════════════════════════════════════════════════

def scrape_site(context, site_name, config, sku, name):
    """Scanează un site pentru produs"""
    page = None
    try:
        page = context.new_page()
        search_term = sku if len(str(sku)) >= 4 else name.split()[0]
        url = config['url'].format(quote_plus(search_term))
        
        # Navigare cu retry
        for attempt in range(2):
            try:
                page.goto(url, timeout=25000, wait_until='domcontentloaded')
                break
            except:
                if attempt == 0:
                    time.sleep(2)
                else:
                    return None
        
        # Human emulation
        human_delay()
        human_move_mouse(page)
        human_scroll(page)
        
        # Accept cookies
        for btn in ['Accept', 'Acceptă', 'Accept all', 'Agree', 'OK']:
            try:
                page.click(f'button:has-text("{btn}")', timeout=1000)
                break
            except:
                pass
        
        # Găsește carduri produse
        cards = page.locator(config['card']).all()
        
        if not cards:
            logger.info(f"   ⚪ {site_name}: 0 carduri")
            return None
        
        logger.info(f"   🔍 {site_name}: {len(cards)} carduri")
        
        # Parcurge cardurile
        for card in cards[:5]:
            try:
                card_text = card.inner_text()
                
                # Validare match
                if not validate_match(sku, name, card_text):
                    continue
                
                # Click pe produs pentru a accesa pagina (JSON-LD e acolo)
                try:
                    link_el = card.locator('a').first
                    href = link_el.get_attribute('href')
                    
                    if href:
                        if not href.startswith('http'):
                            parsed = urlparse(url)
                            href = f"{parsed.scheme}://{parsed.netloc}{href}"
                        
                        # Navighează la pagina produsului
                        page.goto(href, timeout=20000, wait_until='domcontentloaded')
                        human_delay()
                        
                        # PRIORITATE 1: JSON-LD
                        result = extract_from_jsonld(page, sku)
                        if result and result['price'] > 0:
                            return {
                                'name': site_name,
                                'price': result['price'],
                                'url': href,
                                'method': result['method']
                            }
                        
                        # PRIORITATE 2: META tags
                        result = extract_from_meta(page)
                        if result and result['price'] > 0:
                            return {
                                'name': site_name,
                                'price': result['price'],
                                'url': href,
                                'method': result['method']
                            }
                        
                        # PRIORITATE 3: CSS pe pagina produsului
                        result = extract_from_css(page.locator('body'))
                        if result and result['price'] > 0:
                            return {
                                'name': site_name,
                                'price': result['price'],
                                'url': href,
                                'method': result['method']
                            }
                except Exception as e:
                    pass
                
                # Fallback: extrage preț din card (fără click)
                result = extract_from_css(card)
                if result and result['price'] > 0:
                    try:
                        link = card.locator('a').first.get_attribute('href') or url
                        if not link.startswith('http'):
                            parsed = urlparse(url)
                            link = f"{parsed.scheme}://{parsed.netloc}{link}"
                    except:
                        link = url
                    
                    return {
                        'name': site_name,
                        'price': result['price'],
                        'url': link,
                        'method': result['method']
                    }
                    
            except Exception as e:
                continue
        
    except Exception as e:
        logger.debug(f"   ❌ {site_name}: {str(e)[:40]}")
    finally:
        if page:
            page.close()
    
    return None

# ═══════════════════════════════════════════════════════════════
# FUNCȚIA PRINCIPALĂ
# ═══════════════════════════════════════════════════════════════

def scan_product(sku, name, your_price=0):
    """Scanează toate site-urile pentru un produs"""
    found = []
    sku = str(sku).strip()
    name = str(name).strip()
    
    logger.info(f"🔎 Caut: {sku} - {name[:40]}...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale='ro-RO'
        )
        
        for site_name, config in COMPETITORS.items():
            result = scrape_site(context, site_name, config, sku, name)
            
            if result:
                # Calcul diferență %
                if your_price > 0:
                    diff = ((result['price'] - your_price) / your_price) * 100
                    result['diff'] = round(diff, 1)
                else:
                    result['diff'] = 0
                
                found.append(result)
                logger.info(f"   ✅ {site_name}: {result['price']} Lei ({result['diff']:+.1f}%) [{result.get('method', 'N/A')}]")
            
            # Pauză între site-uri
            time.sleep(random.uniform(1, 2))
        
        browser.close()
    
    # Sortează după preț
    found.sort(key=lambda x: x['price'])
    logger.info(f"📊 Total: {len(found)} rezultate")
    
    return found[:5]

# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check', methods=['POST'])
def api_check():
    data = request.json
    sku = data.get('sku', '')
    name = data.get('name', '')
    your_price = float(data.get('price', 0) or 0)
    
    results = scan_product(sku, name, your_price)
    
    return jsonify({
        "status": "success",
        "competitors": results
    })

if __name__ == '__main__':
    logger.info("🚀 PriceMonitor v3.0 (JSON-LD Priority + Human Emulation) pe :8080")
    app.run(host='0.0.0.0', port=8080)
