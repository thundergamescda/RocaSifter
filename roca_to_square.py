"""
Roca Station 2: local receiving, review, trade-in offers, Square and Zebra labels.
Run with no arguments to open the Chrome desktop workspace. Keep the complete
application folder together and install requirements.txt before first launch.
The command-line CSV/label workflow remains available for existing operators.
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
import socket
import tempfile
from urllib.parse import urlsplit, unquote, quote
from http.server import ThreadingHTTPServer
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
APP_VERSION = "2.3.0"
OUTPUT_DIR = os.path.join(APP_DIR, "outputs")


class HTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # Windows SO_REUSEADDR can let two stations bind the same local port.
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


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


def generate_sku(card_name: str, card_num: str, condition: str, printing: str, seq: int,
                 language: str = "English", card_set: str = "") -> str:
    """Card identity is independent of row order, quantity and price."""
    normalize = lambda value: " ".join(str(value).casefold().split())
    condition_aliases = {"nm": "near mint", "lp": "lightly played", "mp": "moderately played",
                         "hp": "heavily played", "dmg": "damaged"}
    condition = normalize(condition)
    condition = condition_aliases.get(condition, condition)
    printing = normalize(printing)
    printing = {"non-foil": "normal", "nonfoil": "normal", "non foil": "normal"}.get(printing, printing)
    identity = [normalize(card_name), normalize(card_num), condition, printing,
                normalize(language), normalize(card_set)]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()
    return f"{int(digest, 16) % 10**12:012d}"


# ==============================================================================
# 2. ROCA SCAN CSV INGESTION & FILTERING
# ==============================================================================

def parse_roca_csv(file_path: str, target_bin: str = "1", min_price: float = 0.70) -> List[Dict[str, Any]]:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Scan export not found at: {file_path}")

    filtered_cards = []

    with open(file_path, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = {col.strip().lower(): col for col in reader.fieldnames or [] if col}
        if not ({"card name", "name"} & set(headers)):
            raise ValueError("The CSV needs a Card Name or Name column.")

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
            card_set = get_col(row, "Set Code", "Set", "Set Name", "Expansion")
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
                raise ValueError(f"Invalid quantity on CSV row {reader.line_num}.") from None
            if qty <= 0:
                raise ValueError(f"Quantity must be positive on CSV row {reader.line_num}.")
            if not card_name:
                continue

            bin_clean = bin_val.lower().replace("bin", "").strip()
            is_target_bin = target_bin == "*" or bin_clean == target_bin.lower().replace("bin", "").strip()

            if is_target_bin or (raw_price >= min_price and not bin_val):
                rounded_price = round_to_nearest_quarter(raw_price)
                sku = get_col(row, "SKU", "Square SKU") or generate_sku(card_name, card_num, condition, printing, seq_counter, language, card_set)

                item = {
                    "card_name": card_name,
                    "card_num": card_num,
                    "condition": condition,
                    "printing": printing,
                    "language": language,
                    "rarity": rarity,
                    "card_set": card_set,
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
                              layout="side", dpi=203):
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
                details = [f"{item.get('card_set') or ''} #{item.get('card_num') or item.get('cardNum') or ''}",
                           str(item.get('condition') or ''), str(item.get('printing') or '')]
                c.setFont("Helvetica", 6)
                for n, line in enumerate(details):
                    fitted = line
                    while c.stringWidth(fitted, "Helvetica", 6) > left_width:
                        fitted = fitted[:-1]
                    if fitted != line:
                        fitted = fitted[:-3] + "..."
                    c.drawString(5, 34 - n * 7, fitted)
                c.setFont("Helvetica-Bold", 12)
                c.drawString(5, 5, price)
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


def generate_zebra_zpl(cards, output_zpl_path, dpi=203, layout="side"):
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
            detail_lines = [f"{item.get('card_set') or ''} #{item.get('card_num') or item.get('cardNum') or ''}",
                            str(item.get('condition') or ''), str(item.get('printing') or '')]
            fields += "".join(f"^FO{dot(14)},{dot(96+n*20)}^A0N,{dot(16)},{dot(8)}^FH^FD{escape(line[:22])}^FS\n"
                              for n, line in enumerate(detail_lines))
            qr_module = 5 if dpi == 203 else 7
            half = pw//2
            qr_x = half + (half-21*qr_module)//2
            qr_y = (ll-21*qr_module)//2 - dot(4)
            commands.append(
                f"^XA\n^CI28^PW{pw}^LL{ll}^LH0,0^LS0\n" + fields +
                f"^FO{dot(14)},{dot(163)}^A0N,{dot(30)},{dot(19)}^FD{price}^FS\n"
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
    location_id: str = "",
    request_log=None
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
        # Combine repeated rows into one variation and the sum of incoming copies.
        grouped = {}
        for card in cards:
            label_fields(card)
            value = barcode_value(card)
            if value in grouped:
                previous = grouped[value]
                identity = lambda c: generate_sku(c.get("card_name", c.get("cardName", "")),
                    c.get("card_num", c.get("cardNum", "")), c.get("condition", "Near Mint"),
                    c.get("printing", "Normal"), 0, c.get("language", "English"), c.get("card_set", ""))
                if identity(previous) != identity(card):
                    raise ValueError(f"SKU {value} is assigned to different card details. Resolve it before uploading.")
                if label_fields(previous)[1] != label_fields(card)[1]:
                    raise ValueError(f"Duplicate SKU {value} has different prices in this CSV. Choose one price first.")
                previous["qty"] += int(card.get("qty", 1))
            else:
                grouped[value] = dict(card, qty=int(card.get("qty", 1)))
        existing_variations = {}
        for value in grouped:
            search_payload = {"object_types": ["ITEM_VARIATION"], "query": {
                    "exact_query": {"attribute_name": "sku", "attribute_value": value}}}
            existing = (request_log("search:" + value, f"{base_url}/v2/catalog/search", search_payload, headers)
                        if request_log else requests.post(f"{base_url}/v2/catalog/search", headers=headers,
                                                          json=search_payload, timeout=30))
            existing.raise_for_status()
            found = existing.json()
            if found.get("errors"):
                raise ValueError(json.dumps(found["errors"]))
            matches = [obj for obj in found.get("objects", []) if not obj.get("is_deleted")]
            if len(matches) > 1 or found.get("cursor"):
                raise ValueError(f"Multiple Square variations have SKU {value}. Resolve the duplicate SKUs in Square first.")
            if matches:
                variation_data = matches[0].get("item_variation_data", {})
                money = variation_data.get("price_money", {})
                incoming_price = label_fields(grouped[value])[1]
                if "amount" in money and (money.get("currency") != "USD" or
                        money["amount"] != int(Decimal(incoming_price[1:]) * 100)):
                    raise ValueError(f"SKU {value} has a different price or currency in Square. "
                                     "Match the CSV price to Square (or update Square) before adding copies, "
                                     "so the printed label and checkout price agree.")
                existing_variations[value] = matches[0]
        cards = list(grouped.values())
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

            if c_sku in existing_variations:
                variation = existing_variations[c_sku]
                pending_inventory.append((variation["id"], int(item.get("qty", 1))))
                # Preserve the existing price, SKU, version and other catalog fields.
                if not variation.get("item_variation_data", {}).get("track_inventory", False):
                    variation = dict(variation)
                    variation["item_variation_data"] = dict(variation.get("item_variation_data", {}), track_inventory=True)
                    objects_payload.append(variation)
                continue

            item_id = f"#item_{c_sku}"
            var_id = f"#var_{c_sku}"
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
            mappings = {}
            if objects_payload:
                response = (request_log(f"catalog:{i}", endpoint, payload, headers) if request_log else
                            requests.post(endpoint, headers=headers, json=payload, timeout=30))
                response.raise_for_status()
                resp_json = response.json()
                if resp_json.get("errors"):
                    raise ValueError(json.dumps(resp_json["errors"]))
                mappings = {m["client_object_id"]: m["object_id"]
                            for m in resp_json.get("id_mappings", [])}
            # Both new and existing variations receive additive stock adjustments.
            created_count = len(chunk)
            total_uploaded += created_count
            changes = []
            for temporary_id, qty in pending_inventory:
                if temporary_id.startswith("#") and temporary_id not in mappings:
                    raise ValueError("Square did not return the variation ID; inventory was not confirmed.")
                changes.append({"type": "ADJUSTMENT", "adjustment": {
                    "catalog_object_id": mappings.get(temporary_id, temporary_id),
                    "location_id": location_id, "from_state": "NONE", "to_state": "IN_STOCK",
                    "quantity": str(qty),
                    "occurred_at": datetime.now(timezone.utc).isoformat()}})
            stock_payload = {"idempotency_key": str(uuid.uuid4()), "changes": changes}
            if request_log:
                for change in changes:
                    change["adjustment"]["reference_id"] = payload["idempotency_key"]
            stock_url = f"{base_url}/v2/inventory/changes/batch-create"
            stock_response = (request_log(f"stock:{i}", stock_url, stock_payload, headers) if request_log else
                              requests.post(stock_url, headers=headers, json=stock_payload, timeout=30))
            stock_response.raise_for_status()
            stock_result = stock_response.json()
            if stock_result.get("errors"):
                raise ValueError(json.dumps(stock_result["errors"]))
            inventory_count += sum(qty for _, qty in pending_inventory)
            log_fn(f"[✓] Synced {created_count} items; added incoming copies to stock at {location_id}.")
        except Exception as e:
            err_msg = f"Square sync error: {str(e)}. If the request timed out, check Square stock before resubmitting this import."
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
        "labelLayout": "side",
        "printerDpi": 203
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                default_cfg.update(json.load(f))
        except Exception:
            pass
    # One-time migration: old installations retained the previous linear layout.
    # After it is saved once, subsequent explicit layout choices are respected.
    if not default_cfg.get("sideFoldDefaultApplied", False):
        default_cfg["labelLayout"] = "side"
        default_cfg["sideFoldDefaultApplied"] = True
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


from station_server import make_handler
RocaStationHandler = make_handler(sys.modules[__name__])


def open_station_browser(url):
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

    return opened_app_window


def launch_desktop_station(reuse_existing=False):
    from roca_update import update_in_progress
    if update_in_progress(APP_DIR):
        raise RuntimeError('Roca is being updated. Wait for the updater to finish; it will reopen the app for you.')
    from station_runtime import RuntimeControl, read_runtime, running_status
    url = f"http://127.0.0.1:{PORT}"
    # Bind before opening Chrome so another installation cannot serve this window.
    try:
        server = HTTPServer(("127.0.0.1", PORT), RocaStationHandler)
    except OSError as exc:
        if reuse_existing:
            # Allow a simultaneous first launch to finish initializing its runtime file.
            import time
            for _ in range(20):
                record = read_runtime(sys.modules[__name__])
                status = running_status(record) if record and record['port'] == PORT else None
                if status:
                    if status.get('stopping'):
                        raise RuntimeError("Roca is stopping. Wait for it to finish before starting again.") from exc
                    if status['version'] != APP_VERSION:
                        raise RuntimeError("An older Roca version is running. Use Stop Roca from its folder first.") from exc
                    if not open_station_browser(url):
                        raise RuntimeError("Install Google Chrome, then start Roca again.") from exc
                    return
                time.sleep(.1)
        raise RuntimeError(f"Cannot start Roca {APP_VERSION}: port {PORT} is already in use. "
                           "Use Stop Roca from the running station's folder, or close the older Roca/Python "
                           "console window, then try again.") from exc
    control = None
    try:
        from station_service import StationService
        server.station = StationService(sys.modules[__name__])
        control = server.runtime_control = RuntimeControl(server, sys.modules[__name__])
        server.station.start_watcher()
        print(f"Roca release {APP_VERSION} | {os.path.abspath(__file__)}")
        print(f"Station running at: {url}")
        print("Use Stop Roca to shut down. Closing Chrome leaves the station running.")
        if not open_station_browser(url):
            if reuse_existing:
                raise RuntimeError("Chrome could not be launched. Install Google Chrome, then restart Roca.")
            print("Chrome could not be launched. Install Google Chrome or paste the station address "
                  "into Chrome manually. Edge will not be opened.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down station.")
    finally:
        if hasattr(server, 'station'):
            server.station.stop_event.set()
            with server.station.lock:
                pass
            if server.station.watcher:
                server.station.watcher.join(timeout=5)
        server.server_close()
        if control:
            control.close()



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
    parser.add_argument("--label-layout", choices=["standard", "fold", "side"], default="side")
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
