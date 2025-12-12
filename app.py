import re
import logging
import time
import json
import os
from urllib.parse import quote_plus
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_file
from flask_cors import CORS
from playwright.sync_api import sync_playwright
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

app = Flask(__name__, template_folder='templates')
CORS(app)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('PriceMonitor')

DEBUG_DIR = '/root/monitor/debug'
DATA_DIR = '/root/monitor/data'
os.makedirs(DEBUG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

SCANS_FILE = f'{DATA_DIR}/scans.json'

def load_scans():
    if os.path.exists(SCANS_FILE):
        try:
            with open(SCANS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []
    return []

def save_scan(sku, name, your_price, competitors):
    scans = load_scans()
    scan_entry = {
        'timestamp': datetime.now().isoformat(),
        'sku': sku,
        'name': name,
        'your_price': your_price,
        'competitors': competitors
    }
    scans.append(scan_entry)
    with open(SCANS_FILE, 'w', encoding='utf-8') as f:
        json.dump(scans, f, ensure_ascii=False, indent=2)
    return scan_entry

def generate_excel_report():
    scans = load_scans()
    if not scans:
        return None
    
    wb = Workbook()
    ws = wb.active
    ws.title = 'Rezumat'
    
    title_fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
    title_font = Font(bold=True, color='FFFFFF', size=14)
    header_fill = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
    header_font = Font(bold=True, color='FFFFFF', size=11)
    product_fill = PatternFill(start_color='D9E1F2', end_color='D9E1F2', fill_type='solid')
    product_font = Font(bold=True, size=11)
    competitor_fill = PatternFill(start_color='E7E6E6', end_color='E7E6E6', fill_type='solid')
    border_thin = Border(left=Side(style='thin', color='000000'), right=Side(style='thin', color='000000'), top=Side(style='thin', color='000000'), bottom=Side(style='thin', color='000000'))
    
    ws.merge_cells('A1:H1')
    title = ws['A1']
    title.value = 'RAPORT MONITORIZARE PREȚURI'
    title.font = title_font
    title.fill = title_fill
    title.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 25
    
    ws.merge_cells('A2:H2')
    date_cell = ws['A2']
    date_cell.value = f"Generat: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    date_cell.font = Font(italic=True, size=10)
    date_cell.alignment = Alignment(horizontal='right')
    
    ws.row_dimensions[3].height = 5
    row = 4
    
    for scan in scans:
        sku = scan.get('sku', 'N/A')
        name = scan.get('name', 'N/A')[:50]
        your_price = scan.get('your_price', 0)
        competitors = scan.get('competitors', [])
        timestamp = scan.get('timestamp', '').split('T')[0]
        
        ws.merge_cells(f'A{row}:H{row}')
        prod_header = ws[f'A{row}']
        prod_header.value = f"SKU: {sku} | {name}"
        prod_header.font = product_font
        prod_header.fill = product_fill
        prod_header.border = border_thin
        prod_header.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)
        ws.row_dimensions[row].height = 20
        row += 1
        
        ws[f'A{row}'].value = 'Preț Nostru:'
        ws[f'A{row}'].font = Font(bold=True)
        ws[f'B{row}'].value = f"{your_price:.2f} Lei"
        ws[f'B{row}'].font = Font(bold=True, color='008000', size=12)
        ws[f'C{row}'].value = f'Data: {timestamp}'
        ws[f'C{row}'].font = Font(italic=True, size=9)
        ws.row_dimensions[row].height = 18
        row += 1
        
        headers = ['Competitor', 'Preț', 'Diferență', 'Status', 'Metodă']
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=row, column=col)
            cell.value = header
            cell.font = header_font
            cell.fill = header_fill
            cell.border = border_thin
            cell.alignment = Alignment(horizontal='center', vertical='center')
        ws.row_dimensions[row].height = 16
        row += 1
        
        for comp in competitors:
            domain = comp.get('name', 'N/A')
            price = comp.get('price', 0)
            diff = comp.get('diff', 0)
            method = comp.get('method', 'N/A')
            
            if diff < -10:
                status_color = '008000'
                status_text = '✓ IEFTIN'
            elif diff > 10:
                status_color = 'FF0000'
                status_text = '✗ SCUMP'
            else:
                status_color = '000000'
                status_text = '= EGAL'
            
            ws[f'A{row}'].value = domain
            ws[f'B{row}'].value = price
            ws[f'B{row}'].number_format = '#,##0.00 "Lei"'
            ws[f'C{row}'].value = f"{diff:+.1f}%"
            ws[f'C{row}'].font = Font(bold=True, color=status_color)
            ws[f'D{row}'].value = status_text
            ws[f'D{row}'].font = Font(color=status_color, bold=True)
            ws[f'E{row}'].value = method
            
            for col in range(1, 6):
                ws.cell(row=row, column=col).fill = competitor_fill
                ws.cell(row=row, column=col).border = border_thin
                ws.cell(row=row, column=col).alignment = Alignment(horizontal='center', vertical='center')
            
            ws.row_dimensions[row].height = 16
            row += 1
        
        ws.row_dimensions[row].height = 8
        row += 1
    
    ws.column_dimensions['A'].width = 18
    ws.column_dimensions['B'].width = 15
    ws.column_dimensions['C'].width = 12
    ws.column_dimensions['D'].width = 12
    ws.column_dimensions['E'].width = 18
    
    ws2 = wb.create_sheet('Statistici')
    ws2['A1'].value = 'STATISTICI'
    ws2['A1'].font = Font(bold=True, size=12, color='FFFFFF')
    ws2['A1'].fill = title_fill
    ws2['A1'].alignment = Alignment(horizontal='center')
    ws2.merge_cells('A1:D1')
    
    total_scans = len(scans)
    total_competitors = sum(len(s.get('competitors', [])) for s in scans)
    
    ws2['A3'].value = 'Total Produse Scanate:'
    ws2['B3'].value = total_scans
    ws2['B3'].font = Font(bold=True, size=12, color='0070C0')
    
    ws2['A4'].value = 'Total Competitori Găsiți:'
    ws2['B4'].value = total_competitors
    ws2['B4'].font = Font(bold=True, size=12, color='0070C0')
    
    ws2.column_dimensions['A'].width = 25
    ws2.column_dimensions['B'].width = 15
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'{DATA_DIR}/Raport_Preturi_FRUMOS_{timestamp}.xlsx'
    wb.save(filename)
    
    return filename

def extract_dimensions(text):
    if not text:
        return []
    pattern = r'(\d+)\s*[x×]\s*(\d+)'
    matches = re.findall(pattern, text, re.IGNORECASE)
    dimensions = []
    for match in matches:
        dim = f"{match[0]}x{match[1]}"
        if dim not in dimensions:
            dimensions.append(dim)
    return dimensions

def normalize_dimensions(dim_list):
    normalized = []
    for dim in dim_list:
        parts = dim.split('x')
        if len(parts) == 2:
            try:
                a, b = int(parts[0]), int(parts[1])
                sorted_dim = f"{min(a,b)}x{max(a,b)}"
                if sorted_dim not in normalized:
                    normalized.append(sorted_dim)
            except:
                pass
    return sorted(normalized)

def validate_dimensions(sku_name, snippet_text, threshold=0.7):
    sku_dims = extract_dimensions(sku_name)
    snippet_dims = extract_dimensions(snippet_text)
    
    if not sku_dims:
        return {'valid': True, 'reason': 'No dims in SKU'}
    
    if not snippet_dims:
        return {'valid': False, 'reason': 'No dims in snippet'}
    
    sku_dims_norm = normalize_dimensions(sku_dims)
    snippet_dims_norm = normalize_dimensions(snippet_dims)
    
    matches = [d for d in sku_dims_norm if d in snippet_dims_norm]
    match_rate = len(matches) / len(sku_dims_norm) if sku_dims_norm else 0
    is_valid = match_rate >= threshold
    
    return {
        'valid': is_valid,
        'reason': f"Match {len(matches)}/{len(sku_dims_norm)}" if is_valid else f"Mismatch {len(matches)}/{len(sku_dims_norm)}"
    }

def extract_foglia_price(text):
    match = re.search(r'([\d.,]+)\s*RON\s*[·●]\s*(?:●\s*)?[ÎI]n stoc', text, re.IGNORECASE)
    if match:
        price = clean_price(match.group(1))
        if price > 0:
            return price
    match = re.search(r'([\d.,]+)\s*RON[^·]*[ÎI]n stoc', text, re.IGNORECASE)
    if match:
        price = clean_price(match.group(1))
        if price > 0:
            return price
    return None

def extract_bagno_price(text):
    """Bagno.ro: max price (main product)"""
    prices = []
    matches = re.finditer(r'([\d.,]+)\s*(?:RON|Lei)', text, re.IGNORECASE)
    for match in matches:
        price = clean_price(match.group(1))
        if price > 0:
            prices.append(price)
    return max(prices) if prices else None

def extract_bagno_price_fixed(text):
    """Bagno.ro FIXED: MIN price (first/main product)"""
    prices = []
    matches = re.finditer(r'([\d.,]+)\s*(?:RON|Lei)', text, re.IGNORECASE)
    for match in matches:
        price = clean_price(match.group(1))
        if price > 0:
            prices.append(price)
    return min(prices) if prices else None

def extract_germanquality_price(text):
    """Germanquality.ro: max price (main product)"""
    prices = []
    matches = re.finditer(r'([\d.,]+)\s*(?:RON|Lei)', text, re.IGNORECASE)
    for match in matches:
        price = clean_price(match.group(1))
        if price > 0:
            prices.append(price)
    return max(prices) if prices else None

def extract_neakaisa_price(text):
    """Neakaisa: max price (main product)"""
    prices = []
    matches = re.finditer(r'([\d.,]+)\s*(?:RON|Lei)', text, re.IGNORECASE)
    for match in matches:
        price = clean_price(match.group(1))
        if price > 0:
            prices.append(price)
    return max(prices) if prices else None

BLOCKED = ['google', 'bing', 'microsoft', 'facebook', 'youtube', 'doarbai', 'termohabitat', 'wikipedia', 'amazon', 'ebay', 'compari.ro']

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

def extract_from_google_html(page, sku):
    results = []
    try:
        html_content = page.content()
        with open(f"{DEBUG_DIR}/google_{sku}_html.html", 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        price_patterns = re.finditer(
            r'([a-z0-9-]+\.ro)[^<>]{0,200}?([\d.,]+)\s*(?:RON|Lei)',
            html_content,
            re.IGNORECASE
        )
        
        for match in price_patterns:
            domain = match.group(1).lower()
            price = clean_price(match.group(2))
            
            if any(b in domain for b in BLOCKED):
                continue
            
            if not domain or len(domain) < 5:
                continue
            if price <= 0:
                continue
            if any(r['domain'] == domain for r in results):
                continue
            
            context = match.group(0).lower()
            transport_words = ['delivery', 'transport', 'livrare', 'shipping', 'expediere', ' sh']
            is_transport = any(tw in context for tw in transport_words)
            
            if not is_transport:
                results.append({'domain': domain, 'price': price, 'source': 'Google HTML'})
                logger.info(f"      🟠 {domain}: {price} Lei (HTML)")
        
        price_patterns_rev = re.finditer(
            r'([\d.,]+)\s*(?:RON|Lei)[^<>]{0,200}?([a-z0-9-]+\.ro)',
            html_content,
            re.IGNORECASE
        )
        
        for match in price_patterns_rev:
            price = clean_price(match.group(1))
            domain = match.group(2).lower()
            
            if any(b in domain for b in BLOCKED):
                continue
            
            if not domain or len(domain) < 5:
                continue
            if price <= 0:
                continue
            if any(r['domain'] == domain for r in results):
                continue
            
            context = match.group(0).lower()
            transport_words = ['delivery', 'transport', 'livrare', 'shipping', 'expediere', ' sh']
            is_transport = any(tw in context for tw in transport_words)
            
            if not is_transport:
                results.append({'domain': domain, 'price': price, 'source': 'Google HTML'})
                logger.info(f"      🟠 {domain}: {price} Lei (HTML)")
        
        if results:
            logger.info(f"   🟠 Metoda HTML: {len(results)} găsite")
    
    except Exception as e:
        logger.info(f"   ⚠️ HTML extract: {str(e)[:40]}")
    
    return results

def google_stealth_search(page, query, sku_for_match=None, sku_name=None):
    results = []
    search_query = f"{query} pret RON"
    url = f"https://www.google.com/search?q={quote_plus(search_query)}&hl=ro&gl=ro"
    file_suffix = sku_for_match or query.replace(' ', '_')[:20]
    
    try:
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        time.sleep(2)
        
        try:
            page.click('button:has-text("Accept all")', timeout=2000)
        except:
            try:
                page.click('button:has-text("Acceptă tot")', timeout=1000)
            except:
                pass
        
        time.sleep(1)
        page.screenshot(path=f"{DEBUG_DIR}/google_{file_suffix}.png")
        
        body_text = page.locator('body').inner_text()
        with open(f"{DEBUG_DIR}/google_{file_suffix}.txt", 'w', encoding='utf-8') as f:
            f.write(body_text)
        
        lines = body_text.split('\n')
        current_domain = None
        
        for i, line in enumerate(lines):
            line_lower = line.lower()
            
            domain_match = re.search(r'(?:https?://)?(?:www\.)?([a-z0-9-]+\.ro)', line_lower)
            if domain_match:
                d = domain_match.group(1)
                if len(d) > 4 and not any(b in d for b in BLOCKED):
                    current_domain = d
            
            query_lower = query.lower()
            has_match = query_lower in line_lower
            if not has_match and len(query.split()) > 1:
                words = query_lower.split()
                matches = sum(1 for w in words if w in line_lower and len(w) > 3)
                has_match = matches >= 2
            
            if has_match and current_domain:
                context = ' '.join(lines[max(0,i-2):min(len(lines),i+3)])
                
                if current_domain == 'germanquality.ro':
                    gq_price = extract_germanquality_price(context)
                    if gq_price and gq_price > 0:
                        if not any(r['domain'] == current_domain for r in results):
                            if sku_name:
                                dim_check = validate_dimensions(sku_name, context)
                                if not dim_check['valid']:
                                    logger.info(f"      🔴 {current_domain}: {gq_price} Lei - REJECTED (dims: {dim_check['reason']})")
                                    continue
                            results.append({'domain': current_domain, 'price': gq_price, 'source': 'Google SERP (GQ)'})
                            logger.info(f"      🟠 {current_domain}: {gq_price} Lei (GQ)")
                        continue
                
                if current_domain == 'foglia.ro':
                    foglia_price = extract_foglia_price(context)
                    if foglia_price and foglia_price > 0:
                        if not any(r['domain'] == current_domain for r in results):
                            if sku_name:
                                dim_check = validate_dimensions(sku_name, context)
                                if not dim_check['valid']:
                                    logger.info(f"      🔴 {current_domain}: {foglia_price} Lei - REJECTED (dims: {dim_check['reason']})")
                                    continue
                            results.append({'domain': current_domain, 'price': foglia_price, 'source': 'Google SERP (Foglia)'})
                            logger.info(f"      🟣 {current_domain}: {foglia_price} Lei (Foglia)")
                        continue
                
                if current_domain == 'bagno.ro':
                    bagno_price = extract_bagno_price_fixed(context)
                    if bagno_price and bagno_price > 0:
                        if not any(r['domain'] == current_domain for r in results):
                            if sku_name:
                                dim_check = validate_dimensions(sku_name, context)
                                if not dim_check['valid']:
                                    logger.info(f"      🔴 {current_domain}: {bagno_price} Lei - REJECTED (dims: {dim_check['reason']})")
                                    continue
                            results.append({'domain': current_domain, 'price': bagno_price, 'source': 'Google SERP (Bagno)'})
                            logger.info(f"      🟡 {current_domain}: {bagno_price} Lei (Bagno)")
                        continue
                
                if current_domain == 'neakaisa.ro':
                    neakaisa_price = extract_neakaisa_price(context)
                    if neakaisa_price and neakaisa_price > 0:
                        if not any(r['domain'] == current_domain for r in results):
                            if sku_name:
                                dim_check = validate_dimensions(sku_name, context)
                                if not dim_check['valid']:
                                    logger.info(f"      🔴 {current_domain}: {neakaisa_price} Lei - REJECTED (dims: {dim_check['reason']})")
                                    continue
                            results.append({'domain': current_domain, 'price': neakaisa_price, 'source': 'Google SERP (Neakaisa)'})
                            logger.info(f"      🟤 {current_domain}: {neakaisa_price} Lei (Neakaisa)")
                        continue
                
                price_patterns = re.finditer(r'([\d.,]+)\s*(?:RON|Lei|lei)', context, re.IGNORECASE)
                valid_prices = []
                for pm in price_patterns:
                    price_value = clean_price(pm.group(1))
                    if price_value <= 0:
                        continue
                    start = max(0, pm.start() - 25)
                    end = min(len(context), pm.end() + 15)
                    price_context = context[start:end].lower()
                    transport_words = ['delivery', 'transport', 'livrare', 'shipping', 'expediere']
                    is_transport = any(tw in price_context for tw in transport_words)
                    if not is_transport:
                        valid_prices.append(price_value)
                
                if valid_prices:
                    price = min(valid_prices)
                    if sku_name:
                        dim_check = validate_dimensions(sku_name, context)
                        if not dim_check['valid']:
                            logger.info(f"      🔴 {current_domain}: {price} Lei - REJECTED (dims: {dim_check['reason']})")
                            continue
                    if not any(r['domain'] == current_domain for r in results):
                        results.append({'domain': current_domain, 'price': price, 'source': 'Google SERP'})
                        logger.info(f"      🟢 {current_domain}: {price} Lei")
        
        logger.info(f"   📸 Google: {len(results)} cu preț")
        
        logger.info(f"   🔍 Metoda 2: bloc...")
        current_domain = None
        domain_line = -1
        
        for i, line in enumerate(lines):
            line_lower = line.lower()
            domain_match = re.search(r'(?:https?://)?(?:www\.)?([a-z0-9-]+\.ro)', line_lower)
            if domain_match:
                d = domain_match.group(1)
                if len(d) > 4 and not any(b in d for b in BLOCKED):
                    current_domain = d
                    domain_line = i
            
            if current_domain and current_domain == 'germanquality.ro' and domain_line >= 0 and i <= domain_line + 6:
                if not any(r['domain'] == current_domain for r in results):
                    block_start = domain_line
                    block_end = min(len(lines), domain_line + 7)
                    block_text = ' '.join(lines[block_start:block_end])
                    
                    gq_price = extract_germanquality_price(block_text)
                    if gq_price and gq_price > 0:
                        if sku_name:
                            dim_check = validate_dimensions(sku_name, block_text)
                            if not dim_check['valid']:
                                logger.info(f"      🔴 {current_domain}: {gq_price} Lei - REJECTED (dims: {dim_check['reason']})")
                                current_domain = None
                                domain_line = -1
                                continue
                        results.append({'domain': current_domain, 'price': gq_price, 'source': 'Google SERP (GQ)'})
                        logger.info(f"      🟠 {current_domain}: {gq_price} Lei (GQ)")
                        current_domain = None
                        domain_line = -1
                        continue
            
            if current_domain and domain_line >= 0 and i <= domain_line + 6:
                query_lower = query.lower()
                if query_lower in line_lower:
                    if any(r['domain'] == current_domain for r in results):
                        continue
                    
                    block_start = domain_line
                    block_end = min(len(lines), domain_line + 7)
                    block_text = ' '.join(lines[block_start:block_end])
                    
                    if current_domain == 'foglia.ro':
                        foglia_price = extract_foglia_price(block_text)
                        if foglia_price and foglia_price > 0:
                            if sku_name:
                                dim_check = validate_dimensions(sku_name, block_text)
                                if not dim_check['valid']:
                                    logger.info(f"      🔴 {current_domain}: {foglia_price} Lei - REJECTED (dims: {dim_check['reason']})")
                                    current_domain = None
                                    domain_line = -1
                                    continue
                            results.append({'domain': current_domain, 'price': foglia_price, 'source': 'Google SERP (Foglia)'})
                            logger.info(f"      🟣 {current_domain}: {foglia_price} Lei (Foglia)")
                            current_domain = None
                            domain_line = -1
                            continue
                    
                    if current_domain == 'bagno.ro':
                        bagno_price = extract_bagno_price_fixed(block_text)
                        if bagno_price and bagno_price > 0:
                            if sku_name:
                                dim_check = validate_dimensions(sku_name, block_text)
                                if not dim_check['valid']:
                                    logger.info(f"      🔴 {current_domain}: {bagno_price} Lei - REJECTED (dims: {dim_check['reason']})")
                                    current_domain = None
                                    domain_line = -1
                                    continue
                            results.append({'domain': current_domain, 'price': bagno_price, 'source': 'Google SERP (Bagno)'})
                            logger.info(f"      🟡 {current_domain}: {bagno_price} Lei (Bagno)")
                            current_domain = None
                            domain_line = -1
                            continue
                    
                    if current_domain == 'neakaisa.ro':
                        neakaisa_price = extract_neakaisa_price(block_text)
                        if neakaisa_price and neakaisa_price > 0:
                            if sku_name:
                                dim_check = validate_dimensions(sku_name, block_text)
                                if not dim_check['valid']:
                                    logger.info(f"      🔴 {current_domain}: {neakaisa_price} Lei - REJECTED (dims: {dim_check['reason']})")
                                    current_domain = None
                                    domain_line = -1
                                    continue
                            results.append({'domain': current_domain, 'price': neakaisa_price, 'source': 'Google SERP (Neakaisa)'})
                            logger.info(f"      🟤 {current_domain}: {neakaisa_price} Lei (Neakaisa)")
                            current_domain = None
                            domain_line = -1
                            continue
                    
                    price_patterns = re.finditer(r'([\d.,]+)\s*(?:RON|Lei|lei)', block_text, re.IGNORECASE)
                    valid_prices = []
                    for pm in price_patterns:
                        price_value = clean_price(pm.group(1))
                        if price_value <= 0:
                            continue
                        start = max(0, pm.start() - 25)
                        end = min(len(block_text), pm.end() + 15)
                        price_context = block_text[start:end].lower()
                        transport_words = ['delivery', 'transport', 'livrare', 'shipping', 'expediere']
                        is_transport = any(tw in price_context for tw in transport_words)
                        if not is_transport:
                            valid_prices.append(price_value)
                    
                    if valid_prices:
                        price = valid_prices[0]
                        if sku_name:
                            dim_check = validate_dimensions(sku_name, block_text)
                            if not dim_check['valid']:
                                logger.info(f"      🔴 {current_domain}: {price} Lei - REJECTED (dims: {dim_check['reason']})")
                                current_domain = None
                                domain_line = -1
                                continue
                        results.append({'domain': current_domain, 'price': price, 'source': 'Google SERP (bloc)'})
                        logger.info(f"      🔵 {current_domain}: {price} Lei (bloc)")
                    
                    current_domain = None
                    domain_line = -1
        
        logger.info(f"   📸 Total după bloc: {len(results)}")
        
        html_results = extract_from_google_html(page, query)
        for r in html_results:
            if not any(existing['domain'] == r['domain'] for existing in results):
                results.append(r)
        
        if html_results:
            logger.info(f"   📸 Total după HTML: {len(results)}")
        
    except Exception as e:
        logger.info(f"   ⚠️ Google: {str(e)[:40]}")
    
    return results

def get_domains_from_bing(page, sku):
    results = []
    try:
        for block in page.locator('.b_algo').all()[:15]:
            try:
                text = block.inner_text()
                text_lower = text.lower()
                
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
                if any(r['domain'] == domain for r in results):
                    continue
                
                has_sku = sku.lower() in text_lower
                price = 0
                price_match = re.search(r'([\d.,]+)\s*(?:RON|Lei|lei)', text)
                if price_match:
                    price = clean_price(price_match.group(1))
                
                results.append({'domain': domain, 'price': price, 'has_sku': has_sku, 'source': 'Bing SERP'})
                
                if price > 0 and has_sku:
                    logger.info(f"      🔵 {domain}: {price} Lei")
                elif has_sku:
                    logger.info(f"      🔵 {domain}: (pe site)")
                    
            except:
                continue
    except:
        pass
    
    return results

def find_price_on_site(page, domain, sku, save_debug=False):
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
        
        error_phrases = ['0 produse', 'nu s-au gasit', 'nu am gasit', 'niciun rezultat', '0 rezultate']
        for phrase in error_phrases:
            if phrase in body_lower and 'produse)' not in body_lower:
                logger.info(f"         ⚠️ {phrase}")
                return None
        
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
        
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        
        page = context.new_page()
        
        try:
            logger.info(f"   🔍 Google #1: SKU...")
            google_results = google_stealth_search(page, sku, sku, sku_name=name)
            
            for r in google_results:
                if r['price'] > 0:
                    found.append({
                        'name': r['domain'],
                        'price': r['price'],
                        'url': f"https://www.{r['domain']}",
                        'method': 'Google SKU'
                    })
            
            if len(found) < 5 and name and len(name) > 10:
                logger.info(f"   🔍 Google #2: Denumire...")
                name_words = name.split()[:6]
                name_query = ' '.join(name_words)
                if sku.upper() not in name_query.upper():
                    name_query += f" {sku}"
                
                google_results_name = google_stealth_search(page, name_query, f"{sku}_name", sku_name=name)
                
                for r in google_results_name:
                    if r['price'] > 0 and not any(f['name'] == r['domain'] for f in found):
                        found.append({
                            'name': r['domain'],
                            'price': r['price'],
                            'url': f"https://www.{r['domain']}",
                            'method': 'Google Name'
                        })
                        logger.info(f"      🟡 {r['domain']}: {r['price']} Lei (din denumire)")
            
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
                    if any(f['name'] == r['domain'] for f in found):
                        continue
                    if r['price'] > 0 and r.get('has_sku'):
                        found.append({
                            'name': r['domain'],
                            'price': r['price'],
                            'url': f"https://www.{r['domain']}",
                            'method': 'Bing SERP'
                        })
            
            logger.info(f"   📊 Total: {len(found)}")
            
        except Exception as e:
            logger.info(f"   ❌ {str(e)[:50]}")
        finally:
            page.close()
        
        browser.close()
    
    for r in found:
        r['diff'] = round(((r['price'] - your_price) / your_price) * 100, 1) if your_price > 0 else 0
    
    if your_price > 0:
        before_filter = len(found)
        found = [r for r in found if -30 <= r['diff'] <= 30]
        filtered_count = before_filter - len(found)
        if filtered_count > 0:
            logger.info(f"   🔻 Filtrat {filtered_count} outliers (±30%)")
    
    found.sort(key=lambda x: x['price'])
    return found[:5]

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
    save_scan(sku, name, your_price, results)
    
    return jsonify({"status": "success", "competitors": results})

@app.route('/api/report', methods=['GET'])
def api_report():
    try:
        filepath = generate_excel_report()
        if filepath and os.path.exists(filepath):
            return send_file(filepath, as_attachment=True, download_name=f'Raport_Preturi_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx')
        return jsonify({"status": "error", "message": "Nu sunt date de generat"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/scans', methods=['GET'])
def api_scans():
    scans = load_scans()
    return jsonify({"status": "success", "count": len(scans), "scans": scans})

@app.route('/debug/<filename>')
def get_debug(filename):
    filepath = f"{DEBUG_DIR}/{filename}"
    if os.path.exists(filepath):
        return send_file(filepath)
    return "Not found", 404

if __name__ == '__main__':
    logger.info("🚀 PriceMonitor v11.0 - Bagno FIX (MIN price) - pe :8080")
    app.run(host='0.0.0.0', port=8080)
