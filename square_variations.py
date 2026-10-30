"""Receive matching-price copies into a variation; preserve other sticker prices.

All catalog lookups finish before any writes. Only newly created variations get
new prices. Existing variations keep their price, version and other properties.
The station supplies a durable request journal for retries after lost responses.
"""
import copy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import uuid


def price_cents(card):
    value = Decimal(str(card.get('final_price', card.get('finalPrice', 0))))
    if not value.is_finite() or not 0 <= value <= 1_000_000:
        raise ValueError('Enter a valid nonnegative selling price.')
    return int((value * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def resolution_key(source_sku, cents):
    return f'{source_sku}:{cents}'


def variant_sku(base_sku, cents):
    raw = json.dumps(['roca-price-variation-v1', base_sku, 'USD', cents], separators=(',', ':'))
    return f'{int(hashlib.sha256(raw.encode()).hexdigest(), 16) % 10**12:012d}'


def card_identity(core, card):
    if hasattr(core, 'generate_card_sku'):
        return core.generate_card_sku(card)
    return core.generate_sku(card.get('card_name', card.get('cardName', '')),
        card.get('card_num', card.get('cardNum', '')), card.get('condition', 'Near Mint'),
        card.get('printing', 'Normal'), 0, card.get('language', 'English'), card.get('card_set', ''))


def family(variation, source_sku, identity):
    try:
        data = json.loads(variation.get('item_variation_data', {}).get('user_data') or '{}')
    except (ValueError, TypeError):
        data = {}
    if isinstance(data, dict) and data.get('roca_price_variation') == 1:
        if data.get('identity') != identity:
            raise ValueError(f'SKU {source_sku} belongs to different card details. Check its SKU override.')
        base = str(data.get('base_sku', ''))
        if len(base) != 12 or not base.isascii() or not base.isdigit():
            raise ValueError(f'SKU {source_sku} has invalid Roca variation metadata.')
        return base
    return source_sku


def effective_price(variation, location):
    data = variation.get('item_variation_data', {})
    pricing = data.get('pricing_type', 'FIXED_PRICING')
    money = data.get('price_money') or {}
    for override in data.get('location_overrides') or []:
        if override.get('location_id') == location:
            pricing = override.get('pricing_type') or pricing
            money = override.get('price_money') or money
    if money and money.get('currency') != 'USD':
        raise ValueError('The existing Square variation uses a different currency. Currency conversion is not automatic.')
    if pricing != 'FIXED_PRICING' or not isinstance(money.get('amount'), int) or isinstance(money.get('amount'), bool):
        return None
    return money['amount']


def upload(core, cards, token, environment, location_id, log_fn, request_log):
    result = {'uploaded_count': 0, 'inventory_count': 0, 'errors': [], 'sku_resolutions': {}}
    base_url = 'https://connect.squareupsandbox.com' if environment == 'sandbox' else 'https://connect.squareup.com'
    headers = {'Authorization': 'Bearer ' + token.strip(), 'Content-Type': 'application/json', 'Square-Version': '2024-09-19'}

    def post(key, path, payload):
        response = (request_log(key, base_url + path, payload, headers) if request_log else
                    core.requests.post(base_url + path, headers=headers, json=payload, timeout=30))
        response.raise_for_status()
        data = response.json()
        if data.get('errors'):
            raise ValueError(json.dumps(data['errors']))
        return data

    found_skus = {}
    def lookup(sku):
        if sku not in found_skus:
            found = post('search:' + sku, '/v2/catalog/search', {'object_types': ['ITEM_VARIATION'],
                'query': {'exact_query': {'attribute_name': 'sku', 'attribute_value': sku}}})
            matches = [v for v in found.get('objects', []) if not v.get('is_deleted')]
            if len(matches) > 1 or found.get('cursor'):
                raise ValueError(f'Multiple Square variations have SKU {sku}. Resolve those duplicate SKUs first.')
            variation = matches[0] if matches else None
            if variation and (not variation.get('id') or variation.get('item_variation_data', {}).get('sku') != sku):
                raise ValueError(f'Square returned an unexpected variation for SKU {sku}. No stock was added.')
            found_skus[sku] = variation
        return found_skus[sku]

    try:
        locations = core.square_locations(token, environment)
        location_id = core.resolve_square_location(locations, location_id, environment)
        groups, identities = {}, {}
        for card in cards:
            core.label_fields(card)
            source = core.barcode_value(card)
            identity = card_identity(core, card)
            if source in identities and identities[source] != identity:
                raise ValueError(f'SKU {source} is assigned to different card details. Resolve it before uploading.')
            identities[source] = identity
            cents = price_cents(card)
            key = resolution_key(source, cents)
            if key in groups:
                groups[key]['qty'] += int(card.get('qty', 1))
            else:
                groups[key] = {'card': card, 'source': source, 'identity': identity, 'cents': cents, 'qty': int(card.get('qty', 1))}

        # Preserve one parent item per card; a different price gets a child variation.
        parents, targets = {}, {}
        for key, group in groups.items():
            source, identity, cents = group['source'], group['identity'], group['cents']
            original = lookup(source)
            root = family(original or {}, source, identity)
            parent_id = (original or {}).get('item_variation_data', {}).get('item_id')
            if root not in parents:
                parents[root] = {'id': parent_id or '#item_' + root, 'new': not original,
                                 'root_price': cents, 'card': group['card'], 'variations': []}
            parent = parents[root]
            if original and parent_id and parent['id'] != parent_id:
                raise ValueError(f'The variations for SKU {root} do not belong to one Square item.')
            if original and effective_price(original, location_id) == cents:
                sku, existing = source, original
            else:
                sku = source if parent['new'] and parent['root_price'] == cents else variant_sku(root, cents)
                existing = lookup(sku) if sku != source else original
                if existing:
                    if parent['new']:
                        raise ValueError(f'SKU {sku} already exists but its original item anchor {root} is missing. Restore the original variation in Square.')
                    if existing.get('item_variation_data', {}).get('item_id') != parent['id'] or family(existing, sku, identity) != root:
                        raise ValueError(f'SKU {sku} is already assigned to another Square item. Resolve this collision first.')
                    if effective_price(existing, location_id) != cents:
                        raise ValueError(f'Price variation SKU {sku} was repriced in Square. Review that variation before receiving more copies.')
                elif original and not parent_id:
                    raise ValueError(f'Square did not return the parent item ID for SKU {source}. No copies were added.')
            owner = (parent['id'], identity, cents)
            if sku in targets:
                target = targets[sku]
                if target['owner'] != owner:
                    raise ValueError(f'Generated SKU {sku} conflicts with another item. No copies were added.')
                target['qty'] += group['qty']
            else:
                target = targets[sku] = {'owner': owner, 'qty': group['qty'], 'id': (existing or {}).get('id', '#var_' + sku)}
                if existing:
                    if not existing.get('item_variation_data', {}).get('track_inventory', False):
                        obj = copy.deepcopy(existing)
                        obj['item_variation_data']['track_inventory'] = True
                        parent['variations'].append(obj)
                else:
                    card = group['card']
                    description = (str(card.get('condition') or 'Near Mint') + ' ' + str(card.get('printing') or 'Normal'))[:210]
                    data = {'item_id': parent['id'], 'name': f'{description} · ${cents / 100:.2f}',
                            'pricing_type': 'FIXED_PRICING', 'price_money': {'amount': cents, 'currency': 'USD'},
                            'sku': sku, 'track_inventory': True,
                            'user_data': json.dumps({'roca_price_variation': 1, 'base_sku': root, 'identity': identity}, separators=(',', ':'))}
                    obj = {'type': 'ITEM_VARIATION', 'id': target['id'], 'present_at_all_locations': True, 'item_variation_data': data}
                    if original:
                        for field in ('present_at_all_locations', 'present_at_location_ids', 'absent_at_location_ids'):
                            if field in original:
                                obj[field] = copy.deepcopy(original[field])
                    parent['variations'].append(obj)
            result['sku_resolutions'][key] = {'source_sku': source, 'price_cents': cents, 'sku': sku, 'identity': identity}

        objects = []
        for root, parent in parents.items():
            variations = parent['variations']
            if parent['new']:
                # A single item and all its initial price variations are created together.
                if len(variations) > 250:
                    raise ValueError('This card has more than 250 different prices in one batch. Review its selling prices first.')
                card = parent['card']
                objects.append({'type': 'ITEM', 'id': parent['id'], 'present_at_all_locations': True,
                    'item_data': {'name': f'{card.get("card_name", card.get("cardName", "Card"))} (#{card.get("card_num", card.get("cardNum", ""))})'[:512],
                                  'description': f'Roca Scan | {card.get("card_set", "")} | {card.get("condition", "")} | {card.get("printing", "")} | {card.get("language", "")}',
                                  'variations': variations}})
            else:
                objects.extend(variations)
    except Exception as exc:
        result['errors'] = [str(exc)]
        return result

    # Keep requests bounded, counting nested variations against Square's object limit.
    chunks, chunk, cost = [], [], 0
    for obj in objects:
        weight = 1 + len(obj.get('item_data', {}).get('variations', []))
        if chunk and (cost + weight > 500 or len(chunk) >= 100):
            chunks.append(chunk); chunk, cost = [], 0
        chunk.append(obj); cost += weight
    if chunk:
        chunks.append(chunk)
    mappings = {}
    try:
        for index, chunk in enumerate(chunks):
            data = post(f'catalog:price-variations:{index}', '/v2/catalog/batch-upsert',
                        {'idempotency_key': str(uuid.uuid4()), 'batches': [{'objects': chunk}]})
            mappings.update({m['client_object_id']: m['object_id'] for m in data.get('id_mappings', [])})
        result['uploaded_count'] = len(targets)
        pending = list(targets.values())
        for start in range(0, len(pending), 100):
            changes = []
            for target in pending[start:start + 100]:
                object_id = mappings.get(target['id'], target['id'])
                if object_id.startswith('#'):
                    raise ValueError('Square did not confirm the variation ID. Stock was not submitted for this group.')
                changes.append({'type': 'ADJUSTMENT', 'adjustment': {'catalog_object_id': object_id,
                    'location_id': location_id, 'from_state': 'NONE', 'to_state': 'IN_STOCK',
                    'quantity': str(target['qty']), 'occurred_at': datetime.now(timezone.utc).isoformat()}})
            payload = {'idempotency_key': str(uuid.uuid4()), 'changes': changes}
            if request_log:
                for change in changes:
                    change['adjustment']['reference_id'] = payload['idempotency_key']
            post(f'stock:price-variations:{start}', '/v2/inventory/changes/batch-create', payload)
            result['inventory_count'] += sum(t['qty'] for t in pending[start:start + 100])
        log_fn(f'[✓] Received {result["inventory_count"]} copies across {len(targets)} price variations at {location_id}.')
    except Exception as exc:
        result['errors'] = [f'Square variation receiving needs a saved retry: {exc}']
    return result
