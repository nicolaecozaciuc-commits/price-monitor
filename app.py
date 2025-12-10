import re
import logging
import time
import random
import unicodedata
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__, template_folder='templates')
CORS(app)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

COMPETITORS = {
    'Dedeman': {'url': 'https://www.dedeman.ro/ro/cautare?q={}', 'card': '.product-item', 'price': '.product-price', 'name': '.product-title', 'link': 'a.product-title'},
    'eMAG': {'url': 'https://www.emag.ro/search/{}', 'card': '.card-item', 'price': '.product-new-price', 'name': '.card-v2-title', 'link': 'a.card-v2-title'},
    'Hornbach': {'url': 'https://www.hornbach.ro/s/{}', 'card': 'article', 'price': '.price-container', 'name': 'h2', 'link': 'a'},
    'LeroyMerlin': {'url': 'https://www.leroymerlin.ro/search/{}', 'card': 'app-product-card', 'price': '.price-container', 'name': 'a[title]', 'link': 'a[title]'},
    'Romstal': {'url': 'https://www.romstal.ro/cautare.html?q={}', 'card': '.product-item', 'price': '.product-price', 'name': '.product-title', 'link': 'a.product-title'},
    'BricoDepot': {'url': 'https://www.bricodepot.ro/cautare/?q={}', 'card': '.product-item', 'price': '.price-box', 'name': '.product-name', 'link': 'a.product-name'},
    'Obsentum': {'url': 'https://obsentum.com/catalogsearch/result/?q={}', 'card': '.product-item', 'price': '.price', 'name': '.product-item-link', 'link': '.product-item-link'},
    'Sanex': {'url': 'https://www.sanex.ro/index.php?route=product/search&search={}', 'card': '.product-layout', 'price': '.price', 'name': 'h4 a', 'link': 'h4 a'},
    'GemiBai': {'url': 'https://store.gemibai.ro/index.php?route=product/search&search={}', 'card': '.product-thumb', 'price': '.price', 'name': '.caption h4 a', 'link': '.caption h4 a'}
}

def normalize_text(text):
    """Elimină diacritice și caractere speciale"""
    if not text: return ""
    text = unicodedata.normalize('NFD', text).encode('ascii', 'ignore').decode()
    return text.lower().strip()

def clean_price(text):
    if not text: return 0
    text_lower = text.lower()
    # Ignoră prețuri de transport, rate lunare
    if any(x in text_lower for x in ['luna', 'rata', 'transport', 'livrare', '/luna', 'lei/']): 
        return 0
    matches = re.findall(r'(\d[\d\.,]*)', text)
    if not matches: return 0
    # Ia cel mai mare număr (prețul principal)
    prices = []
    for m in matches:
        p = m.replace('.', '').replace(',', '.')
        try: prices.append(float(p))
        except: pass
    # Filtrează prețuri prea mici (sub 10 lei = probabil greșeală)
    prices = [p for p in prices if p > 10]
    return max(prices) if prices else 0

def validate_match(sku, target_name, found_name):
    sku = normalize_text(str(sku))
    found_name = normalize_text(found_name)
    target_name = normalize_text(target_name)
    
    # 1. SKU Match exact (word boundary - evită E3067 să match-uiască E30678)
    if len(sku) > 3:
        if re.search(r'\b' + re.escape(sku) + r'\b', found_name):
            return True
        # Sau dacă SKU e la început/sfârșit
        if found_name.startswith(sku) or found_name.endswith(sku):
            return True
    
    # 2. Match Nume (ignoră cuvinte comune)
    stop_words = {'pentru', 'cm', 'alb', 'alba', 'negru', 'cu', 'de', 'si', 'la', 'din', 'x', 'mm'}
    target_parts = [w for w in target_name.split() if w not in stop_words and len
