"""Exact existing-operation receiving actions; no plans, claims or agreements.

The coordinator supplies verified registered context and owns local application.
This bridge preserves an immutable durable intent and the existing encrypted
receiving journal. Cached/lookup results never grant a new action by themselves.
"""
import copy
from datetime import datetime
import re
import uuid
from urllib.parse import quote

from hosted_labels import fingerprint, profile as label_profile
from receiving_evidence import provider_id
from station_cloud import CloudError, ORIGIN
from station_hosted_receive import KEY, RECEIPT, HASH
from station_http_json import preflight_request_body, RequestBodyError
from station_receiving_restoration_proof import restoration_source_hash, restoration_state_hash


FIELDS = {'version','serviceOrigin','tenantId','store','intentId','stockReceiptId','financialReceiptId','tradeId',
          'receiptHash','receivingId','serverRevision','planHash','restorationSourceHash','proofHash','stateBasisHash',
          'operationHash','provider','labelProfile','kind','requestKey','body','createdAt'}
ACTIONS = {'commit','recover','authorize','cancel'}


def _invalid(message='Keep the exact original receiving action and refresh its registered stock record.', status=409):
    return CloudError(message,status=status,pending=True,code='ORIGINAL_STOCK_ACTION_INVALID')


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise ValueError()
    return value


def _hash(value):
    if type(value) is not str or not HASH.fullmatch(value):
        raise ValueError()
    return value


def _same(left,right):
    return fingerprint(left)==fingerprint(right)


def validate_intent(intent):
    try:
        if type(intent) is not dict or set(intent)!=FIELDS or type(intent['version']) is not int or intent['version']!=1 or intent['serviceOrigin']!=ORIGIN:
            raise ValueError()
        for key in ('tenantId','intentId','tradeId','receivingId'):_uuid(intent[key])
        for key in ('receiptHash','planHash','restorationSourceHash','proofHash','stateBasisHash','operationHash'):_hash(intent[key])
        if (type(intent['serverRevision']) is not int or not 0<=intent['serverRevision']<=2**63-1
            or type(intent['store']) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,49}',intent['store'])
            or any(type(intent[key]) is not str or not RECEIPT.fullmatch(intent[key]) for key in ('stockReceiptId','financialReceiptId'))
            or intent['stockReceiptId']==intent['financialReceiptId'] or intent['kind'] not in ACTIONS
            or type(intent['requestKey']) is not str or not KEY.fullmatch(intent['requestKey'])):raise ValueError()
        at=datetime.fromisoformat(intent['createdAt'])
        if at.tzinfo is None or at.utcoffset() is None:raise ValueError()
        chosen=label_profile(intent['labelProfile'])
        if not _same(chosen,intent['labelProfile']):raise ValueError()
        provider=intent['provider']
        if type(provider) is not dict or set(provider)!={'merchant_id','environment','application_id','location_id'} or provider['environment'] not in ('production','sandbox'):raise ValueError()
        for key in ('merchant_id','application_id','location_id'):provider_id(provider[key])
        body=intent['body'];allowed={'request_key','revision','plan_hash'}
        if intent['kind']=='commit':allowed|={'confirmed','excluded_not_received_ack'}
        elif intent['kind']=='authorize':allowed.add('resume_same_operation')
        elif intent['kind']=='cancel':allowed.add('reason')
        if (type(body) is not dict or set(body)!=allowed or body.get('request_key')!=intent['requestKey']
            or type(body.get('revision')) is not int or body['revision']!=intent['serverRevision'] or body.get('plan_hash')!=intent['planHash']):raise ValueError()
        if intent['kind']=='commit' and (body['confirmed'] is not True or body['excluded_not_received_ack'] is not True):raise ValueError()
        if intent['kind']=='authorize' and body['resume_same_operation'] is not True:raise ValueError()
        if intent['kind']=='cancel' and (type(body['reason']) is not str or not 10<=len(body['reason'])<=200 or body['reason']!=body['reason'].strip() or any(ord(c)<32 or ord(c)==127 for c in body['reason'])):raise ValueError()
        preflight_request_body(body)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
        raise _invalid() from None
    return intent


def prepare_action(kind,request_key,*,intent_id,stock_receipt_id,financial_receipt_id,trade_id,receipt_hash,
                   tenant_id,store,operation,restoration_source,proof_hash,created_at,reason=None):
    """Build exact wire terms from already verified original context; no I/O."""
    try:
        source=restoration_source;admission=source['admission'];state=operation['state']
        if (kind not in ACTIONS or type(source['version']) is not int or source['version']!=1
            or source['workflowKind']!='accepted_trade' or operation['workflow_kind']!='accepted_trade'
            or source['acceptedTradeId']!=trade_id or source['acceptedReceiptHash']!=receipt_hash
            or source['receivingId']!=operation['receiving_id'] or type(source['serverRevision']) is not int
            or type(operation['revision']) is not int or source['serverRevision']!=operation['revision'] or source['planHash']!=operation['plan_hash']
            or source['restorationSourceHash']!=restoration_source_hash(source)
            or source['stateBasisHash']!=restoration_state_hash(operation,admission)
            or type(admission) is not dict or set(admission)!={'everAdmitted','catalogStepPresent','inventoryStepPresent','leaseActive'}
            or any(type(value) is not bool for value in admission.values())):raise ValueError()
        for left,right in (('physicalIntakeId','physical_intake_id'),('physicalManifestHash','physical_manifest_hash'),
                           ('sourceSnapshotHash','source_snapshot_hash'),('acceptedTradeId','accepted_trade_id'),
                           ('acceptedReceiptHash','accepted_receipt_hash')):
            if source[left]!=operation[right]:raise ValueError()
        if kind in ('commit','cancel'):
            eligible=state=='ready' and not any(admission.values())
        elif kind=='authorize':
            eligible=(state=='catalog_confirmed' and admission['everAdmitted'] and admission['catalogStepPresent']
                      and not admission['inventoryStepPresent'] and not admission['leaseActive']
                      and operation['catalog_confirmed'] is True and operation['inventory_confirmed'] is False
                      and any(row['action']=='create_price' for row in operation['targets']))
        else:
            eligible=(state in ('catalog_pending','inventory_pending','outcome_unknown','needs_review') and admission['everAdmitted']
                      and (admission['catalogStepPresent'] or admission['inventoryStepPresent']))
        if not eligible:raise ValueError()
        body={'request_key':request_key,'revision':source['serverRevision'],'plan_hash':source['planHash']}
        if kind=='commit':body.update(confirmed=True,excluded_not_received_ack=True)
        elif kind=='authorize':body['resume_same_operation']=True
        elif kind=='cancel':body['reason']=reason.strip() if type(reason) is str else reason
        intent={'version':1,'serviceOrigin':ORIGIN,'tenantId':tenant_id,'store':store,'intentId':intent_id,
            'stockReceiptId':stock_receipt_id,'financialReceiptId':financial_receipt_id,'tradeId':trade_id,'receiptHash':receipt_hash,
            'receivingId':source['receivingId'],'serverRevision':source['serverRevision'],'planHash':source['planHash'],
            'restorationSourceHash':source['restorationSourceHash'],'proofHash':proof_hash,'stateBasisHash':source['stateBasisHash'],
            'operationHash':fingerprint(operation),'provider':copy.deepcopy(operation['provider']),
            'labelProfile':copy.deepcopy(operation['client_label_profile']),'kind':kind,'requestKey':request_key,'body':body,'createdAt':created_at}
        validate_intent(intent)
    except (ValueError,TypeError,KeyError,AttributeError):
        raise _invalid('This original operation does not allow that action. Refresh its original evidence.') from None
    return intent


class ReceivingActionTransport:
    def __init__(self,cloud):self.cloud=cloud

    def _authority(self,intent,expected):
        validate_intent(intent)
        if self.cloud._profile_snapshot()['profileMode']!='hosted':raise _invalid('Use this store’s bound hosted profile for original-stock actions.')
        authority=self.cloud.record_authority(expected_session_id=expected)
        if (authority.get('tenantId')!=intent['tenantId'] or authority.get('store')!=intent['store']
            or authority.get('sessionId')!=expected or authority.get('role') not in ('owner','manager')):
            raise CloudError('A current owner or manager of this store must review the original receiving action.',status=403,pending=True)
        saved=self.cloud._saved();self.cloud._expected(saved,expected)
        return saved

    @staticmethod
    def _row_matches(row,intent):
        return (row['kind']==intent['kind'] and row['receipt_id']==intent['stockReceiptId']
                and row['receiving_id']==intent['receivingId'] and _same(row['body'],intent['body'])
                and _same(row['label_profile'],intent['labelProfile'])
                and row['fingerprint']==fingerprint({'body':intent['body'],'label_profile':intent['labelProfile']}))

    def _ensure(self,intent,expected):
        def change(rows,saved):
            matches=[row for row in rows if row['store']==intent['store'] and row['request_key']==intent['requestKey']]
            if matches:
                if len(matches)!=1 or not self._row_matches(matches[0],intent):raise _invalid()
                return rows,copy.deepcopy(matches[0])
            if len(rows)>=50:raise _invalid('Recover existing saved receiving actions before starting another.')
            row={'kind':intent['kind'],'store':intent['store'],'receipt_id':intent['stockReceiptId'],
                 'receiving_id':intent['receivingId'],'request_key':intent['requestKey'],'body':copy.deepcopy(intent['body']),
                 'label_profile':copy.deepcopy(intent['labelProfile']),
                 'fingerprint':fingerprint({'body':intent['body'],'label_profile':intent['labelProfile']}),'created_at':self.cloud.clock()}
            rows.append(row);return rows,copy.deepcopy(row)
        return self.cloud.receiving.storage.update(change,expected)

    @staticmethod
    def _result(result,intent):
        # Full current source/target/catalog/terminal proof belongs to the
        # coordinator's fresh coherent read, never to a cached HTTP result.
        if (type(result) is not dict or result.get('receiving_id')!=intent['receivingId']
            or type(result.get('revision')) is not int or result['revision']!=intent['serverRevision']
            or result.get('plan_hash')!=intent['planHash'] or result.get('workflow_kind')!='accepted_trade'
            or result.get('accepted_trade_id')!=intent['tradeId'] or result.get('accepted_receipt_hash')!=intent['receiptHash']
            or not _same(result.get('provider'),intent['provider']) or not _same(result.get('client_label_profile'),intent['labelProfile'])):
            raise _invalid('The saved action result does not match its original receiving operation.',502)
        return result

    def lookup(self,intent,expected):
        saved=self._authority(intent,expected)
        row=self.cloud.receiving.row(intent['requestKey'],expected)
        if row is not None and not self._row_matches(row,intent):raise _invalid()
        if row is not None and 'result' in row:
            result=self._result(row['result'],intent);source='journal'
        else:
            status=self.cloud._call('GET','/api/receiving/requests/'+quote(intent['requestKey'],safe=''),token=saved['token'])
            if type(status.get('found')) is not bool or set(status)!={'found','result'}:raise _invalid('The original receiving request is uncertain. Keep its same key.',502)
            if status['found'] is False:
                if status['result'] is not None:raise _invalid('The original receiving request lookup is malformed.',502)
                self._authority(intent,expected)
                return {'found':False,'result':None,'source':'not_found'}
            result=self._result(status['result'],intent);source='request_lookup'
            self._ensure(intent,expected)
            self.cloud.receiving.remember(intent['requestKey'],result,expected,intent['kind'])
        self._authority(intent,expected)
        return {'found':True,'result':copy.deepcopy(result),'source':source}

    def perform(self,intent,expected,*,dispatch_confirmed=False,before_dispatch=None):
        status=self.lookup(intent,expected)
        if status['found']:return {'result':status['result'],'resolution':status['source']}
        if dispatch_confirmed is not True or not callable(before_dispatch):
            raise _invalid('Review and explicitly confirm this same saved action before retrying its original key.')
        row=self._ensure(intent,expected)
        if 'result' in row:
            result=self._result(row['result'],intent);self._authority(intent,expected)
            return {'result':copy.deepcopy(result),'resolution':'journal'}
        saved=self._authority(intent,expected)
        # The coordinator's fresh source/authority/final local CAS is the last
        # pre-dispatch step. Do not insert another remote read after it.
        before_dispatch()
        try:
            result=self.cloud._call('POST','/api/receiving/'+intent['receivingId']+'/'+intent['kind'],
                                    token=saved['token'],data=intent['body'])
            self._result(result,intent)
            self.cloud.receiving.remember(intent['requestKey'],result,expected,intent['kind'])
        except CloudError as error:
            error.pending=True
            raise
        self._authority(intent,expected)
        return {'result':copy.deepcopy(result),'resolution':'dispatched'}

    def acknowledge(self,intent,expected,*,applied_result_fingerprint):
        self._authority(intent,expected)
        try:_hash(applied_result_fingerprint)
        except (ValueError,TypeError):raise _invalid() from None
        def change(rows,saved):
            row=next((row for row in rows if row['store']==intent['store'] and row['request_key']==intent['requestKey']),None)
            if row is not None and (not self._row_matches(row,intent) or row.get('result_fingerprint')!=applied_result_fingerprint):raise _invalid()
            return [item for item in rows if not (item['store']==intent['store'] and item['request_key']==intent['requestKey'])],None
        self.cloud.receiving.storage.update(change,expected)
        self._authority(intent,expected)
        return {'acknowledged':True,'requestKey':intent['requestKey']}
