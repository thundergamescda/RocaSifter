"""Store pricing policy. Verified sales/guide data is distinct from market references."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pricing_review import Identity, source_summary, price

RESEARCH_SOURCES = ('tcg_sales', 'tcg_listings', 'pricecharting')


def dollars(value):
    return float(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def extreme(old, new, preferences):
    difference = abs(Decimal(str(new)) - Decimal(str(old)))
    minimum = Decimal(str(preferences.get('pricingAlertDollars', 1)))
    percent = Decimal(str(preferences.get('pricingAlertPercent', 15))) / 100
    return difference >= minimum and (old <= 0 or difference > abs(Decimal(str(old))) * percent)


def retail_label_price(value, game):
    amount = Decimal(str(value))
    return 1.0 if game == 'pokemon' and Decimal('.50') <= amount <= 1 else dollars(amount)


def evaluate(card, observations, preferences, current_time=None):
    """Use sold median (75%) and exact guide (25%); prefer lowest listing for intake.

    All three sources must pass the existing identity, dates, currency, single-card
    and actual-paid-price checks before automatic correction becomes eligible.
    """
    clock = current_time or datetime.now(timezone.utc)
    target = Identity.from_card(card)
    result = {'status':'incomplete','intake_price':None,'retail_price':None,'auto_eligible':False,
              'flags':[],'sources':{},'identity_key':target.cache_key,'checked_at':clock.isoformat()}
    if not target.complete or target.game not in ('pokemon','magic'):
        return dict(result,status='needs_identity',message='Confirm the exact game, card, printing, language and condition/grade.')
    if target.is_graded:
        return dict(result,message='Raw TCGplayer comparisons cannot price this slab. Exact grader/grade guide or sales evidence is required.')
    for source in RESEARCH_SOURCES:
        result['sources'][source]=source_summary(target,source,observations,clock)
    if any(s['status']!='checked' for s in result['sources'].values()):
        return dict(result,message='Some required pricing evidence is missing or needs verification. No automatic correction.')
    sales=Decimal(str(result['sources']['tcg_sales']['median']))
    guide=Decimal(str(result['sources']['pricecharting']['median']))
    listing=min(price(r['amount']) for r in result['sources']['tcg_listings']['records'])
    anchors=[sales,guide,listing]
    if extreme(min(anchors),max(anchors),preferences):
        result['flags'].append('Pricing sources disagree by more than the store’s discrepancy threshold.')
    intake=listing*Decimal(str(preferences.get('intakeValuePercent',90)))/100
    retail=(sales*Decimal('.75')+guide*Decimal('.25'))*Decimal(str(preferences.get('retailValuePercent',100)))/100
    result.update(intake_price=dollars(intake),retail_price=retail_label_price(retail,target.game),
                  status='conflict' if result['flags'] else 'ready',auto_eligible=not result['flags'],
                  message='Review the pricing discrepancy.' if result['flags'] else 'Recent sold prices and the guide support this price; intake uses the lowest matching listing.')
    return result


def apply_label_rule(receipt):
    """Apply the explicit Pokémon dollar rule while retaining the intake value."""
    if receipt.get('inventory_status') in ('received','committing','needs_attention'):
        return
    pokemon=receipt.get('pricing_game')=='pokemon'
    for card in receipt['cards']:
        previous=card.get('label_rounding')
        if previous:
            if card['final_price']==previous['rounded']:
                if pokemon:
                    continue
                card['final_price']=previous['original']
                if previous.get('intake_added') and card.get('intake_value')==previous['original']:
                    card.pop('intake_value',None)
            card.pop('label_rounding',None)
        amount=card['final_price']
        rounded=retail_label_price(amount,'pokemon' if pokemon else '')
        if rounded!=amount:
            intake_added='intake_value' not in card
            if intake_added:
                card['intake_value']=amount
            card['final_price']=rounded
            card['reviewed']=False
            card['label_rounding']={'original':amount,'rounded':rounded,'intake_added':intake_added}
