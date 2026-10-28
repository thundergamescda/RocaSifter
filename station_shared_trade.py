"""Durable desktop handoff to hosted accepted trades; never local wallet credit."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
import uuid
from station_cloud import CloudError
from station_store import StationError, now

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
    if locked(receipt):raise StationError('This batch has a saved shared trade. Open Shared trade to confirm or cancel its hosted draft before changing it.')


def cents(value):
    try:
        raw=Decimal(str(value));rounded=raw.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        if not raw.is_finite() or raw!=rounded or not 0<=rounded<=1_000_000:raise ValueError()
        return int(rounded*100)
    except (InvalidOperation,ValueError):raise StationError('Each shared-trade intake value must be a nonnegative USD amount with at most two decimal places.') from None


def lines_for(receipt):
    result=[];certificates=set();total=0
    selected=[card for card in receipt.get('cards',[]) if card.get('include',True)]
    if not 1<=len(selected)<=1000:raise StationError('A shared trade needs between 1 and 1,000 included card rows.')
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
        result.append({'id':ident,'identity':identity,'quantity':card['qty'],
                       'unit_intake_cents':value,
                       'included':bool(included),'reviewed':bool(card.get('reviewed'))})
    if not any(row['included'] for row in result):raise StationError('Include at least one reviewed card in the shared trade.')
    return result


def snapshot(receipt):
    return {'title':receipt['title'][:200],'lines':lines_for(receipt)}


class SharedTrade:
    def __init__(self,service):self.service=service;self.cloud=service.cloud

    def _session(self,expected,store=None,*,manager=False):
        saved=self.cloud._saved();self.cloud._expected(saved,expected)
        if not saved or saved['expires_at']<=self.cloud.clock():raise CloudError('Sign in to the store that owns this shared trade.',status=401)
        if store and saved['store']!=store:raise CloudError('Sign in to the store that owns this saved trade. Its customer and credit cannot be moved to another store.',status=409)
        if saved['role'] not in (('owner','manager') if manager else ('owner','manager','buyer')):
            raise CloudError('An owner or manager must accept shared trade credit.' if manager else 'A buyer, manager or owner must open shared trade receipts.',status=403)
        return saved

    def _read(self,ident):
        with self.service.lock:return self.service.active_receipt(ident)

    def _save(self,receipt,event,detail):
        self.service.store.save_receipt(receipt,(event,detail))

    def _assert_snapshot(self,receipt):
        if fingerprint(snapshot(receipt))!=receipt['shared_trade']['source_hash']:
            raise StationError('The local source no longer matches this saved shared trade. Keep the hosted receipt and contact the store owner to recover the original snapshot; no credit will be guessed or issued again.')

    def _body(self,receipt,kind):
        intent=receipt['shared_trade']
        if kind=='create':
            return {'request_key':intent['create_key'],'customer_id':intent['customer_id'],
                    'source_reference':intent.get('source_reference',receipt['id']),**snapshot(receipt),
                    **({'replace_voided_source':True} if intent.get('replace_voided_source') else {})}
        if kind=='accept':return {'request_key':intent['accept_key'],'version':intent['accept_version'],
            'customer_id':intent['customer_id'],'offer_type':'credit','expected_offer_cents':intent['expected_offer_cents'],
            'customer_agreed':True,'items_received':True}
        return {'request_key':intent['void_key'],'version':intent['void_version'],'reason':'Desktop operator cancelled this shared-credit draft before acceptance.'}

    def _perform(self,receipt,kind,expected):
        intent=receipt['shared_trade'];key=intent[kind+'_key'];row=self.cloud.trade_journal(key,expected_session_id=expected)
        body=self._body(receipt,kind)
        if row:
            if row['body']!=body:raise CloudError('The saved trade journal differs from the locked local intent. Contact the store owner; this action cannot be rewritten.',status=409,pending=True)
            if row.get('result') is not None:return row['result']
            status=self.cloud.trade_request(key,expected_session_id=expected)
            if status.get('found') is True and isinstance(status.get('result'),dict):return status['result']
            if status.get('found') is not False:raise CloudError('The service could not confirm the saved trade request. Keep this batch for recovery.',status=502,pending=True)
        if kind=='create':return self.cloud.trade_create(body,receipt['id'],expected_session_id=expected)
        method=self.cloud.trade_accept if kind=='accept' else self.cloud.trade_void
        return method(intent['trade_id'],body,receipt['id'],expected_session_id=expected)

    def _ensure_created(self,receipt,expected):
        intent=receipt['shared_trade']
        if intent.get('trade_id'):return receipt
        self._assert_snapshot(receipt);result=self._perform(receipt,'create',expected)
        ident=self.cloud.customer_id(result.get('trade_id'))
        with self.service.lock:
            receipt=self._read(receipt['id']);self._assert_snapshot(receipt)
            receipt['shared_trade'].update(trade_id=ident,state='draft',message='Hosted draft saved. Review the hosted offer before accepting credit.')
            self._save(receipt,'shared_trade_draft','Hosted trade draft confirmed; no credit issued.')
        self.cloud.trade_finish(intent['create_key'],expected_session_id=expected)
        return receipt

    def _verify(self,receipt,trade):
        intent=receipt['shared_trade'];self._assert_snapshot(receipt)
        expected=lines_for(receipt)
        actual=[{key:line.get(key) for key in ('id','identity','quantity','unit_intake_cents','included','reviewed')} for line in trade.get('lines',[])]
        if (trade.get('id')!=intent['trade_id'] or trade.get('customer_id')!=intent['customer_id']
            or trade.get('source_reference')!=intent.get('source_reference',receipt['id']) or trade.get('title')!=receipt['title'][:200]
            or actual!=expected):raise StationError('The hosted receipt differs from the locked local source. Ask an owner to review the hosted receipt or cancel its draft; no local acceptance will be inferred.')

    @staticmethod
    def _confirmation(trade):
        return fingerprint({key:trade.get(key) for key in ('id','customer_id','source_reference','version','lines_hash','credit_offer_cents','cash_offer_cents','intake_total_cents','state')})

    def _reconcile(self,receipt,trade,expected):
        self._verify(receipt,trade);intent=receipt['shared_trade'];evidence=trade.get('accepted_receipt');result=trade.get('acceptance_result')
        if (trade.get('state')!='accepted_credit' or not isinstance(evidence,dict) or not isinstance(result,dict)
            or fingerprint(evidence)!=trade.get('receipt_hash') or evidence.get('selected_offer')!='credit'
            or evidence.get('customer',{}).get('id')!=intent['customer_id'] or evidence.get('source_reference')!=intent.get('source_reference',receipt['id'])
            or evidence.get('trade_id')!=trade['id'] or evidence.get('receipt_version')!=trade.get('version')
            or evidence.get('currency')!='USD' or evidence.get('lines_hash')!=trade.get('lines_hash')
            or evidence.get('lines')!=[line for line in trade.get('lines',[]) if line.get('included')]
            or evidence.get('credit_entry_id')!=result.get('credit_entry_id') or not result.get('credit_entry_id')
            or evidence.get('accepted_amount_cents')!=trade.get('accepted_amount_cents')
            or result.get('accepted_amount_cents')!=trade.get('accepted_amount_cents')
            or evidence.get('credit_policy_state')!='unreviewed' or result.get('checkout_eligible') is not False):
            raise StationError('The hosted accepted-credit evidence cannot be verified. Keep this saved intent and ask the store owner to recover it; no second credit will be issued.')
        if intent.get('accept_key') and (trade.get('accepted_amount_cents')!=intent.get('expected_offer_cents') or evidence.get('version_before_acceptance')!=intent.get('accept_version')):
            raise StationError('The hosted acceptance differs from this computer’s saved agreement. Owner recovery is required; no local purchase will be guessed.')
        with self.service.lock:
            receipt=self._read(receipt['id']);self._assert_snapshot(receipt)
            if receipt.get('purchase') and receipt['purchase'].get('hosted_trade_id')!=trade['id']:raise StationError('A different local purchase is already recorded. The shared receipt needs owner recovery.')
            included=[card for card in receipt['cards'] if card.get('include',True)]
            # Keep accepted intake values independent of later retail label prices.
            for card in included:card['intake_value']=cents(card.get('intake_value',card['final_price']))/100
            receipt['purchase']={'scope':'hosted','type':'credit','amount':trade['accepted_amount_cents']/100,
                'at':evidence['accepted_at'],'intake_total':evidence['intake_total_cents']/100,
                'items':[{'row_id':card['row_id'],'qty':card['qty'],'intake_value':card['intake_value']} for card in included],
                'hosted_trade_id':trade['id'],'credit_entry_id':result['credit_entry_id'],'receipt_hash':trade['receipt_hash'],
                'store':intent['store'],'credit_policy_state':'unreviewed','checkout_eligible':False}
            receipt['shared_trade'].update(state='confirmed',server_version=trade['version'],receipt_hash=trade['receipt_hash'],
                credit_entry_id=result['credit_entry_id'],message='Shared credit confirmed once. Square checkout is not enabled; tax policy remains unreviewed.')
            self._save(receipt,'shared_trade_confirmed','Confirmed hosted trade credit mirrored locally. No local wallet entry or Square stock changed.')
        for key in ('create_key','accept_key'):
            if intent.get(key):self.cloud.trade_finish(intent[key],expected_session_id=expected)
        return receipt

    def _view(self,receipt,trade=None,account=None):
        return {'receipt':receipt,'trade':trade,'customer':account.get('customer') if account else None,
                'confirmationHash':self._confirmation(trade) if trade and trade.get('state')=='draft' else None}

    def prepare(self,ident,data,expected):
        with self.cloud.lock:
            saved=self._session(expected);customer=self.cloud.customer_id(data.get('customerId'))
            # A customer must be explicitly selected and independently confirmed.
            account=self.cloud.account(customer,expected_session_id=expected)
            if account.get('customer',{}).get('id')!=customer:raise CloudError('The shared customer response did not match your selection.',status=502)
            with self.service.lock:
                receipt=self._read(ident)
                if receipt.get('purchase') or receipt.get('tracked_credit'):raise StationError('An accepted local buy cannot be converted to shared credit. Start a new reviewed trade for new physical cards.')
                if locked(receipt):raise StationError('This batch already has a saved shared trade. Open it to continue or cancel.')
                if receipt.get('mode')!='trade' or receipt.get('inventory_status')!='not_received':raise StationError('Use an unreceived customer trade-in batch for shared credit.')
                if receipt.get('revision')!=data.get('revision'):raise StationError('This batch changed. Save and reopen it before choosing a shared customer.')
                source=snapshot(receipt);previous=receipt.get('shared_trade')
                if previous:
                    receipt.setdefault('shared_trade_history',[]).append({key:previous.get(key) for key in ('trade_id','store','state','receipt_hash')})
                receipt['shared_trade']={'state':'creating','store':saved['store'],'customer_id':customer,
                    'source_reference':previous.get('source_reference',receipt['id']) if previous else receipt['id']+':'+hashlib.sha256(str(receipt['source_hash']).encode()).hexdigest(),
                    'source_hash':fingerprint(source),'source_revision':receipt['revision'],'create_key':uuid.uuid4().hex,
                    'created_at':now(),'replace_voided_source':bool(previous and previous.get('state')=='cancelled'),
                    'message':'Shared trade intent saved. The source is locked while the hosted draft is confirmed.'}
                self._save(receipt,'shared_trade_intent','Shared trade intent saved before network mutation. Local source locked.')
            receipt=self._ensure_created(receipt,expected);trade=self.cloud.trade_get(receipt['shared_trade']['trade_id'],expected_session_id=expected)
            self._verify(receipt,trade)
            if trade.get('state')=='accepted_credit':receipt=self._reconcile(receipt,trade,expected)
            return self._view(receipt,trade,account)

    def status(self,ident,expected):
        with self.cloud.lock:
            receipt=self._read(ident);intent=receipt.get('shared_trade')
            if not intent:raise StationError('This batch has no saved shared trade.')
            self._session(expected,intent['store'])
            if intent['state']=='cancelled':
                trade=self.cloud.trade_get(intent['trade_id'],expected_session_id=expected)
                if trade.get('state')!='voided':raise StationError('The hosted cancellation cannot be confirmed. Ask the store owner to recover this receipt.')
                for key in ('create_key','void_key'):
                    if intent.get(key):self.cloud.trade_finish(intent[key],expected_session_id=expected)
                return self._view(receipt,trade)
            if intent['state']=='confirmed':
                trade=self.cloud.trade_get(intent['trade_id'],expected_session_id=expected)
                if trade.get('receipt_hash')!=intent.get('receipt_hash') or trade.get('state')!='accepted_credit':raise StationError('The hosted confirmation changed. Ask the store owner to recover this receipt.')
                for key in ('create_key','accept_key'):
                    if intent.get(key):self.cloud.trade_finish(intent[key],expected_session_id=expected)
                return self._view(receipt,trade)
            receipt=self._ensure_created(receipt,expected);intent=receipt['shared_trade']
            if intent['state']=='accepting':self._perform(receipt,'accept',expected)
            elif intent['state']=='voiding':self._perform(receipt,'void',expected)
            trade=self.cloud.trade_get(intent['trade_id'],expected_session_id=expected)
            if trade.get('state')=='accepted_credit':receipt=self._reconcile(receipt,trade,expected)
            elif trade.get('state')=='voided':
                with self.service.lock:
                    receipt=self._read(ident);receipt['shared_trade'].update(state='cancelled',server_version=trade['version'],message='Hosted draft cancellation confirmed. No shared credit was issued.')
                    self._save(receipt,'shared_trade_cancelled','Hosted trade draft confirmed void. Local source unlocked; no credit issued.')
                for key in ('create_key','void_key'):
                    if intent.get(key):self.cloud.trade_finish(intent[key],expected_session_id=expected)
            elif trade.get('state')!='draft':
                raise StationError('The hosted trade has a different final state. Ask the store owner to recover its saved receipt; it cannot become a new shared-credit acceptance.')
            else:self._verify(receipt,trade)
            account=self.cloud.account(intent['customer_id'],expected_session_id=expected)
            return self._view(receipt,trade,account)

    def accept(self,ident,data,expected):
        with self.cloud.lock:
            receipt=self._read(ident);intent=receipt.get('shared_trade')
            if not intent:raise StationError('Save and review a hosted trade draft first.')
            self._session(expected,intent['store'],manager=True)
            if intent['state'] in ('accepting','confirmed'):return self.status(ident,expected)
            if intent['state']!='draft':raise StationError('Recover the saved trade before accepting it.')
            if data.get('customerAgreed') is not True or data.get('itemsReceived') is not True:raise StationError('Confirm customer agreement and physical receipt of the included cards.')
            trade=self.cloud.trade_get(intent['trade_id'],expected_session_id=expected);self._verify(receipt,trade)
            if trade.get('state')=='accepted_credit':return self.status(ident,expected)
            if trade.get('state')!='draft' or data.get('confirmationHash')!=self._confirmation(trade):raise StationError('The hosted offer changed. Refresh and review it before accepting.')
            amount=trade.get('credit_offer_cents')
            if type(amount) is not int or amount<=0:raise StationError('The hosted shared-credit offer must be positive.')
            with self.service.lock:
                receipt=self._read(ident);self._assert_snapshot(receipt)
                receipt['shared_trade'].update(state='accepting',accept_key=uuid.uuid4().hex,accept_version=trade['version'],
                    expected_offer_cents=amount,agreement_at=now(),message='Acceptance saved. Confirming the same shared-credit action; keep this batch for recovery.')
                self._save(receipt,'shared_trade_accept_intent','Explicit customer agreement and physical item receipt saved before hosted credit acceptance.')
            self._perform(receipt,'accept',expected)
            return self.status(ident,expected)

    def cancel(self,ident,expected):
        with self.cloud.lock:
            receipt=self._read(ident);intent=receipt.get('shared_trade')
            if not intent:raise StationError('This batch has no saved shared trade to cancel.')
            self._session(expected,intent['store'])
            if intent['state'] in ('accepting','confirmed','voiding','cancelled'):return self.status(ident,expected)
            # Even an uncertain create is resolved with its original key before
            # cancelling. A missing request-status row alone cannot prove no I/O.
            receipt=self._ensure_created(receipt,expected);intent=receipt['shared_trade']
            trade=self.cloud.trade_get(intent['trade_id'],expected_session_id=expected)
            if trade.get('state')!='draft':return self.status(ident,expected)
            with self.service.lock:
                receipt=self._read(ident);receipt['shared_trade'].update(state='voiding',void_key=uuid.uuid4().hex,
                    void_version=trade['version'],message='Cancellation saved. The source remains locked until the hosted draft is confirmed void.')
                self._save(receipt,'shared_trade_void_intent','Explicit hosted-draft cancellation saved before network mutation.')
            self._perform(receipt,'void',expected)
            return self.status(ident,expected)
