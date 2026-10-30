"""Original desktop intake provenance and typed central physical-intake binding.

The original manifest is captured once, before operator edits or price changes.
It is not reconstructed from an older receipt's current mutable cards.
"""
import copy
import hashlib
import json
import re
import uuid

from intake_manifest import ManifestError, capture_manifest, source_lines, validate_manifest, manifest_digest, semantic_fingerprint
from station_cloud import CloudError
from station_store import StationError, now


IDENTITY_FIELDS = {
    'name':'card_name','game':'game','game_label':'game_label','set':'card_set',
    'number':'card_num','condition':'condition','printing':'printing','language':'language',
    'grading_company':'grader','grade':'grade','grade_qualifier':'grade_qualifier',
    'certification':'cert_number','year':'year','sport':'sport','league':'league','team':'team',
    'player':'player','serial_number':'serial_number','manufacturer':'manufacturer',
}


def original_identity(card):
    """Preserve source facts; do not guess missing game, grader or certificate."""
    identity={key:card.get(field,'') for key,field in IDENTITY_FIELDS.items()}
    graded_hint=re.search(r'\b(psa|bgs|cgc|sgc|tag|ace|hga|beckett|graded|slab)\b',str(identity['condition']),re.I)
    identity['kind']='slab' if graded_hint or any(identity[key] for key in
        ('grading_company','grade','grade_qualifier','certification')) else 'card'
    return {key:value for key,value in identity.items() if value or key in ('name','game','kind')}


def _attach(receipt,kind,rows,raw_csv_sha256=None):
    try:
        manifest=capture_manifest(kind,rows,raw_csv_sha256=raw_csv_sha256)
        by_row={number:line['source_line_key'] for line in source_lines(manifest) for number in line['source_rows']}
        keys=[by_row[card['source_manifest_row']] for card in receipt['cards']]
    except (ManifestError,KeyError,TypeError):
        receipt['source_manifest_error']='Original source facts exceed the hosted intake limits or need an exact game/slab identity. Keep this local batch; reimport a verified source or start a separate reviewed physical intake for hosted acceptance.'
        return
    receipt['source_manifest']=manifest
    receipt.pop('source_manifest_error',None)
    for card,key in zip(receipt['cards'],keys):card['source_line_key']=key


def capture_original(receipt,kind,raw_csv_sha256=None):
    """Called only at creation, never on save/load/restart or an old receipt."""
    rows=[]
    for ordinal,card in enumerate(receipt['cards'],1):
        number=card.get('source_row') if kind=='csv' else ordinal
        if type(number) is not int or number<1:number=ordinal
        card['source_manifest_row']=number
        rows.append({'identity':original_identity(card),'quantity':card['qty'],'source_row':number})
    _attach(receipt,kind,rows,raw_csv_sha256)


def append_original_manual(receipt,card):
    """Append the new original entry while preserving every earlier original row."""
    previous=receipt.get('source_manifest')
    if receipt.get('intake_claim') or receipt.get('shared_trade'):
        raise StationError('This physical intake already has a saved hosted reference. Start another batch for different physical cards.')
    if not previous or receipt.get('source_manifest_error'):
        receipt['source_manifest_error']='This older or mixed-source batch has no complete original manual manifest. Keep it local or start a new manual physical intake; its original facts cannot be reconstructed from current cards.'
        return
    try:manifest=validate_manifest(previous)
    except ManifestError:
        receipt['source_manifest_error']='The saved original intake manifest needs owner review. Do not recreate it from current edited cards.'
        return
    if manifest['kind']!='manual':
        receipt['source_manifest_error']='Manual cards added to a scanner batch need a separate physical intake. Keep this batch local or create a new manual batch for hosted acceptance.'
        return
    rows=copy.deepcopy(manifest['rows']);number=max((row.get('source_row',0) for row in rows),default=0)+1
    card['source_manifest_row']=number
    rows.append({'identity':original_identity(card),'quantity':card['qty'],'source_row':number})
    _attach(receipt,'manual',rows)


def required_manifest(receipt):
    if receipt.get('source_manifest_error'):raise StationError(receipt['source_manifest_error'])
    if not receipt.get('source_manifest'):
        raise StationError('This older batch has no original intake manifest. Reimport the verified original CSV or have the owner review a new central manual intake; current edited cards cannot be used as an original source.')
    try:return validate_manifest(receipt['source_manifest'])
    except ManifestError:raise StationError('The original intake manifest is invalid. Keep the batch for owner recovery; it cannot be rebuilt from current edited cards.') from None


def source_key_for(receipt,card):
    manifest=required_manifest(receipt)
    valid={line['source_line_key'] for line in source_lines(manifest)}
    key=card.get('source_line_key')
    if key not in valid:raise StationError('This card has no preserved original source line. Reimport the verified intake; do not invent a new source identity.')
    return key


def locked(receipt):
    intent=receipt.get('intake_claim')
    return bool(intent and intent.get('state')!='confirmed')


def centrally_bound(receipt):
    return bool(receipt.get('intake_claim') or receipt.get('shared_trade',{}).get('claim_binding_version')
                or (receipt.get('purchase') or {}).get('scope')=='hosted'
                or (receipt.get('purchase') or {}).get('physical_intake_id'))


def require_editable(receipt):
    if locked(receipt):raise StationError('This batch has a saved physical-intake action. Open its intake reference and recover the original action before changing the batch.')


class IntakeClaims:
    def __init__(self,service):self.service=service

    @property
    def cloud(self):return self.service.cloud

    def _session(self,expected,store=None):
        saved=self.cloud._saved();self.cloud._expected(saved,expected)
        if not saved or saved['expires_at']<=self.cloud.clock():raise CloudError('Sign in to the store that owns this physical intake.',status=401)
        if store and saved['store']!=store:raise CloudError('This saved physical intake belongs to another store. Sign in to that store; its identity cannot be transferred.',status=409)
        if saved['role'] not in ('owner','manager'):raise CloudError('An owner or manager must identify the central physical intake.',status=403)
        return saved

    def _legacy_guard(self,receipt):
        with self.service.store.db() as db:
            old=db.execute("SELECT 1 FROM operations WHERE receipt_id=? AND (key LIKE 'catalog:%' OR key LIKE 'stock:%') LIMIT 1",(receipt['id'],)).fetchone()
        if old or receipt.get('square_connection') or 'catalog_mode' in receipt:
            raise StationError('This batch is bound to its original Square receiving path. Recover its saved provider requests; it cannot be switched to hosted intake.')

    def _assert_source(self,receipt):
        manifest=required_manifest(receipt);intent=receipt['intake_claim']
        if manifest_digest(manifest)!=intent['source_manifest_hash']:
            raise StationError('The original intake changed after the saved claim intent. Keep the original server reference for owner recovery; do not create another intake.')
        return manifest

    def _verify(self,receipt,claim):
        manifest=self._assert_source(receipt)
        try:
            if not isinstance(claim,dict):raise ValueError()
            ident=self.cloud.customer_id(claim.get('physical_intake_id'))
            remote=validate_manifest(claim.get('source_manifest'))
            if (claim.get('manifest_hash')!=manifest_digest(remote)
                or claim.get('semantic_hash')!=semantic_fingerprint(remote)
                or semantic_fingerprint(manifest)!=claim['semantic_hash']
                or type(claim.get('version')) is not int or claim['version']<0
                or claim.get('state') not in ('open','consumed')
                or not isinstance(claim.get('physical_reference'),str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._:/-]{2,99}',claim['physical_reference'])
                or not isinstance(claim.get('policy'),dict)
                or claim['policy'].get('one_operation_per_claim') is not True
                or claim['policy'].get('new_acceptance_requires_fresh_mfa') is not True
                or claim['policy'].get('excluded_rows_reusable') is not False
                or claim['policy'].get('physical_identity_proven_by_hash') is not False):raise ValueError()
            if claim.get('bound_trade_id'):self.cloud.customer_id(claim['bound_trade_id'])
            expected=receipt['intake_claim'].get('physical_intake_id') or receipt['intake_claim'].get('selected_id')
            if expected and ident!=expected:raise ValueError()
            return ident
        except (ManifestError,ValueError,TypeError,CloudError):
            raise CloudError('The hosted physical-intake evidence does not match the saved original source. Keep the batch and reference for owner recovery.',status=502,pending=True) from None

    def _body(self,receipt):
        intent=receipt['intake_claim']
        body={'request_key':intent['request_key'],'physical_reference':intent['requested_reference'],
              'source_manifest':self._assert_source(receipt),'physical_delivery_verified':True}
        if intent.get('additional_delivery_of'):
            body.update(additional_delivery_of=intent['additional_delivery_of'],additional_delivery_confirmed=True,
                        additional_delivery_reason=intent['additional_delivery_reason'])
        return body

    def _perform(self,receipt,expected):
        intent=receipt['intake_claim']
        if intent.get('selected_id'):return self.cloud.intake_get(intent['selected_id'],expected_session_id=expected)
        key=intent['request_key'];body=self._body(receipt);row=self.cloud.intake_journal(key,expected_session_id=expected)
        if row:
            if row['receipt_id']!=receipt['id'] or row['body']!=body:
                raise CloudError('The encrypted intake journal differs from the saved local intent. Recover the original source; this request cannot be rewritten.',status=409,pending=True)
            if 'result' in row:return row['result']
            status=self.cloud.intake_request(key,expected_session_id=expected)
            if status.get('found') is True and isinstance(status.get('result'),dict):
                self.cloud.intake_remember(key,status['result'],expected_session_id=expected);return status['result']
            if status.get('found') is not False:raise CloudError('The server could not confirm the saved intake request. Keep the original reference and retry recovery.',status=502,pending=True)
        return self.cloud.intake_create(body,receipt['id'],expected_session_id=expected)

    def _mirror(self,receipt,claim,expected):
        self._verify(receipt,claim)
        with self.service.lock:
            receipt=self.service.active_receipt(receipt['id']);self._verify(receipt,claim)
            receipt['intake_claim'].update(state='confirmed',physical_intake_id=claim['physical_intake_id'],
                manifest_hash=claim['manifest_hash'],semantic_hash=claim['semantic_hash'],
                physical_reference=claim['physical_reference'],server_state=claim['state'],server_version=claim['version'],
                message='Central physical intake confirmed. Excluded copies remain unreceived; this reference can authorize only one accepted trade or receiving operation.')
            self.service.store.save_receipt(receipt,('intake_claim_confirmed','Central physical intake confirmed against the original source. No credit or Square stock changed.'))
        if receipt['intake_claim'].get('request_key'):self.cloud.intake_finish(receipt['intake_claim']['request_key'],expected_session_id=expected)
        return self.view(receipt,claim)

    def view(self,receipt,claim=None):
        manifest=required_manifest(receipt);groups=source_lines(manifest);selected=[row for row in receipt['cards'] if row.get('include',True)]
        return {'receipt':receipt,'claim':claim,'intakeSummary':{
            'originalRows':len(manifest['rows']),'originalCopies':sum(row['quantity'] for row in groups),
            'selectedRows':len(selected),'selectedCopies':sum(row['qty'] for row in selected),
            'excludedCopies':max(0,sum(row['quantity'] for row in groups)-sum(row['qty'] for row in selected)),
            'overBudget':sum(row['qty'] for row in selected)>sum(row['quantity'] for row in groups),
            'oneOperationPerClaim':True,'continuationSupported':False}}

    def prepare(self,ident,data,expected):
        with self.cloud.lock:
            saved=self._session(expected)
            with self.service.lock:existing=bool(self.service.active_receipt(ident).get('intake_claim'))
            if existing:return self.status(ident,expected)
            with self.service.lock:
                receipt=self.service.active_receipt(ident);self._legacy_guard(receipt);manifest=required_manifest(receipt)
                if receipt.get('purchase') or receipt.get('tracked_credit') or receipt.get('inventory_status')!='not_received':
                    raise StationError('Create or select the physical intake before accepting a purchase or receiving stock. Existing accepted records cannot be converted.')
                if receipt.get('intake_claim'):raise StationError('This physical intake changed. Open its saved reference to recover it.')
                if receipt.get('shared_trade'):raise StationError('Recover the saved hosted trade before binding a new intake; pending request bodies cannot be changed.')
                if receipt.get('revision')!=data.get('revision'):raise StationError('This batch changed. Save and reopen it before identifying its physical intake.')
                selected=data.get('physicalIntakeId');reference=data.get('physicalReference','')
                if selected:selected=self.cloud.customer_id(selected)
                elif not isinstance(reference,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._:/-]{2,99}',reference.strip()):
                    raise StationError('Enter or scan the physical intake tag/reference. Do not invent another reference for the same physical cards.')
                if data.get('physicalDeliveryVerified') is not True:
                    raise StationError('Confirm the physical intake tag identifies these actual cards before creating its central reference.')
                additional=data.get('additionalDeliveryOf');reason=data.get('additionalDeliveryReason','')
                if additional:
                    additional=self.cloud.customer_id(additional)
                    if selected or data.get('additionalDeliveryConfirmed') is not True or not isinstance(reason,str) or not 10<=len(reason.strip())<=200:
                        raise StationError('A separate physical delivery needs a linked original intake, a clear reason and explicit physical confirmation. The server requires recent enrolled MFA.')
                receipt['intake_claim']={'state':'selecting' if selected else 'creating','store':saved['store'],
                    'source_manifest_hash':manifest_digest(manifest),'created_at':now(),
                    'message':'Original physical-intake intent saved. Recover this same action before creating another reference.'}
                if selected:receipt['intake_claim']['selected_id']=selected
                else:receipt['intake_claim'].update(request_key=uuid.uuid4().hex,requested_reference=reference.strip())
                if additional:receipt['intake_claim'].update(additional_delivery_of=additional,additional_delivery_reason=reason.strip())
                self.service.store.save_receipt(receipt,('intake_claim_intent','Original source and physical reference saved before the encrypted journal and hosted request.'))
            return self._mirror(receipt,self._perform(receipt,expected),expected)

    def status(self,ident,expected):
        with self.cloud.lock:
            receipt=self.service.active_receipt(ident);intent=receipt.get('intake_claim')
            if not intent:raise StationError('Choose the central physical intake reference for this batch first.')
            self._session(expected,intent['store']);self._assert_source(receipt)
            if intent['state']=='confirmed':
                claim=self.cloud.intake_get(intent['physical_intake_id'],expected_session_id=expected);self._verify(receipt,claim)
                if intent.get('request_key'):self.cloud.intake_finish(intent['request_key'],expected_session_id=expected)
                return self.view(receipt,claim)
            return self._mirror(receipt,self._perform(receipt,expected),expected)

    def select_existing(self,ident,data,expected):
        """Explicitly use an authoritative equivalent reference after a conflict."""
        with self.cloud.lock:
            receipt=self.service.active_receipt(ident);intent=receipt.get('intake_claim')
            if not intent:return self.prepare(ident,data,expected)
            self._session(expected,intent['store']);self._assert_source(receipt)
            if intent.get('state')=='confirmed':return self.status(ident,expected)
            if intent.get('additional_delivery_of'):
                raise StationError('Recover the original separate-delivery request. An uncertain additional delivery cannot be changed into the original/base intake.')
            selected=self.cloud.customer_id(data.get('physicalIntakeId'))
            if data.get('physicalDeliveryVerified') is not True:raise StationError('Confirm this existing reference identifies the same physical cards.')
            if intent.get('request_key'):
                status=self.cloud.intake_request(intent['request_key'],expected_session_id=expected)
                if status.get('found') is True:
                    result=status.get('result')
                    if not isinstance(result,dict) or result.get('physical_intake_id')!=selected:
                        raise StationError('The original request confirmed a different central intake. Recover its exact result; do not switch references.')
                    self.cloud.intake_remember(intent['request_key'],result,expected_session_id=expected)
                    return self._mirror(receipt,result,expected)
                if status.get('found') is not False:raise CloudError('The original intake request is uncertain. Keep its journal and retry recovery.',status=502,pending=True)
            claim=self.cloud.intake_get(selected,expected_session_id=expected)
            candidate=copy.deepcopy(receipt);candidate['intake_claim']['selected_id']=selected;self._verify(candidate,claim)
            with self.service.lock:
                receipt=self.service.active_receipt(ident);self._assert_source(receipt);receipt['intake_claim']['selected_id']=selected
                self.service.store.save_receipt(receipt,('intake_claim_selected','Explicit existing central intake selected after checking the original request and source evidence.'))
            return self._mirror(receipt,claim,expected)

    def for_trade(self,receipt,expected,*,allow_consumed=False):
        self._legacy_guard(receipt);required_manifest(receipt)
        available={line['source_line_key']:line['quantity'] for line in source_lines(receipt['source_manifest'])};used={}
        for card in receipt['cards']:
            if not card.get('include',True):continue
            key=source_key_for(receipt,card);used[key]=used.get(key,0)+card['qty']
            if used[key]>available[key]:raise StationError('Selected quantities exceed the original intake. Review the original physical cards; do not create another source or delivery to inflate its quantity.')
        intent=receipt.get('intake_claim')
        if not intent or intent.get('state')!='confirmed':raise StationError('Create or open the central physical intake reference before saving a hosted trade. A local manual request ID is not a physical intake tag.')
        self._session(expected,intent['store'])
        claim=self.cloud.intake_get(intent['physical_intake_id'],expected_session_id=expected);self._verify(receipt,claim)
        if (claim.get('state')!='open' or claim.get('bound_trade_id')) and not (allow_consumed and claim.get('state')=='consumed' and claim.get('bound_trade_id')):
            raise CloudError('This physical intake was already consumed by an accepted trade or receiving operation. Open its original receipt; excluded copies cannot become a new delivery.',status=409,code='PHYSICAL_INTAKE_CONFLICT',physical_intake_id=claim['physical_intake_id'])
        return claim

    def additional(self,ident,data,expected):
        """A separate explicit linked physical delivery, never a retry shortcut."""
        with self.cloud.lock:
            receipt=self.service.active_receipt(ident);old=receipt.get('intake_claim')
            if not old or old.get('state')!='confirmed':raise StationError('Recover the original physical intake before attesting a separate delivery.')
            self._session(expected,old['store']);manifest=self._assert_source(receipt);self._legacy_guard(receipt)
            if (receipt.get('purchase') or receipt.get('tracked_credit') or receipt.get('inventory_status')!='not_received'
                or receipt.get('shared_trade',{}).get('state') not in (None,'cancelled')):
                raise StationError('An accepted or active trade cannot change its physical intake. Use a separate batch for genuinely different physical cards.')
            reference=data.get('physicalReference','');reason=data.get('additionalDeliveryReason','')
            if (not isinstance(reference,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._:/-]{2,99}',reference.strip())
                or not isinstance(reason,str) or not 10<=len(reason.strip())<=200
                or any(ord(char)<32 or ord(char)==127 for char in reason)
                or data.get('physicalDeliveryVerified') is not True or data.get('additionalDeliveryConfirmed') is not True):
                raise StationError('Enter a different physical tag, a 10–200 character reason, and confirm a genuinely separate physical delivery. Fresh enrolled MFA is required by the server.')
            if reference.strip().casefold()==old['physical_reference']:raise StationError('A separate physical delivery needs its own physical tag; keep the original reference for the original cards.')
            with self.service.lock:
                receipt=self.service.active_receipt(ident)
                if receipt['revision']!=data.get('revision') or receipt.get('intake_claim')!=old:raise StationError('The physical intake changed. Reopen it before attesting a separate delivery.')
                receipt.setdefault('intake_claim_history',[]).append({key:old.get(key) for key in ('store','physical_intake_id','physical_reference','manifest_hash','state')})
                receipt['intake_claim']={'state':'creating','store':old['store'],'source_manifest_hash':manifest_digest(manifest),
                    'request_key':uuid.uuid4().hex,'requested_reference':reference.strip(),'additional_delivery_of':old['physical_intake_id'],
                    'additional_delivery_reason':reason.strip(),'created_at':now(),
                    'message':'Separate physical delivery explicitly attested. Recover this exact linked request; do not switch it to the base intake.'}
                self.service.store.save_receipt(receipt,('intake_additional_intent','Linked separate physical-delivery intent and manager reason saved before journal or hosted I/O.'))
            return self._mirror(receipt,self._perform(receipt,expected),expected)
