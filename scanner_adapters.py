"""Validated, vendor-neutral card intake previews. No inventory or network writes.

Adapters describe documented column contracts, not scanner hardware support.
TCGplayer marketplace SKU IDs are preserved separately from Square SKUs. A
successful preview is never approval to receive a stock snapshot as a delivery.
"""
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import json
import re
import unicodedata

MAX_ROWS = 100_000
MAX_BYTES = 20_000_000
MAX_QUANTITY = 100_000
MAX_PRICE = Decimal('1000000')

GAME_LABELS = {
    'pokemon': 'Pokémon', 'magic': 'Magic: The Gathering', 'riftbound': 'Riftbound',
    'onepiece': 'One Piece', 'unionarena': 'Union Arena', 'gundam': 'Gundam',
    'sports': 'Sports',
}


def _key(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value)).strip().casefold().split())


GAME_ALIASES = {
    'pokémon': 'pokemon', 'pokémon tcg': 'pokemon', 'pokemon tcg': 'pokemon',
    'magic: the gathering': 'magic', 'magic the gathering': 'magic', 'mtg': 'magic',
    'riftbound tcg': 'riftbound', 'riftbound: league of legends trading card game': 'riftbound',
    'one piece': 'onepiece', 'one piece card game': 'onepiece', 'one piece tcg': 'onepiece',
    'union arena': 'unionarena', 'union arena tcg': 'unionarena',
    'gundam card game': 'gundam', 'gundam tcg': 'gundam',
    'sport': 'sports', 'sports cards': 'sports',
}

# Alias sets reflect existing Roca intake and the documented TCGplayer formats.
# Ambiguous duplicate aliases must agree row-by-row; no column silently wins.
ALIASES = {
    'card_name': ('Card Name', 'Name', 'Product Name'),
    'card_num': ('Card #', 'Card Number', 'Number', 'Collector Number'),
    'card_set': ('Set Code', 'Set', 'Set Name', 'Expansion'),
    'condition': ('Condition',),
    'printing': ('Printing', 'Finish', 'Treatment'),
    'language': ('Language',),
    'rarity': ('Rarity',),
    'game': ('Game', 'Product Line'),
    'price': ('Listing Price', 'Price', 'TCG Marketplace Price'),
    'market_price': ('Market Price', 'TCG Market Price'),
    'quantity': ('Qty', 'Quantity'),
    'bin': ('Bin', 'Bin Number'),
    'storage_location': ('Storage Location', 'Storage'),
    'source_sku': ('SKU',),
    'square_sku': ('Square SKU',),
    'tcgplayer_sku': ('TCGplayer ID', 'TCG Player ID'),
    'source_product_id': ('Product ID',),
    'source_sequence': ('Import Sequence ID', 'Sequence ID', 'Sequence', 'Seq'),
    'source_image': ('Image Filename', 'Image File', 'Image Name'),
    'grader': ('Grader', 'Grading Company'),
    'grade': ('Grade',),
    'grade_qualifier': ('Grade Qualifier', 'Grade Label'),
    'cert_number': ('Certification Number', 'Certification', 'Cert Number', 'Cert #'),
    'serial_number': ('Serial Number', 'Serial #'),
    'year': ('Year',),
    'manufacturer': ('Manufacturer', 'Brand'),
    'team': ('Team',),
    'player': ('Player',),
}
QUANTITY_COLUMNS = ('Qty', 'Quantity', 'Add to Quantity', 'Total Quantity', 'Set Stock')
PRICE_COLUMNS = ('Listing Price', 'Price', 'TCG Marketplace Price', 'Market Price', 'TCG Market Price')
CONDITIONS = {'nm': 'Near Mint', 'near mint': 'Near Mint', 'lp': 'Lightly Played',
              'lightly played': 'Lightly Played', 'mp': 'Moderately Played',
              'moderately played': 'Moderately Played', 'hp': 'Heavily Played',
              'heavily played': 'Heavily Played', 'dmg': 'Damaged', 'damaged': 'Damaged'}
FINISHES = {'non-foil': 'Normal', 'nonfoil': 'Normal', 'non foil': 'Normal',
            'normal': 'Normal', 'foil': 'Foil', 'holo': 'Holofoil',
            'holo foil': 'Holofoil', 'holofoil': 'Holofoil',
            'reverse holo': 'Reverse Holofoil', 'reverse holofoil': 'Reverse Holofoil',
            'reverse foil': 'Reverse Holofoil'}
LANGUAGES = {'en': 'English', 'eng': 'English', 'english': 'English',
             'ja': 'Japanese', 'jp': 'Japanese', 'jpn': 'Japanese', 'japanese': 'Japanese',
             'de': 'German', 'german': 'German', 'es': 'Spanish', 'spanish': 'Spanish',
             'fr': 'French', 'french': 'French', 'it': 'Italian', 'italian': 'Italian',
             'ko': 'Korean', 'korean': 'Korean', 'zh-cn': 'Simplified Chinese',
             'zh-tw': 'Traditional Chinese'}


@dataclass(frozen=True)
class IntakeIssue:
    row: int
    column: str
    code: str
    message: str
    severity: str = 'error'


@dataclass
class ImportPreview:
    adapter: str
    cards: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    quantity_column: str = ''
    quantity_semantics: str = 'count'
    requires_quantity_confirmation: bool = False
    unknown_columns: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    sample_rows: list = field(default_factory=list)

    @property
    def can_import(self):
        return bool(self.cards) and not any(i.severity == 'error' for i in self.issues)

    def as_dict(self):
        return {'adapter': self.adapter, 'cards': self.cards,
                'issues': [asdict(i) for i in self.issues], 'can_import': self.can_import,
                'quantity_column': self.quantity_column, 'quantity_semantics': self.quantity_semantics,
                'requires_quantity_confirmation': self.requires_quantity_confirmation,
                'unknown_columns': self.unknown_columns, 'columns': self.columns,
                'sample_rows': self.sample_rows}

    def require_valid(self):
        if not self.can_import:
            raise IntakeValidationError(self.issues or [IntakeIssue(1, '', 'empty', 'No card rows found.')])
        return self.cards


class IntakeValidationError(ValueError):
    def __init__(self, issues):
        self.issues = tuple(issues)
        first = next((i for i in issues if i.severity == 'error'), issues[0])
        super().__init__(f'CSV row {first.row}: {first.message}')


def normalize_game(value):
    """Known games get stable keys; other game names remain distinct and visible."""
    original = str(value or '').strip()
    key = _key(original)
    if not key:
        return '', ''
    known = GAME_ALIASES.get(key, key)
    if known in GAME_LABELS:
        return known, GAME_LABELS[known]
    if re.fullmatch(r'other:[a-z0-9][a-z0-9-]{0,99}', key):
        return key, original.removeprefix('other:')
    # Do not label an additional game as Pokémon or Magic to obtain a price.
    slug = re.sub(r'[^a-z0-9]+', '-', unicodedata.normalize('NFKD', key).encode('ascii', 'ignore').decode()).strip('-')
    if not slug:
        slug = hashlib.sha256(key.encode()).hexdigest()[:16]
    return 'other:' + slug, original


def spreadsheet_safe(value):
    """Use at CSV export boundaries; keep the stored card identity unmodified."""
    text = '' if value is None else str(value)
    dangerous = text.startswith(('\t', '\r', '\n')) or text.lstrip().startswith(('=', '+', '-', '@'))
    return "'" + text if dangerous else text


def identity_key(card):
    """Identity includes game, slab certificate and sports/serial variants.

    This is an intake fingerprint, not an automatic migration of existing SKUs.
    Preserve Square SKU mappings until a reviewed migration resolves them.
    """
    fields = ('game', 'game_label', 'card_name', 'card_set', 'card_num', 'condition',
              'printing', 'language', 'grader', 'grade', 'grade_qualifier', 'cert_number',
              'serial_number', 'year', 'manufacturer', 'team', 'player')
    values = {key: _key(card.get(key, '')) for key in fields}
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _money(value):
    text = str(value).strip()
    if not text:
        return None
    if not re.fullmatch(r'\$?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?', text):
        raise ValueError('Use a nonnegative USD price with at most two decimal places; formulas and negative values are not prices.')
    try:
        result = Decimal(text.replace('$', '').replace(',', ''))
    except InvalidOperation:
        raise ValueError('Enter a valid USD price.') from None
    if not result.is_finite() or not 0 <= result <= MAX_PRICE:
        raise ValueError('Price must be between $0 and $1,000,000.')
    return int(result * 100)


def _text(value, field_name, *, limit=500):
    text = str(value or '').strip()
    if len(text) > limit:
        raise ValueError(f'{field_name} must be at most {limit} characters.')
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in text):
        raise ValueError(f'{field_name} contains a control character; correct the source cell.')
    return text


def _quantity(value):
    text = str(value).strip()
    if not re.fullmatch(r'[0-9]+', text) or not 1 <= int(text) <= MAX_QUANTITY:
        raise ValueError('Quantity must be a whole number from 1 to 100,000; stock removals are not receiving rows.')
    return int(text)


def _combined_condition(value):
    """TCG inventory condition labels can carry printing; do not guess treatments."""
    key = _key(value)
    for suffix in ('reverse holofoil', 'reverse holo', 'holofoil', 'foil'):
        if key.endswith(' ' + suffix):
            condition = key[:-len(suffix)].strip()
            if condition in CONDITIONS:
                return CONDITIONS[condition], FINISHES[suffix]
    return CONDITIONS.get(key, value), ''


def _detect(headers):
    keys = set(headers)
    if {'product name', 'product line'} <= keys or 'tcgplayer id' in keys or 'tcg player id' in keys:
        return 'tcgplayer_inventory'
    if {'sku', 'name', 'product id', 'language', 'printing', 'condition', 'quantity'} <= keys:
        return 'tcgplayer_app'
    if 'card name' in keys and ('bin' in keys or 'bin number' in keys):
        return 'roca'
    return 'canonical'


def parse_csv(content, *, adapter='auto', default_game='', column_mapping=None,
              quantity_column=None, price_column=None):
    """Preview UTF-8 CSV/TSV; return all row errors without receiving any stock.

    ``column_mapping`` maps canonical ALIASES field names to exact source headers.
    ``quantity_column`` is mandatory when a file mixes add and snapshot quantities.
    Price source selection is explicit when multiple nonempty price columns differ.
    Blank sale price stays None even if a market reference is available.
    """
    result = ImportPreview(adapter)
    def issue(row, column, code, message, severity='error'):
        result.issues.append(IntakeIssue(row, column, code, message, severity))
    if isinstance(content, bytes):
        try:content = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            issue(1, '', 'encoding', 'Save the export as UTF-8 CSV before importing.');return result
    if not isinstance(content, str):
        issue(1, '', 'type', 'Import CSV text or a UTF-8 CSV file.');return result
    if len(content.encode('utf-8')) > MAX_BYTES:
        issue(1, '', 'size', 'This export exceeds the 20 MB intake limit; split the batch.');return result
    content = content.lstrip('\ufeff')
    if '\x00' in content:
        issue(1, '', 'binary', 'This is not a text CSV; export the spreadsheet as UTF-8 CSV.');return result
    first_line = content.splitlines()[0] if content.splitlines() else ''
    try:dialect = csv.Sniffer().sniff(first_line, delimiters=',\t;')
    except csv.Error:dialect = csv.excel
    reader = csv.reader(io.StringIO(content, newline=''), dialect=dialect, strict=True)
    try:header = next(reader)
    except (StopIteration, csv.Error):
        issue(1, '', 'headers', 'A header row and at least one card row are required.');return result
    result.columns = list(header)
    headers = {}
    if len(header) > 100:
        issue(1, '', 'header_limit', 'This CSV exceeds 100 columns; map a smaller card export.');return result
    for name in header:
        key = _key(name)
        if not key or key in headers:
            issue(1, name, 'duplicate_header', 'Column headers must be nonempty and unique, including capitalization and spacing.')
        try:_text(name, 'Column name', limit=150)
        except ValueError as error:issue(1, name, 'header', str(error))
        headers[key] = name
    if result.issues:return result
    # The first few source rows help an operator map unfamiliar column names.
    # A separate bounded reader leaves the validation reader's line numbers exact.
    try:
        sample = csv.reader(io.StringIO(content, newline=''), dialect=dialect, strict=True)
        next(sample)
        for _ in range(5):
            values = next(sample, None)
            if values is None:break
            result.sample_rows.append([value[:200] for value in values[:100]])
        del sample
    except csv.Error:
        issue(2, '', 'csv', 'Malformed CSV quoting or a cell exceeds the supported field limit.');return result
    result.adapter = _detect(headers) if adapter == 'auto' else adapter
    if result.adapter not in ('roca', 'canonical', 'tcgplayer_app', 'tcgplayer_inventory', 'mapped'):
        issue(1, '', 'adapter', 'Choose a supported CSV format or map its columns.');return result
    if column_mapping is not None and (not isinstance(column_mapping, dict) or
            any(not isinstance(k, str) or not isinstance(v, str) for k, v in column_mapping.items())):
        issue(1, '', 'mapping', 'Map each intake field to a source column name.');return result
    mapping = dict(column_mapping or {})
    if any(k not in ALIASES for k in mapping):
        issue(1, '', 'mapping', 'A column mapping names an unsupported intake field.');return result
    for field_name, name in mapping.items():
        if _key(name) not in headers:
            issue(1, str(name), 'mapping', f'The mapped {field_name} column is missing.')
    mapped_headers = {field_name: [headers[_key(mapping[field_name])]] if field_name in mapping and _key(mapping[field_name]) in headers
                      else [headers[_key(alias)] for alias in aliases if _key(alias) in headers]
                      for field_name, aliases in ALIASES.items()}
    if not mapped_headers['card_name']:
        issue(1, '', 'name_header', 'Map a Card Name, Name or Product Name column.');return result
    quantities = [headers[_key(c)] for c in QUANTITY_COLUMNS if _key(c) in headers]
    explicit_quantity = bool(quantity_column or 'quantity' in mapping)
    if 'quantity' in mapping:quantity_column = mapping['quantity']
    if quantity_column:
        if _key(quantity_column) not in headers:
            issue(1, quantity_column, 'quantity_column', 'The selected quantity column is missing.');return result
        quantity_column = headers[_key(quantity_column)]
    else:
        semantic_columns = {_key(c) for c in quantities}
        modes = {'snapshot' if c in ('total quantity', 'set stock') else 'add' if c == 'add to quantity' else 'count' for c in semantic_columns}
        if len(modes) > 1 or len(semantic_columns & {'total quantity', 'set stock'}) > 1:
            issue(1, '', 'quantity_selection', 'This file includes different quantity meanings. Select the delivery quantity column; totals are stock snapshots.');return result
        quantity_column = quantities[0] if quantities else ''
    result.quantity_column = quantity_column
    result.quantity_semantics = 'snapshot' if _key(quantity_column) in ('total quantity', 'set stock') else 'add' if _key(quantity_column) == 'add to quantity' else 'count'
    result.requires_quantity_confirmation = result.quantity_semantics == 'snapshot'
    if result.requires_quantity_confirmation:
        issue(1, quantity_column, 'snapshot_quantity', 'This quantity is a stock snapshot. Confirm receiving intent before adding any stock.', 'warning')
    if price_column and _key(price_column) not in headers:
        issue(1, price_column, 'price_column', 'The selected price column is missing.');return result
    if price_column:price_column = headers[_key(price_column)]
    known = {_key(h) for values in mapped_headers.values() for h in values} | {_key(c) for c in quantities} | {'currency'}
    result.unknown_columns = [name for key, name in headers.items() if key not in known]
    seen_rows = 0
    try:
        for row in reader:
            row_number = reader.line_num
            if not any(str(v).strip() for v in row):continue
            seen_rows += 1
            if seen_rows > MAX_ROWS:
                issue(row_number, '', 'row_limit', 'This export exceeds 100,000 card rows; split the batch.');break
            if len(row) != len(header):
                issue(row_number, '', 'row_width', 'This row has missing or extra columns; check CSV quoting.');continue
            values = dict(zip(header, row))
            row_errors = len(result.issues)
            def take(field_name):
                choices = [(name, values[name].strip()) for name in mapped_headers[field_name] if values[name].strip()]
                if len({_key(v) for _, v in choices}) > 1:
                    issue(row_number, '/'.join(n for n, _ in choices), 'alias_conflict', f'Conflicting {field_name} columns; map the intended source.')
                return choices[0][1] if choices else ''
            card = {}
            for key in ('card_name', 'card_num', 'card_set', 'condition', 'printing', 'language', 'rarity', 'bin',
                        'storage_location', 'grader', 'grade', 'grade_qualifier', 'cert_number', 'serial_number',
                        'year', 'manufacturer', 'team', 'player'):
                try:card[key] = _text(take(key), key)
                except ValueError as error:
                    issue(row_number, key, 'text', str(error));card[key] = ''
            if not card['card_name']:
                issue(row_number, 'card_name', 'name', 'A card name is required; nameless rows are not silently skipped.')
            game_value = take('game') or default_game
            try:game_value = _text(game_value, 'Game', limit=100)
            except ValueError as error:issue(row_number, 'game', 'game', str(error));game_value = ''
            card['game'], card['game_label'] = normalize_game(game_value)
            if not card['game']:
                issue(row_number, 'game', 'missing_game', 'Choose the game for this row or batch before price research.', 'warning')
            original_condition = card['condition']
            card['condition'], combined_finish = _combined_condition(card['condition'])
            if combined_finish and card['printing'] and FINISHES.get(_key(card['printing']), card['printing']) != combined_finish:
                issue(row_number, 'printing', 'finish_conflict', 'Condition and printing columns describe different finishes.')
            card['printing'] = FINISHES.get(_key(card['printing']), card['printing']) or combined_finish
            card['language'] = LANGUAGES.get(_key(card['language']), card['language'])
            for key, default in (('condition', 'Near Mint'), ('printing', 'Normal'), ('language', 'English')):
                if not card[key]:
                    card[key] = default
                    issue(row_number, key, 'assumed_identity', f'{key.replace("_", " ").capitalize()} was not supplied; confirm {default} against the card.', 'warning')
            if any(card[k] for k in ('grader', 'grade', 'grade_qualifier', 'cert_number')):
                card['item_type'] = 'slab'
                if not card['grader'] or not card['grade']:
                    issue(row_number, 'grade', 'slab_identity', 'A slab needs both grading company and grade; keep its certificate as text.')
                if original_condition and _key(original_condition) not in ('graded', 'slab'):
                    # A condition like PSA 10 can be validated by the pricing identity
                    # layer; do not reconcile contradictory explicit grading here.
                    labels = (card['grader'] + ' ' + card['grade'],
                              card['grader'] + ' ' + card['grade'] + ' ' + card['grade_qualifier'],
                              card['grader'] + ' ' + card['grade_qualifier'] + ' ' + card['grade'])
                    if _key(original_condition) not in {_key(label) for label in labels}:
                        issue(row_number, 'condition', 'slab_condition', 'A slab has a raw-card condition and separate grade; correct the conflicting identity.')
                if not card['cert_number']:
                    issue(row_number, 'cert_number', 'missing_certificate', 'Record the slab certificate before treating it as a unique physical item.', 'warning')
                if re.fullmatch(r'[0-9]+(?:\.0|\.5)?', card['grade']):
                    card['grade'] = format(Decimal(card['grade']).normalize(), 'f')
                card['condition'] = ' '.join(part for part in (card['grader'], card['grade'], card['grade_qualifier']) if part)
            else:card['item_type'] = 'card'
            try:
                quantity_values = [values[name].strip() for name in quantities if values[name].strip()]
                raw_quantity = (values[quantity_column].strip() if explicit_quantity else
                                quantity_values[0] if quantity_values else '')
                if not explicit_quantity and len({_quantity(value) for value in quantity_values}) > 1:
                    raise ValueError('Quantity columns disagree. Map the intended number of copies before importing.')
                if not raw_quantity:
                    if result.adapter.startswith('tcgplayer') or quantity_column:
                        raise ValueError('Quantity is blank; enter the number of copies to import.')
                    raw_quantity = '1'
                    issue(row_number, 'quantity', 'assumed_quantity', 'Quantity was not supplied; confirm one copy.', 'warning')
                card['qty'] = _quantity(raw_quantity)
                if card['cert_number'] and card['qty'] != 1:
                    raise ValueError('A certified slab must have one copy per certificate; enter each slab separately.')
            except ValueError as error:issue(row_number, quantity_column or 'quantity', 'quantity', str(error))
            currency = values.get(headers.get('currency', ''), '').strip()
            if currency and currency.upper() != 'USD':
                issue(row_number, 'currency', 'currency', 'This intake expects USD. Convert and confirm non-USD prices explicitly.')
            try:
                sale_text = values[price_column].strip() if price_column else take('price')
                card['price_cents'] = _money(sale_text)
                card['market_price_cents'] = _money(take('market_price'))
                card['final_price'] = card['price_cents'] / 100 if card['price_cents'] is not None else None
                card['raw_price'] = sale_text
                card['price_source_column'] = price_column or (mapped_headers['price'][0] if sale_text else '')
                if card['price_cents'] is None:
                    issue(row_number, 'price', 'missing_sale_price', 'No selling price was supplied. A market reference is not an approved label price.', 'warning')
            except ValueError as error:issue(row_number, price_column or 'price', 'price', str(error))
            source_ids = {}
            for key in ('source_sku', 'tcgplayer_sku', 'source_product_id', 'source_sequence', 'source_image', 'square_sku'):
                try:value = _text(take(key), key, limit=150)
                except ValueError as error:issue(row_number, key, 'identifier', str(error));value = ''
                if value:source_ids[key] = value
            if result.adapter == 'tcgplayer_app' and source_ids.get('source_sku'):
                source_ids['tcgplayer_sku'] = source_ids.pop('source_sku')
            square_sku = source_ids.get('square_sku', '')
            if square_sku and not re.fullmatch(r'[0-9]{12}', square_sku):
                issue(row_number, 'square_sku', 'square_sku', 'An explicit Square SKU must be exactly 12 digits; marketplace IDs are separate.')
            card.update(source_ids=source_ids, sku_override=square_sku,
                        source_adapter=result.adapter, source_row=row_number,
                        quantity_semantics=result.quantity_semantics, reviewed=False, include=True)
            if source_ids.get('source_sku'):
                issue(row_number, 'SKU', 'ambiguous_sku', 'Source SKU was preserved separately. Map it explicitly if it is an existing Square SKU.', 'warning')
            if len(result.issues) == row_errors or not any(i.severity == 'error' for i in result.issues[row_errors:]):
                card['intake_identity'] = identity_key(card)
                result.cards.append(card)
    except csv.Error as error:
        issue(reader.line_num, '', 'csv', 'Malformed CSV quoting or a cell exceeds the supported field limit.')
    if not result.cards and not any(i.severity == 'error' for i in result.issues):
        issue(2, '', 'empty', 'No card rows found.')
    return result


def parse_csv_strict(content, **options):
    """Atomic import boundary: raise if any row is invalid, even if others parsed."""
    preview = parse_csv(content, **options)
    preview.require_valid()
    return preview


def normalize_manual_card(item, *, game=''):
    """Apply the same strict identity/money rules to a manually entered card/slab.

    Returns a review-required card. Persisting it and handling an idempotent
    request ID remain the receipt service's responsibility.
    """
    if not isinstance(item, dict):
        raise IntakeValidationError([IntakeIssue(2, '', 'item', 'Enter card details as an object.')])
    for key in ('condition', 'printing'):
        if key in item and not str(item[key]).strip() and not (key == 'condition' and item.get('grader') and item.get('grade')):
            raise IntakeValidationError([IntakeIssue(2, key, 'identity', f'Enter the {key} for this manual item.')])
    fields = ('card_name', 'card_num', 'card_set', 'condition', 'printing', 'language',
              'rarity', 'bin', 'storage_location', 'grader', 'grade', 'grade_qualifier',
              'cert_number', 'serial_number', 'year', 'manufacturer', 'team', 'player')
    content = io.StringIO(newline='');writer = csv.writer(content)
    writer.writerow([ALIASES[key][0] for key in fields] + ['Quantity', 'Price', 'Game'])
    writer.writerow([item.get(key, '') for key in fields] +
                    [item.get('qty', 1), item.get('final_price', item.get('price', '')),
                     item.get('game_label') or item.get('game') or game])
    result = parse_csv_strict(content.getvalue(), adapter='canonical').cards[0]
    if result['price_cents'] is None:
        raise IntakeValidationError([IntakeIssue(2, 'price', 'price', 'Enter the selling price for this manual item.')])
    result['entry_source'] = 'manual'
    return result
