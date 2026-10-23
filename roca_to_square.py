"""
Roca Sifter to Square POS & Zebra Label Desktop Launcher
--------------------------------------------------------
Double-clicking this file launches the Drag-and-Drop Station window on your
desktop and powers 1-click automatic Square POS API uploads and Zebra 2"x1"
thermal label printing in the background.

Key Features:
- Parses Roca Scan & Identify CSV exports
- Filters for Bin 1 line items ($0.70+ sift criteria)
- Rounds pricing to the nearest $0.25 increment ($0.70 -> $0.75, $0.88 -> $1.00, $1.45 -> $1.50)
- Generates standardized Code 128 barcodes matching Square SKUs
- Uploads items & variations to Square Catalog API (/v2/catalog/batch-upsert)
- Generates 2" x 1" thermal label PDFs (ReportLab) and direct Zebra ZPL files
"""

import os
import sys
import csv
import math
import uuid
import hashlib
from datetime import datetime, timezone
import json
import re
import argparse
import subprocess
import platform
import shutil
import tempfile
from urllib.parse import urlsplit, unquote, quote
from http.server import HTTPServer, BaseHTTPRequestHandler
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Any, Callable

try:
    import requests
except ImportError:
    requests = None

try:
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas
    from reportlab.graphics.barcode import code128, qr
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics import renderPDF
except ImportError:
    canvas = None
    code128 = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(APP_DIR, "roca_settings.json")
HTML_APP_FILE = os.path.join(APP_DIR, "roca_square_label_app.html")
PORT = 8765
APP_VERSION = "1.1.2"
OUTPUT_DIR = os.path.join(APP_DIR, "outputs")


# ==============================================================================
# 1. CORE BUSINESS LOGIC: PRICING & SKU GENERATION (NEAREST $0.25)
# ==============================================================================

def round_to_nearest_quarter(price: float) -> float:
    """
    Rounds pricing to the nearest $0.25 increment (standard half-up rounding).
    Examples:
      $0.70 -> $0.75
      $0.79 -> $0.75
      $0.88 -> $1.00
      $1.10 -> $1.00
      $1.15 -> $1.25
      $1.45 -> $1.50
      $1.79 -> $1.75
    """
    if price <= 0:
        return 0.0
    d_price = Decimal(str(price))
    quarters = (d_price / Decimal("0.25")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(quarters * Decimal("0.25"))


def clean_string_for_sku(text: str, max_length: int = 8) -> str:
    cleaned = re.sub(r'[^A-Za-z0-9]', '', text).upper()
    return cleaned[:max_length]


def generate_sku(card_name: str, card_num: str, condition: str, printing: str, seq: int) -> str:
    words = [w for w in re.split(r'[\s\W]+', card_name) if w]
    if len(words) >= 2:
        name_slug = "".join(w[0].upper() for w in words[:3])
    else:
        name_slug = clean_string_for_sku(card_name, max_length=4)

    num_slug = clean_string_for_sku(card_num, max_length=4) or "000"

    cond_lower = condition.lower()
    if "near" in cond_lower or "nm" in cond_lower:
        cond_code = "NM"
    elif "light" in cond_lower or "lp" in cond_lower:
        cond_code = "LP"
    elif "mod" in cond_lower or "mp" in cond_lower:
        cond_code = "MP"
    elif "heavy" in cond_lower or "hp" in cond_lower:
        cond_code = "HP"
    elif "damage" in cond_lower or "dmg" in cond_lower:
        cond_code = "DMG"
    else:
        cond_code = "SP"

    print_lower = printing.lower()
    finish_code = "F" if "foil" in print_lower else "N"

    return f"{name_slug}-{num_slug}-{cond_code}-{finish_code}-{seq:03d}"


# ==============================================================================
# 2. ROCA SCAN CSV INGESTION & FILTERING
# ==============================================================================

def parse_roca_csv(file_path: str, target_bin: str = "1", min_price: float = 0.70) -> List[Dict[str, Any]]:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Scan export not found at: {file_path}")

    filtered_cards = []

    with open(file_path, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = {col.strip().lower(): col for col in reader.fieldnames or []}

        def get_col(row, *aliases):
            for alias in aliases:
                col_name = headers.get(alias.lower())
                if col_name and row.get(col_name) is not None:
                    return str(row[col_name]).strip()
            return ""

        seq_counter = 1
        for row in reader:
            card_name = get_col(row, "Card Name", "Name")
            card_num = get_col(row, "Card #", "Card Number", "Number")
            condition = get_col(row, "Condition") or "Near Mint"
            printing = get_col(row, "Printing") or "Normal"
            language = get_col(row, "Language") or "English"
            rarity = get_col(row, "Rarity")
            raw_price_str = get_col(row, "Listing Price", "Price", "Market Price")
            bin_val = get_col(row, "Bin")
            qty_str = get_col(row, "Qty", "Quantity") or "1"

            try:
                raw_price = float(re.sub(r'[^\d.]', '', raw_price_str))
            except ValueError:
                raw_price = 0.0

            try:
                qty = int(qty_str)
            except ValueError:
                qty = 1

            bin_clean = bin_val.lower().replace("bin", "").strip()
            is_target_bin = (bin_clean == target_bin.lower()) or (target_bin.lower() in bin_clean) or (bin_val == "1")

            if is_target_bin or (raw_price >= min_price and not bin_val):
                rounded_price = round_to_nearest_quarter(raw_price)
                sku = generate_sku(card_name, card_num, condition, printing, seq_counter)

                item = {
                    "card_name": card_name,
                    "card_num": card_num,
                    "condition": condition,
                    "printing": printing,
                    "language": language,
                    "rarity": rarity,
                    "raw_price": raw_price,
                    "final_price": rounded_price,
                    "sku": sku,
                    "qty": qty,
                    "bin": bin_val,
                    "seq": seq_counter
                }
                filtered_cards.append(item)
                seq_counter += 1

    return filtered_cards


# ==============================================================================
# 3. ZEBRA THERMAL LABEL GENERATION (PDF & ZPL)
# ==============================================================================

def barcode_value(item):
    """Short numeric Code 128-C payload; use the same value as Square's SKU."""
    original = str(item.get("sku") or "").strip()
    if not original:
        raise ValueError("Each card needs a SKU before labels or uploads can be generated.")
    if re.fullmatch(r"[0-9]{12}", original):
        return original
    return f"{int(hashlib.sha256(original.encode()).hexdigest(), 16) % 10**12:012d}"


def label_fields(item):
    name = str(item.get("card_name") or item.get("cardName") or "Card")
    price = float(item.get("final_price", item.get("finalPrice", 0)))
    qty = int(item.get("qty", 1))
    if qty <= 0:
        raise ValueError("Card quantities must be positive integers.")
    return name, f"${price:.2f}", barcode_value(item), qty


def generate_zebra_pdf_labels(cards, output_pdf_path, width_in=2.0, height_in=1.0,
                              layout="standard", dpi=203):
    if canvas is None or code128 is None:
        raise RuntimeError("PDF labels require reportlab: pip install reportlab")
    if layout not in ("standard", "fold", "side") or dpi not in (203, 300):
        raise ValueError("Use standard/fold/side layout and 203/300 DPI.")
    width, height = width_in * inch, height_in * inch
    c = canvas.Canvas(output_pdf_path, pagesize=(width, height))
    for item in cards:
        name, price, value, qty = label_fields(item)
        if layout == "side":
            # A 12-digit linear Code 128 at 2 dots/module cannot fit a 1 inch half.
            # QR preserves the exact SKU and provides a full four-module quiet zone.
            from reportlab.lib.utils import simpleSplit
            for _ in range(qty):
                left_width = width / 2 - 12
                c.setFont("Helvetica-Bold", 8)
                lines = simpleSplit(name, "Helvetica-Bold", 8, left_width)
                for n, line in enumerate(lines[:3]):
                    if n == 2 and len(lines) > 3:
                        line = line[:-3] + "..."
                    c.drawString(5, height - 11 - n * 9, line)
                details = f"#{item.get('card_num') or item.get('cardNum') or ''} {item.get('condition') or ''}"
                c.setFont("Helvetica", 6)
                for n, line in enumerate(simpleSplit(details, "Helvetica", 6, left_width)[:2]):
                    c.drawString(5, 32 - n * 7, line)
                c.setFont("Helvetica-Bold", 12)
                c.drawString(5, 8, price)
                widget = qr.QrCodeWidget(value, barLevel='M', barBorder=4)
                x0, y0, x1, y1 = widget.getBounds()
                size = min(width/2 - 8, height - 12)
                drawing = Drawing(size, size, transform=[size/(x1-x0), 0, 0, size/(y1-y0), 0, 0])
                drawing.add(widget)
                renderPDF.draw(drawing, c, width * .75 - size/2, 11)
                c.setFont("Helvetica", 5)
                c.drawCentredString(width * .75, 4, value)
                c.showPage()
            continue
        # Two dots per narrow bar at 203 DPI, three at 300 DPI. Never shrink to fit.
        module = (2 if dpi == 203 else 3) * 72 / dpi
        bc = code128.Code128(value, barHeight=23 if layout == "fold" else 30,
                             barWidth=module, quiet=True, lquiet=10*module, rquiet=10*module,
                             humanReadable=False)
        if bc.width > width - 8:
            raise ValueError("Barcode does not fit this label width at a scannable module size.")
        for _ in range(qty):
            c.saveState()
            if layout == "fold":
                # Rotate the front half so text reads upright after folding over a top edge.
                c.translate(width, height * 1.5)
                c.rotate(180)
                name_y, price_y = height - 11, height - 25
            else:
                name_y, price_y = height - 11, height - 25
            c.setFont("Helvetica-Bold", 8)
            fitted = name
            while c.stringWidth(fitted, "Helvetica-Bold", 8) > width - 12:
                fitted = fitted[:-1]
            if fitted != name:
                fitted = fitted[:-3] + "..."
            c.drawCentredString(width/2, name_y, fitted)
            c.setFont("Helvetica-Bold", 12)
            c.drawCentredString(width/2, price_y, price)
            c.restoreState()
            bc.drawOn(c, (width-bc.width)/2, 9)
            c.setFont("Helvetica", 6)
            c.drawCentredString(width/2, 2, value)
            c.showPage()
    c.save()
    print(f"[✓] Generated PDF thermal labels: {output_pdf_path}")


def generate_zebra_zpl(cards, output_zpl_path, dpi=203, layout="standard"):
    if dpi not in (203, 300) or layout not in ("standard", "fold", "side"):
        raise ValueError("Use standard/fold/side layout and 203/300 DPI.")
    pw, ll = dpi * 2, dpi
    scale = dpi / 203
    dot = lambda n: round(n * scale)
    module = 2 if dpi == 203 else 3
    # 12 digits in Code C: start + 6 pairs + checksum + stop = 101 modules.
    bar_width = 101 * module
    x = (pw - bar_width) // 2
    commands = []
    for item in cards:
        name, price, value, qty = label_fields(item)
        # Hex-escape all field data so names cannot inject ZPL commands.
        escape = lambda t: "".join(f"_{b:02X}" for b in t.encode("utf-8"))
        if layout == "side":
            # Model 2 QR, M correction; 12 numeric digits fit version 1 (21 modules).
            # Keep four blank modules around the symbol on the right half.
            from reportlab.lib.utils import simpleSplit
            name_lines = simpleSplit(name, "Helvetica-Bold", 8, 60)[:3]
            fields = "".join(f"^FO{dot(14)},{dot(10+n*25)}^A0N,{dot(22)},{dot(10)}^FH^FD{escape(line)}^FS\n"
                             for n, line in enumerate(name_lines))
            details = f"#{item.get('card_num') or item.get('cardNum') or ''} {item.get('condition') or ''}"
            detail_lines = simpleSplit(details, "Helvetica", 6, 60)[:2]
            fields += "".join(f"^FO{dot(14)},{dot(105+n*19)}^A0N,{dot(16)},{dot(8)}^FH^FD{escape(line)}^FS\n"
                              for n, line in enumerate(detail_lines))
            qr_module = 5 if dpi == 203 else 7
            half = pw//2
            qr_x = half + (half-21*qr_module)//2
            qr_y = (ll-21*qr_module)//2 - dot(4)
            commands.append(
                f"^XA\n^CI28^PW{pw}^LL{ll}^LH0,0^LS0\n" + fields +
                f"^FO{dot(14)},{dot(154)}^A0N,{dot(34)},{dot(20)}^FD{price}^FS\n"
                f"^FO{qr_x},{qr_y}^BQN,2,{qr_module}^FDMA,{value}^FS\n"
                f"^FO{half},{dot(183)}^A0N,{dot(13)},{dot(8)}^FB{half},1,0,C^FD{value}^FS\n"
                f"^PQ{qty}^XZ\n")
            continue
        orientation = "I" if layout == "fold" else "N"
        name_y, price_y = (dot(56), dot(10)) if layout == "fold" else (dot(8), dot(39))
        zpl = (
            f"^XA\n^CI28^PW{pw}^LL{ll}^LH0,0^LS0\n"
            f"^FO{dot(16)},{name_y}^A0{orientation},{dot(21)},{dot(9)}"
            f"^FB{pw-dot(32)},1,0,C^FH^FD{escape(name[:40])}^FS\n"
            f"^FO{dot(16)},{price_y}^A0{orientation},{dot(31)},{dot(22)}"
            f"^FB{pw-dot(32)},1,0,C^FD{price}^FS\n"
            f"^BY{module},2^FO{x},{dot(112 if layout == 'fold' else 91)}"
            f"^BCN,{dot(65 if layout == 'fold' else 85)},N,N,N"
            f"^FD>;{value}^FS\n"
            f"^FO0,{dot(181)}^A0N,{dot(16)},{dot(12)}^FB{pw},1,0,C^FD{value}^FS\n"
            f"^PQ{qty}^XZ\n"
        )
        commands.append(zpl)
    with open(output_zpl_path, "w", encoding="utf-8") as f:
        f.writelines(commands)
    print(f"[✓] Generated Zebra ZPL script: {output_zpl_path}")


# ==============================================================================
# 4. SQUARE CATALOG API INTEGRATION
# ==============================================================================

def square_locations(access_token, environment):
    """Read-only connection check; never create catalog items to diagnose access."""
    if requests is None:
        raise ValueError("Install requests using Install dependencies.cmd.")
    token = str(access_token).strip()
    if not token:
        raise ValueError("Enter your Square access token, then click Load Square locations.")
    environment = str(environment).strip().lower()
    if environment not in ("production", "sandbox"):
        raise ValueError("Select Production or Sandbox.")
    base_url = ("https://connect.squareupsandbox.com" if environment == "sandbox"
                else "https://connect.squareup.com")
    try:
        response = requests.get(f"{base_url}/v2/locations", headers={
            "Authorization": f"Bearer {token}", "Square-Version": "2024-09-19"}, timeout=30)
    except requests.RequestException:
        raise ValueError("Could not reach Square. Check your connection and try Load Square locations again.") from None
    if response.status_code == 401:
        raise ValueError(f"Square rejected this token in {environment.title()} (HTTP 401). "
                         "Use a token for the selected environment; replace an expired or revoked token.")
    if response.status_code == 403:
        raise ValueError("Square denied location access (HTTP 403). Check this merchant's token "
                         "and MERCHANT_PROFILE_READ permission.")
    if not 200 <= response.status_code < 300:
        raise ValueError(f"Square location lookup failed (HTTP {response.status_code}). No items were uploaded.")
    data = response.json()
    if data.get("errors"):
        codes = ", ".join(str(e.get("code", "UNKNOWN")) for e in data["errors"])
        raise ValueError(f"Square location lookup failed: {codes}.")
    return [{"id": loc["id"], "name": loc.get("name") or loc["id"],
             "status": loc.get("status", "UNKNOWN"), "currency": loc.get("currency", "")}
            for loc in data.get("locations", [])]


def resolve_square_location(locations, location_id, environment):
    location_id = str(location_id or "").strip()
    active = [loc for loc in locations if loc.get("status") == "ACTIVE"]
    available = "; ".join(f"{loc['name'] if 'name' in loc else loc['id']} ({loc['id']})" for loc in active)
    if not active:
        raise ValueError(f"Square accepted the connection to {environment.title()}, but returned no active locations. "
                         "Check the merchant account and activate the receiving location in Square Dashboard.")
    if location_id and location_id not in {loc["id"] for loc in active}:
        raise ValueError(f"Saved location '{location_id}' is not an active location returned by this token "
                         f"in {environment.title()}. Available: {available}. "
                         "Click Load Square locations and select your receiving store. "
                         "No catalog items or inventory were uploaded.")
    if not location_id:
        if len(active) != 1:
            raise ValueError(f"Choose the receiving location before uploading. Available: {available}. "
                             "Use Load Square locations in the app or --square-location in the CLI.")
        return active[0]["id"]
    return location_id


def upload_to_square(
    cards: List[Dict[str, Any]],
    access_token: str,
    environment: str = "production",
    log_fn: Callable[[str], None] = print,
    location_id: str = ""
) -> Dict[str, Any]:
    if requests is None or not access_token.strip():
        return {"uploaded_count": 0, "inventory_count": 0, "errors": ["Install requests and supply a Square access token."]}

    base_url = "https://connect.squareupsandbox.com" if environment.lower() == "sandbox" else "https://connect.squareup.com"
    endpoint = f"{base_url}/v2/catalog/batch-upsert"

    headers = {
        "Authorization": f"Bearer {access_token.strip()}",
        "Content-Type": "application/json",
        "Square-Version": "2024-09-19"
    }

    # Resolve the receiving store before creating anything. Never guess among stores.
    try:
        locations = square_locations(access_token, environment)
        location_id = resolve_square_location(locations, location_id, environment)
        values = set()
        for card in cards:
            label_fields(card)
            value = barcode_value(card)
            if value in values:
                raise ValueError("Duplicate barcode in this import. Give each card row a unique SKU.")
            values.add(value)
            existing = requests.post(f"{base_url}/v2/catalog/search", headers=headers,
                json={"object_types": ["ITEM_VARIATION"], "query": {
                    "exact_query": {"attribute_name": "sku", "attribute_value": value}}}, timeout=30)
            existing.raise_for_status()
            found = existing.json()
            if found.get("errors"):
                raise ValueError(json.dumps(found["errors"]))
            if found.get("objects"):
                raise ValueError(f"SKU {value} already exists. Import stopped to avoid duplicate items "
                                 "or resetting stock after sales. Reconcile this card in Square first.")
    except Exception as exc:
        return {"uploaded_count": 0, "inventory_count": 0, "errors": [str(exc)]}

    batch_size = 100
    total_uploaded = 0
    inventory_count = 0
    errors = []

    for i in range(0, len(cards), batch_size):
        chunk = cards[i:i + batch_size]
        objects_payload = []
        pending_inventory = []

        for item in chunk:
            c_name = item.get("card_name") or item.get("cardName") or "Card"
            c_num = item.get("card_num") or item.get("cardNum") or "000"
            c_cond = item.get("condition") or "Near Mint"
            c_print = item.get("printing") or "Normal"
            c_lang = item.get("language") or "English"
            c_price = float(item.get("final_price") or item.get("finalPrice") or 0.0)
            c_sku = barcode_value(item)

            item_id = f"#item_{uuid.uuid4().hex[:12]}"
            var_id = f"#var_{uuid.uuid4().hex[:12]}"
            price_cents = int(round(c_price * 100))
            pending_inventory.append((var_id, int(item.get("qty", 1))))

            obj = {
                "type": "ITEM",
                "id": item_id,
                "present_at_all_locations": True,
                "item_data": {
                    "name": f"{c_name} (#{c_num})",
                    "description": f"Roca Scan | {c_cond} | {c_print} | {c_lang}",
                    "variations": [
                        {
                            "type": "ITEM_VARIATION",
                            "id": var_id,
                            "present_at_all_locations": True,
                            "item_variation_data": {
                                "name": f"{c_cond} {c_print}",
                                "pricing_type": "FIXED_PRICING",
                                "price_money": {
                                    "amount": price_cents,
                                    "currency": "USD"
                                },
                                "sku": c_sku,
                                "track_inventory": True
                            }
                        }
                    ]
                }
            }
            objects_payload.append(obj)

        payload = {
            "idempotency_key": str(uuid.uuid4()),
            "batches": [{"objects": objects_payload}]
        }

        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=30)
            if response.status_code == 200:
                resp_json = response.json()
                if resp_json.get("errors"):
                    raise ValueError(json.dumps(resp_json["errors"]))
                created_count = len(chunk)
                total_uploaded += created_count
                mappings = {m["client_object_id"]: m["object_id"]
                            for m in resp_json.get("id_mappings", [])}
                changes = []
                for temporary_id, qty in pending_inventory:
                    if temporary_id not in mappings:
                        raise ValueError("Square did not return the variation ID; inventory was not confirmed.")
                    changes.append({"type": "PHYSICAL_COUNT", "physical_count": {
                        "catalog_object_id": mappings[temporary_id],
                        "location_id": location_id, "state": "IN_STOCK",
                        "quantity": str(qty),
                        "occurred_at": datetime.now(timezone.utc).isoformat()}})
                stock_response = requests.post(f"{base_url}/v2/inventory/changes/batch-create",
                    headers=headers, json={"idempotency_key": str(uuid.uuid4()),
                                         "changes": changes}, timeout=30)
                stock_response.raise_for_status()
                stock_result = stock_response.json()
                if stock_result.get("errors"):
                    raise ValueError(json.dumps(stock_result["errors"]))
                inventory_count += sum(qty for _, qty in pending_inventory)
                log_fn(f"[✓] Synced {created_count} items and their starting stock at {location_id}.")
            else:
                err_msg = f"HTTP {response.status_code}: {response.text}"
                log_fn(f"[!] Square error: {err_msg}")
                errors.append(err_msg)
        except Exception as e:
            err_msg = f"Network error: {str(e)}"
            log_fn(f"[!] {err_msg}")
            errors.append(err_msg)

    return {"uploaded_count": total_uploaded, "inventory_count": inventory_count, "errors": errors}


# ==============================================================================
# 5. DESKTOP LAUNCHER SERVER (POWERS THE DRAG-AND-DROP WINDOW)
# ==============================================================================

def load_settings() -> Dict[str, Any]:
    default_cfg = {
        "targetBin": "1",
        "minPrice": 0.70,
        "roundingMode": "nearest_25",
        "squareToken": "",
        "autoPdf": True,
        "squareLocationId": "",
        "squareEnvironment": "production",
        "labelLayout": "standard",
        "printerDpi": 203
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                default_cfg.update(json.load(f))
        except Exception:
            pass
    return default_cfg


def save_settings(cfg: Dict[str, Any]):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def open_file_in_os(filepath: str):
    try:
        if platform.system() == "Windows":
            os.startfile(filepath)
        elif platform.system() == "Darwin":
            subprocess.call(["open", filepath])
        else:
            subprocess.call(["xdg-open", filepath])
    except Exception:
        pass


class RocaStationHandler(BaseHTTPRequestHandler):
    def log_message(self, format_str, *args):
        pass

    def send_json(self, data, status=200):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def local_request(self):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            self.send_json({"errors": ["Use the station's 127.0.0.1 address."]}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{expected}":
            self.send_json({"errors": ["Open the interface through the Python launcher."]}, 403)
            return False
        return True

    def do_GET(self):
        if not self.local_request():
            return
        path = urlsplit(self.path).path
        if path == "/api/version":
            self.send_json({"version": APP_VERSION})
            return
        if path == "/api/settings":
            self.send_json(load_settings())
            return
        if path.startswith("/files/"):
            name = unquote(path[len("/files/"):])
            if name != os.path.basename(name) or "/" in name or "\\" in name or not name.endswith((".pdf", ".zpl")):
                self.send_json({"errors": ["Invalid output filename."]}, 400)
                return
            filename = os.path.join(OUTPUT_DIR, name)
            content_type = "application/pdf" if name.endswith(".pdf") else "text/plain; charset=utf-8"
        elif path == "/":
            filename = HTML_APP_FILE
            content_type = "text/html; charset=utf-8"
        else:
            self.send_json({"errors": ["Not found."]}, 404)
            return
        try:
            with open(filename, "rb") as f:
                payload = f.read()
        except FileNotFoundError:
            self.send_json({"errors": ["File missing. Extract the complete release ZIP, including the HTML interface."]}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if not self.local_request():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 20_000_000:
                raise ValueError("Upload must be between 1 byte and 20 MB.")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            if self.path == "/api/settings":
                save_settings(body)
                self.send_json({"status": "ok"})
                return
            cfg = load_settings()
            cfg.update(body.get("settings", {}))
            if self.path == "/api/square/locations":
                locations = square_locations(cfg.get("squareToken", ""), cfg.get("squareEnvironment", "production"))
                self.send_json({"environment": cfg.get("squareEnvironment", "production"), "locations": locations})
                return
            if self.path == "/api/import":
                # Parse in Python so the GUI and CLI share filters and SKU generation.
                with tempfile.TemporaryDirectory() as directory:
                    csv_path = os.path.join(directory, "scan.csv")
                    with open(csv_path, "w", encoding="utf-8", newline="") as f:
                        f.write(body.get("csv", ""))
                    cards = parse_roca_csv(csv_path, target_bin=str(cfg.get("targetBin", "1")),
                                           min_price=float(cfg.get("minPrice", 0.70)))
                self.send_json({"cards": cards, "barcodes": [barcode_value(card) for card in cards]})
                return
            if self.path != "/api/process":
                self.send_json({"errors": ["Not found."]}, 404)
                return
            cards = body.get("cards", [])
            if not cards:
                raise ValueError("No matching cards. Choose a CSV and check the bin filter.")
            for card in cards:
                label_fields(card)
            upload = bool(body.get("uploadToSquare", False))
            token = str(cfg.get("squareToken", "")).strip()
            if upload and not token:
                raise ValueError("Enter a Square token before uploading, or choose Create labels only.")
            save_settings(cfg)
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            prefix = "roca_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
            pdf_name, zpl_name = prefix + ".pdf", prefix + ".zpl"
            pdf_path, zpl_path = os.path.join(OUTPUT_DIR, pdf_name), os.path.join(OUTPUT_DIR, zpl_name)
            layout, dpi = cfg.get("labelLayout", "standard"), int(cfg.get("printerDpi", 203))
            generate_zebra_pdf_labels(cards, pdf_path, layout=layout, dpi=dpi)
            generate_zebra_zpl(cards, zpl_path, layout=layout, dpi=dpi)
            res = {"uploaded_count": 0, "inventory_count": 0, "errors": []}
            if upload:
                res = upload_to_square(cards, token, environment=cfg.get("squareEnvironment", "production"),
                                       location_id=cfg.get("squareLocationId", ""))
            self.send_json({
                "version": APP_VERSION,
                "status": "partial_error" if res["errors"] else "ok",
                "errors": res["errors"], "uploaded": res["uploaded_count"],
                "inventoryCount": res["inventory_count"],
                "pdfPath": pdf_path, "zplPath": zpl_path,
                "pdfUrl": "/files/" + quote(pdf_name), "zplUrl": "/files/" + quote(zpl_name)})
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            self.send_json({"status": "error", "errors": [str(exc)]}, 400)


def launch_desktop_station():
    url = f"http://127.0.0.1:{PORT}"
    print("=================================================================")
    print("   Roca Sifter -> Square POS & Zebra Drag-and-Drop Station      ")
    print("=================================================================")
    # Bind before opening Chrome: an older running station must not serve this window.
    try:
        server = HTTPServer(("127.0.0.1", PORT), RocaStationHandler)
    except OSError as exc:
        raise RuntimeError(f"Cannot start Roca {APP_VERSION}: port {PORT} is already in use. "
                           "Close the older Roca/Python station window and try again.") from exc
    print(f"Roca release {APP_VERSION} | {os.path.abspath(__file__)}")
    print(f"Station running at: {url}")
    print("Opening drag-and-drop window... (Close this window when done)")

    # Chrome is often installed outside PATH on Windows.
    chrome_candidates = []
    if platform.system() == "Windows":
        chrome_candidates.append(shutil.which("chrome.exe"))
        # Chrome can be registered at a custom location outside PATH.
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                                        0, winreg.KEY_READ | view) as key:
                        chrome_candidates.append(winreg.QueryValueEx(key, "")[0].strip('"'))
                except OSError:
                    pass
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            directory = os.environ.get(variable)
            if directory:
                chrome_candidates.append(os.path.join(directory, "Google", "Chrome", "Application", "chrome.exe"))
    elif platform.system() == "Darwin":
        chrome_candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        ]
    else:
        chrome_candidates = [shutil.which("google-chrome"), shutil.which("google-chrome-stable")]

    opened_app_window = False
    for browser_exe in chrome_candidates:
        if browser_exe and os.path.isfile(browser_exe):
            try:
                subprocess.Popen([browser_exe, f"--app={url}", "--window-size=1150,820"])
                opened_app_window = True
                break
            except OSError:
                continue

    if not opened_app_window:
        print("Chrome could not be launched. Install Google Chrome, then restart the station, "
              "or paste the station address into Chrome manually. Edge will not be opened.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down station.")
    finally:
        server.server_close()


# ==============================================================================
# 6. CLI ENTRYPOINT
# ==============================================================================

def main():
    if len(sys.argv) == 1:
        launch_desktop_station()
        return

    parser = argparse.ArgumentParser(description="Process Roca Sifter scans, round prices to nearest $0.25, generate Zebra labels, and upload to Square POS.")
    parser.add_argument("--csv", help="Path to Roca Scan & Identify CSV export.")
    parser.add_argument("--bin", default="1", help="Target sift bin to process (default: 1).")
    parser.add_argument("--min-price", type=float, default=0.70, help="Minimum raw listing price filter (default: 0.70).")
    parser.add_argument("--pdf-out", default="zebra_labels_2x1.pdf", help="Output path for Zebra label PDF.")
    parser.add_argument("--zpl-out", default="zebra_labels.zpl", help="Output path for Zebra raw ZPL file.")
    parser.add_argument("--square-token", default=os.getenv("SQUARE_ACCESS_TOKEN", ""), help="Square API Access Token.")
    parser.add_argument("--label-layout", choices=["standard", "fold", "side"], default="standard")
    parser.add_argument("--dpi", type=int, choices=[203, 300], default=203)
    parser.add_argument("--square-location", default=os.getenv("SQUARE_LOCATION_ID", ""))
    parser.add_argument("--sandbox", action="store_true", help="Use Square Sandbox environment instead of Production.")
    parser.add_argument("--dry-run", action="store_true", help="Run full parsing and label generation without uploading to Square.")

    args = parser.parse_args()

    if not args.csv:
        launch_desktop_station()
        return

    cards = parse_roca_csv(args.csv, target_bin=args.bin, min_price=args.min_price)
    if not cards:
        print("[!] No cards matched the specified criteria. Exiting.")
        sys.exit(0)

    print(f"Found {len(cards)} matching cards.")
    generate_zebra_pdf_labels(cards, args.pdf_out, layout=args.label_layout, dpi=args.dpi)
    generate_zebra_zpl(cards, args.zpl_out, layout=args.label_layout, dpi=args.dpi)

    if not args.dry_run and args.square_token:
        env = "sandbox" if args.sandbox else "production"
        result = upload_to_square(cards, args.square_token, environment=env, location_id=args.square_location)
        if result["errors"]:
            print("[!] " + "; ".join(result["errors"]), file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()