"""Receipt workflows, folder intake and restart-safe Square request journal."""
import copy
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import io
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid
from station_store import Store, Vault, AdminAuth, StationError, now

MODES = ('bulk', 'trade', 'labels', 'online')
CARD_FIELDS = ('card_name', 'card_num', 'card_set', 'condition', 'printing', 'language',
               'qty', 'final_price', 'include', 'reviewed', 'storage_location', 'sku_override')


def money(value):
    try:
        result = Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        if not result.is_finite() or result < 0 or result > 1_000_000:
            raise ValueError()
        return float(result)
    except (ValueError, InvalidOperation):
        raise StationError('Enter a valid, nonnegative price.') from None


class StoredResponse:
    def __init__(self, data):
        self.data = data
        self.status_code = 200
    def json(self):
        return self.data
    def raise_for_status(self):
        pass


class StationService:
    def __init__(self, core):
        self.core = core
        self.store = Store(Path(core.SETTINGS_FILE).parent / 'station_data')
        self.vault = Vault(self.store.root)
        self.auth = AdminAuth(self.store)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.watch_status = {'state': 'off', 'message': 'Folder pickup is off.'}
        self.watch_observed = {}
        self.watcher = None
        if self.store.get('preferences') is None:
            old = core.load_settings()
            self.store.put('preferences', {
                'labelLayout': 'side', 'printerDpi': old.get('printerDpi', 203),
                'minPrice': old.get('minPrice', .70), 'cashPercent': 35, 'creditPercent': 50,
                'squareEnvironment': old.get('squareEnvironment', 'production'),
                'squareLocationId': old.get('squareLocationId', ''),
                'watchFolder': '', 'watchMode': 'labels'})
        self.migrate_legacy_token()
        self.watch_status = self.pickup_status()
        # A restart during a commit keeps the same receipt and journal for safe retry.
        for receipt in self.store.summaries():
            if receipt.get('inventory_status') == 'committing':
                full = self.store.receipt(receipt['id'])
                full['inventory_status'] = 'needs_attention'
                full['error'] = 'The station restarted during receiving. Retry this receipt using the saved operation.'
                self.store.save_receipt(full, ('interrupted', 'Receiving interrupted; saved operation retained.'))

    def migrate_legacy_token(self):
        path = Path(self.core.SETTINGS_FILE)
        if not path.exists():
            return
        try:
            old = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            return
        token = old.get('squareToken', '').strip()
        if token:
            if not self.vault.exists():
                self.vault.set(token)
            # Verify the existing vault is readable before removing the legacy copy.
            if not self.vault.read():
                raise StationError('Save the Square credential in Admin before migration.')
            old.pop('squareToken', None)
            from station_store import private_write
            private_write(path, json.dumps(old, indent=2).encode())

    def preferences(self):
        return self.store.get('preferences', {})

    def public_settings(self):
        pref = self.preferences()
        return dict({key: pref[key] for key in ('labelLayout', 'printerDpi', 'minPrice', 'cashPercent', 'creditPercent')}, squareConfigured=self.vault.exists() and bool(pref.get('squareLocationId')), squareEnvironment=pref['squareEnvironment'])

    def admin_settings(self):
        return dict(self.preferences(), tokenSaved=self.vault.exists())

    def save_preferences(self, changes):
        pref = self.preferences()
        pref.update({key: changes[key] for key in pref if key in changes})
        if pref['labelLayout'] not in ('side', 'standard', 'fold') or int(pref['printerDpi']) not in (203, 300):
            raise StationError('Choose a supported label layout and printer resolution.')
        if pref['squareEnvironment'] not in ('production', 'sandbox') or pref['watchMode'] not in MODES:
            raise StationError('Choose a valid environment and intake workflow.')
        for key in ('cashPercent', 'creditPercent'):
            pref[key] = money(pref[key])
            if pref[key] > 100:
                raise StationError('Offer percentages must be between 0 and 100.')
        pref['minPrice'] = money(pref['minPrice'])
        folder = str(pref['watchFolder']).strip()
        if folder and Path(folder).resolve() == Path(self.core.OUTPUT_DIR).resolve():
            raise StationError('Choose the Roca export folder, not this app’s generated output folder.')
        if folder and not Path(folder).is_dir():
            raise StationError('The export folder does not exist on this computer.')
        pref['watchFolder'] = folder
        pref['squareLocationId'] = str(pref['squareLocationId']).strip()
        token = str(changes.get('squareToken', '')).strip()
        connection_changed = token or any(pref[k] != self.preferences().get(k) for k in ('squareEnvironment', 'squareLocationId'))
        if connection_changed and (token or self.vault.exists()):
            locations = self.core.square_locations(token or self.vault.read(), pref['squareEnvironment'])
            pref['squareLocationId'] = self.core.resolve_square_location(locations, pref['squareLocationId'], pref['squareEnvironment'])
        if token:
            self.vault.set(token)
        previous_folder = self.preferences().get('watchFolder', '')
        if folder and (not previous_folder or Path(folder).resolve() != Path(previous_folder).resolve()):
            # Existing downloads are not new deliveries. Record them before enabling pickup.
            self.store.put('export_checkpoint', self.export_baseline(folder))
            self.watch_observed.clear()
        self.store.put('preferences', pref)
        self.watch_status = self.pickup_status()
        return self.admin_settings()

    def downloads_folder(self):
        folder = None
        if os.name == 'nt':
            import ctypes
            # FOLDERID_Downloads respects Windows folder redirection.
            folder_id = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID('374DE290-123F-4565-9164-39C4925E467B').bytes_le)
            value = ctypes.c_wchar_p()
            get_folder = ctypes.windll.shell32.SHGetKnownFolderPath
            get_folder.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
            get_folder.restype = ctypes.c_long
            if get_folder(ctypes.byref(folder_id), 0, None, ctypes.byref(value)) == 0:
                try:
                    folder = Path(value.value)
                finally:
                    free = ctypes.windll.ole32.CoTaskMemFree
                    free.argtypes = [ctypes.c_void_p]
                    free.restype = None
                    free(ctypes.cast(value, ctypes.c_void_p))
        folder = folder or Path.home() / 'Downloads'
        if not folder.is_dir():
            raise StationError('Downloads was not found. Enter the folder Roca exports into instead.')
        return str(folder)

    def pickup_status(self):
        folder = self.preferences().get('watchFolder', '')
        return {'state': 'starting' if folder else 'off', 'folder': folder,
                'message': 'Checking for new Roca exports.' if folder else 'Choose an export folder to enable automatic pickup.'}

    def parse(self, content):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'scan.csv'
            path.write_text(content, encoding='utf-8-sig')
            cards = self.core.parse_roca_csv(str(path), target_bin='*', min_price=0)
        if not cards:
            raise StationError('No card rows found. Export a Roca CSV with a Card Name or Name column.')
        for card in cards:
            card.update(row_id=uuid.uuid4().hex, include=True, reviewed=False,
                        storage_location='', sku_override='')
            # An explicit CSV SKU must survive identity normalization until deliberately removed.
            generated = self.core.generate_sku(card['card_name'], card['card_num'], card['condition'],
                card['printing'], 0, card['language'], card.get('card_set', ''))
            if str(card['sku']) != generated:
                card['sku_override'] = str(card['sku'])
        return cards

    def import_csv(self, content, filename, mode='bulk', new_delivery=False, intake_source='manual'):
        if mode not in MODES:
            raise StationError('Choose a workflow.')
        digest = hashlib.sha256(content.lstrip('\ufeff').replace('\r\n', '\n').encode()).hexdigest()
        with self.lock:
            existing = self.store.find_source(digest)
            if existing and not new_delivery:
                return {'receipt': self.store.receipt(existing), 'duplicate': True}
            cards = self.parse(content)
            receipt = {'id': 'R-' + time.strftime('%Y%m%d') + '-' + uuid.uuid4().hex[:6].upper(),
                'source_hash': digest, 'source_name': Path(filename).name[:200],
                'title': Path(filename).stem[:100] or 'New receipt', 'created': now(), 'mode': mode,
                'cards': cards, 'inventory_status': 'not_received', 'inventory_intent': None,
                'purchase': None, 'requires_purchase': mode == 'trade', 'customer_reference': '', 'notes': '', 'channel': 'tcgplayer',
                'storage_strategy': 'bin', 'storage_location': '', 'prepared': False,
                'intake_source': intake_source, 'pickup_pending': intake_source == 'folder',
                'cash_percent': self.preferences()['cashPercent'],
                'credit_percent': self.preferences()['creditPercent'], 'files': {}, 'error': ''}
            self.store.save_receipt(receipt, ('imported', f'{len(cards)} card rows imported into {mode}.'))
            return {'receipt': receipt, 'duplicate': False}

    def acknowledge_pickup(self, receipt_id):
        with self.lock:
            receipt = self.active_receipt(receipt_id)
            if receipt.get('pickup_pending'):
                receipt['pickup_pending'] = False
                self.store.save_receipt(receipt)
            return receipt

    def active_receipt(self, receipt_id):
        receipt = self.store.receipt(receipt_id)
        if receipt.get('deleted_at'):
            raise StationError('This receipt was deleted. Restore it before using it.')
        return receipt

    def delete_receipt(self, receipt_id, revision):
        with self.lock:
            receipt = self.store.receipt(receipt_id)
            if receipt.get('deleted_at'):
                return receipt
            if receipt['revision'] != revision:
                raise StationError('This receipt changed in another window. Reload it before deleting.')
            if receipt['inventory_status'] in ('committing', 'needs_attention'):
                raise StationError('Resolve the saved receiving operation before deleting this receipt.')
            receipt['deleted_at'] = now()
            self.store.save_receipt(receipt, ('deleted', 'Receipt removed from the active list. Square stock and receiving history kept.'))
            return receipt

    def restore_receipt(self, receipt_id):
        with self.lock:
            receipt = self.store.receipt(receipt_id)
            if receipt.get('deleted_at'):
                receipt.pop('deleted_at')
                self.store.save_receipt(receipt, ('restored', 'Receipt restored with its original inventory status. No stock changed.'))
            return receipt

    def validate_card(self, card):
        result = dict(card)
        for key in ('card_name', 'card_num', 'card_set', 'condition', 'printing', 'language', 'storage_location', 'sku_override'):
            result[key] = str(result.get(key, '')).strip()[:500]
        if not result['card_name'] or not result['condition'] or not result['printing']:
            raise StationError('Each card needs a name, condition, and finish / treatment.')
        quantity = str(result.get('qty', ''))
        if not quantity.isdecimal() or not 1 <= int(quantity) <= 100_000:
            raise StationError('Card quantity must be a whole number from 1 to 100,000.')
        result['qty'] = int(quantity)
        result['final_price'] = money(result.get('final_price', 0))
        for key in ('include', 'reviewed'):
            if not isinstance(result.get(key), bool):
                raise StationError('Review and inclusion flags must be true or false.')
        override = result['sku_override']
        if override and not (override.isascii() and override.isdigit() and len(override) == 12):
            raise StationError('An existing Square SKU must be exactly 12 digits.')
        result['sku'] = override or self.core.generate_sku(result['card_name'], result['card_num'],
            result['condition'], result['printing'], 0, result['language'], result['card_set'])
        return result

    def save_receipt(self, receipt_id, data):
        with self.lock:
            old = self.active_receipt(receipt_id)
            if data.get('revision') != old['revision']:
                raise StationError('This receipt changed in another window. Reload it before saving.')
            if old['inventory_status'] in ('committing', 'needs_attention'):
                raise StationError('Receiving is in progress or requires a saved retry. Card details are locked.')
            mode = data.get('mode', old['mode'])
            if mode not in MODES:
                raise StationError('Choose a valid workflow.')
            if mode == 'trade' and old['inventory_status'] == 'received' and not old['purchase']:
                raise StationError('Already received stock cannot become a new trade-in purchase.')
            old['mode'] = mode
            if mode == 'trade':
                old['requires_purchase'] = True
            for key in ('title', 'notes', 'customer_reference', 'storage_location'):
                if key in data:
                    old[key] = str(data[key]).strip()[:2000]
            for key, allowed in [('inventory_intent', (None, 'new', 'existing')), ('channel', ('tcgplayer', 'ebay')),
                                 ('storage_strategy', ('bin', 'display'))]:
                if key in data:
                    if data[key] not in allowed:
                        raise StationError('Invalid workflow setting.')
                    old[key] = data[key]
            for key in ('cash_percent', 'credit_percent'):
                if key in data:
                    old[key] = money(data[key])
                    if old[key] > 100:
                        raise StationError('Offer percentages must be 0–100.')
            if 'cards' in data:
                prior = {c['row_id']: c for c in old['cards']}
                rows = []
                for incoming in data['cards']:
                    if incoming.get('row_id') not in prior and not incoming.get('split_from'):
                        raise StationError('Unknown card row.')
                    base = prior.get(incoming.get('row_id')) or prior.get(incoming.get('split_from'))
                    if not base:
                        raise StationError('The original split row was not found.')
                    merged = dict(base)
                    merged.update({key: incoming[key] for key in CARD_FIELDS if key in incoming})
                    merged['row_id'] = incoming.get('row_id') or uuid.uuid4().hex
                    rows.append(self.validate_card(merged))
                if not rows or len({c['row_id'] for c in rows}) != len(rows):
                    raise StationError('Keep at least one row and unique row identifiers.')
                if old['inventory_status'] == 'received' or old['purchase']:
                    frozen = lambda c: {k: c.get(k) for k in CARD_FIELDS if k != 'storage_location'}
                    if len(rows) != len(old['cards']) or any(frozen(a) != frozen(b) for a, b in zip(rows, old['cards'])):
                        raise StationError('Purchased or received cards are locked. Create a separate correction receipt.')
                old['cards'] = rows
            self.store.save_receipt(old, ('saved', f'Receipt saved in {mode}.'))
            return old

    def approved_cards(self, receipt):
        cards = [self.validate_card(c) for c in receipt['cards'] if c.get('include', True)]
        if not cards:
            raise StationError('Include at least one card.')
        if any(not c['reviewed'] for c in cards):
            raise StationError('Review the identity, condition, finish and price of every included card first.')
        if receipt.get('requires_purchase') and not receipt['purchase']:
            raise StationError('Record an accepted buy before receiving or labeling a trade-in.')
        return cards

    def accept_buy(self, receipt_id, offer_type):
        with self.lock:
            receipt = self.active_receipt(receipt_id)
            if receipt['purchase']:
                return receipt
            if receipt['mode'] != 'trade' or offer_type not in ('cash', 'credit'):
                raise StationError('Choose a cash or store-credit offer on a trade-in receipt.')
            included = [c for c in receipt['cards'] if c.get('include', True)]
            if not included or any(not c['reviewed'] for c in included):
                raise StationError('Review all included cards before recording the accepted offer.')
            retail = sum(Decimal(str(c['final_price'])) * c['qty'] for c in included)
            percent = Decimal(str(receipt[offer_type + '_percent']))
            amount = money(retail * percent / 100)
            receipt['purchase'] = {'type': offer_type, 'amount': amount, 'at': now()}
            root = Path(self.core.OUTPUT_DIR)
            root.mkdir(parents=True, exist_ok=True)
            buy_file = receipt['id'] + '-accepted-buy.pdf'
            self.buy_receipt(root / buy_file, receipt, included)
            receipt['files']['buy'] = buy_file
            self.store.save_receipt(receipt, ('buy_accepted', f'Accepted {offer_type} offer: ${amount:.2f}. Payment is recorded outside this app.'))
            return receipt

    def buy_receipt(self, path, receipt, cards):
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import simpleSplit
        c=canvas.Canvas(str(path),pagesize=(612,792))
        y=735
        def heading():
            c.setFont('Helvetica-Bold',20);c.drawString(40,750,'Accepted buy record')
            c.setFont('Helvetica',9);c.drawString(40,730,receipt['id']+' | '+receipt['purchase']['at'][:10])
        heading()
        y=704
        c.setFont('Helvetica',10)
        c.drawString(40,y,'Reference: '+receipt.get('customer_reference','')[:85]);y-=22
        c.setFont('Helvetica-Bold',13)
        c.drawString(40,y,f"Agreed {receipt['purchase']['type']} offer: ${receipt['purchase']['amount']:.2f}");y-=20
        c.setFont('Helvetica',8);c.drawString(40,y,'Acceptance record only. Payment or store credit is handled outside this application.');y-=30
        for card in cards:
            if y<70:
                c.showPage();heading();y=700
            c.setFont('Helvetica-Bold',10)
            for line in simpleSplit(f"{card['qty']} x {card['card_name']}",'Helvetica-Bold',10,515)[:2]:
                c.drawString(40,y,line);y-=13
            c.setFont('Helvetica',8)
            detail=f"{card['card_set']} #{card['card_num']} | {card['condition']} | {card['printing']} | {card['language']}"
            for line in simpleSplit(detail,'Helvetica',8,515)[:2]:
                c.drawString(40,y,line);y-=11
            c.drawString(40,y,f"Proposed retail per copy: ${card['final_price']:.2f}");y-=22
        c.save()

    def journal_post(self, receipt_id, key, endpoint, payload, headers):
        with self.store.db() as db:
            row = db.execute('SELECT request,response FROM operations WHERE receipt_id=? AND key=?', (receipt_id,key)).fetchone()
            if row:
                saved = json.loads(row['request'])
                if saved['endpoint'] != endpoint:
                    raise StationError('Connection changed during a saved operation. Restore the original environment.')
                payload = saved['payload']
                if row['response']:
                    return StoredResponse(json.loads(row['response']))
            else:
                db.execute('INSERT INTO operations VALUES (?,?,?,NULL)',
                    (receipt_id,key,json.dumps({'endpoint':endpoint,'payload':payload})))
        response = self.core.requests.post(endpoint, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        if not result.get('errors'):
            with self.store.db() as db:
                db.execute('UPDATE operations SET response=? WHERE receipt_id=? AND key=?', (json.dumps(result),receipt_id,key))
        return response

    def receive(self, receipt_id):
        # Single receiving lane prevents parallel receipt commits, including repeated clicks.
        with self.lock:
            receipt = self.active_receipt(receipt_id)
            if receipt['inventory_status'] == 'received':
                return receipt
            if receipt['mode'] == 'bulk':
                raise StationError('Bulk sift does not receive inventory. Move this receipt to a receiving workflow.')
            if receipt['inventory_intent'] == 'existing':
                raise StationError('This receipt is set to not add inventory (already-owned stock or labels only). Change the inventory choice before receiving new copies.')
            if receipt['inventory_intent'] != 'new':
                raise StationError('Choose whether this batch should add inventory before receiving it.')
            cards = self.approved_cards(receipt)
            config = self.preferences()
            token = self.vault.read()
            if not token or not config.get('squareLocationId'):
                raise StationError('Ask an admin to save and test the Square connection and receiving location.')
            connection = {k: config[k] for k in ('squareEnvironment','squareLocationId')}
            if receipt.get('square_connection') and receipt['square_connection'] != connection:
                raise StationError('Restore the Square environment and location used by this receipt before retrying.')
            receipt['square_connection'] = connection
            receipt['inventory_status'] = 'committing'
            self.store.save_receipt(receipt, ('receiving', 'Approved receipt submitted to Square.'))
            try:
                result = self.core.upload_to_square(cards, token, environment=config['squareEnvironment'],
                    location_id=config['squareLocationId'], log_fn=lambda _: None,
                    request_log=lambda key,url,payload,headers: self.journal_post(receipt_id,key,url,payload,headers))
                receipt['inventory_result'] = result
                receipt['inventory_status'] = 'needs_attention' if result['errors'] else 'received'
                receipt['error'] = '\n'.join(result['errors'])
            except Exception:
                receipt['inventory_status'] = 'needs_attention'
                receipt['error'] = 'Square could not confirm the operation. Retry this receipt; its saved requests will be reused.'
            if receipt['inventory_status'] == 'needs_attention':
                with self.store.db() as db:
                    mutated = db.execute("SELECT 1 FROM operations WHERE receipt_id=? AND (key LIKE 'catalog:%' OR key LIKE 'stock:%') LIMIT 1", (receipt_id,)).fetchone()
                    if not mutated:
                        # Preflight failures have not written Square stock/catalog. Allow correction.
                        db.execute('DELETE FROM operations WHERE receipt_id=?', (receipt_id,))
                        receipt['inventory_status'] = 'not_received'
                        receipt.pop('square_connection', None)
            self.store.save_receipt(receipt, (receipt['inventory_status'], receipt['error'] or 'Incoming stock confirmed in Square.'))
            return receipt

    def labels(self, receipt_id, layout=None, dpi=None):
        with self.lock:
            receipt = self.active_receipt(receipt_id)
            cards = self.approved_cards(receipt)
            if receipt['mode'] == 'bulk':
                raise StationError('Move this receipt to Price & label before printing retail labels.')
            config = self.preferences()
            layout, dpi = layout or config['labelLayout'], int(dpi or config['printerDpi'])
            prefix = receipt_id + '-' + uuid.uuid4().hex[:6]
            root = Path(self.core.OUTPUT_DIR)
            root.mkdir(parents=True, exist_ok=True)
            self.core.generate_zebra_pdf_labels(cards,str(root/(prefix+'.pdf')),layout=layout,dpi=dpi)
            self.core.generate_zebra_zpl(cards,str(root/(prefix+'.zpl')),layout=layout,dpi=dpi)
            receipt['files'].update(pdf=prefix+'.pdf', zpl=prefix+'.zpl')
            self.store.save_receipt(receipt, ('labels', f'Labels generated for {sum(c["qty"] for c in cards)} copies. No inventory changed.'))
            return receipt

    def prepare_online(self, receipt_id):
        with self.lock:
            receipt = self.active_receipt(receipt_id)
            cards = self.approved_cards(receipt)
            if receipt['mode'] != 'online':
                raise StationError('Open Online preparation to create a listing batch.')
            if not receipt['storage_location'] and any(not c['storage_location'] for c in cards):
                raise StationError('Assign a bin or display-case location before preparing this batch.')
            root = Path(self.core.OUTPUT_DIR)
            root.mkdir(parents=True, exist_ok=True)
            prefix = receipt_id + '-listing-' + uuid.uuid4().hex[:6]
            with (root/(prefix+'.csv')).open('w',encoding='utf-8-sig',newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['Square SKU','Card Name','Set','Number','Condition','Finish','Language','Qty','Price','Storage','Channel'])
                for c in cards:
                    values = [self.core.barcode_value(c),c['card_name'],c['card_set'],c['card_num'],c['condition'],c['printing'],
                              c['language'],c['qty'],f'{c["final_price"]:.2f}',c['storage_location'] or receipt['storage_location'],receipt['channel']]
                    # Spreadsheet exports must not interpret card names as formulas.
                    writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in values])
            ticket = prefix+'.pdf'
            self.ticket(root/ticket, receipt, cards)
            receipt['files'].update(listing=prefix+'.csv', ticket=ticket)
            receipt['prepared'] = True
            self.store.save_receipt(receipt, ('listing_prepared', f'{receipt["channel"]} review sheet and storage ticket generated. Not published.'))
            return receipt

    def ticket(self, path, receipt, cards):
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import simpleSplit
        c = canvas.Canvas(str(path),pagesize=(144,72))
        lines = [receipt['channel'].upper()+' • '+receipt['storage_strategy'].upper(), receipt['id'],
                 receipt['storage_location'] or 'See row storage locations',
                 f'{len(cards)} rows / {sum(card["qty"] for card in cards)} copies', 'PREPARATION TICKET — NOT PUBLISHED']
        y=61
        for i,line in enumerate(lines):
            font,size=('Helvetica-Bold',8) if i==0 else ('Helvetica',6.5)
            c.setFont(font,size)
            for part in simpleSplit(line,font,size,132)[:2]:
                c.drawString(6,y,part);y-=9
        c.save()

    def create_task(self, data):
        required = ('order_reference','card_name','storage_location')
        if any(not str(data.get(k,'')).strip() for k in required):
            raise StationError('Provide an order reference, card and storage location.')
        quantity = str(data.get('qty',1))
        if not quantity.isdecimal() or not 1 <= int(quantity) <= 100_000:
            raise StationError('Enter a valid whole-number quantity.')
        task = {k:str(data.get(k,'')).strip()[:500] for k in ('order_reference','card_name','storage_location','channel','notes')}
        task.update(id=uuid.uuid4().hex, qty=int(quantity), status='pending', created=now())
        with self.store.db() as db:
            db.execute('INSERT INTO tasks VALUES (?,?,?)',(task['id'],now(),json.dumps(task)))
        return task

    def tasks(self):
        with self.store.db() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT body FROM tasks ORDER BY updated DESC LIMIT 150')]

    def complete_task(self, task_id):
        with self.store.db() as db:
            row = db.execute('SELECT body FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not row:
                raise StationError('Pull task not found.')
            task=json.loads(row[0]);task.update(status='pulled', completed=now())
            db.execute('UPDATE tasks SET updated=?,body=? WHERE id=?',(now(),json.dumps(task),task_id))
        return task

    def start_watcher(self):
        if self.watcher:
            return
        self.watcher=threading.Thread(target=self.watch_loop, daemon=True)
        self.watcher.start()

    def watch_loop(self):
        while not self.stop_event.wait(2):
            try:
                self.poll_exports()
            except Exception as exc:
                self.watch_status = dict(self.pickup_status(), state='error', message=str(exc))

    @staticmethod
    def export_files(folder):
        return sorted((p for p in Path(folder).iterdir()
                       if p.suffix.lower() == '.csv' and p.is_file() and not p.is_symlink()),
                      key=lambda p: (p.stat().st_mtime_ns, p.name))

    @staticmethod
    def file_signature(path):
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns]

    def export_baseline(self, folder):
        try:
            return {'folder': str(Path(folder).resolve()),
                    'files': {p.name: {'signature': self.file_signature(p)} for p in self.export_files(folder)}}
        except OSError:
            raise StationError('The export folder could not be read. Check its location and permissions.') from None

    def poll_exports(self):
        with self.lock:
            config = self.preferences()
            folder = config.get('watchFolder', '')
            status = self.pickup_status()
            if not folder:
                self.watch_status = status
                self.watch_observed.clear()
                return
            root = str(Path(folder).resolve())
            checkpoint = self.store.get('export_checkpoint', {})
            if checkpoint.get('folder') != root:
                # Also covers upgrading an already configured station once.
                checkpoint = self.export_baseline(folder)
                self.store.put('export_checkpoint', checkpoint)
                self.watch_observed.clear()
            files = self.export_files(folder)
            records = checkpoint['files']
            present = {p.name for p in files}
            updated = False
            for name in list(records):
                if name not in present:
                    del records[name]
                    updated = True
            self.watch_observed = {k: v for k, v in self.watch_observed.items() if k[0] == root and k[1] in present}
            read_errors = []
            for path in files:
                if self.stop_event.is_set():
                    return
                try:
                    signature = self.file_signature(path)
                    if records.get(path.name, {}).get('signature') == signature:
                        continue
                    key = (root, path.name)
                    if self.watch_observed.get(key) != signature:
                        self.watch_observed[key] = signature
                        continue
                    if signature[0] > 20_000_000:
                        raise StationError('File exceeds the 20 MB limit.')
                    content = path.read_text(encoding='utf-8-sig')
                    if self.file_signature(path) != signature:
                        continue
                    self.import_csv(content, path.name, config['watchMode'], intake_source='folder')
                    records[path.name] = {'signature': signature}
                    updated = True
                except OSError:
                    # A browser or scanner can still hold the file. Retry after it settles.
                    read_errors.append(f'{path.name}: Could not read this export. Check file access; pickup will retry.')
                    continue
                except (ValueError, UnicodeError, csv.Error, InvalidOperation) as exc:
                    records[path.name] = {'signature': signature, 'error': f'{path.name}: {exc}'}
                    updated = True
            if updated:
                self.store.put('export_checkpoint', checkpoint)
            errors = read_errors + [r['error'] for r in records.values() if r.get('error')]
            self.watch_status = dict(status, state='error' if errors else 'watching',
                                     message=errors[0] if errors else 'Ready for the next export from Roca.')
