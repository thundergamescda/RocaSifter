"""Explicit continuation of a registered original receiving operation.

The server keeps its original plan and provider-step keys. Local durable intent,
coherent source proof and same-child application precede journal acknowledgement.
"""
import copy
import json
import math
import secrets
import threading
import time
import uuid

from hosted_labels import fingerprint
from station_cloud import CloudError,ORIGIN
from station_profile import ProfilePolicyError
from station_store import now
from station_receiving_restore import MARKER,_id,_integer,_uuid
from station_receiving_action_transport import ReceivingActionTransport,prepare_action,validate_intent,ACTIONS
import station_receiving_action_registry as intents
import station_receiving_registry as projections
import station_receiving_reference_fences as reference_fences


def _error(message='The original action changed. Review its saved stock record before continuing.',
           code='ORIGINAL_STOCK_ACTION_CHANGED',status=409):
    return CloudError(message,status=status,pending=True,code=code)


class OriginalReceivingActions:
    def __init__(self,service,*,clock=None):
        self.service=service;self.clock=clock or time.monotonic
        self._previews={};self._lock=threading.RLock()

    @property
    def cloud(self):return self.service.cloud

    @property
    def restoration(self):return self.service.receiving_restoration

    @property
    def transport(self):return ReceivingActionTransport(self.cloud)

    def clear(self):
        with self._lock:self._previews.clear()

    def _time(self):
        value=self.clock()
        if type(value) not in (int,float) or not math.isfinite(value):raise _error()
        return value

    def _purge(self):
        stamp=self._time();self._previews={key:value for key,value in self._previews.items() if value['expires']>stamp}
        return stamp

    def _stop(self):
        event=getattr(self.service,'stop_event',None)
        if event is not None and event.is_set():raise _error('The app is stopping. Keep the same original action for recovery.',status=503)

    def _input(self,data,expected,kind):
        allowed={'stockReceiptId','revision','sessionId'}
        if kind=='preview':allowed|={'action','reason'}
        elif kind=='perform':allowed|={'intentId','requestKey','confirmationHash','actionConfirmed','excludedNotReceivedAck',
                                      'recordExistingAdmissionConfirmed'}
        elif kind=='status':allowed|={'intentId'}
        try:
            if (type(data) is not dict or set(data)-allowed or type(expected) is not str or not 1<=len(expected)<=200
                or data.get('sessionId',expected)!=expected):raise ValueError()
            _id(data.get('stockReceiptId'))
            if kind!='status' or 'revision' in data:_integer(data.get('revision'),1)
            if kind=='preview' and (data.get('action') not in ACTIONS or 'reason' in data and data['action']!='cancel'):raise ValueError()
            if 'intentId' in data:_uuid(data['intentId'])
            if kind=='perform':
                _uuid(data.get('intentId'))
                if 'excludedNotReceivedAck' in data and data['excludedNotReceivedAck'] is not True:raise ValueError()
                if 'recordExistingAdmissionConfirmed' in data and data['recordExistingAdmissionConfirmed'] is not True:raise ValueError()
                if (data.get('actionConfirmed') is not True or type(data.get('requestKey')) is not str
                    or type(data.get('confirmationHash')) is not str or len(data['confirmationHash'])!=64
                    or any(c not in '0123456789abcdef' for c in data['confirmationHash'])):raise ValueError()
        except (ValueError,TypeError,KeyError):raise _error('Use the current original-stock action preview and its exact confirmation.',code='ORIGINAL_STOCK_ACTION_INPUT',status=400) from None

    def _context(self,ident,revision,expected,*,require_current=False):
        """Current full proof, retaining the separately verified local snapshot."""
        _id(ident)
        with self.cloud.lock:
            selected=self.restoration._raw(ident);receipt=selected['receipt'];marker=receipt.get(MARKER)
            try:
                if revision is not None and receipt['revision']!=revision:raise ValueError()
                if type(marker) is not dict:raise ValueError()
                financial_id=_id(marker.get('financialReceiptId'));receiving_id=_uuid(marker.get('receivingId'))
                if financial_id==ident:raise ValueError()
                financial=self.restoration._raw(financial_id)
                data={'financialReceiptId':financial_id,'financialRevision':financial['receipt']['revision'],
                      'receivingId':receiving_id,'sessionId':expected}
                self.restoration._selection=data
                context=self.restoration._context(data,expected)
                child=context['child'];proof=context['proof']
                if (not child or child['deleted'] is not None or child['receipt']['id']!=ident
                    or child['raw']!=selected['raw'] or context['financial']['raw']!=financial['raw']
                    or proof['workflowKind']!='accepted_trade'):raise ValueError()
                if require_current and any(marker.get(key)!=proof[key] for key in
                        ('receivingId','serverRevision','planHash','restorationSourceHash','proofHash')):
                    raise _error('Refresh the original stock record before starting a new action.',code='ORIGINAL_STOCK_ACTION_REFRESH_REQUIRED')
            except (CloudError,ProfilePolicyError):raise
            except (ValueError,TypeError,KeyError):raise _error() from None
            self.restoration._reauthorize(context,expected)
            with self.cloud._session_guard(),self.service.lock:self.restoration._final(context,expected)
            context['_selection']=data
            return context

    @staticmethod
    def _facts(context):
        return fingerprint({'child':context['child']['hash'],'financial':context['financial']['hash'],
            'link':context['registry']['link'],'snapshot':context['snapshot'],'role':context['bundle']['role'],
            'operation':context['bundle']['operation'],'proof':context['proof']['proofHash']})

    @staticmethod
    def _review(context):
        """Fresh server facts, distinct from the saved child until applied."""
        bundle=context['bundle'];proof=context['proof'];child=context['child']['receipt'];marker=child[MARKER]
        return copy.deepcopy({'version':1,'serviceOrigin':ORIGIN,'tenantId':bundle['tenantId'],'store':bundle['store'],
            'stockReceiptId':child['id'],'revision':child['revision'],'financialReceiptId':marker['financialReceiptId'],
            'tradeId':bundle['trade']['id'],'receiptHash':bundle['trade']['receipt_hash'],
            'receivingReceiptHash':bundle['operation']['receipt']['receipt_hash'] if proof['received'] else None,
            **{key:proof[key] for key in ('receivingId','serverRevision','planHash','restorationSourceHash','proofHash',
                'workflowKind','state','received','admission','labelProfile','provider')},
            'counts':proof['quantityBudget'],'savedProofCurrent':all(marker.get(key)==proof[key] for key in
                ('receivingId','serverRevision','planHash','restorationSourceHash','proofHash'))})

    def _read_response(self,context,expected,payload,*,row=None,intent_id=None):
        result={**payload,'review':self._review(context)}
        expected_row=tuple(row[key] for key in intents.FIELDS) if row else None
        self.restoration._reauthorize(context,expected)
        with self.cloud._session_guard(),self.service.lock:
            self._stop();self.restoration._final(context,expected)
            query='SELECT '+','.join(intents.FIELDS)+' FROM '+intents.TABLE+' WHERE service_origin=? AND tenant_id=?'
            values=(ORIGIN,context['bundle']['tenantId'])
            if intent_id:query+=' AND intent_id=?';values+=(intent_id,)
            else:query+=" AND receiving_id=? AND state IN ('pending','applied')";values+=(context['proof']['receivingId'],)
            with self.service.store.db() as db:actual=db.execute(query,values).fetchall()
            if len(actual)>1 or (tuple(actual[0]) if actual else None)!=expected_row:raise _error()
        return result

    def _session(self,context,expected,*,unlocked=False):
        if unlocked:saved=self.cloud.assert_recovery_session_unlocked(expected,context['bundle']['tenantId'],context['bundle']['store'])
        else:
            with self.cloud._session_guard():return self._session(context,expected,unlocked=True)
        if saved.get('role') not in ('owner','manager'):raise _error(status=403)
        expiry=saved.get('expires_at');stamp=self.cloud.clock()
        if (type(expiry) not in (int,float) or not math.isfinite(expiry) or type(stamp) not in (int,float)
            or not math.isfinite(stamp) or expiry<=stamp):raise _error(code='ORIGINAL_STOCK_ACTION_SESSION_CHANGED')
        return (expected,context['bundle']['tenantId'],context['bundle']['store'],saved['role'],expiry)

    @staticmethod
    def _bound(intent,context):
        validate_intent(intent);bundle=context['bundle'];proof=context['proof'];marker=context['child']['receipt'][MARKER]
        expected={'serviceOrigin':ORIGIN,'tenantId':bundle['tenantId'],'store':bundle['store'],
            'stockReceiptId':context['child']['receipt']['id'],'financialReceiptId':marker['financialReceiptId'],
            'tradeId':bundle['trade']['id'],'receiptHash':bundle['trade']['receipt_hash'],
            'receivingId':proof['receivingId'],'serverRevision':proof['serverRevision'],'planHash':proof['planHash'],
            'provider':proof['provider'],'labelProfile':proof['labelProfile']}
        if any(fingerprint(intent[key])!=fingerprint(value) for key,value in expected.items()):raise _error()

    def _prepare_intent(self,context,kind,reason=None):
        bundle=context['bundle'];child=context['child']['receipt']
        return prepare_action(kind,uuid.uuid4().hex,intent_id=str(uuid.uuid4()),stock_receipt_id=child['id'],
            financial_receipt_id=child[MARKER]['financialReceiptId'],trade_id=bundle['trade']['id'],
            receipt_hash=bundle['trade']['receipt_hash'],tenant_id=bundle['tenantId'],store=bundle['store'],
            operation=bundle['operation'],restoration_source=bundle['restorationSource'],
            proof_hash=context['proof']['proofHash'],created_at=now(),reason=reason)

    @staticmethod
    def _execution_mode(context,row,intent):
        if row and row['state']=='applied':return 'apply_saved_result'
        admission=context['proof']['admission']
        if (row and row['state']=='pending' and intent['kind']=='commit'
            and context['proof']['state'] in ('catalog_confirmed','received')
            and admission['everAdmitted'] and (admission['catalogStepPresent'] or admission['inventoryStepPresent'])):
            # Existing backend branches record this exact request's current
            # result before any new provider admission. Never create an intent
            # for this path or infer that its earlier POST caused the stock.
            return 'record_existing_admission'
        return 'dispatch_action'

    def _eligible(self,intent,context,mode):
        if mode!='dispatch_action':return
        candidate=prepare_action(intent['kind'],intent['requestKey'],intent_id=intent['intentId'],
            stock_receipt_id=intent['stockReceiptId'],financial_receipt_id=intent['financialReceiptId'],
            trade_id=intent['tradeId'],receipt_hash=intent['receiptHash'],tenant_id=intent['tenantId'],store=intent['store'],
            operation=context['bundle']['operation'],restoration_source=context['bundle']['restorationSource'],
            proof_hash=context['proof']['proofHash'],created_at=intent['createdAt'],reason=intent['body'].get('reason'))
        if fingerprint(candidate['body'])!=fingerprint(intent['body']):raise _error()
        if intent['kind']!='recover' and candidate['operationHash']!=intent['operationHash']:raise _error()

    def _row(self,context,intent_id=None):
        tenant=context['bundle']['tenantId']
        if intent_id:
            row=intents.read_intent(self.service.store,tenant,intent_id=intent_id)
        else:row=intents.read_active(self.service.store,tenant,context['proof']['receivingId'])
        if row and row['receipt_id']!=context['child']['receipt']['id']:
            raise _error('A saved action belongs to another original stock record. Recover that record first.')
        return row

    def _registry_args(self,context,expected):
        return {'child':context['child']['receipt'],
            'expected_sources':self.restoration._expected_sources(context,include_child=True),
            'link':context['registry']['link'],'snapshot':context['snapshot'],
            'session_facts':self._session(context,expected)}

    def _commit(self,context,expected,prepared,*,reservation=False):
        self.restoration._reauthorize(context,expected)
        with self.cloud._session_guard(),self.service.lock:
            self._stop();session=self._session(context,expected,unlocked=True)
            method=intents.commit_reservation if reservation else intents.commit_transition
            return method(self.service.store,prepared,current_session_facts=session)['intent']

    def preview(self,data,expected):
        self._input(data,expected,'preview');self._stop()
        context=self._context(data['stockReceiptId'],data['revision'],expected)
        active=self._row(context)
        if active:
            intent=json.loads(active['intent_body']);self._bound(intent,context)
            if intent['kind']!=data['action']:raise _error('Check the saved original action before choosing a different one.')
        else:
            context=self._context(data['stockReceiptId'],data['revision'],expected,require_current=True)
            intent=self._prepare_intent(context,data['action'],data.get('reason'))
        mode=self._execution_mode(context,active,intent)
        self._eligible(intent,context,mode)
        facts=self._facts(context)
        confirmation=fingerprint({'domain':'stock-and-sleeve:original-stock-action-preview:v1',
                                  'random':secrets.token_hex(32),'intent':intent,'facts':facts,'executionMode':mode})
        self.restoration._reauthorize(context,expected)
        with self.cloud._session_guard(),self.service.lock:
            self.restoration._final(context,expected)
            with self._lock:
                stamp=self._purge()
                while len(self._previews)>=16:self._previews.pop(next(iter(self._previews)))
                self._previews[confirmation]={'intent':intent,'facts':facts,'executionMode':mode,
                                              'sessionId':expected,'expires':stamp+300}
        proof=context['proof']
        return {'preview':{'version':1,'stockReceiptId':data['stockReceiptId'],'revision':data['revision'],
            'intentId':intent['intentId'],'requestKey':intent['requestKey'],'action':intent['kind'],
            'receivingId':proof['receivingId'],'serverRevision':proof['serverRevision'],'planHash':proof['planHash'],
            'proofHash':proof['proofHash'],'state':proof['state'],'admission':proof['admission'],
            'counts':proof['quantityBudget'],'labelProfile':proof['labelProfile'],'provider':proof['provider'],
            'pending':active is not None,'intentState':active['state'] if active else None,
            'reason':intent['body'].get('reason'),'executionMode':mode,
            'requiresFreshMfa':mode=='dispatch_action' and intent['kind'] in ('commit','authorize')},
            'confirmationHash':confirmation,'expiresIn':300,'review':self._review(context)}

    def _cached(self,data,expected):
        with self._lock:
            self._purge();value=copy.deepcopy(self._previews.get(data['confirmationHash']))
        if (not value or value['sessionId']!=expected or value['intent']['stockReceiptId']!=data['stockReceiptId']
            or value['intent']['intentId']!=data['intentId'] or value['intent']['requestKey']!=data['requestKey']):
            raise _error('The original action preview expired. Review the same saved request again.',code='ORIGINAL_STOCK_ACTION_PREVIEW_EXPIRED')
        value['confirmationHash']=data['confirmationHash']
        return value

    def _before_dispatch(self,intent,row,cached,expected):
        self._stop()
        current=self._context(intent['stockReceiptId'],None,expected)
        self._bound(intent,current)
        actual=self._row(current,intent['intentId'])
        if (actual!=row or actual['state']!='pending' or self._facts(current)!=cached['facts']
            or self._execution_mode(current,actual,intent)!=cached['executionMode']):raise _error()
        self._eligible(intent,current,cached['executionMode'])
        expected_row=tuple(row[key] for key in intents.FIELDS)
        expected_preview={key:value for key,value in cached.items() if key!='confirmationHash'}
        self.restoration._reauthorize(current,expected)
        with self.cloud._session_guard(),self.service.lock:
            self._stop();self.restoration._final(current,expected)
            with self.service.store.db() as db:
                reference_fences.assert_unfenced(db,ORIGIN,row['tenant_id'],row['intent_id'],row['request_key'])
                selected=db.execute('SELECT '+','.join(intents.FIELDS)+' FROM '+intents.TABLE+
                    ' WHERE service_origin=? AND tenant_id=? AND intent_id=?',
                    (row['service_origin'],row['tenant_id'],row['intent_id'])).fetchone()
                if selected is None or tuple(selected)!=expected_row:raise _error()
            with self._lock:
                self._purge()
                if self._previews.get(cached['confirmationHash'])!=expected_preview:raise _error()

    def _sync_once(self,context,expected):
        """Publish fresh verified progress into the same registered local child."""
        child=context['child'];bundle=context['bundle'];proof=context['proof'];snapshot=context['snapshot'];link=context['registry']['link']
        with self.cloud.lock:
            self.restoration._selection=context['_selection']
            receipt=self.restoration._build(bundle,proof,child['receipt']['id'],existing=child['receipt'])
            prepared=projections.prepare(self.service.store,receipt,proof,bundle['restorationSource'],bundle['operation'],
                child['receipt'][MARKER]['financialReceiptId'],existing_revision=child['receipt']['revision'])
        self.restoration._reauthorize(context,expected)
        with self.cloud._session_guard(),self.service.lock:
            self._stop()
            self.restoration._final(context,expected)
            projections.commit_existing(self.service.store,prepared,self.restoration._expected_sources(context),
                expected_link_revision=link['revision'],expected_child_raw=child['raw'],expected_child_deleted_at=None,
                expected_snapshot_source_body=snapshot['plan_snapshot'],expected_snapshot_operation_body=snapshot['operation_snapshot'],
                expected_snapshot_source_hash=snapshot['restoration_source_hash'],expected_snapshot_operation_hash=snapshot['operation_snapshot_hash'])
        return self._context(child['receipt']['id'],None,expected)

    def _sync(self,context,expected):
        for _ in range(3):
            if context['child']['receipt'][MARKER]['proofHash']==context['proof']['proofHash']:return context
            context=self._sync_once(context,expected)
        if context['child']['receipt'][MARKER]['proofHash']==context['proof']['proofHash']:return context
        raise _error('The original operation is still progressing. Check this same action again; its key and evidence are kept.')

    def _finish(self,row,result,expected,resolution):
        intent=json.loads(row['intent_body']);context=self._context(intent['stockReceiptId'],None,expected)
        self._bound(intent,context);self.transport._result(result,intent)
        return self._complete(row,context,expected,fingerprint(result),resolution)

    def _reaffirm(self,row,context,expected,result_fingerprint):
        """Persist current same-child proof before removing a completed key."""
        context=self._sync(context,expected)
        current=self._row(context,row['intent_id'])
        if current is None or current['intent_body']!=row['intent_body']:raise _error()
        if current['state']!='acknowledged':
            prepared=intents.prepare_applied(self.service.store,expected_intent=current,
                result_fingerprint=result_fingerprint,**self._registry_args(context,expected))
            current=self._commit(context,expected,prepared)
        if current['result_fingerprint']!=result_fingerprint:raise _error()
        return current,context

    def _complete(self,row,context,expected,wire_fingerprint,resolution):
        intent=json.loads(row['intent_body']);self._bound(intent,context)
        current,context=self._reaffirm(row,context,expected,wire_fingerprint)
        self.transport.acknowledge(intent,expected,applied_result_fingerprint=wire_fingerprint)
        context=self._context(intent['stockReceiptId'],None,expected)
        self._bound(intent,context)
        # A different client can refresh the same child during journal ACK.
        # Retain the same result/key and attest that newer proof before closing.
        current,context=self._reaffirm(current,context,expected,wire_fingerprint)
        if current['state']!='acknowledged':
            prepared=intents.prepare_acknowledged(self.service.store,expected_intent=current,**self._registry_args(context,expected))
            current=self._commit(context,expected,prepared)
        return self._response(context,current,resolution)

    def _response(self,context,row,resolution):
        self.restoration._reauthorize(context,context['bundle']['sessionId'])
        expected_row=tuple(row[key] for key in intents.FIELDS)
        with self.cloud._session_guard(),self.service.lock:
            self._stop();self.restoration._final(context,context['bundle']['sessionId'])
            with self.service.store.db() as db:
                actual=db.execute('SELECT '+','.join(intents.FIELDS)+' FROM '+intents.TABLE+
                    ' WHERE service_origin=? AND tenant_id=? AND intent_id=?',
                    (row['service_origin'],row['tenant_id'],row['intent_id'])).fetchone()
                if actual is None or tuple(actual)!=expected_row:raise _error()
        return {'stockReceipt':context['child']['receipt'],'review':self._review(context),
            'action':{'intentId':row['intent_id'],'requestKey':row['request_key'],'kind':row['action_kind'],
                'intentState':row['state'],'resolution':resolution,'receivingId':row['receiving_id'],
                'serverRevision':row['server_revision'],'planHash':row['plan_hash'],
                'state':context['proof']['state'],'received':context['proof']['received'],
                'executionMode':'record_existing_admission' if resolution=='recorded_existing_admission'
                    else 'dispatch_action' if resolution=='dispatched' else 'apply_saved_result'},
            'financialActionPerformed':False,'requestDispatched':resolution in ('dispatched','recorded_existing_admission')}

    def perform(self,data,expected):
        self._input(data,expected,'perform');self._stop()
        current=self._context(data['stockReceiptId'],None,expected)
        existing=self._row(current,data['intentId'])
        if existing:
            saved_intent=json.loads(existing['intent_body']);self._bound(saved_intent,current)
            if saved_intent['requestKey']!=data['requestKey']:raise _error()
            if existing['state']!='pending':return self.status({'stockReceiptId':data['stockReceiptId'],'intentId':data['intentId']},expected)
            found=self.transport.lookup(saved_intent,expected)
            if found['found']:return self._finish(existing,found['result'],expected,found['source'])
        cached=self._cached(data,expected)
        context=self._context(data['stockReceiptId'],data['revision'],expected)
        intent=cached['intent'];self._bound(intent,context)
        if (intent['kind']=='commit' and data.get('excludedNotReceivedAck') is not True
            or intent['kind']!='commit' and 'excludedNotReceivedAck' in data):
            raise _error('Explicitly confirm which accepted copies will remain outside this stock update.',
                         code='ORIGINAL_STOCK_ACTION_INPUT',status=400)
        if self._facts(context)!=cached['facts']:raise _error()
        old=self._row(context,intent['intentId']);active=self._row(context)
        mode=self._execution_mode(context,old,intent)
        if mode!=cached['executionMode']:raise _error()
        if (mode=='record_existing_admission' and data.get('recordExistingAdmissionConfirmed') is not True
            or mode!='record_existing_admission' and 'recordExistingAdmissionConfirmed' in data):
            raise _error('Explicitly review and confirm checking this existing result with its same saved request.',
                         code='ORIGINAL_STOCK_ACTION_INPUT',status=400)
        self._eligible(intent,context,mode)
        prepared=intents.prepare_reservation(self.service.store,intent=intent,expected_active=active,
            expected_intent=old,**self._registry_args(context,expected))
        row=self._commit(context,expected,prepared,reservation=True)
        if row['state']!='pending':return self.status({'stockReceiptId':data['stockReceiptId'],'intentId':intent['intentId']},expected)
        outcome=self.transport.perform(intent,expected,dispatch_confirmed=True,
            before_dispatch=lambda:self._before_dispatch(intent,row,cached,expected))
        resolution='recorded_existing_admission' if mode=='record_existing_admission' and outcome['resolution']=='dispatched' else outcome['resolution']
        return self._finish(row,outcome['result'],expected,resolution)

    def status(self,data,expected):
        self._input(data,expected,'status');self._stop()
        context=self._context(data['stockReceiptId'],data.get('revision'),expected)
        row=self._row(context,data.get('intentId'))
        if row is None:return self._read_response(context,expected,{'found':False,'financialActionPerformed':False},
            intent_id=data.get('intentId'))
        intent=json.loads(row['intent_body']);self._bound(intent,context)
        if row['state'] in ('applied','acknowledged'):
            return {'found':True,**self._complete(row,context,expected,row['result_fingerprint'],'applied')}
        outcome=self.transport.lookup(intent,expected)
        if not outcome['found']:
            context=self._context(data['stockReceiptId'],None,expected);self._bound(intent,context)
            return self._read_response(context,expected,{'found':False,'action':{'intentId':row['intent_id'],
                'requestKey':row['request_key'],'kind':row['action_kind'],'intentState':row['state'],
                'receivingId':row['receiving_id'],'serverRevision':row['server_revision'],'planHash':row['plan_hash'],
                'state':context['proof']['state']},'financialActionPerformed':False,'pending':True},
                row=row,intent_id=data.get('intentId'))
        return {'found':True,**self._finish(row,outcome['result'],expected,outcome['source'])}
