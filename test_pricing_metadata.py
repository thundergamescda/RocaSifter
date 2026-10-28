"""Offline pricing identity/link and queued-metadata regression tests."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from pricing_review import Identity, lookup_links, rejected_reason
from station_pricing import Research, TCGCSV, proposals
from station_store import Store

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
CARD = {'game': 'pokemon', 'card_name': 'Fictional Card A', 'card_set': 'Fictional Set',
        'card_num': '001/100', 'printing': 'Holofoil', 'condition': 'Near Mint',
        'language': 'English', 'final_price': 3.13, 'qty': 1, 'row_id': 'one'}


def evidence(card):
    return {'source': 'ebay_sold', 'record_id': 'fictional-sale-1', 'identity': copy.deepcopy(card),
            'currency': 'USD', 'price_basis': 'item', 'amount': 10,
            'attribution': 'exact', 'url': 'https://www.ebay.com/itm/fictional',
            'is_lot': False, 'quantity': 1, 'graded': Identity.from_card(card).is_graded,
            'signed': False, 'altered': False, 'proxy': False,
            'observed_at': NOW.isoformat(), 'sold_at': NOW.isoformat(),
            'sale_price_verified': True, 'active': True, 'guide_verified': True}


class PricingMetadataTests(unittest.TestCase):
    def test_required_games_use_correct_labels_without_pokemon_fallback(self):
        examples = [('riftbound', 'Riftbound'), ('onepiece', 'One Piece'), ('unionarena', 'Union Arena'),
                    ('gundam', 'Gundam'), ('sports', 'Sports'),
                    ('other:flesh-and-blood', 'Flesh and Blood')]
        for game, label in examples:
            with self.subTest(game=game):
                links = lookup_links(CARD | {'game': game, 'game_label': label})
                tcg = urlsplit(links['tcgplayer']);query = parse_qs(tcg.query)
                self.assertEqual(tcg.hostname, 'www.tcgplayer.com')
                self.assertEqual(tcg.path, '/search/all/product')
                self.assertNotIn('productLineName', query)
                self.assertTrue(query['q'][0].startswith(label + ' '))
                ebay = parse_qs(urlsplit(links['ebay_sold']).query)['_nkw'][0]
                self.assertTrue(ebay.startswith(label + ' '))
                self.assertNotIn('Pokemon', ebay)
                self.assertNotIn('Magic The Gathering', ebay)
        for game, path in (('pokemon', 'pokemon'), ('magic', 'magic')):
            links = lookup_links(CARD | {'game': game})
            self.assertEqual(urlsplit(links['tcgplayer']).path, f'/search/{path}/product')

    def test_missing_game_does_not_invent_a_game_and_query_text_cannot_change_destination(self):
        raw = CARD | {'game': '', 'card_name': 'A & B #1 / ?x=<script>', 'card_num': '03/10'}
        for key, link in lookup_links(raw).items():
            if key.endswith('_role'):continue
            uri = urlsplit(link)
            self.assertEqual(uri.scheme, 'https')
            self.assertIsNone(uri.username)
            self.assertIn(uri.hostname, ('www.ebay.com', 'www.tcgplayer.com', 'www.pricecharting.com'))
            query = parse_qs(uri.query)
            text = query.get('_nkw', query.get('q'))[0]
            self.assertIn('A & B #1 / ?x=<script>', text)
            self.assertNotIn('Pokemon', text)

    def test_sports_serial_and_grading_details_are_retained_in_manual_searches(self):
        card = CARD | {'game': 'sports', 'condition': 'Graded', 'grader': 'BGS', 'grade': '10',
                       'grade_qualifier': 'Black Label', 'serial_number': '03/10', 'year': '2024',
                       'manufacturer': 'Topps', 'team': 'Fictional Team', 'player': 'Fictional Player'}
        links = lookup_links(card)
        for key in ('ebay_sold', 'pricecharting'):
            query = parse_qs(urlsplit(links[key]).query)
            text = query.get('_nkw', query.get('q'))[0]
            for detail in ('03/10', '2024', 'Topps', 'Fictional Team', 'Fictional Player', 'black label', '10'):
                self.assertIn(detail, text)
        self.assertEqual(links['tcgplayer_role'], 'raw_card_reference')

    def test_game_aliases_and_custom_labels_have_stable_distinct_identity(self):
        for left, right in [('Pokémon TCG', 'pokemon'), ('MTG', 'Magic: The Gathering'),
                            ('One Piece Card Game', 'onepiece'), ('Union Arena', 'unionarena')]:
            self.assertEqual(Identity.from_card(CARD | {'game': left}), Identity.from_card(CARD | {'game': right}))
        left = Identity.from_card(CARD | {'game': 'Flesh and Blood'})
        right = Identity.from_card(CARD | {'game': 'other:flesh-and-blood', 'game_label': 'Flesh and Blood'})
        self.assertEqual(left, right)
        self.assertNotEqual(left.cache_key, Identity.from_card(CARD | {'game': 'sports'}).cache_key)

    def test_cache_distinguishes_serial_sports_and_physical_certificates(self):
        original = Identity.from_card(CARD)
        for key, value in [('serial_number', '03/10'), ('year', '2024'), ('manufacturer', 'Topps'),
                           ('team', 'Fixture Team'), ('player', 'Fixture Player')]:
            self.assertNotEqual(original.cache_key, Identity.from_card(CARD | {key: value}).cache_key)
        slab = CARD | {'condition': 'Graded', 'grader': 'PSA', 'grade': '10', 'cert_number': '0000123'}
        second = slab | {'cert_number': '0000124'}
        self.assertNotEqual(Identity.from_card(slab).cache_key, Identity.from_card(second).cache_key)
        # Different physical certificates do not invalidate otherwise exact sale comps.
        self.assertEqual(Identity.from_card(slab), Identity.from_card(second))
        self.assertIsNone(rejected_reason(Identity.from_card(slab), evidence(second), NOW))

    def test_explicit_grade_tier_survives_and_ambiguous_grade_jobs_do_not_coalesce(self):
        explicit = CARD | {'condition': 'Graded', 'grader': 'BGS', 'grade': '10', 'grade_qualifier': 'Black Label'}
        inferred = CARD | {'condition': 'BGS Black Label 10'}
        self.assertEqual(Identity.from_card(explicit), Identity.from_card(inferred))
        ordinary = explicit | {'grade_qualifier': ''}
        self.assertNotEqual(Identity.from_card(explicit).cache_key, Identity.from_card(ordinary).cache_key)
        invalid = [Identity.from_card(CARD | {'condition': value}) for value in ('PSA 9.5', 'CGC 10', 'BGS Black Label 9.5')]
        self.assertTrue(all(not identity.complete for identity in invalid))
        self.assertEqual(len({identity.cache_key for identity in invalid}), len(invalid))

    def test_raw_generic_evidence_cannot_match_serial_or_sports_specific_targets(self):
        for key, value in [('serial_number', '03/10'), ('year', '2024'), ('manufacturer', 'Topps'),
                           ('team', 'Fixture Team'), ('player', 'Fixture Player')]:
            target = CARD | {key: value}
            self.assertIsNotNone(rejected_reason(Identity.from_card(target), evidence(CARD), NOW))
            self.assertIsNone(rejected_reason(Identity.from_card(target), evidence(target), NOW))
        target = CARD | {'serial_number': '03/10'}
        self.assertIsNotNone(rejected_reason(Identity.from_card(target), evidence(CARD | {'serial_number': '04/10'}), NOW))

    def test_cached_generic_market_anchors_cannot_price_specialized_or_unsupported_items(self):
        cards = [CARD | {'game': value} for value in ('riftbound', 'onepiece', 'unionarena', 'gundam', 'sports', 'Flesh and Blood')]
        cards += [CARD | {'serial_number': '03/10'}, CARD | {'year': '2024'}, CARD | {'condition': 'PSA 10'}]
        for card in cards:
            with self.subTest(card=card):
                result = proposals({'card': card, 'status': 'reference', 'checked_at': datetime.now(timezone.utc).isoformat(),
                                    'anchors': [10], 'sources': {}}, {'pricingCompleteOnly': False})
                self.assertIsNone(result['retail_price']);self.assertIsNone(result['intake_price'])
                self.assertFalse(result['auto_eligible'])
                self.assertIn(result['status'], ('unavailable', 'needs_identity'))


class PricingQueueMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.store = Store(Path(self.temp.name) / 'station')
        self.provider = Mock();self.provider.lookup.side_effect = AssertionError('Provider calls forbidden for specialized items')
        self.research = Research(self.store, threading.Event(), lambda: {'pricingEnabled': True, 'pricingCompleteOnly': False}, provider=self.provider)

    def tearDown(self):self.temp.cleanup()

    def test_single_and_receipt_enqueue_preserve_all_exact_metadata(self):
        card = CARD | {'game': 'sports', 'game_label': 'Sports', 'condition': 'Graded', 'grader': 'CGC',
                       'grade': '10', 'grade_qualifier': 'Pristine', 'cert_number': '0000123',
                       'serial_number': '03/10', 'year': '2024', 'manufacturer': 'Topps',
                       'team': 'Fixture Team', 'player': 'Fixture Player'}
        key = self.research.enqueue(card)
        second = card | {'cert_number': '0000124', 'row_id': 'two'}
        self.research.enqueue_receipt({'cards': [card, second], 'pricing_game': 'sports'})
        with self.store.db() as db:rows = db.execute('SELECT key,card FROM price_jobs').fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            queued = json.loads(row['card'])
            for field in ('game', 'game_label', 'grader', 'grade', 'grade_qualifier', 'serial_number', 'year', 'manufacturer', 'team', 'player'):
                self.assertEqual(queued[field], card[field])
            self.assertTrue(queued['cert_number'].startswith('0000'))
            self.assertEqual(Identity.from_card(queued).cache_key, row['key'])
        self.assertIn(key, {row['key'] for row in rows})

    def test_workers_and_direct_market_provider_never_fetch_unsupported_or_specific_variants(self):
        cards = [CARD | {'game': value} for value in ('riftbound', 'onepiece', 'unionarena', 'gundam', 'sports', 'Flesh and Blood')]
        cards += [CARD | {'serial_number': '03/10'}, CARD | {'year': '2024'}, CARD | {'condition': 'PSA 10'}]
        for card in cards:self.research.enqueue(card)
        for _ in cards:self.assertTrue(self.research.run_one())
        self.assertFalse(self.research.run_one());self.provider.lookup.assert_not_called()
        direct = TCGCSV(self.research)
        with patch.object(direct, 'fetch', side_effect=AssertionError('No network')) as fetch:
            for card in cards:
                result = direct.lookup(card)
                self.assertIn(result['status'], ('unavailable', 'needs_identity'))
            fetch.assert_not_called()


if __name__ == '__main__':unittest.main(verbosity=2)
