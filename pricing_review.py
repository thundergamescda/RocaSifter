"""Conservative, offline pricing review rules. Not connected to production workflows yet.

Sources must supply their own identity, dates, price basis and provenance. A search
query or requested card is never evidence that a result describes that variant.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from statistics import median
import unicodedata
from urllib.parse import urlsplit


REQUIRED_SOURCES = ('tcg_market', 'tcg_sales', 'tcg_listings', 'ebay_sold', 'ebay_listings')
SALE_SOURCES = ('tcg_sales', 'ebay_sold')
SOURCE_LABELS = {
    'tcg_market': 'TCGplayer market', 'tcg_sales': 'TCGplayer completed sales',
    'tcg_listings': 'TCGplayer current listings', 'ebay_sold': 'eBay completed sales',
    'ebay_listings': 'eBay current listings',
}


def text(value):
    # Keep gender, star, edition, stamp and treatment information in the identity.
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return ' '.join(value.replace('\u2019', "'").split())


def number(value):
    # 006/165 and 6/165 are equivalent. 006/165 and 006/198 are not.
    value = text(value).lstrip('#').strip()
    return '/'.join(re.sub(r'(?<![a-z0-9])0+(?=\d)', '', part) for part in value.split('/'))


def finish(value):
    value = text(value)
    return {
        'non-foil': 'normal', 'nonfoil': 'normal', 'non foil': 'normal', 'non-holo': 'normal',
        'holo': 'holofoil', 'holo foil': 'holofoil',
        'reverse holo': 'reverse holofoil', 'reverse foil': 'reverse holofoil',
    }.get(value, value)


def condition(value):
    value = text(value)
    return {'nm': 'near mint', 'lp': 'lightly played', 'mp': 'moderately played',
            'hp': 'heavily played', 'dmg': 'damaged'}.get(value, value)


def language(value):
    value = text(value)
    return {'en': 'english', 'eng': 'english', 'ja': 'japanese', 'jp': 'japanese',
            'jpn': 'japanese', 'de': 'german', 'es': 'spanish', 'fr': 'french'}.get(value, value)


def game(value):
    value = text(value)
    return {'pokémon': 'pokemon', 'pokemon tcg': 'pokemon', 'pokémon tcg': 'pokemon'}.get(value, value)


@dataclass(frozen=True)
class Identity:
    game: str
    name: str
    card_set: str
    number: str
    finish: str
    condition: str
    language: str

    @classmethod
    def from_card(cls, card):
        return cls(game(card.get('game')), text(card.get('card_name')), text(card.get('card_set')),
                   number(card.get('card_num')), finish(card.get('printing')),
                   condition(card.get('condition')), language(card.get('language')))

    @property
    def complete(self):
        return all((self.game, self.name, self.card_set, self.number, self.finish, self.condition, self.language))

    @property
    def cache_key(self):
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def price(value):
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0 or result > 1_000_000:
            return None
        return result.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        # An undated / local-time source is not verifiable current evidence.
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def rejected_reason(target, evidence, current_time):
    if not target.complete or Identity.from_card(evidence.get('identity', {})) != target:
        return 'Exact game, card, set, number, finish, condition and language were not verified.'
    if evidence.get('currency') != 'USD' or evidence.get('price_basis') != 'item':
        return 'Currency or item-price basis is not comparable.'
    if price(evidence.get('amount')) is None:
        return 'No valid positive price.'
    if evidence.get('attribution') != 'exact':
        return 'This record is shared across printings or its attribution is unknown.'
    if not evidence.get('record_id') or not evidence.get('url'):
        return 'No source record or source link.'
    source = evidence.get('source')
    try:
        link = urlsplit(str(evidence['url']))
        domains = ('tcgplayer.com',) if source and source.startswith('tcg_') else (
            'ebay.com', 'ebay.co.uk', 'ebay.ca', 'ebay.com.au', 'ebay.de', 'ebay.es', 'ebay.fr')
        if link.scheme != 'https' or link.username or not any(
                link.hostname == d or (link.hostname or '').endswith('.' + d) for d in domains):
            return 'Source link does not belong to the stated marketplace.'
    except ValueError:
        return 'Invalid source link.'
    if evidence.get('is_lot') is not False or isinstance(evidence.get('quantity'), bool) or evidence.get('quantity') != 1:
        return 'Not verified as a single-card comparison.'
    if any(evidence.get(k) is not False for k in ('graded', 'signed', 'altered', 'proxy')):
        return 'The comparison is graded, altered, signed, a proxy, or those attributes are unverified.'
    observed = timestamp(evidence.get('observed_at'))
    if observed is None or not current_time - timedelta(hours=36) <= observed <= current_time + timedelta(minutes=5):
        return 'Source snapshot is missing, stale or in the future.'
    if source in SALE_SOURCES:
        sold = timestamp(evidence.get('sold_at'))
        if sold is None or not current_time - timedelta(days=30) <= sold <= current_time:
            return 'Sale date is missing, older than 30 days, or in the future.'
        if evidence.get('sale_price_verified') is not True:
            return 'The actual paid amount was not verified (including accepted offers).'
    elif evidence.get('active') is not True:
        return 'Current availability was not verified.'
    return None


def review(card, observations, current_time=None):
    """Return a proposal and audit trail; never change a card, label, or stock count.

    Five checked sources are required for a consensus proposal. Market/listing
    snapshots must be <=36 hours old, and completed sales <=30 days old. Listing
    prices provide context; they do not get averaged into completed sale prices.
    """
    current_time = current_time or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError('Use a timezone-aware review time.')
    target = Identity.from_card(card)
    result = {'identity_key': target.cache_key, 'checked_at': current_time.isoformat(),
              'status': 'needs_review', 'suggested_price': None, 'sources': {}, 'reasons': []}
    if card.get('entry_source') == 'manual':
        return dict(result, status='manual_price', reasons=['Keep the operator’s custom selling price.'])
    if not target.game:
        return dict(result, status='needs_identity', reasons=['Identify the game so Pokémon cannot be silently skipped.'])
    if target.game != 'pokemon':
        return dict(result, status='not_pokemon', reasons=['This policy applies to Pokémon.'])
    if not target.complete:
        return dict(result, status='needs_identity', reasons=['Complete the set, number, finish, condition and language before comparing.'])
    if target.condition not in ('near mint', 'lightly played', 'moderately played', 'heavily played', 'damaged'):
        return dict(result, status='needs_identity', reasons=['Graded and custom conditions need a separate verified pricing policy.'])
    for source in REQUIRED_SOURCES:
        accepted = {}
        rejected = []
        candidates = [e for e in observations if e.get('source') == source]
        for evidence in candidates:
            reason = rejected_reason(target, evidence, current_time)
            key = str(evidence.get('record_id', ''))
            if reason:
                rejected.append({'record_id': key, 'reason': reason})
            elif key in accepted:
                if price(accepted[key]['amount']) != price(evidence['amount']):
                    rejected.append({'record_id': key, 'reason': 'Duplicate source ID has conflicting prices.'})
            else:
                accepted[key] = evidence
        values = [price(e['amount']) for e in accepted.values()]
        minimum = 3 if source in SALE_SOURCES else 1
        summary = {'status': 'checked' if len(values) >= minimum and not rejected else 'needs_review',
                   'count': len(values), 'required_count': minimum,
                   'median': float(median(values)) if values else None,
                   'records': list(accepted.values()), 'rejected': rejected}
        if not candidates:
            summary['status'] = 'unavailable'
        result['sources'][source] = summary
        if summary['status'] != 'checked':
            result['reasons'].append(f'{SOURCE_LABELS[source]}: {summary["status"].replace("_", " ")}.')
    if result['reasons']:
        return result
    market = Decimal(str(result['sources']['tcg_market']['median']))
    sales = [Decimal(str(result['sources'][s]['median'])) for s in SALE_SOURCES]
    if any(abs(value - market) / market > Decimal('.20') for value in sales):
        result['reasons'].append('TCGplayer market and recent sold medians disagree by more than 20%.')
        return result
    for source in SALE_SOURCES:
        values = [price(e['amount']) for e in result['sources'][source]['records']]
        mid = median(values)
        if max(values) / min(values) > 2 or any(abs(v - mid) / mid > Decimal('.50') for v in values):
            result['reasons'].append(f'{SOURCE_LABELS[source]} contains a wide spread or outliers.')
    for source in ('tcg_listings', 'ebay_listings'):
        asking = Decimal(str(result['sources'][source]['median']))
        if abs(asking - market) / market > Decimal('.50'):
            result['reasons'].append(f'{SOURCE_LABELS[source]} differs from market by more than 50%.')
    if result['reasons']:
        return result
    result.update(status='proposal_ready', suggested_price=float(market))
    result['reasons'] = ['Exact current TCGplayer market is corroborated by both recent completed-sale sources. Operator approval remains required.']
    return result


def review_batch(cards, observations_by_identity, current_time=None):
    # Every row gets an explicit result; an unrecognized game is not skipped as a non-Pokémon card.
    return [dict(row_id=c.get('row_id'), **review(c, observations_by_identity.get(Identity.from_card(c).cache_key, []), current_time))
            for c in cards]
