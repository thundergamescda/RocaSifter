"""Durable desktop handoff to hosted accepted trades; never local wallet credit."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime
import copy
import hashlib
import json
import re
import uuid
from station_cloud import CloudError
from station_store import StationError, now
from station_intake_claim import source_key_for, required_manifest, require_ordinary_receipt
from intake_manifest import source_lines

IDENTITY_FIELDS = {'name':'card_name','game':'game','game_label':'game_label','set':'card_set',
    'number':'card_num','condition':'condition','printing':'printing','language':'language',
    'grading_company':'grader','grade':'grade','grade_qualifier':'grade_qualifier',
    'certification':'cert_number','year':'year','sport':'sport','league':'league','team':'team',
    'player':'player','serial_number':'serial_number','manufacturer':'manufacturer'}
IDENTITY_LIMITS={'name':200,'game':100,'game_label':100,'set':200,'number':100,'condition':100,
    'printing':100,'language':50,'grading_company':100,'grade':30,'grade_qualifier':100,
    'certification':100,'year':20,'sport':100,'league':100,'team':100,'player':150,'serial_number':100,'manufacturer':100}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def locked(receipt):
    intent=receipt.get('shared_trade')
    return bool(intent and intent.get('state') not in ('confirmed','cancelled'))


def require_editable(receipt):
    require_ordinary_receipt(receipt)
    if locked(receipt):raise StationError('This batch has a saved shared trade. Open Shared trade to confirm or cancel its hosted draft before changing it.')


def cents(value):
    try:
        raw=Decimal(str(value));rounded=raw.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        if not raw.is_finite() or raw!=rounded or not 0<=rounded<=1_000_000:raise ValueError()
        return int(rounded*100)
    except (InvalidOperation,ValueError):raise StationError('Each shared-trade intake value must be a nonnegative USD amount with at most two decimal places.') from None


def lines_for(receipt,*,source_keys=False):
    require_ordinary_receipt(receipt)
    result=[];certificates=set();total=0
    selected=[card for card in receipt.get('cards',[]) if card.get('include',True)]
    if not 1<=len(selected)<=1000:raise StationError('A shared trade needs between 1 and 1,000 included card rows.')
    # One immutable manifest validates the whole projection. Revalidating all
    # original rows for each selected card made a 1,000-row trade quadratic.
    source_keys_available={line['source_line_key'] for line in source_lines(required_manifest(receipt))} if source_keys else None
    for card in selected:
        identity={key:str(card.get(field,'') or '').strip() for key,field in IDENTITY_FIELDS.items()}
        identity['game']=identity['game'] or receipt.get('pricing_game','')
        for key,value in identity.items():
            if len(value)>IDENTITY_LIMITS[key] or any(ord(char)<32 or ord(char)==127 for char in value):
                raise StationError('Shared trade '+key.replace('_',' ')+' exceeds its supported length or contains control characters. Correct it before creating a hosted draft.')
        if not identity['name']:raise StationError('Each included shared-trade card needs a name.')
        if identity['game'].startswith('other:') and not identity['game_label']:raise StationError('An additional shared-trade game needs its exact readable game name.')
        graded_hint=re.search(r'\b(psa|bgs|cgc|sgc|tag|ace|hga|beckett|graded|slab)\b',identity['condition'],re.I)
        identity['kind']='slab' if graded_hint or any(identity[key] for key in ('grading_company','grade','grade_qualifier','certification')) else 'card'
        if not identity['game']:raise StationError('Choose the game for every shared-trade card.')
        if identity['kind']=='slab' and (not all(identity[key] for key in ('grading_company','grade','certification')) or card['qty']!=1):
            raise StationError('Shared trade slabs need a grader, grade, individual certificate and quantity one.')
        if identity['kind']=='slab':
            certificate=(identity['grading_company'].casefold(),identity['certification'].casefold())
            if certificate in certificates:raise StationError('The same certified slab cannot appear twice in a shared trade.')
            certificates.add(certificate)
        identity={key:value for key,value in identity.items() if value or key in ('name','game','kind')}
        try:ident=str(uuid.UUID(str(card['row_id'])))
        except (ValueError,TypeError,AttributeError):raise StationError('This card row needs a valid saved identity. Reopen the batch.') from None
        included=card.get('include',True)
        if included and (not card.get('reviewed') or card.get('price_missing')):raise StationError('Review every included card and resolve its selling price before creating a shared trade.')
        if type(card.get('qty')) is not int or not 1<=card['qty']<=100000:raise StationError('Shared trade quantities must be whole numbers from 1 to 100,000.')
        value=cents(card.get('intake_value',card['final_price']));total+=value*card['qty']
        if total>100_000_000:raise StationError('The included shared-trade intake value exceeds $1,000,000. Split the physical intake before creating a draft.')
        source_key=card.get('source_line_key') if source_keys else None
        if source_keys and (not isinstance(source_key,str) or source_key not in source_keys_available):
            raise StationError('This card has no preserved original source line. Reimport the verified intake; do not invent a new source identity.')
        result.append({'id':ident,'identity':identity,'quantity':card['qty'],
                       'unit_intake_cents':value,
                       'included':bool(included),'reviewed':bool(card.get('reviewed')),
                       **({'source_line_key':source_key} if source_keys else {})})
    if not any(row['included'] for row in result):raise StationError('Include at least one reviewed card in the shared trade.')
    return result


def snapshot(receipt,*,source_keys=False):
    require_ordinary_receipt(receipt)
    return {'title':receipt['title'][:200],'lines':lines_for(receipt,source_keys=source_keys)}


class SharedTrade:
    def __init__(self,service):self.service=service;self.cloud=service.cloud

    def _session(self,expected,store=None,*,manager=False):
        saved=self.cloud._saved();self.cloud._expected(saved,expected)
        if not saved or saved['expires_at']<=self.cloud.clock():raise CloudError('Sign in to the store that owns this shared trade.',status=401)
        if store and saved['store']!=store:raise CloudError('Sign in to the store that owns this saved trade. Its customer and credit cannot be moved to another store.',status=409)
        if saved['role'] not in (('owner','manager') if manager else ('owner','manager','buyer')):
            raise CloudError('An owner or manager must accept a hosted trade agreement.' if manager else 'A buyer, manager or owner must open shared trade receipts.',status=403)
        return saved

    def _read(self,ident):
        with self.service.lock:
            receipt=self.service.active_receipt(ident);require_ordinary_receipt(receipt)
            return receipt

    def _save(self,receipt,event,detail):
        require_ordinary_receipt(receipt)
        self.service.store.save_receipt(receipt,(event,detail))

    def _require_fresh_review(self,receipt):
        # Current-row consistency gates new intents, not historical proof/replay.
        for card in receipt.get('cards',[]):
            if card.get('include',True):self.service._require_physical_review(card)

    def _assert_snapshot(self,receipt):
        if fingerprint(self._source(receipt))!=receipt['shared_trade']['source_hash']:
            raise StationError('The local source no longer matches this saved shared trade. Keep the hosted receipt and contact the store owner to recover the original snapshot; no credit will be guessed or issued again.')

    @staticmethod
    def _source(receipt):
        require_ordinary_receipt(receipt)
        if 'original_trade_recovery' in receipt:
            from station_trade_recovery import recovered_source_snapshot
            return recovered_source_snapshot(receipt)
        return snapshot(receipt,source_keys=receipt.get('shared_trade',{}).get('claim_binding_version')==1)

    @staticmethod
    def _contract(receipt):
        require_ordinary_receipt(receipt)
        intent=receipt.get('shared_trade')
        if not isinstance(intent,dict):raise StationError('This batch has no saved shared trade.')
        if 'offer_contract_version' not in intent:
            if intent.get('selected_offer','credit')!='credit':raise StationError('A legacy saved credit agreement cannot be changed to another offer. Recover its original journal.')
            return 1
        if type(intent['offer_contract_version']) is not int or intent['offer_contract_version']!=2:
            raise StationError('This saved trade uses an unsupported offer contract. Keep its original intent and journal for owner recovery.')
        if 'selected_offer' in intent and (intent['selected_offer'] not in ('cash','credit')
            or intent.get('state') not in ('accepting','confirmed','voiding','cancelled')):
            raise StationError('This saved trade contains an invalid or premature offer choice. Keep its original intent for owner recovery.')
        return 2

    def _selected(self,receipt):
        intent=receipt['shared_trade']
        if self._contract(receipt)==1:return 'credit'
        selected=intent.get('selected_offer')
        if selected not in ('cash','credit'):raise StationError('The saved agreement has no valid immutable offer choice. Recover its original intent; do not choose another offer.')
        return selected

    def _body(self,receipt,kind):
        require_ordinary_receipt(receipt)
        intent=receipt['shared_trade'];version=self._contract(receipt)
        if kind=='create':
            return {'request_key':intent['create_key'],'customer_id':intent['customer_id'],
                    'source_reference':intent.get('source_reference',receipt['id']),**self._source(receipt),
                    **({'physical_intake_id':intent['physical_intake_id'],'physical_manifest_hash':intent['physical_manifest_hash']} if intent.get('claim_binding_version')==1 else {}),
                    **({'replace_voided_source':True} if intent.get('replace_voided_source') else {})}
        if kind=='accept':
            if type(intent.get('expected_offer_cents')) is not int or not 0<intent['expected_offer_cents']<=100_000_000 or type(intent.get('accept_version')) is not int or intent['accept_version']<0:
                raise StationError('The saved agreement amount or version cannot be verified. Keep its original journal for owner recovery.')
            return {'request_key':intent['accept_key'],'version':intent['accept_version'],
            'customer_id':intent['customer_id'],'offer_type':self._selected(receipt),'expected_offer_cents':intent['expected_offer_cents'],
            'customer_agreed':True,'items_received':True,
            **({'claim_reviewed':True} if intent.get('claim_binding_version')==1 else {})}
        return {'request_key':intent['void_key'],'version':intent['void_version'],
            'reason':('Desktop operator cancelled this shared-credit draft before acceptance.' if version==1 else
                      'Desktop operator cancelled this hosted trade draft before acceptance.')}

    def _journal(self,receipt,kind,expected):
        require_ordinary_receipt(receipt)
        intent=receipt['shared_trade'];key=intent.get(kind+'_key')
        row=self.cloud.trade_journal(key,expected_session_id=expected) if key else None
        if row is None:return None
        body=row.get('body');valid=isinstance(body,dict)
        if valid and kind=='create':
            # Validate the saved original source, not today's mutable working
            # title/cards on a cancelled or confirmed receipt.
            source={name:body.get(name) for name in ('title','lines')}
            original={'request_key':key,'customer_id':intent['customer_id'],
                'source_reference':intent.get('source_reference',receipt['id']),**source,
                **({'physical_intake_id':intent['physical_intake_id'],'physical_manifest_hash':intent['physical_manifest_hash']} if intent.get('claim_binding_version')==1 else {}),
                **({'replace_voided_source':True} if intent.get('replace_voided_source') else {})}
            valid=body==original and fingerprint(source)==intent['source_hash']
        elif valid:valid=body==self._body(receipt,kind)
        if (not valid or row.get('kind')!='trade_'+kind or row.get('store')!=intent['store']
            or row.get('request_key')!=key or row.get('receipt_id')!=receipt['id']
            or row.get('customer_id') is not None
            or row.get('trade_id')!=(None if kind=='create' else intent.get('trade_id'))
            or row.get('fingerprint')!=self.cloud._fingerprint(body)):
            raise CloudError('The saved trade journal differs from the original local intent. Keep its original request for owner recovery; it cannot be acknowledged or rewritten.',status=409,pending=True)
        return copy.deepcopy(row)

    def _journals(self,receipt,expected):
        require_ordinary_receipt(receipt)
        return {receipt['shared_trade'][kind+'_key']:self._journal(receipt,kind,expected)
                for kind in ('create','accept','void') if receipt['shared_trade'].get(kind+'_key')}

    def _acknowledge(self,receipt,expected,bindings):
        require_ordinary_receipt(receipt)
        for kind in ('create','accept','void'):
            key=receipt['shared_trade'].get(kind+'_key')
            if key:
                if key not in bindings:
                    raise CloudError('The original saved trade request could not be proved. Keep its journal for owner recovery.',status=409,pending=True)
                self.cloud.trade_finish(key,expected_session_id=expected,original_row=bindings[key])

    def _perform(self,receipt,kind,expected,bindings=None):
        require_ordinary_receipt(receipt)
        intent=receipt['shared_trade'];key=intent[kind+'_key']
        bindings=bindings if bindings is not None else {}
        if key not in bindings:bindings[key]=self._journal(receipt,kind,expected)
        row=copy.deepcopy(bindings[key]);body=row['body'] if row else self._body(receipt,kind)
        if row:
            if row['body']!=body:raise CloudError('The saved trade journal differs from the locked local intent. Contact the store owner; this action cannot be rewritten.',status=409,pending=True)
            if row.get('result') is not None:return row['result']
            status=self.cloud.trade_request(key,expected_session_id=expected)
            if status.get('found') is True and isinstance(status.get('result'),dict):return status['result']
            if status.get('found') is not False:raise CloudError('The service could not confirm the saved trade request. Keep this batch for recovery.',status=502,pending=True)
        holder={}
        try:
            if kind=='create':return self.cloud.trade_create(body,receipt['id'],expected_session_id=expected,_journal_holder=holder)
            method=self.cloud.trade_accept if kind=='accept' else self.cloud.trade_void
            return method(intent['trade_id'],body,receipt['id'],expected_session_id=expected,_journal_holder=holder)
        finally:
            if 'row' in holder:bindings[key]=copy.deepcopy(holder['row'])

    def _ensure_created(self,receipt,expected,bindings=None):
        require_ordinary_receipt(receipt)
        bindings=bindings if bindings is not None else self._journals(receipt,expected)
        intent=receipt['shared_trade'];self._contract(receipt)
        if intent.get('trade_id'):return receipt
        self._assert_snapshot(receipt);result=self._perform(receipt,'create',expected,bindings)
        ident=self.cloud.customer_id(result.get('trade_id'))
        with self.service.lock:
            receipt=self._read(receipt['id']);self._assert_snapshot(receipt)
            receipt['shared_trade'].update(trade_id=ident,state='draft',message='Hosted draft saved. Review the hosted offers before recording an agreement.')
            self._save(receipt,'shared_trade_draft','Hosted trade draft confirmed; no credit issued.')
        self.cloud.trade_finish(intent['create_key'],expected_session_id=expected,original_row=bindings[intent['create_key']])
        return receipt

    def _verify(self,receipt,trade):
        require_ordinary_receipt(receipt)
        intent=receipt['shared_trade'];self._contract(receipt);self._assert_snapshot(receipt)
        if not isinstance(trade,dict) or not isinstance(trade.get('lines'),list) or any(not isinstance(line,dict) for line in trade['lines']):
            raise StationError('The hosted receipt has invalid source lines. Keep its original intent for owner recovery.')
        bound=intent.get('claim_binding_version')==1
        expected=self._source(receipt)['lines'] if 'original_trade_recovery' in receipt else lines_for(receipt,source_keys=bound)
        keys=('id','identity','quantity','unit_intake_cents','included','reviewed')+(('source_line_key',) if bound else ())
        actual=[{key:line.get(key) for key in keys} for line in trade.get('lines',[])]
        if (trade.get('id')!=intent['trade_id'] or trade.get('customer_id')!=intent['customer_id']
            or trade.get('source_reference')!=intent.get('source_reference',receipt['id']) or trade.get('title')!=receipt['title'][:200]
            or actual!=expected
            or (bound and (trade.get('physical_intake_id')!=intent['physical_intake_id'] or trade.get('physical_manifest_hash')!=intent['physical_manifest_hash']))):raise StationError('The hosted receipt differs from the locked local source. Ask an owner to review the hosted receipt or cancel its draft; no local acceptance will be inferred.')

    @staticmethod
    def _confirmation(trade):
        return fingerprint({key:trade.get(key) for key in ('id','customer_id','source_reference','version','lines_hash','credit_offer_cents','cash_offer_cents','intake_total_cents','state','physical_intake_id','physical_manifest_hash')})

    def _offer_confirmation(self,receipt,trade,kind):
        if self._contract(receipt)==1:return self._confirmation(trade) if kind=='credit' else None
        return fingerprint({'contract':'stock-and-sleeve-hosted-offer-v2','offer_type':kind,
            'amount_cents':trade.get(kind+'_offer_cents'),'trade_confirmation':self._confirmation(trade)})

    def _confirmed_claim(self,receipt,trade,expected,claim=None):
        require_ordinary_receipt(receipt)
        intent=receipt['shared_trade']
        if intent.get('claim_binding_version')!=1:return None
        saved=receipt.get('intake_claim') or {}
        if (claim is None and saved.get('server_state')=='consumed' and saved.get('bound_trade_id')==trade['id']
            and saved.get('consumed_trade_receipt_hash')==trade.get('receipt_hash')
            and saved.get('physical_intake_id')==intent['physical_intake_id'] and saved.get('manifest_hash')==intent['physical_manifest_hash']):
            return {'physical_intake_id':saved['physical_intake_id'],'state':'consumed','bound_trade_id':trade['id'],
                    'manifest_hash':saved['manifest_hash'],'version':saved['server_version']}
        claim=claim or self.cloud.intake_get(intent['physical_intake_id'],expected_session_id=expected)
        self.service.intake_claims._verify(receipt,claim)
        if (claim.get('physical_intake_id')!=intent['physical_intake_id'] or claim.get('state')!='consumed'
            or claim.get('bound_trade_id')!=trade['id'] or claim.get('manifest_hash')!=intent['physical_manifest_hash']):
            raise StationError('The accepted agreement and physical intake consumption do not agree. Keep the original receipt for owner recovery; no new agreement will be issued.')
        return claim

    def _terminal(self,receipt,trade):
        require_ordinary_receipt(receipt)
        """Read-only verification shared by financial mirror, status and printing."""
        self._verify(receipt,trade);intent=receipt['shared_trade'];version=self._contract(receipt)
        evidence=trade.get('accepted_receipt');result=trade.get('acceptance_result')
        allowed={'trade_schema_version','trade_id','version_before_acceptance','receipt_version','customer','title',
            'source_reference','currency','lines','lines_hash','intake_total_cents','cash_offer_cents','credit_offer_cents',
            'offer_terms','selected_offer','accepted_amount_cents','accepted_at','accepted_by','accepted_by_role',
            'customer_agreed','items_received','inventory_received','cash_payment_executed','credit_policy_state',
            'credit_entry_id','physical_intake','physical_intake_id','physical_manifest_hash'}
        result_keys={'trade_id','state','version','accepted_amount_cents','offer_type','receipt_hash','credit_entry_id',
            'cash_payment_executed','inventory_received','credit_policy_state','checkout_eligible','balance_after_cents'}
        try:
            if not isinstance(evidence,dict) or not isinstance(result,dict) or set(evidence)-allowed or set(result)-result_keys:raise ValueError()
            kind=evidence.get('selected_offer');amount=trade.get('accepted_amount_cents');state='accepted_'+str(kind)
            if (kind not in ('credit','cash') or version==1 and kind!='credit' or trade.get('state')!=state
                or trade.get('selected_offer')!=kind or type(amount) is not int or not 0<amount<=100_000_000
                or type(trade.get('version')) is not int or trade['version']<1
                or type(evidence.get('trade_schema_version')) is not int or evidence['trade_schema_version'] not in (1,2)
                or fingerprint(evidence)!=trade.get('receipt_hash') or result.get('receipt_hash')!=trade['receipt_hash']
                or result.get('trade_id')!=trade['id'] or result.get('state')!=state or result.get('offer_type')!=kind
                or type(result.get('version')) is not int or result['version']!=trade['version']
                or type(evidence.get('receipt_version')) is not int or evidence['receipt_version']!=trade['version']
                or type(evidence.get('version_before_acceptance')) is not int
                or evidence['version_before_acceptance']+1!=trade['version']
                or evidence.get('currency')!='USD' or evidence.get('trade_id')!=trade['id']
                or evidence.get('source_reference')!=intent.get('source_reference',receipt['id']) or evidence.get('title')!=trade['title']
                or evidence.get('lines_hash')!=trade.get('lines_hash') or fingerprint(trade['lines'])!=trade['lines_hash']
                or evidence.get('lines')!=[line for line in trade['lines'] if line.get('included')]
                or any(type(value.get('accepted_amount_cents')) is not int or value['accepted_amount_cents']!=amount for value in (evidence,result))
                or any(value.get('cash_payment_executed') is not False or value.get('inventory_received') is not False for value in (evidence,result))
                or result.get('checkout_eligible') is not False or evidence.get('customer_agreed') is not True or evidence.get('items_received') is not True
                or evidence.get('accepted_by_role') not in ('owner','manager')):raise ValueError()
            customer=evidence.get('customer')
            if (not isinstance(customer,dict) or set(customer)!={'id','display_name'} or customer['id']!=intent['customer_id']
                or not isinstance(customer['display_name'],str) or not 1<=len(customer['display_name'])<=200):raise ValueError()
            if (not isinstance(evidence.get('accepted_at'),str) or len(evidence['accepted_at'])>60
                or datetime.fromisoformat(evidence['accepted_at']).tzinfo is None
                or str(uuid.UUID(evidence['accepted_by']))!=evidence['accepted_by']):raise ValueError()
            for key in ('cash_offer_cents','credit_offer_cents','intake_total_cents'):
                if type(evidence.get(key)) is not int or not 0<=evidence[key]<=100_000_000 or evidence[key]!=trade.get(key):raise ValueError()
            if evidence[kind+'_offer_cents']!=amount:raise ValueError()
            for line in trade['lines']:
                if (set(line)-{'id','identity','quantity','unit_intake_cents','included','reviewed','line_total_cents','source_line_key'}
                    or type(line.get('line_total_cents')) is not int or line['line_total_cents']!=line['quantity']*line['unit_intake_cents']):raise ValueError()
            if sum(line['line_total_cents'] for line in trade['lines'] if line['included'])!=evidence['intake_total_cents']:raise ValueError()
            if evidence.get('trade_schema_version')==2:
                terms={key:trade.get(key) for key in ('cash_bps','credit_bps','cash_override_cents','credit_override_cents','override_reason')}
                if evidence.get('offer_terms')!=terms:raise ValueError()
            if 'credit_entry_id' not in evidence or 'credit_entry_id' not in result:raise ValueError()
            entry=evidence['credit_entry_id'];policy='unreviewed' if kind=='credit' else 'not_applicable'
            if result['credit_entry_id']!=entry or any(value.get('credit_policy_state')!=policy for value in (evidence,result)):raise ValueError()
            if kind=='cash':
                if entry is not None or trade.get('ledger_entry_id') is not None:raise ValueError()
            elif not isinstance(entry,str) or str(uuid.UUID(entry))!=entry or trade.get('ledger_entry_id')!=entry:raise ValueError()
            if intent.get('claim_binding_version')==1:
                if (evidence.get('physical_intake_id')!=intent['physical_intake_id']
                    or evidence.get('physical_manifest_hash')!=intent['physical_manifest_hash']):raise ValueError()
                if evidence['trade_schema_version']==2:
                    physical=evidence.get('physical_intake');original={line['source_line_key']:line['quantity'] for line in source_lines(required_manifest(receipt))}
                    selected={}
                    for line in evidence['lines']:selected[line['source_line_key']]=selected.get(line['source_line_key'],0)+line['quantity']
                    expected_physical={'id':intent['physical_intake_id'],'manifest_hash':intent['physical_manifest_hash'],
                        'semantic_hash':receipt['intake_claim']['semantic_hash'],'selected_quantities':selected,'original_quantities':original,
                        'excluded_quantities':{key:quantity-selected.get(key,0) for key,quantity in original.items()},
                        'all_original_rows_claimed':True,'excluded_rows_reusable':False,'claim_reviewed':True}
                    if physical!=expected_physical:raise ValueError()
            if intent.get('accept_key') and (kind!=self._selected(receipt) or type(intent.get('expected_offer_cents')) is not int
                or amount!=intent['expected_offer_cents'] or type(intent.get('accept_version')) is not int
                or evidence['version_before_acceptance']!=intent['accept_version']):raise ValueError()
            if 'selected_offer' in intent and intent['selected_offer']!=kind:raise ValueError()
        except (ValueError,TypeError,KeyError,AttributeError):
            raise StationError('The hosted accepted agreement cannot be verified. Keep its original intent for owner recovery; no purchase, second agreement or credit will be inferred.') from None
        return kind,evidence,result

    def _reconcile(self,receipt,trade,expected,claim=None,bindings=None):
        require_ordinary_receipt(receipt)
        bindings=bindings if bindings is not None else self._journals(receipt,expected)
        kind,evidence,result=self._terminal(receipt,trade);intent=receipt['shared_trade']
        claim=self._confirmed_claim(receipt,trade,expected,claim)
        with self.service.lock:
            receipt=self._read(receipt['id']);self._assert_snapshot(receipt)
            if receipt.get('purchase') and receipt['purchase'].get('hosted_trade_id')!=trade['id']:raise StationError('A different local purchase is already recorded. The shared receipt needs owner recovery.')
            if receipt.get('purchase') and (receipt['purchase'].get('scope')!='hosted' or receipt['purchase'].get('type')!=kind
                or cents(receipt['purchase'].get('amount'))!=trade['accepted_amount_cents'] or receipt['purchase'].get('receipt_hash')!=trade['receipt_hash']):
                raise StationError('The existing local purchase differs from the verified hosted agreement. Keep both records for owner recovery.')
            included=[card for card in receipt['cards'] if card.get('include',True)]
            # Keep accepted intake values independent of later retail label prices.
            for card in included:card['intake_value']=cents(card.get('intake_value',card['final_price']))/100
            receipt['purchase']={'scope':'hosted','type':kind,'amount':trade['accepted_amount_cents']/100,
                'at':evidence['accepted_at'],'intake_total':evidence['intake_total_cents']/100,
                'items':[{'row_id':card['row_id'],'qty':card['qty'],'intake_value':card['intake_value']} for card in included],
                'hosted_trade_id':trade['id'],'credit_entry_id':result['credit_entry_id'],'receipt_hash':trade['receipt_hash'],
                'store':intent['store'],'credit_policy_state':evidence['credit_policy_state'],'checkout_eligible':False,
                'cash_payment_executed':False,'inventory_received':False,
                'accepted_receipt':copy.deepcopy(evidence),'acceptance_result':copy.deepcopy(result)}
            receipt['shared_trade'].update(state='confirmed',server_version=trade['version'],receipt_hash=trade['receipt_hash'],
                credit_entry_id=result['credit_entry_id'],message=('Cash agreement recorded. Payment execution is not recorded by Stock & Sleeve.' if kind=='cash' else
                    'Shared credit confirmed once. Square checkout is not enabled; tax policy remains unreviewed.'))
            if self._contract(receipt)==2:receipt['shared_trade']['selected_offer']=kind
            if claim:receipt['intake_claim'].update(server_state='consumed',server_version=claim['version'],bound_trade_id=trade['id'],consumed_trade_receipt_hash=trade['receipt_hash'])
            self._save(receipt,'shared_trade_confirmed','Confirmed hosted '+kind+' agreement mirrored locally. No local wallet, payment execution or Square stock changed.')
        self._acknowledge(receipt,expected,bindings)
        return receipt

    def _view(self,receipt,trade=None,account=None):
        version=self._contract(receipt);intent=receipt['shared_trade'];state=intent.get('state')
        selected='credit' if version==1 else intent.get('selected_offer')
        if state=='draft' and version==2 and 'selected_offer' in intent:
            raise StationError('An unaccepted trade cannot contain a preselected agreement. Keep its original intent for owner review.')
        actionable=bool(trade and trade.get('state')=='draft' and state=='draft')
        offers={}
        for kind in ('cash','credit'):
            amount=trade.get(kind+'_offer_cents') if trade else None
            if selected==kind and state in ('accepting','confirmed'):
                amount=intent.get('expected_offer_cents',trade.get('accepted_amount_cents') if trade else None)
            available=actionable and (version==2 or kind=='credit') and type(amount) is int and amount>0
            offers[kind]={'offerType':kind,'amountCents':amount,'available':bool(available),
                          'confirmationHash':self._offer_confirmation(receipt,trade,kind) if available else None}
        return {'receipt':receipt,'trade':trade,'customer':account.get('customer') if account else None,
                'offerContractVersion':version,'lockedOfferType':selected,'offers':offers,
                'confirmationHash':self._confirmation(trade) if version==1 and trade and trade.get('state')=='draft' else None}

    def prepare(self,ident,data,expected):
        with self.cloud.lock:
            saved=self._session(expected);customer=self.cloud.customer_id(data.get('customerId'))
            preview=self._read(ident)
            if preview.get('shared_trade'):self._contract(preview)
            # A customer must be explicitly selected and independently confirmed.
            account=self.cloud.account(customer,expected_session_id=expected)
            if account.get('customer',{}).get('id')!=customer:raise CloudError('The shared customer response did not match your selection.',status=502)
            if preview.get('hosted_receive',{}).get('state') not in (None,'cancelled_before_write','retired_before_creation'):
                raise StationError('This physical intake has a saved hosted receiving operation. Its financial route is locked; recover the original operation instead of creating new trade credit.')
            claim=self.service.intake_claims.for_trade(preview,expected,allow_consumed=True)
            bound_trade=self.cloud.trade_get(claim['bound_trade_id'],expected_session_id=expected) if claim.get('state')=='consumed' else None
            with self.service.lock:
                receipt=self._read(ident)
                if receipt.get('purchase') or receipt.get('tracked_credit'):raise StationError('An accepted local buy cannot be converted to shared credit. Start a new reviewed trade for new physical cards.')
                if locked(receipt):raise StationError('This batch already has a saved shared trade. Open it to continue or cancel.')
                if receipt.get('mode')!='trade' or receipt.get('inventory_status')!='not_received':raise StationError('Use an unreceived customer trade-in batch for shared credit.')
                if receipt.get('revision')!=data.get('revision'):raise StationError('This batch changed. Save and reopen it before choosing a shared customer.')
                if claim.get('state')=='consumed':
                    # Discovery is authorized by the current owner/manager claim
                    # read. A fresh import must not become a replacement source.
                    trade=bound_trade
                    ident=self.cloud.customer_id(trade.get('id')) if isinstance(trade,dict) else None
                    if ident!=claim.get('bound_trade_id') or trade.get('state') not in ('accepted_cash','accepted_credit'):
                        raise StationError('The consumed intake has no verifiable accepted original trade. Keep its reference for owner recovery.')
                    candidate=copy.deepcopy(receipt)
                    previous=receipt.get('shared_trade') or {}
                    try:
                        source=snapshot(candidate,source_keys=True)
                        candidate['shared_trade']={'state':'recovering','store':saved['store'],'customer_id':customer,
                            'offer_contract_version':2,'trade_id':ident,
                            'source_reference':previous.get('source_reference',receipt['id']) if previous else receipt['id']+':'+hashlib.sha256(str(receipt['source_hash']).encode()).hexdigest(),
                            'source_hash':fingerprint(source),'claim_binding_version':1,
                            'physical_intake_id':claim['physical_intake_id'],'physical_manifest_hash':claim['manifest_hash']}
                        self._verify(candidate,trade)
                    except StationError:
                        raise CloudError('This intake already belongs to an accepted original trade. Recover that original record; the imported batch was kept unchanged.',
                            status=409,code='ORIGINAL_TRADE_RECOVERY_REQUIRED',original_trade_id=ident) from None
                if claim.get('state')!='consumed':
                    if 'shared_trade' in receipt:
                        previous=receipt['shared_trade']
                        if not isinstance(previous,dict) or previous.get('state')!='cancelled':
                            raise StationError('This batch already has saved trade evidence. Recover its original action before creating another draft.')
                        self._contract(receipt)
                    self._require_fresh_review(receipt)
                source=snapshot(receipt,source_keys=True);previous=receipt.get('shared_trade')
                if previous:
                    receipt.setdefault('shared_trade_history',[]).append({key:previous.get(key) for key in ('trade_id','store','state','receipt_hash')})
                receipt['shared_trade']={'state':'creating','store':saved['store'],'customer_id':customer,'offer_contract_version':2,
                    'source_reference':previous.get('source_reference',receipt['id']) if previous else receipt['id']+':'+hashlib.sha256(str(receipt['source_hash']).encode()).hexdigest(),
                    'source_hash':fingerprint(source),'source_revision':receipt['revision'],'create_key':uuid.uuid4().hex,
                    'claim_binding_version':1,'physical_intake_id':claim['physical_intake_id'],'physical_manifest_hash':claim['manifest_hash'],
                    'created_at':now(),'replace_voided_source':bool(previous and previous.get('state')=='cancelled'),
                    'message':'Shared trade intent saved. The source is locked while the hosted draft is confirmed.'}
                if claim.get('state')=='consumed':
                    trade=bound_trade
                    receipt['shared_trade'].update(state='recovering',trade_id=trade['id'],message='Checking the original accepted hosted receipt for this same copied batch.')
                    # Exact source/customer/row evidence must match before a
                    # copied receipt may mirror the existing accepted result.
                    self._verify(receipt,trade)
                    if trade.get('state') not in ('accepted_credit','accepted_cash'):raise StationError('This intake belongs to another final hosted agreement. Open its original hosted receipt; no new agreement can be issued.')
                    self._save(receipt,'shared_trade_recovery','Existing consumed intake matched this exact copied source. Recovering its original accepted receipt without creating another draft.')
                    receipt=self._reconcile(receipt,trade,expected,claim)
                    return self._view(receipt,trade,account)
                self._save(receipt,'shared_trade_intent','Shared trade intent saved before network mutation. Local source locked.')
            bindings=self._journals(receipt,expected)
            receipt=self._ensure_created(receipt,expected,bindings);trade=self.cloud.trade_get(receipt['shared_trade']['trade_id'],expected_session_id=expected)
            self._verify(receipt,trade)
            if trade.get('state') in ('accepted_credit','accepted_cash'):receipt=self._reconcile(receipt,trade,expected,bindings=bindings)
            return self._view(receipt,trade,account)

    def status(self,ident,expected,_journal_bindings=None):
        with self.cloud.lock:
            receipt=self._read(ident);intent=receipt.get('shared_trade')
            if not intent:raise StationError('This batch has no saved shared trade.')
            self._contract(receipt)
            self._session(expected,intent['store'])
            bindings=_journal_bindings if _journal_bindings is not None else self._journals(receipt,expected)
            if intent['state']=='cancelled':
                trade=self.cloud.trade_get(intent['trade_id'],expected_session_id=expected)
                if trade.get('state')!='voided':raise StationError('The hosted cancellation cannot be confirmed. Ask the store owner to recover this receipt.')
                self._acknowledge(receipt,expected,bindings)
                return self._view(receipt,trade)
            if intent['state']=='confirmed':
                trade=self.cloud.trade_get(intent['trade_id'],expected_session_id=expected)
                if trade.get('receipt_hash')!=intent.get('receipt_hash') or trade.get('state') not in ('accepted_credit','accepted_cash'):raise StationError('The hosted confirmation changed. Ask the store owner to recover this receipt.')
                self._terminal(receipt,trade)
                claim=self._confirmed_claim(receipt,trade,expected)
                if claim and (receipt['intake_claim'].get('server_state')!='consumed' or receipt['intake_claim'].get('bound_trade_id')!=trade['id']):
                    with self.service.lock:
                        receipt=self._read(ident);receipt['intake_claim'].update(server_state='consumed',server_version=claim['version'],bound_trade_id=trade['id'],consumed_trade_receipt_hash=trade['receipt_hash'])
                        self._save(receipt,'intake_consumption_confirmed','Authoritative physical intake consumption refreshed for the existing accepted credit receipt.')
                self._acknowledge(receipt,expected,bindings)
                return self._view(receipt,trade)
            receipt=self._ensure_created(receipt,expected,bindings);intent=receipt['shared_trade']
            if intent['state']=='accepting':self._perform(receipt,'accept',expected,bindings)
            elif intent['state']=='voiding':
                try:self._perform(receipt,'void',expected,bindings)
                except CloudError as error:
                    # A concurrent acceptance can win the same server row lock.
                    # Read its terminal authority before clearing either journal.
                    if error.status!=409:raise
            trade=self.cloud.trade_get(intent['trade_id'],expected_session_id=expected)
            if trade.get('state') in ('accepted_credit','accepted_cash'):receipt=self._reconcile(receipt,trade,expected,bindings=bindings)
            elif trade.get('state')=='voided':
                with self.service.lock:
                    receipt=self._read(ident);receipt['shared_trade'].update(state='cancelled',server_version=trade['version'],message='Hosted draft cancellation confirmed. No shared credit was issued.')
                    self._save(receipt,'shared_trade_cancelled','Hosted trade draft confirmed void. Local source unlocked; no credit issued.')
                self._acknowledge(receipt,expected,bindings)
            elif trade.get('state')!='draft':
                raise StationError('The hosted trade has a different final state. Ask the store owner to recover its saved receipt; it cannot become a new shared-credit acceptance.')
            else:self._verify(receipt,trade)
            account=self.cloud.account(intent['customer_id'],expected_session_id=expected)
            return self._view(receipt,trade,account)

    def record(self,ident,expected):
        """Verified accepted record for printing; never replay a pending action."""
        with self.cloud.lock:
            receipt=self._read(ident)
            if receipt.get('id')!=ident:
                raise StationError('The saved receipt identity changed. Keep its original reference for owner recovery.')
            intent=receipt.get('shared_trade')
            if not intent or intent.get('state')!='confirmed':
                raise StationError('Recover and confirm the original hosted agreement before printing its accepted record.')
            self._contract(receipt);self._session(expected,intent['store'])
            authority=self.cloud.read_accepted_trade(intent['trade_id'],expected_session_id=expected)
            if authority.get('sessionId')!=expected or authority.get('store')!=intent['store']:
                raise CloudError('The original agreement belongs to a different signed-in store.',status=409)
            trade=authority['trade']
            if trade.get('receipt_hash')!=intent.get('receipt_hash'):
                raise StationError('The hosted confirmation changed. Ask the store owner to recover this receipt.')
            proof=None
            if 'original_trade_recovery' in receipt:
                self._terminal(receipt,trade)
                self._confirmed_claim(receipt,trade,expected)
            else:
                from station_financial_record import ordinary_record_proof
                proof=ordinary_record_proof(receipt,trade)
            view=self._view(receipt,trade)
            view['recordAuthority']={key:authority.get(key) for key in ('sessionId','tenantId','store','role')}
            if proof is not None:view['recordProof']=proof
            return view

    def accept(self,ident,data,expected):
        with self.cloud.lock:
            receipt=self._read(ident);intent=receipt.get('shared_trade')
            if not intent:raise StationError('Save and review a hosted trade draft first.')
            version=self._contract(receipt);choice=data.get('offerType')
            self._session(expected,intent['store'],manager=True)
            if version==2 and choice not in ('cash','credit'):
                raise StationError('Explicitly choose the hosted cash agreement or shared-credit offer.')
            if version==1:
                if choice not in (None,'credit'):
                    raise StationError('This legacy saved draft is credit-only. Confirm its cancellation before explicitly creating a new cash-capable draft.')
                choice='credit'
            if intent['state'] in ('accepting','confirmed') and choice!=self._selected(receipt):
                raise StationError('This agreement already has an immutable offer choice. Recover its original saved action instead of choosing another offer.')
            if intent['state'] in ('accepting','confirmed'):return self.status(ident,expected)
            bindings=self._journals(receipt,expected)
            if receipt.get('hosted_receive',{}).get('state') not in (None,'cancelled_before_write','retired_before_creation'):
                raise StationError('A saved hosted receiving operation locks this intake against new trade acceptance. Recover the original operation; do not issue credit for inventory-only stock.')
            if intent['state']!='draft':raise StationError('Recover the saved trade before accepting it.')
            if data.get('customerAgreed') is not True or data.get('itemsReceived') is not True:raise StationError('Confirm customer agreement and physical receipt of the included cards.')
            if intent.get('claim_binding_version')==1 and data.get('claimReviewed') is not True:raise StationError('Review the central physical intake, selected copies and excluded-copy limitation before accepting this trade.')
            expected_intent=copy.deepcopy(intent);expected_revision=receipt['revision']
            trade=self.cloud.trade_get(intent['trade_id'],expected_session_id=expected);self._verify(receipt,trade)
            if trade.get('state') in ('accepted_credit','accepted_cash'):return self.status(ident,expected,_journal_bindings=bindings)
            if trade.get('state')!='draft' or data.get('confirmationHash')!=self._offer_confirmation(receipt,trade,choice):raise StationError('The hosted offer changed. Refresh and review it before accepting.')
            amount=trade.get(choice+'_offer_cents')
            if type(amount) is not int or not 0<amount<=100_000_000:raise StationError('The selected hosted offer must be a positive supported integer amount.')
            with self.service.lock:
                receipt=self._read(ident);current=receipt.get('shared_trade')
                if receipt['revision']!=expected_revision or current!=expected_intent:
                    raise StationError('This trade changed while the offer was being checked. Refresh its saved draft before accepting.')
                if (not isinstance(current,dict) or current.get('state')!='draft'
                    or any(key in current for key in ('accept_key','accept_version','expected_offer_cents','agreement_at','void_key','void_version'))):
                    raise StationError('This draft contains a saved or incomplete acceptance action. Keep its original intent and journal for recovery; no request key will be replaced.')
                if (receipt.get('purchase') or receipt.get('tracked_credit') or 'original_trade_recovery' in receipt
                    or receipt.get('mode')!='trade' or receipt.get('inventory_status')!='not_received'
                    or receipt.get('hosted_receive',{}).get('state') not in (None,'cancelled_before_write','retired_before_creation')):
                    raise StationError('This intake is no longer eligible for a new trade acceptance. Recover its original saved operation.')
                self._assert_snapshot(receipt);self._require_fresh_review(receipt)
                receipt['shared_trade'].update(state='accepting',accept_key=uuid.uuid4().hex,accept_version=trade['version'],
                    expected_offer_cents=amount,agreement_at=now(),message='Acceptance saved. Confirming the same '+choice+' agreement; keep this batch for recovery.')
                if version==2:receipt['shared_trade']['selected_offer']=choice
                self._save(receipt,'shared_trade_accept_intent','Explicit '+choice+' customer agreement and physical item receipt saved before hosted acceptance.')
            self._perform(receipt,'accept',expected,bindings)
            return self.status(ident,expected,_journal_bindings=bindings)

    def cancel(self,ident,expected):
        with self.cloud.lock:
            receipt=self._read(ident);intent=receipt.get('shared_trade')
            if not intent:raise StationError('This batch has no saved shared trade to cancel.')
            self._contract(receipt)
            self._session(expected,intent['store'])
            if intent['state'] in ('confirmed','voiding','cancelled'):return self.status(ident,expected)
            # Even an uncertain create is resolved with its original key before
            # cancelling. A missing request-status row alone cannot prove no I/O.
            bindings=self._journals(receipt,expected)
            receipt=self._ensure_created(receipt,expected,bindings);intent=receipt['shared_trade']
            trade=self.cloud.trade_get(intent['trade_id'],expected_session_id=expected)
            if trade.get('state')!='draft':return self.status(ident,expected,_journal_bindings=bindings)
            with self.service.lock:
                receipt=self._read(ident);receipt['shared_trade'].update(state='voiding',void_key=uuid.uuid4().hex,
                    void_version=trade['version'],message='Cancellation saved. The source remains locked until the hosted draft is confirmed void.')
                self._save(receipt,'shared_trade_void_intent','Explicit hosted-draft cancellation saved before network mutation.')
            try:self._perform(receipt,'void',expected,bindings)
            except CloudError as error:
                if error.status==409:return self.status(ident,expected,_journal_bindings=bindings)
                raise
            return self.status(ident,expected,_journal_bindings=bindings)
