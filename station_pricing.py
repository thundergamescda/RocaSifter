"""Background price research and explicit, identity-bound price proposals.

The public TCGCSV feed provides TCGplayer market references, not individual sold
records or condition-specific quotes. Unconnected sources are always visible.
No code in this module writes card prices, labels or inventory.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
import json
from collections import OrderedDict
import sqlite3
import threading
import time
import uuid

import requests
from pricing_review import Identity, finish, game, number, price, text, timestamp
from station_store import now
from store_pricing import evaluate, retail_label_price, extreme

DEFAULTS = {'pricingEnabled': True, 'pricingGame': 'pokemon', 'pricingCompleteOnly': False,
            'intakeValuePercent': 90, 'retailValuePercent': 100,
            'pricingAlertPercent':15, 'pricingAlertDollars':1, 'pricingAutoCorrect':True}
SOURCES = ('tcgplayer', 'tcg_sales', 'tcg_lowest', 'pricecharting')
LABELS = {'tcgplayer': 'TCGplayer market reference', 'tcg_sales': 'TCGplayer recent sold prices',
          'tcg_lowest': 'TCGplayer lowest listing', 'pricecharting': 'PriceCharting guide'}


def identity(card, batch_game=''):
    result = dict(card)
    supplied = game(card.get('game'))
    selected = game(batch_game)
    result['game'] = supplied or selected
    if supplied and selected and supplied != selected:
        result['game'] = 'conflicting games'
    return result


def cents(value):
    return float(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def proposals(result, preferences):
    """Only market/sold/guide anchors set prices; asking-price outliers never do."""
    if result.get('observations'):
        checked=timestamp(result.get('checked_at'));expiry=timestamp(result.get('valid_until'))
        clock=datetime.now(timezone.utc)
        if not checked or not timedelta(minutes=-5)<=clock-checked<=timedelta(hours=36) or (expiry and clock>expiry):
            return dict(result,status='stale',intake_price=None,retail_price=None,auto_eligible=False,message='Research expired. Research this card again.')
        reviewed=evaluate(result['card'],result['observations'],preferences)
        labels={'tcg_sales':'TCGplayer recent sold prices','tcg_listings':'TCGplayer lowest listing','pricecharting':'PriceCharting guide'}
        source_fields={s:dict(v,label=labels[s]) for s,v in reviewed['sources'].items()}
        return dict(result,**{k:v for k,v in reviewed.items() if k not in ('checked_at','sources')},sources=source_fields)
    output = dict(result, intake_price=None, retail_price=None,auto_eligible=False,
                  flags=['Individual TCGplayer sales and PriceCharting are not connected; this is a partial market reference.'])
    anchors = [price(v) for v in result.get('anchors', [])]
    anchors = [v for v in anchors if v is not None]
    if not anchors or result.get('status') not in ('reference', 'ready'):
        return output
    checked = timestamp(result.get('checked_at'))
    valid_until = timestamp(result.get('valid_until'))
    if (not checked or not timedelta(minutes=-5) <= datetime.now(timezone.utc) - checked <= timedelta(hours=36)
            or (valid_until and datetime.now(timezone.utc) > valid_until)):
        return dict(output, status='stale', message='Research expired. Research this card again.')
    if preferences.get('pricingCompleteOnly', DEFAULTS['pricingCompleteOnly']) and result.get('status') != 'ready':
        return dict(output, status='incomplete', message='Waiting for all required sources. Partial market references are shown below.')
    if extreme(min(anchors),max(anchors),preferences):
        return dict(output, status='conflict', message='Comparable values exceed the store’s discrepancy threshold. Set a price after reviewing the evidence.')
    lowest=price(result.get('sources',{}).get('tcgplayer',{}).get('low_listing'))
    intake = min(anchors+([lowest] if lowest else [])) * Decimal(str(preferences.get('intakeValuePercent', 90))) / 100
    retail = max(anchors) * Decimal(str(preferences.get('retailValuePercent', 100))) / 100
    return dict(output, intake_price=cents(intake), retail_price=retail_label_price(retail,result.get('card',{}).get('game')))


class Research:
    def __init__(self, store, stop_event, preferences, provider=None):
        self.store, self.stop_event, self.preferences = store, stop_event, preferences
        self.worker = None
        self.wake = threading.Event()
        self.provider = provider or TCGCSV(self)
        with self.store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS price_jobs (
                    key TEXT PRIMARY KEY, card TEXT NOT NULL, state TEXT NOT NULL,
                    requested TEXT NOT NULL, result TEXT, attempt TEXT);
                CREATE INDEX IF NOT EXISTS price_jobs_state ON price_jobs(state,requested);
                CREATE TABLE IF NOT EXISTS price_http_cache (
                    path TEXT PRIMARY KEY, fetched REAL NOT NULL, body TEXT NOT NULL);
            ''')
            db.execute("UPDATE price_jobs SET state='queued',attempt=NULL WHERE state='running'")

    def enqueue(self, card, batch_game='', force=False):
        target = identity(card, batch_game)
        key = Identity.from_card(target).cache_key
        with self.store.db() as db:
            prior = db.execute('SELECT state,result FROM price_jobs WHERE key=?', (key,)).fetchone()
            if prior:
                result = json.loads(prior['result']) if prior['result'] else {}
                checked = timestamp(result.get('checked_at'))
                lifetime = timedelta(minutes=5) if result.get('status') == 'unavailable' else timedelta(hours=12)
                expiry = timestamp(result.get('valid_until'))
                fresh = checked and datetime.now(timezone.utc) - checked < lifetime and (not expiry or expiry > datetime.now(timezone.utc))
                # Force does not interrupt an active lookup or duplicate a batch's copies.
                if prior['state'] in ('queued', 'running') or (fresh and not force):
                    return key
            fields = ('game', 'card_name', 'card_set', 'card_num', 'printing', 'condition', 'language')
            db.execute('INSERT OR REPLACE INTO price_jobs VALUES (?,?,?,?,?,NULL)',
                       (key, json.dumps({k: target.get(k, '') for k in fields}), 'queued', now(), None))
        self.wake.set()
        return key

    def enqueue_receipt(self, receipt, force=False):
        if not self.preferences().get('pricingEnabled', True):
            return
        # One transaction for large exports; nested db calls are avoided.
        unique = {}
        for card in receipt['cards']:
            target = identity(card, receipt.get('pricing_game', ''))
            unique[Identity.from_card(target).cache_key] = target
        with self.store.db() as db:
            for key, target in unique.items():
                prior = db.execute('SELECT state,result FROM price_jobs WHERE key=?', (key,)).fetchone()
                if prior:
                    result = json.loads(prior['result']) if prior['result'] else {}
                    checked = timestamp(result.get('checked_at'))
                    lifetime = timedelta(minutes=5) if result.get('status') == 'unavailable' else timedelta(hours=12)
                    expiry = timestamp(result.get('valid_until'))
                    fresh = checked and datetime.now(timezone.utc) - checked < lifetime and (not expiry or expiry > datetime.now(timezone.utc))
                    if prior['state'] in ('queued', 'running') or (fresh and not force):
                        continue
                fields = ('game', 'card_name', 'card_set', 'card_num', 'printing', 'condition', 'language')
                db.execute('INSERT OR REPLACE INTO price_jobs VALUES (?,?,?,?,?,NULL)',
                           (key, json.dumps({k: target.get(k, '') for k in fields}), 'queued', now(), None))
        self.wake.set()

    def get(self, key):
        with self.store.db() as db:
            row = db.execute('SELECT state,result FROM price_jobs WHERE key=?', (key,)).fetchone()
        if not row:
            return {'key': key, 'status': 'not_requested', 'message': 'Price research has not been requested.'}
        if row['state'] in ('queued', 'running'):
            return {'key': key, 'status': row['state'], 'message': 'Waiting for research.' if row['state'] == 'queued' else 'Researching this card…'}
        return dict(proposals(json.loads(row['result']), self.preferences()), key=key)

    def receipt_status(self, receipt):
        cards, counts, jobs = {}, {}, {}
        targets = [(c['row_id'], Identity.from_card(identity(c, receipt.get('pricing_game', ''))).cache_key) for c in receipt['cards']]
        keys = list({key for _, key in targets})
        saved = {}
        with self.store.db() as db:
            for start in range(0, len(keys), 500):
                batch = keys[start:start + 500]
                saved.update({r['key']: r for r in db.execute('SELECT key,state,result FROM price_jobs WHERE key IN (' + ','.join('?' for _ in batch) + ')', batch)})
        preferences = self.preferences()
        for row_id, key in targets:
            if key not in jobs:
                row = saved.get(key)
                if not row:
                    result = {'status': 'not_requested'}
                elif row['state'] in ('queued', 'running'):
                    result = {'status': row['state']}
                else:
                    result = proposals(json.loads(row['result']), preferences)
                jobs[key] = {k: result.get(k) for k in ('status', 'intake_price', 'retail_price','auto_eligible','flags','checked_at','message')}
                jobs[key]['key'] = key
            result = jobs[key]
            cards[row_id] = result
            counts[result['status']] = counts.get(result['status'], 0) + 1
        return {'cards': cards, 'counts': counts, 'game': receipt.get('pricing_game', ''),
                'enabled': self.preferences().get('pricingEnabled', True), 'completeOnly': self.preferences().get('pricingCompleteOnly', DEFAULTS['pricingCompleteOnly'])}

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self.loop, name='roca-price-research', daemon=True)
        self.worker.start()

    def loop(self):
        while not self.stop_event.is_set():
            try:
                worked = self.run_one()
            except sqlite3.Error:
                # Shutdown can close/remove a temporary store while a request finishes.
                worked = False
            if not worked:
                self.wake.wait(1)
                self.wake.clear()

    def run_one(self):
        if self.stop_event.is_set() or not self.preferences().get('pricingEnabled', True):
            return False
        attempt = uuid.uuid4().hex
        with self.store.db() as db:
            row = db.execute("SELECT key,card FROM price_jobs WHERE state='queued' ORDER BY requested,key LIMIT 1").fetchone()
            if not row:
                return False
            db.execute("UPDATE price_jobs SET state='running',attempt=? WHERE key=?", (attempt, row['key']))
        card = json.loads(row['card'])
        result = {'identity_key': row['key'], 'checked_at': now(), 'status': 'unavailable', 'anchors': [],
                  'card':card,
                  'sources': {s: {'label': LABELS[s], 'status': 'not_connected', 'message': 'Data access has not been connected.'} for s in SOURCES},
                  'message': 'No comparable price was retrieved.'}
        try:
            target = Identity.from_card(card)
            if not target.complete or target.game not in ('pokemon', 'magic'):
                result.update(status='needs_identity', message='Confirm the game, exact set, collector number, finish, condition and language.')
            elif target.is_graded:
                result.update(status='unavailable', message='Slab pricing needs verified same-grader/grade sold or guide data. Raw-card prices are never used for slabs.')
                result['sources']['tcgplayer'].update(status='not_applicable', message='Raw-card reference; excluded from slab valuation.')
            else:
                data = self.provider.lookup(card)
                result['sources']['tcgplayer'] = dict(data, label=LABELS['tcgplayer'])
                if data.get('observations'):
                    result['observations']=data['observations']
                if data.get('low_listing'):
                    result['sources']['tcg_lowest']={'label':LABELS['tcg_lowest'],'status':'reference','low':data['low_listing'],
                        'message':'Daily low listing snapshot; individual condition-specific listings are not verified.'}
                if data.get('status') == 'reference' and price(data.get('market')):
                    result.update(status='reference', anchors=[data['market']],
                                  valid_until=data.get('valid_until'),
                                  message='Limited evidence: daily TCGplayer market/low references. Individual sold prices and PriceCharting have not been checked.')
                else:
                    result.update(status=data.get('status', 'unavailable'), message=data.get('message', result['message']))
        except SourceError as exc:
            result['sources']['tcgplayer'].update(status='unavailable', message=str(exc))
            result['message'] = str(exc)
        except Exception:
            result['message'] = 'Research could not complete. Your current price is unchanged. Try research again.'
        result['checked_at'] = now()
        if self.stop_event.is_set():
            return False  # The persisted running job resumes on the next launch.
        with self.store.db() as db:
            db.execute("UPDATE price_jobs SET state='done',result=? WHERE key=? AND attempt=?", (json.dumps(result), row['key'], attempt))
        return True


class SourceError(ValueError):
    pass


class TCGCSV:
    """Anonymous daily TCGplayer market snapshots; never claims sold/condition data."""
    def __init__(self, research):
        self.research = research
        self.blocked_until = 0
        self.memory = OrderedDict()
        self.next_request = 0

    def fetch(self, path):
        if self.research.stop_event.is_set():
            raise SourceError('Research stopped. The saved job will resume on the next launch.')
        cached = self.memory.get(path)
        if cached and self.cache_fresh(path, *cached):
            self.memory.move_to_end(path)
            return cached[1]
        with self.research.store.db() as db:
            row = db.execute('SELECT fetched,body FROM price_http_cache WHERE path=?', (path,)).fetchone()
        if row and self.cache_fresh(path, row['fetched'], json.loads(row['body'])):
            data = json.loads(row['body'])
            self.remember(path, row['fetched'], data)
            return data
        if time.time() < self.blocked_until:
            raise SourceError('The pricing feed is unavailable or rate-limited. Automatic requests are paused for 5 minutes.')
        if self.research.stop_event.wait(max(0, self.next_request - time.monotonic())):
            raise SourceError('Research stopped. The saved job will resume on the next launch.')
        self.next_request = time.monotonic() + .35
        try:
            response = requests.get('https://tcgcsv.com/tcgplayer/' + path, timeout=(5, 15),
                                    headers={'User-Agent': 'RocaStation/2.4 price research', 'Accept': 'application/json'},
                                    allow_redirects=False)
            if response.status_code != 200:
                raise SourceError('The public TCGplayer market feed could not be reached. No price was changed.')
            if len(response.content) > 20_000_000:
                raise SourceError('The price feed response is too large.')
            data = response.json()
            if data.get('success') is not True or not isinstance(data.get('results'), list):
                raise SourceError('The price feed returned an unsupported response. No price was changed.')
            modified = response.headers.get('Last-Modified')
            if modified and path.endswith('/prices'):
                updated = parsedate_to_datetime(modified)
                if not updated.tzinfo or not timedelta(minutes=-5) <= datetime.now(timezone.utc) - updated <= timedelta(hours=36):
                    raise SourceError('The pricing feed snapshot is stale or undated. No price was changed.')
            data = {'results': data['results'], 'downloaded_at': now(), 'source_updated_at': modified or None}
        except (requests.RequestException, ValueError, TypeError, AttributeError, OverflowError) as exc:
            self.blocked_until = time.time() + 300
            if isinstance(exc, SourceError):
                raise
            raise SourceError('The public TCGplayer market feed is unavailable. Current prices were kept.') from None
        if self.research.stop_event.is_set():
            raise SourceError('Research stopped. The saved job will resume on the next launch.')
        with self.research.store.db() as db:
            db.execute('INSERT OR REPLACE INTO price_http_cache VALUES (?,?,?)', (path, time.time(), json.dumps(data)))
        self.remember(path, time.time(), data)
        return data

    def remember(self, path, fetched, data):
        self.memory[path] = (fetched, data)
        self.memory.move_to_end(path)
        while len(self.memory) > 24:
            self.memory.popitem(last=False)

    @staticmethod
    def cache_fresh(path, fetched, data):
        if not 0 <= time.time() - fetched < 12 * 3600:
            return False
        if path.endswith('/prices') and data.get('source_updated_at'):
            try:
                updated = parsedate_to_datetime(data['source_updated_at'])
                return bool(updated.tzinfo and timedelta(minutes=-5) <= datetime.now(timezone.utc) - updated <= timedelta(hours=36))
            except (ValueError, TypeError, OverflowError):
                return False
        return True

    def lookup(self, card):
        target = Identity.from_card(card)
        if target.language != 'english':
            return {'status': 'needs_identity', 'message': 'This public feed has no verified matching-language price.'}
        if target.condition != 'near mint':
            return {'status': 'needs_identity', 'message': 'This feed is not condition-specific. A played/damaged card needs condition-specific evidence.'}
        category = '3' if target.game == 'pokemon' else '1'
        groups = self.fetch(category + '/groups')['results']
        matches = [g for g in groups if target.card_set in (text(g.get('name')), text(g.get('abbreviation')))]
        if len(matches) != 1:
            return {'status': 'needs_identity', 'message': 'No single exact set match. Enter the full marketplace set name; no similar set is substituted.'}
        group_id = str(matches[0].get('groupId', ''))
        if not group_id.isdecimal():
            raise SourceError('The price feed returned an invalid set ID.')
        base = category + '/' + group_id
        products = self.fetch(base + '/products')['results']
        matches = []
        for product in products:
            numbers = [str(e.get('value', '')) for e in product.get('extendedData', []) if text(e.get('name')) == 'number']
            if len(numbers) != 1 or number(numbers[0]) != target.number:
                continue
            names = {text(product.get('name')), text(product.get('cleanName'))}
            # Only remove the independently verified collector-number suffix.
            names.update(n.removesuffix(' - ' + text(numbers[0])) for n in list(names))
            if target.name in names:
                matches.append(product)
        if len(matches) != 1:
            return {'status': 'needs_identity', 'message': 'The exact name and collector number did not identify one product. No similar card was priced.'}
        product = matches[0]
        snapshot = self.fetch(base + '/prices')
        matching = [p for p in snapshot['results'] if str(p.get('productId')) == str(product.get('productId')) and finish(p.get('subTypeName')) == target.finish]
        if len(matching) != 1 or not price(matching[0].get('marketPrice')):
            return {'status': 'needs_identity', 'message': 'No exact finish/treatment market price. Premium variants are never replaced with ordinary foil.'}
        row = matching[0]
        modified = snapshot['source_updated_at']
        if not modified:
            return {'status': 'unavailable', 'message': 'The feed did not supply a verifiable price snapshot date. No suggestion was made.'}
        updated = parsedate_to_datetime(modified)
        if not updated.tzinfo or not timedelta(minutes=-5) <= datetime.now(timezone.utc) - updated <= timedelta(hours=36):
            return {'status': 'unavailable', 'message': 'This market snapshot is stale. Research again after the feed refreshes.'}
        return {'status': 'reference', 'market': float(price(row['marketPrice'])),
                'low_listing': float(price(row['lowPrice'])) if price(row.get('lowPrice')) else None,
                'product_id': product['productId'], 'product_name': product.get('name'),
                'set_name': groups[[str(g.get('groupId')) for g in groups].index(group_id)].get('name'),
                'finish': row['subTypeName'], 'downloaded_at': snapshot['downloaded_at'],
                'source_updated_at': snapshot['source_updated_at'], 'currency': 'USD',
                'valid_until': (updated + timedelta(hours=36)).isoformat(),
                'message': 'Daily TCGplayer market reference via TCGCSV. Not condition-specific; not individual completed sales. Low listing is context only.'}
