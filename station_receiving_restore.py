"""Explicit local stock projections of verified original receiving plans.

No financial/claim/provider mutation or journal authority is used here. Network,
proof work and serialization precede the short session/SQLite commit guards.
"""
import copy
from datetime import datetime
import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
import uuid

from hosted_labels import fingerprint, mark
from intake_manifest import validate_manifest, manifest_digest, semantic_fingerprint, source_lines, ManifestError
from station_cloud import CloudError, ORIGIN
from station_financial_record import ordinary_record_proof
from station_store import StationError, now
from station_trade_recovery import recovered_marker, validate_recovered_projection
from station_shared_trade import IDENTITY_FIELDS
from station_receiving_restoration_proof import verify_restoration_source, RestorationProofError
from station_receiving_budget import operation_quantity_budget
from station_recovery_review import OriginalTradeReview
from hosted_labels import profile as label_profile
from receiving_evidence import provider_id
import station_receiving_registry as registry

MARKER='original_receiving_recovery'
LOCAL=re.compile(r'R-[A-Za-z0-9-]{1,100}')
HASH=re.compile('[a-f0-9]{64}')
BUNDLE={'sessionId','tenantId','store','role','trade','claim','history','operation','restorationSource'}


def _error(message='The original receiving preview changed. Preview it again; the agreement, import and original operation were kept.',code='ORIGINAL_RECEIVING_PREVIEW_CHANGED',status=409):
    return CloudError(message,status=status,code=code)


def _id(value):
    if type(value) is not str or not LOCAL.fullmatch(value):raise ValueError()
    return value


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value))!=value:raise ValueError()
    return value


def _integer(value,minimum=0,maximum=2**63-1):
    if type(value) is not int or not minimum<=value<=maximum:raise ValueError()
    return value


def _hash(value):
    if type(value) is not str or not HASH.fullmatch(value):raise ValueError()
    return value


def _raw_hash(value):return hashlib.sha256(value.encode('utf-8')).hexdigest()


class OriginalReceivingRestoration:
    def __init__(self,service,*,clock=None):
        self.service=service;self.clock=clock or time.monotonic
        self._previews={};self._lock=threading.RLock();self._namespace=None

    @property
    def cloud(self):return self.service.cloud

    def clear(self):
        with self._lock:self._previews.clear();self._namespace=None

    def _time(self):
        value=self.clock()
        if type(value) not in (int,float) or not math.isfinite(value):raise _error(code='ORIGINAL_RECEIVING_PREVIEW_EXPIRED')
        return value

    def _purge(self):
        current=self._time()
        self._previews={key:value for key,value in self._previews.items() if value['expires']>current}
        return current

    def _input(self,data,expected,kind):
        base={'financialReceiptId','financialRevision','receivingId','fromImportReceiptId','fromImportRevision','sessionId'}
        allowed=base|({'confirmationHash','offset','limit'} if kind=='rows' else set())
        if kind=='history':allowed={'financialReceiptId','financialRevision','sessionId'}
        if kind=='adopt':allowed=base|{'expectedServerRevision','expectedPlanHash','expectedRestorationSourceHash',
            'confirmationHash','restoreOriginalPlanConfirmed','samePhysicalDeliveryConfirmed','restoreExisting'}
        try:
            if type(data) is not dict or set(data)-allowed or type(expected) is not str or not expected or len(expected)>200:raise ValueError()
            if 'sessionId' in data and data['sessionId']!=expected:raise ValueError()
            _id(data.get('financialReceiptId'));_integer(data.get('financialRevision'),1)
            if kind!='history':_uuid(data.get('receivingId'))
            if data.get('fromImportReceiptId') is not None:
                _id(data['fromImportReceiptId']);_integer(data.get('fromImportRevision'),1)
                if data['fromImportReceiptId']==data['financialReceiptId']:raise ValueError()
            elif data.get('fromImportRevision') is not None:raise ValueError()
            if kind in ('rows','adopt'):_hash(data.get('confirmationHash'))
            if kind=='rows':_integer(data.get('offset',0));_integer(data.get('limit',100),1,100)
            if kind=='adopt':
                _integer(data.get('expectedServerRevision'));_hash(data.get('expectedPlanHash'));_hash(data.get('expectedRestorationSourceHash'))
                if data.get('restoreOriginalPlanConfirmed') is not True:raise ValueError()
                for key in ('restoreExisting','samePhysicalDeliveryConfirmed'):
                    if key in data and type(data[key]) is not bool:raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise _error('Use the original receiving preview and its exact confirmation; prices, quantities and profiles cannot be overridden.',code='ORIGINAL_RECEIVING_INPUT_INVALID',status=400) from None

    def _raw(self,ident,*,active=True):
        with self.service.store.db() as db:
            row=db.execute('SELECT id,body,deleted_at FROM receipts WHERE id=?',(ident,)).fetchone()
        if not row or active and row['deleted_at'] is not None:raise _error('The selected local record is no longer active. Preview it again.')
        try:
            receipt=json.loads(row['body'])
            if type(receipt) is not dict or receipt.get('id')!=row['id'] or receipt.get('deleted_at')!=row['deleted_at']:raise ValueError()
            _integer(receipt.get('revision'),1)
        except (ValueError,TypeError,KeyError):raise _error() from None
        return {'receipt':receipt,'raw':row['body'],'deleted':row['deleted_at'],'hash':_raw_hash(row['body'])}

    def _authority(self,bundle,expected,*,history=False):
        try:
            fields=BUNDLE-{'operation','restorationSource'} if history else BUNDLE
            if type(bundle) is not dict or set(bundle)!=fields or bundle['sessionId']!=expected or bundle['role'] not in ('owner','manager'):raise ValueError()
            _uuid(bundle['tenantId'])
            if type(bundle['store']) is not str or not 1<=len(bundle['store'])<=200:raise ValueError()
            if (self.service.profile.safe_snapshot()['profileMode']!='hosted'
                or self.service.profile.binding()!={'tenant_id':bundle['tenantId'],'service_origin':ORIGIN}):raise ValueError()
        except (ValueError,TypeError,KeyError,AttributeError):raise _error('Use the original store’s bound hosted profile and current owner or manager session.',code='ORIGINAL_RECEIVING_PROFILE_REQUIRED') from None
        namespace=(ORIGIN,bundle['tenantId'],bundle['store'],expected)
        with self._lock:
            if namespace!=self._namespace:self._previews.clear();self._namespace=namespace

    def _financial(self,local,bundle):
        receipt=local['receipt'];trade=bundle['trade'];claim=bundle['claim']
        marker=recovered_marker(receipt)
        if marker is None:
            proof=ordinary_record_proof(receipt,trade)
            token=proof['financialToken']
        else:
            verified=validate_recovered_projection(receipt)
            if marker['tenantId']!=bundle['tenantId'] or marker['tradeId']!=trade['id'] or marker['receiptHash']!=trade['receipt_hash']:raise _error()
            self.service.shared_trades._terminal(receipt,trade)
            token=fingerprint({'marker':marker,'source':verified['source'],'receiptHash':trade['receipt_hash']})
        intake=receipt.get('intake_claim') or {};purchase=receipt.get('purchase') or {}
        if (purchase.get('scope')!='hosted' or purchase.get('hosted_trade_id')!=trade['id']
            or purchase.get('receipt_hash')!=trade['receipt_hash'] or purchase.get('store')!=bundle['store']
            or intake.get('physical_intake_id')!=claim['physical_intake_id'] or intake.get('manifest_hash')!=claim['manifest_hash']
            or semantic_fingerprint(validate_manifest(receipt.get('source_manifest')))!=claim['semantic_hash']):raise _error()
        return token

    def _import(self,data,bundle):
        if data.get('fromImportReceiptId') is None:return None
        local=self._raw(data['fromImportReceiptId']);receipt=local['receipt']
        if receipt['revision']!=data['fromImportRevision']:raise _error()
        if (any(key in receipt for key in ('shared_trade','hosted_receive','tracked_credit',MARKER,'original_trade_recovery'))
            or receipt.get('purchase') is not None):
            raise _error('The linked intake already has saved work. Keep its original action; it cannot be replaced.',code='ORIGINAL_RECEIVING_IMPORT_LOCKED')
        manifest=validate_manifest(receipt.get('source_manifest'));intake=receipt.get('intake_claim')
        if semantic_fingerprint(manifest)!=bundle['claim']['semantic_hash']:raise _error(code='ORIGINAL_RECEIVING_SOURCE_MISMATCH')
        if intake and (intake.get('state')!='confirmed' or intake.get('physical_intake_id')!=bundle['claim']['physical_intake_id']):raise _error(code='ORIGINAL_RECEIVING_SOURCE_MISMATCH')
        return local

    def _existing(self,bundle,facts):
        state=registry.read(self.service.store,bundle['tenantId'],facts['receivingId'])
        if type(state) is not dict or set(state)!={'link','snapshots','ordinary_references'}:raise _error(code='ORIGINAL_RECEIVING_REGISTRY_UNAVAILABLE')
        if state['ordinary_references']:
            raise _error('A local record already references this operation. Open or review that record; restoration will not choose or rewrite an ordinary mirror.',code='ORIGINAL_RECEIVING_LOCAL_AMBIGUOUS')
        link=state['link'];snapshots=state['snapshots']
        if link and (link['financial_receipt_id']!=self._selection['financialReceiptId'] or link['server_revision']>facts['serverRevision']):raise _error()
        selected=[row for row in snapshots if row['server_revision']==facts['serverRevision']]
        if len(selected)>1:raise _error(code='ORIGINAL_RECEIVING_LOCAL_AMBIGUOUS')
        if not selected:return state,None,None
        snapshot=selected[0];local=self._raw(snapshot['receipt_id'],active=False)
        self._validate_child(local['receipt'],snapshot,bundle)
        if snapshot['plan_hash']!=facts['planHash'] or not link or link['active_receipt_id']!=local['receipt']['id'] or link['server_revision']!=facts['serverRevision']:raise _error()
        return state,snapshot,local

    def _validate_child(self,receipt,snapshot,bundle):
        try:
            source=json.loads(snapshot['plan_snapshot']);operation=json.loads(snapshot['operation_snapshot'])
            if _raw_hash(snapshot['operation_snapshot'])!=snapshot['operation_snapshot_hash']:raise ValueError()
            old=verify_restoration_source(source,operation,bundle['claim'],bundle['trade']['accepted_receipt'])
            if (snapshot['service_origin']!=ORIGIN or snapshot['tenant_id']!=bundle['tenantId']
                or snapshot['receiving_id']!=old['receivingId'] or snapshot['server_revision']!=old['serverRevision']
                or type(snapshot['server_revision']) is not int or snapshot['receipt_id']!=receipt['id']
                or snapshot['plan_hash']!=old['planHash'] or snapshot['restoration_source_hash']!=old['restorationSourceHash']
                or registry.canonical(source)!=snapshot['plan_snapshot'] or registry.canonical(operation)!=snapshot['operation_snapshot']):raise ValueError()
            marker=receipt[MARKER]
            expected={'version':1,'serviceOrigin':ORIGIN,'tenantId':bundle['tenantId'],
                'financialReceiptId':self._selection['financialReceiptId'],'tradeId':bundle['trade']['id'],
                'receiptHash':bundle['trade']['receipt_hash'],'physicalIntakeId':bundle['claim']['physical_intake_id'],
                'physicalManifestHash':bundle['claim']['manifest_hash'],'receivingId':old['receivingId'],
                'serverRevision':old['serverRevision'],'planHash':old['planHash'],
                'restorationSourceHash':old['restorationSourceHash'],'proofHash':old['proofHash'],
                'snapshotReference':old['receivingId']+':'+str(old['serverRevision'])}
            if (not isinstance(marker,dict) or set(marker)!=set(expected)|{'recoveredAt'}
                or type(marker.get('version')) is not int or marker['version']!=1
                or type(marker.get('serverRevision')) is not int or not 0<=marker['serverRevision']<=2**63-1
                or fingerprint({key:marker[key] for key in expected})!=fingerprint(expected)
                or type(marker.get('recoveredAt')) is not str or len(marker['recoveredAt'])>60
                or datetime.fromisoformat(marker['recoveredAt']).tzinfo is None):raise ValueError()
            validate_manifest(receipt.get('source_manifest'))
            if (receipt.get('purchase') is not None or any(key in receipt for key in ('shared_trade','tracked_credit','original_trade_recovery'))
                or receipt.get('mode')!='labels' or receipt.get('requires_purchase') is not False or receipt.get('inventory_intent') is not None
                or fingerprint(receipt.get('source_manifest'))!=fingerprint(bundle['claim']['source_manifest'])):raise ValueError()
            rebuilt=self._build(bundle,old,receipt['id'],existing=receipt)
            if fingerprint(receipt['cards'])!=fingerprint(rebuilt['cards']):raise ValueError()
            for key in ('source_hash','inventory_status','hosted_receive','intake_claim','hosted_label_profile',
                        'intake_source','pricing_game','mixed_games'):
                if fingerprint(receipt.get(key))!=fingerprint(rebuilt.get(key)):raise ValueError()
        except (ValueError,TypeError,KeyError,RestorationProofError):raise _error('The existing stock projection or its original snapshot changed. Keep it for owner review.',code='ORIGINAL_RECEIVING_PROJECTION_INVALID') from None

    def _context(self,data,expected):
        financial=self._raw(data['financialReceiptId'])
        if financial['receipt']['revision']!=data['financialRevision']:raise _error()
        trade_id=(financial['receipt'].get('purchase') or {}).get('hosted_trade_id');_uuid(trade_id)
        bundle=self.cloud.read_receiving_restoration(trade_id,data['receivingId'],expected_session_id=expected)
        self._authority(bundle,expected)
        token=self._financial(financial,bundle)
        facts=verify_restoration_source(bundle['restorationSource'],bundle['operation'],bundle['claim'],bundle['trade']['accepted_receipt'])
        linked=self._import(data,bundle)
        state,snapshot,child=self._existing(bundle,facts)
        return {'bundle':bundle,'proof':facts,'financial':financial,'financialToken':token,'import':linked,
                'registry':state,'snapshot':snapshot,'child':child}

    def output_context(self,stock_receipt_id,revision,expected):
        """Read a current received stock projection without adopting or refreshing it.

        The stock marker supplies its original financial/operation identities.
        Output callers cannot select replacements. They must recheck this context
        after rendering and use the exact raw comparisons at publication time.
        """
        try:
            _id(stock_receipt_id);_integer(revision,1)
            if type(expected) is not str or not 1<=len(expected)<=200:raise ValueError()
        except (ValueError,TypeError):
            raise _error('Open the current original-stock record before preparing labels.',
                         code='ORIGINAL_STOCK_OUTPUT_INPUT_INVALID',status=400) from None
        with self.cloud.lock:
            selected=self._raw(stock_receipt_id);receipt=selected['receipt'];marker=receipt.get(MARKER)
            try:
                if receipt['revision']!=revision or type(marker) is not dict:raise ValueError()
                financial_id=_id(marker.get('financialReceiptId'));receiving_id=_uuid(marker.get('receivingId'))
                if financial_id==stock_receipt_id:raise ValueError()
                financial=self._raw(financial_id)
            except (ValueError,TypeError,KeyError):
                raise _error('The original-stock selection changed. Reopen its verified record.',
                             code='ORIGINAL_STOCK_OUTPUT_SELECTION_INVALID') from None
            data={'financialReceiptId':financial_id,'financialRevision':financial['receipt']['revision'],
                  'receivingId':receiving_id,'sessionId':expected}
            self._selection=data
            context=self._context(data,expected);child=context['child'];proof=context['proof']
            if (not child or child['receipt']['id']!=stock_receipt_id or child['deleted'] is not None
                or child['raw']!=selected['raw'] or context['financial']['raw']!=financial['raw']
                or proof['received'] is not True or proof['labelsVerified'] is not True
                or any(marker.get(key)!=proof[key] for key in ('receivingId','serverRevision','planHash',
                         'restorationSourceHash','proofHash'))):
                raise _error('Labels need the current received original-stock projection. Review and explicitly refresh the stock record first; inventory and the agreement were kept.',
                             code='ORIGINAL_STOCK_OUTPUT_REFRESH_REQUIRED')
            self._reauthorize(context,expected)
            with self.cloud._session_guard(),self.service.lock:
                self._final(context,expected)
            return context

    def _cache_facts(self,context,data,expected,proposed_id):
        bundle=context['bundle'];proof=context['proof'];child=context['child'];linked=context['import'];state=context['registry']
        return {'serviceOrigin':ORIGIN,'tenantId':bundle['tenantId'],'store':bundle['store'],'role':bundle['role'],'sessionId':expected,
            'financialReceiptId':data['financialReceiptId'],'financialRevision':data['financialRevision'],
            'financialRawHash':context['financial']['hash'],'financialToken':context['financialToken'],
            'tradeId':bundle['trade']['id'],'receiptHash':bundle['trade']['receipt_hash'],
            'physicalIntakeId':bundle['claim']['physical_intake_id'],'physicalManifestHash':bundle['claim']['manifest_hash'],
            'fromImportReceiptId':data.get('fromImportReceiptId'),'fromImportRevision':data.get('fromImportRevision'),
            'importRawHash':linked['hash'] if linked else None,
            'receivingId':proof['receivingId'],'serverRevision':proof['serverRevision'],'planHash':proof['planHash'],
            'restorationSourceHash':proof['restorationSourceHash'],'stateBasisHash':proof['stateBasisHash'],
            'proofHash':proof['proofHash'],'providerHash':fingerprint(proof['provider']),'profileHash':fingerprint(proof['labelProfile']),
            'existingReceiptId':child['receipt']['id'] if child else None,'existingRawHash':child['hash'] if child else None,
            'existingDeletedAt':child['deleted'] if child else None,
            'linkRevision':state['link']['revision'] if state['link'] else None,
            'snapshotSourceRawHash':_raw_hash(context['snapshot']['plan_snapshot']) if context['snapshot'] else None,
            'snapshotOperationRawHash':_raw_hash(context['snapshot']['operation_snapshot']) if context['snapshot'] else None,
            'proposedReceiptId':proposed_id}

    def _expected_sources(self,context,*,include_child=False):
        values=[context['financial']]
        if context['import']:values.append(context['import'])
        if include_child and context['child']:values.append(context['child'])
        return tuple((value['receipt']['id'],value['raw'],value['deleted']) for value in values)

    def _final(self,context,expected):
        bundle=context['bundle']
        saved=self.cloud.assert_recovery_session_unlocked(expected,bundle['tenantId'],bundle['store'])
        if saved.get('role') not in ('owner','manager'):raise _error(code='ORIGINAL_RECEIVING_SESSION_CHANGED')
        # Exact raw-body comparisons only: proof hashing and DTO serialization
        # have already completed outside this short guard.
        with self.service.store.db() as db:
            for ident,raw,deleted in self._expected_sources(context,include_child=True):
                row=db.execute('SELECT body,deleted_at FROM receipts WHERE id=?',(ident,)).fetchone()
                if not row or tuple(row)!=(raw,deleted):raise _error()
            link=context['registry']['link']
            actual=db.execute('''SELECT revision,active_receipt_id,server_revision,plan_hash,financial_receipt_id FROM receiving_recovery_links
                WHERE service_origin=? AND tenant_id=? AND receiving_id=?''',
                (ORIGIN,bundle['tenantId'],context['proof']['receivingId'])).fetchone()
            expected_link=(link['revision'],link['active_receipt_id'],link['server_revision'],link['plan_hash'],link['financial_receipt_id']) if link else None
            if (tuple(actual) if actual else None)!=expected_link:raise _error()
            fields=('service_origin','tenant_id','receiving_id','server_revision','receipt_id','plan_hash',
                    'restoration_source_hash','plan_snapshot','operation_snapshot','operation_snapshot_hash')
            snapshot=db.execute('SELECT '+','.join(fields)+''' FROM receiving_recovery_snapshots
                WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND server_revision=?''',
                (ORIGIN,bundle['tenantId'],context['proof']['receivingId'],context['proof']['serverRevision'])).fetchone()
            expected_snapshot=tuple(context['snapshot'][key] for key in fields) if context['snapshot'] else None
            if (tuple(snapshot) if snapshot else None)!=expected_snapshot:raise _error()

    def _reauthorize(self,context,expected):
        """Fresh remote read after heavy work, always outside local guards."""
        bundle=context['bundle'];authority=self.cloud.record_authority(expected_session_id=expected)
        if authority.get('role') not in ('owner','manager'):
            raise _error('An owner or manager must open original receiving records.',code='ORIGINAL_RECEIVING_ROLE_REQUIRED',status=403)
        if any(authority.get(key)!=bundle[key] for key in ('sessionId','tenantId','store','role')):raise _error()

    def _cached(self,data,expected):
        with self._lock:
            self._purge();value=copy.deepcopy(self._previews.get(data['confirmationHash']))
        if not value or value['facts']['sessionId']!=expected:raise _error('The original receiving preview expired or changed. Reopen it.',code='ORIGINAL_RECEIVING_PREVIEW_EXPIRED')
        facts=value['facts']
        for key in ('financialReceiptId','financialRevision','receivingId','fromImportReceiptId','fromImportRevision'):
            if data.get(key)!=facts.get(key):raise _error()
        return value

    def _match(self,current,previous,*,pending=None):
        retry=bool(pending and current['existingReceiptId']==previous['proposedReceiptId']
            and current['existingRawHash']==pending['childRawHash'] and current['existingDeletedAt'] is None
            and current['linkRevision']==pending['linkRevision']
            and current['snapshotSourceRawHash']==pending['sourceRawHash']
            and current['snapshotOperationRawHash']==pending['operationRawHash'])
        mutable={'existingReceiptId','existingRawHash','existingDeletedAt','linkRevision','snapshotSourceRawHash','snapshotOperationRawHash'} if retry else set()
        if any(current[key]!=value for key,value in previous.items() if key not in mutable):raise _error()

    def _live(self,confirmation):
        self._purge()
        if confirmation not in self._previews:raise _error(code='ORIGINAL_RECEIVING_PREVIEW_EXPIRED')

    def _build(self,bundle,proof,ident,*,existing=None):
        stamp=now();cards=[]
        for row in proof['stockRows']:
            card={field:row['identity'].get(key,'') for key,field in IDENTITY_FIELDS.items()}
            old=next((card for card in (existing or {}).get('cards',[]) if card.get('row_id')==row['reviewLineId']),None)
            card.update(row_id=row['reviewLineId'],source_line_key=row['sourceLineKey'],qty=row['quantity'],
                intake_value=(row['unitIntakeCents'] or 0)/100,final_price=row['sellingCents']/100,
                include=True,reviewed=True,price_confirmed=row['sellingCents']>0 or row['zeroPriceConfirmed'],price_missing=False,
                entry_source='hosted_receiving_restoration',storage_location=old.get('storage_location','') if old else '',
                sku=row['targetSku'],sku_override='',operator_price=True)
            if proof['received'] and proof['labelsVerified']:
                card['hosted_sku_resolution']=mark(card,receiving_id=proof['receivingId'],receipt_hash=proof['terminalReceiptHash'],
                    review_line_id=row['reviewLineId'],source_line_key=row['sourceLineKey'],identity_hash=row['identityHash'],
                    sku=row['targetSku'],selling_cents=row['sellingCents'],confirmed_quantity=row['quantity'])
            cards.append(card)
        marker={'version':1,'serviceOrigin':ORIGIN,'tenantId':bundle['tenantId'],
            'financialReceiptId':self._selection['financialReceiptId'],'tradeId':bundle['trade']['id'],
            'receiptHash':bundle['trade']['receipt_hash'],'physicalIntakeId':bundle['claim']['physical_intake_id'],
            'physicalManifestHash':bundle['claim']['manifest_hash'],'receivingId':proof['receivingId'],
            'serverRevision':proof['serverRevision'],'planHash':proof['planHash'],'restorationSourceHash':proof['restorationSourceHash'],
            'proofHash':proof['proofHash'],'snapshotReference':proof['receivingId']+':'+str(proof['serverRevision']),
            'recoveredAt':existing[MARKER]['recoveredAt'] if existing else stamp}
        result=copy.deepcopy(existing) if existing else {}
        games={row['identity']['game'] for row in proof['stockRows']}
        result.update(id=ident,source_hash='hosted-original-receiving:'+proof['receivingId']+':'+str(proof['serverRevision']),
            source_name='Original hosted receiving plan',title=existing.get('title') if existing else 'Original stock plan',
            created=existing.get('created') if existing else stamp,updated=stamp,revision=existing['revision']+1 if existing else 1,
            mode='labels',cards=cards,source_manifest=copy.deepcopy(bundle['claim']['source_manifest']),
            inventory_status='received' if proof['received'] else 'not_received',inventory_intent=None,
            pricing_game=next(iter(games)) if len(games)==1 else '',mixed_games=len(games)>1,
            purchase=None,requires_purchase=False,prepared=False,pickup_pending=False,intake_source='hosted_receiving_restoration',
            intake_summary={},files={},error=proof['labelError'] or '',channel=existing.get('channel','tcgplayer') if existing else 'tcgplayer',
            notes=existing.get('notes','') if existing else '',customer_reference='',storage_strategy='bin',storage_location=existing.get('storage_location','') if existing else '',
            cash_percent=35,credit_percent=50,hosted_label_profile=copy.deepcopy(proof['labelProfile']))
        result[MARKER]=marker
        result['intake_claim']={'state':'confirmed','store':bundle['store'],'source_manifest_hash':bundle['claim']['manifest_hash'],
            'physical_intake_id':bundle['claim']['physical_intake_id'],'manifest_hash':bundle['claim']['manifest_hash'],
            'semantic_hash':bundle['claim']['semantic_hash'],'server_state':bundle['claim']['state'],'server_version':bundle['claim']['version'],
            'physical_reference':bundle['claim']['physical_reference'],'bound_trade_id':bundle['claim']['bound_trade_id']}
        result['hosted_receive']={'version':2,'restoration_projection':True,'receiving_id':proof['receivingId'],
            'server_revision':proof['serverRevision'],'plan_hash':proof['planHash'],'store':bundle['store'],
            'state':'confirmed' if proof['received'] else 'cancelled_before_write' if proof['state']=='cancelled_before_write' else 'planned' if proof['state']=='ready' else 'needs_attention',
            'server_state':proof['state'],'label_profile':copy.deepcopy(proof['labelProfile']),
            'snapshot_reference':marker['snapshotReference'],'message':'Exact original hosted plan; no new agreement, credit or provider action.'}
        return result

    def history(self,data,expected):
        self._input(data,expected,'history')
        with self.cloud.lock:
            local=self._raw(data['financialReceiptId'])
            if local['receipt']['revision']!=data['financialRevision']:raise _error()
            ident=(local['receipt'].get('purchase') or {}).get('hosted_trade_id');_uuid(ident)
            bundle=self.cloud.original_trade_inventory(ident,expected_session_id=expected)
            self._authority(bundle,expected,history=True);self._financial(local,bundle)
            manifest=validate_manifest(bundle['claim']['source_manifest'])
            if manifest_digest(manifest)!=bundle['claim']['manifest_hash'] or semantic_fingerprint(manifest)!=bundle['claim']['semantic_hash']:raise _error()
            operation=OriginalTradeReview.verify_history(bundle);summaries=[]
            if operation:
                chosen=label_profile(operation.get('client_label_profile'));provider=operation.get('provider')
                if (type(provider) is not dict or set(provider)!={'merchant_id','application_id','environment','location_id'}
                    or provider['environment'] not in ('production','sandbox')):raise _error()
                for key in ('merchant_id','application_id','location_id'):provider_id(provider[key])
                if operation['workflow_kind']=='accepted_trade':
                    claim=bundle['claim'];context={'source_manifest':manifest,'intake_claim':{'state':'confirmed',
                        'physical_intake_id':claim['physical_intake_id'],'manifest_hash':claim['manifest_hash'],
                        'source_manifest_hash':claim['manifest_hash'],'semantic_hash':claim['semantic_hash'],'bound_trade_id':claim['bound_trade_id']}}
                    counts=operation_quantity_budget(context,operation,bundle['trade']['accepted_receipt']);selected=counts['selectedCopies']
                else:
                    original={row['source_line_key']:row['quantity'] for row in source_lines(manifest)};selected_map={};seen=set()
                    targets=operation.get('targets')
                    if type(targets) is not list or not 1<=len(targets)<=1000:raise _error()
                    for row in targets:
                        review=_uuid(row.get('review_line_id'));key=_hash(row.get('source_line_key'));quantity=_integer(row.get('quantity'),1,100000)
                        if review in seen or key not in original:raise _error()
                        seen.add(review);selected_map[key]=selected_map.get(key,0)+quantity
                        if selected_map[key]>original[key]:raise _error()
                    excluded={key:quantity-selected_map.get(key,0) for key,quantity in original.items()}
                    if fingerprint(operation.get('selected_quantities'))!=fingerprint(selected_map) or fingerprint(operation.get('excluded_quantities'))!=fingerprint(excluded):raise _error()
                    selected=sum(selected_map.values())
                _integer(operation['revision']);summaries=[{'receivingId':operation['receiving_id'],'serverRevision':operation['revision'],
                    'planHash':operation['plan_hash'],'state':operation['state'],'labelProfile':chosen,'provider':copy.deepcopy(provider),'selectedCopies':selected}]
            result={'financialReceiptId':data['financialReceiptId'],'financialRevision':data['financialRevision'],
                    'operations':summaries,'lookupComplete':True}
            self._reauthorize({'bundle':bundle},expected)
            with self.cloud._session_guard(),self.service.lock:
                saved=self.cloud.assert_recovery_session_unlocked(expected,bundle['tenantId'],bundle['store'])
                if saved['role'] not in ('owner','manager'):raise _error(status=403)
                with self.service.store.db() as db:
                    row=db.execute('SELECT body,deleted_at FROM receipts WHERE id=?',(data['financialReceiptId'],)).fetchone()
                    if not row or tuple(row)!=(local['raw'],None):raise _error()
                return result

    def preview(self,data,expected):
        self._input(data,expected,'preview')
        with self.cloud.lock:
            self._selection=data
            context=self._context(data,expected);proof=context['proof'];bundle=context['bundle'];child=context['child']
            proposed=child['receipt']['id'] if child else 'R-'+time.strftime('%Y%m%d')+'-'+uuid.uuid4().hex[:12].upper()
            facts=self._cache_facts(context,data,expected,proposed)
            confirmation=fingerprint({'domain':'stock-and-sleeve:original-receiving-preview:v1','random':secrets.token_hex(32),'facts':facts})
            preview={'version':1,'financialReceiptId':data['financialReceiptId'],'tradeId':bundle['trade']['id'],
                'receiptHash':bundle['trade']['receipt_hash'],'receivingId':proof['receivingId'],'serverRevision':proof['serverRevision'],
                'planHash':proof['planHash'],'restorationSourceHash':proof['restorationSourceHash'],'state':proof['state'],
                'proofHash':proof['proofHash'],'workflowKind':proof['workflowKind'],
                'admission':proof['admission'],'provider':proof['provider'],'labelProfile':proof['labelProfile'],
                'counts':proof['quantityBudget'],'rows':proof['stockRows'][:100],'rowsOffset':0,'rowsLimit':100,'totalRows':len(proof['stockRows']),
                'existingProjectionId':child['receipt']['id'] if child else None,'restoreRequired':bool(child and child['deleted']),
                'samePhysicalDeliveryRequired':context['import'] is not None,'allowedActions':[],
                'received':proof['received'],'labelsVerified':proof['labelsVerified'],'labelError':proof['labelError']}
            result={'preview':preview,'confirmationHash':confirmation,'expiresIn':300}
            self._reauthorize(context,expected)
            with self.cloud._session_guard(),self.service.lock:
                self._final(context,expected)
                with self._lock:
                    current=self._purge()
                    while len(self._previews)>=16:self._previews.pop(next(iter(self._previews)))
                    self._previews[confirmation]={'facts':facts,'expires':current+300}
                return result

    def rows(self,data,expected):
        self._input(data,expected,'rows')
        with self.cloud.lock:
            self._selection=data;previous=self._cached(data,expected)['facts'];context=self._context(data,expected)
            current=self._cache_facts(context,data,expected,previous['proposedReceiptId']);self._match(current,previous)
            offset=data.get('offset',0);limit=data.get('limit',100);proof=context['proof']
            result={'version':1,'receivingId':proof['receivingId'],'serverRevision':proof['serverRevision'],'proofHash':proof['proofHash'],
                'offset':offset,'limit':limit,'totalRows':len(proof['stockRows']),'rows':proof['stockRows'][offset:offset+limit]}
            self._reauthorize(context,expected)
            with self.cloud._session_guard(),self.service.lock:
                self._final(context,expected)
                with self._lock:self._live(data['confirmationHash'])
                return result

    def adopt(self,data,expected):
        self._input(data,expected,'adopt')
        with self.cloud.lock:
            self._selection=data;cached=self._cached(data,expected);previous=cached['facts']
            for requested,stored in (('expectedServerRevision','serverRevision'),('expectedPlanHash','planHash'),('expectedRestorationSourceHash','restorationSourceHash')):
                if data[requested]!=previous[stored]:raise _error()
            if previous['fromImportReceiptId'] is not None and data.get('samePhysicalDeliveryConfirmed') is not True:
                raise _error('Confirm that this linked intake is the same physical delivery.',code='ORIGINAL_RECEIVING_PHYSICAL_CONFIRMATION_REQUIRED')
            restore=data.get('restoreExisting',False)
            if bool(previous['existingDeletedAt'])!=restore:raise _error('Explicitly restore the same selected deleted stock record only.',code='ORIGINAL_RECEIVING_RESTORE_REQUIRED')
            context=self._context(data,expected);current=self._cache_facts(context,data,expected,previous['proposedReceiptId'])
            self._match(current,previous,pending=cached.get('pending'))
            child=context['child'];proof=context['proof'];bundle=context['bundle'];link=context['registry']['link']
            existing=child['receipt'] if child else None
            effective_restore=bool(restore and child and child['deleted'])
            receipt=self._build(bundle,proof,previous['proposedReceiptId'],existing=existing)
            if effective_restore:receipt.pop('deleted_at',None)
            prepared=registry.prepare(self.service.store,receipt,proof,bundle['restorationSource'],bundle['operation'],data['financialReceiptId'],
                **({'existing_revision':existing['revision']} if existing else {}))
            expected_sources=self._expected_sources(context)
            snapshot=context['snapshot']
            noop=bool(child and not effective_restore and prepared.source_body==snapshot['plan_snapshot'] and prepared.operation_body==snapshot['operation_snapshot'])
            pending={'childRawHash':child['hash'] if noop else _raw_hash(prepared.receipt_body),
                'sourceRawHash':_raw_hash(prepared.source_body),'operationRawHash':_raw_hash(prepared.operation_body),
                'linkRevision':link['revision'] if noop else link['revision']+1 if link else 1}
            self._reauthorize(context,expected)
            with self.cloud._session_guard(),self.service.lock:
                self._final(context,expected)
                with self._lock:
                    self._live(data['confirmationHash']);self._previews[data['confirmationHash']]['pending']=pending
                if child:
                    saved=registry.commit_existing(self.service.store,prepared,expected_sources,
                        expected_link_revision=link['revision'],expected_child_raw=child['raw'],expected_child_deleted_at=child['deleted'],
                        expected_snapshot_source_body=snapshot['plan_snapshot'],expected_snapshot_operation_body=snapshot['operation_snapshot'],
                        expected_snapshot_source_hash=snapshot['restoration_source_hash'],expected_snapshot_operation_hash=snapshot['operation_snapshot_hash'],
                        restore_existing=effective_restore)
                else:
                    saved=registry.commit_new(self.service.store,prepared,expected_sources,
                        expected_link_revision=link['revision'] if link else None)
            local=self._raw(saved['receipt_id'])
            if local['hash']!=pending['childRawHash']:raise _error()
            response={'stockReceipt':local['receipt'],'restoration':{'receivingId':proof['receivingId'],'serverRevision':proof['serverRevision'],
                'planHash':proof['planHash'],'reused':child is not None,'restored':restore,'financialReceiptId':data['financialReceiptId']},
                'operation':{key:bundle['operation'][key] for key in ('receiving_id','revision','plan_hash','state','inventory_confirmed','catalog_confirmed')},
                'readOnlyFinancial':True,'providerActionPerformed':False}
            # A response lost after a successful commit must retain the same
            # identity; a session change before releasing its PII still denies.
            after=registry.read(self.service.store,bundle['tenantId'],proof['receivingId'])
            snapshots=[row for row in after['snapshots'] if row['server_revision']==proof['serverRevision']]
            expected_link={'service_origin':ORIGIN,'tenant_id':bundle['tenantId'],'receiving_id':proof['receivingId'],
                'revision':saved['link_revision'],'active_receipt_id':local['receipt']['id'],
                'server_revision':proof['serverRevision'],'plan_hash':proof['planHash'],'financial_receipt_id':data['financialReceiptId']}
            if (after['ordinary_references'] or not after['link'] or after['link']['revision']!=saved['link_revision']
                or fingerprint(after['link'])!=fingerprint(expected_link) or len(snapshots)!=1
                or snapshots[0]['receipt_id']!=local['receipt']['id'] or snapshots[0]['plan_hash']!=proof['planHash']
                or snapshots[0]['plan_snapshot']!=prepared.source_body or snapshots[0]['operation_snapshot']!=prepared.operation_body
                or snapshots[0]['restoration_source_hash']!=prepared.source_hash or snapshots[0]['operation_snapshot_hash']!=prepared.operation_hash):raise _error()
            post=dict(context,child=local,registry=after,snapshot=snapshots[0])
            self._reauthorize(post,expected)
            with self.cloud._session_guard(),self.service.lock:
                self._final(post,expected)
                with self._lock:self._live(data['confirmationHash'])
                return response
