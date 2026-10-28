"""Offline pricing evaluation and the app's direct source-search link helper.

Sources must supply their own identity, dates, price basis and provenance. A search
query or requested card is never evidence that a result describes that variant.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from statistics import median
import unicodedata
from urllib.parse import urlsplit, urlencode
from scanner_adapters import GAME_LABELS, normalize_game


REQUIRED_SOURCES = ('tcg_market', 'tcg_sales', 'tcg_listings', 'ebay_sold', 'ebay_listings', 'pricecharting')
SALE_SOURCES = ('tcg_sales', 'ebay_sold')
GRADED_SOURCES = ('ebay_sold', 'ebay_listings', 'pricecharting')
SOURCE_LABELS = {
    'tcg_market': 'TCGplayer market', 'tcg_sales': 'TCGplayer completed sales',
    'tcg_listings': 'TCGplayer current listings', 'ebay_sold': 'eBay completed sales',
    'ebay_listings': 'eBay current listings', 'pricecharting': 'PriceCharting guide',
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
    if value == 'conflicting games':
        return value
    return normalize_game(value)[0]


def grading(card):
    raw = text(card.get('condition'))
    match = re.fullmatch(r'(psa|bgs|cgc|sgc|tag|ace)\s*(?:(black label|pristine|perfect|gem mint)\s*)?(10|[1-9](?:\.5)?)(?:\s*(black label|pristine|perfect|gem mint))?', raw)
    if match:
        parsed = (match[1], match[3], match[2] or match[4] or '')
        if parsed[0] == 'psa' and parsed[2] == 'gem mint':
            parsed = (parsed[0], parsed[1], '')
    else:
        parsed = ('', '', '')
    explicit = (text(card.get('grader')), text(card.get('grade')), text(card.get('grade_qualifier')))
    if explicit[0] == 'psa' and explicit[2] == 'gem mint':
        explicit = (explicit[0], explicit[1], '')
    if any(explicit):
        if not explicit[0] or not re.fullmatch(r'10|[1-9](?:\.5)?', explicit[1]):
            return ('?', '?', '?')
        if any(parsed) and explicit != parsed:
            return ('?', '?', '?')
        if not any(parsed) and raw not in ('graded', 'slab', ''):
            return ('?', '?', '?')
        parsed = explicit
    if any(parsed):
        grader, grade, qualifier = parsed
        allowed = {'psa': ('',), 'bgs': ('', 'black label'),
                   'cgc': ('', 'gem mint', 'pristine', 'perfect'), 'sgc': ('',), 'tag': ('',), 'ace': ('',)}
        if (grader not in allowed or qualifier not in allowed[grader]
                or (qualifier in ('black label','pristine','perfect') and grade != '10')
                or (grader == 'cgc' and grade == '10' and not qualifier)
                or (grader == 'psa' and grade == '9.5')):
            return ('?', '?', '?')
    return parsed


@dataclass(frozen=True)
class Identity:
    game: str
    name: str
    card_set: str
    number: str
    finish: str
    condition: str
    language: str
    grader: str = ''
    grade: str = ''
    grade_qualifier: str = ''
    # Certificate is a physical-item cache boundary. Different certificates can
    # still be same-grader/grade/tier sale comparables, so equality excludes it.
    cert_number: str = field(default='', compare=False)
    serial_number: str = ''
    year: str = ''
    manufacturer: str = ''
    team: str = ''
    player: str = ''
    custom_game_label: str = ''
    ambiguous_grading: str = ''

    @classmethod
    def from_card(cls, card):
        grader, grade, qualifier = grading(card)
        selected_game = game(card.get('game'))
        _, label = normalize_game(card.get('game_label') or card.get('game'))
        ambiguous = json.dumps([text(card.get(key)) for key in ('condition', 'grader', 'grade', 'grade_qualifier')]) if grader == '?' else ''
        return cls(selected_game, text(card.get('card_name')), text(card.get('card_set')),
                   number(card.get('card_num')), finish(card.get('printing')),
                   'graded' if grader else condition(card.get('condition')), language(card.get('language')),
                   grader, grade, qualifier, str(card.get('cert_number') or '').strip(),
                   text(card.get('serial_number')), text(card.get('year')), text(card.get('manufacturer')),
                   text(card.get('team')), text(card.get('player')),
                   text(label) if selected_game.startswith('other:') else '', ambiguous)

    @property
    def complete(self):
        return (all((self.game, self.name, self.card_set, self.number, self.finish, self.condition, self.language))
                and self.game != 'conflicting games' and self.grader != '?'
                and (not self.cert_number or self.is_graded))

    @property
    def is_graded(self):
        return bool(self.grader and self.grade and self.grader != '?')

    @property
    def has_specialized_variant(self):
        return self.game == 'sports' or any((self.serial_number, self.year, self.manufacturer, self.team, self.player))

    @property
    def cache_key(self):
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def lookup_links(card):
    """Direct search links are navigation aids, never evidence of a checked price."""
    target = Identity.from_card(card)
    product_line = target.game if target.game in ('magic', 'pokemon') else 'all'
    _, supplied_label = normalize_game(card.get('game_label') or card.get('game'))
    prefix = {'magic': 'Magic The Gathering', 'pokemon': 'Pokemon'}.get(target.game,
              GAME_LABELS.get(target.game) or supplied_label)
    details = [str(card.get(k) or '').strip() for k in
               ('card_name', 'card_set', 'card_num', 'printing', 'language', 'year', 'manufacturer', 'team', 'player', 'serial_number')]
    base = ' '.join(part for part in [prefix, *details] if part)
    slab = target.is_graded or target.grader == '?' or bool(re.match(r'^(psa|bgs|cgc|sgc|tag|ace)\b',text(card.get('condition'))))
    descriptor = ' '.join((target.grader, target.grade_qualifier, target.grade)) if target.is_graded else str(card.get('condition') or '')
    ebay_query = base + ' ' + descriptor + ' -proxy -replica -lot -digital'
    if not slab:
        ebay_query += ' -PSA -BGS -CGC -graded'
    return {
        'ebay_sold':'https://www.ebay.com/sch/i.html?' + urlencode({'_nkw':ebay_query,'LH_Sold':'1','LH_Complete':'1'}),
        'ebay_listings':'https://www.ebay.com/sch/i.html?' + urlencode({'_nkw':ebay_query,'LH_BIN':'1'}),
        'pricecharting':'https://www.pricecharting.com/search-products?' + urlencode({'type':'prices','q':' '.join((base, descriptor)).strip(),'go':'Go'}),
        'tcgplayer':f'https://www.tcgplayer.com/search/{product_line}/product?' + urlencode(
            dict(q=base, view='grid', **({'productLineName':product_line} if product_line != 'all' else {}))),
        'tcgplayer_role':'raw_card_reference' if slab or target.has_specialized_variant else
            'manual_marketplace_search' if product_line == 'all' else 'condition_specific_comparison',
    }


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
        return 'Exact game, card, set, number, finish, condition, language, grade/tier and supplied serial/sports details were not verified.'
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
        domains = (('tcgplayer.com',) if source and source.startswith('tcg_') else ('pricecharting.com',)
                   if source == 'pricecharting' else ('ebay.com','ebay.co.uk','ebay.ca','ebay.com.au','ebay.de','ebay.es','ebay.fr'))
        if link.scheme != 'https' or link.username or not any(
                link.hostname == d or (link.hostname or '').endswith('.' + d) for d in domains):
            return 'Source link does not belong to the stated marketplace.'
    except ValueError:
        return 'Invalid source link.'
    if evidence.get('is_lot') is not False or isinstance(evidence.get('quantity'), bool) or evidence.get('quantity') != 1:
        return 'Not verified as a single-card comparison.'
    if evidence.get('graded') is not target.is_graded or any(evidence.get(k) is not False for k in ('signed', 'altered', 'proxy')):
        return 'Grading state or signed/altered/proxy exclusions do not match or are unverified.'
    observed = timestamp(evidence.get('observed_at'))
    if observed is None or not current_time - timedelta(hours=36) <= observed <= current_time + timedelta(minutes=5):
        return 'Source snapshot is missing, stale or in the future.'
    if source in SALE_SOURCES:
        sold = timestamp(evidence.get('sold_at'))
        if sold is None or not current_time - timedelta(days=30) <= sold <= current_time:
            return 'Sale date is missing, older than 30 days, or in the future.'
        if evidence.get('sale_price_verified') is not True:
            return 'The actual paid amount was not verified (including accepted offers).'
    elif source == 'pricecharting':
        if evidence.get('guide_verified') is not True:
            return 'The exact PriceCharting grade/tier and guide value were not verified.'
    elif evidence.get('active') is not True:
        return 'Current availability was not verified.'
    return None


def source_summary(target, source, observations, current_time):
    accepted = {}
    rejected = []
    candidates = [e for e in observations if e.get('source') == source]
    for evidence in candidates:
        reason = rejected_reason(target, evidence, current_time)
        key = str(evidence.get('record_id', ''))
        if reason:
            rejected.append({'record_id':key, 'reason':reason})
        elif key in accepted:
            if price(accepted[key]['amount']) != price(evidence['amount']):
                rejected.append({'record_id':key, 'reason':'Duplicate source ID has conflicting prices.'})
        else:
            accepted[key] = evidence
    values = [price(e['amount']) for e in accepted.values()]
    minimum = 3 if source in SALE_SOURCES else 1
    return {'status':('checked' if len(values) >= minimum and not rejected else 'needs_review') if candidates else 'unavailable',
            'count':len(values), 'required_count':minimum, 'median':float(median(values)) if values else None,
            'records':list(accepted.values()), 'rejected':rejected}


def review(card, observations, current_time=None, *, lookup_manual=False):
    """Compare direct-source evidence; never mutate a card, price, label or stock.

    Raw cards use TCGplayer market/sales/listings, eBay sold/current, and an exact guide.
    Slabs use same-grader/grade/tier eBay sold/current listings and PriceCharting.
    Guide values must match the exact identity too. Manual lookups are opt-in.
    """
    current_time = current_time or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError('Use a timezone-aware review time.')
    target = Identity.from_card(card)
    result = {'identity_key':target.cache_key, 'checked_at':current_time.isoformat(),
              'status':'needs_review', 'suggested_price':None, 'sources':{}, 'reasons':[],
              'lookup_links':lookup_links(card)}
    if card.get('entry_source') == 'manual' and not lookup_manual:
        return dict(result, status='manual_price', reasons=['Keep the operator’s custom selling price. An explicit lookup can compare it.'])
    if not target.game:
        return dict(result, status='needs_identity', reasons=['Identify the game so Pokémon cannot be silently skipped.'])
    if target.game != 'pokemon':
        return dict(result, status='not_pokemon', reasons=['This policy applies to Pokémon.'])
    if not target.complete:
        return dict(result, status='needs_identity', reasons=['Complete the identity, including an unambiguous grade/tier, before comparing.'])
    if not target.is_graded and target.condition not in ('near mint','lightly played','moderately played','heavily played','damaged'):
        return dict(result, status='needs_identity', reasons=['Identify the raw condition or the exact grader, grade and tier.'])
    required = GRADED_SOURCES if target.is_graded else REQUIRED_SOURCES
    result['required_sources'] = list(required)
    result['tcgplayer_role'] = 'raw_card_reference' if target.is_graded else 'condition_specific_comparison'
    for source in required:
        summary = source_summary(target,source,observations,current_time)
        result['sources'][source] = summary
        if summary['status'] != 'checked':
            result['reasons'].append(f'{SOURCE_LABELS[source]}: {summary["status"].replace("_", " ")}.')
    if result['reasons']:
        return result
    primary = 'ebay_sold' if target.is_graded else 'tcg_market'
    anchor = Decimal(str(result['sources'][primary]['median']))
    corroborators = ('pricecharting',) if target.is_graded else (*SALE_SOURCES, 'pricecharting')
    if any(abs(Decimal(str(result['sources'][s]['median'])) - anchor) / anchor > Decimal('.20') for s in corroborators):
        result['reasons'].append('Exact guide/market value and recent sold medians disagree by more than 20%.')
    for source in (s for s in required if s in SALE_SOURCES):
        values = [price(e['amount']) for e in result['sources'][source]['records']]
        mid = median(values)
        if max(values) / min(values) > 2 or any(abs(v-mid) / mid > Decimal('.50') for v in values):
            result['reasons'].append(f'{SOURCE_LABELS[source]} contains a wide spread or outliers.')
    for source in (s for s in required if s.endswith('_listings')):
        asking = Decimal(str(result['sources'][source]['median']))
        if abs(asking-anchor) / anchor > Decimal('.50'):
            result['reasons'].append(f'{SOURCE_LABELS[source]} differs from sold/market value by more than 50%.')
    if result['reasons']:
        return result
    result.update(status='proposal_ready', suggested_price=float(anchor))
    result['reasons'] = ['Exact direct-source comparisons agree. Operator approval remains required; a manual price is not overwritten.']
    return result


def review_batch(cards, observations_by_identity, current_time=None, *, batch_game=None):
    # Separate game batches can supply the game once; each row still has its own exact identity.
    results = []
    for original in cards:
        card = dict(original)
        if batch_game:
            if card.get('game') and game(card['game']) != game(batch_game):
                results.append({'row_id':card.get('row_id'), 'status':'needs_identity', 'suggested_price':None,
                                'reasons':['The row game conflicts with the selected batch game.']})
                continue
            card['game'] = batch_game
        results.append(dict(row_id=card.get('row_id'), **review(card, observations_by_identity.get(Identity.from_card(card).cache_key, []), current_time)))
    return results
