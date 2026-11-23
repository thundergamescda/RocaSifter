"""Read-only original inventory-only history, anchored to a saved physical intake.

No claim mirroring, journal acknowledgement, local projection or provider action.
Remote reads, proof verification and serialization precede the short local check.
"""
import copy
import json
import re
import uuid

from hosted_labels import fingerprint
from intake_manifest import validate_manifest, manifest_digest, semantic_fingerprint, source_lines, ManifestError
from station_cloud import CloudError, ORIGIN
from station_intake_claim import required_manifest, require_ordinary_receipt
from station_receiving_restoration_proof import verify_restoration_source, RestorationProofError
from station_store import StationError


def _error(message='The original intake review changed. Refresh it; your batch and original plan were kept.',*,status=409,code='INTAKE_RECEIVING_CHANGED'):
    return CloudError(message,status=status,code=code)


def _integer(value,minimum=0,maximum=2**63-1):
    if type(value) is not int or not minimum<=value<=maximum:raise ValueError()
    return value


def _hash(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):raise ValueError()
    return value


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value))!=value:raise ValueError()
    return value


class IntakeReceivingReview:
    def __init__(self,service):self.service=service

    @property
    def cloud(self):return self.service.cloud

    def _input(self,data,expected,kind):
        fields={'receiptId','revision','sessionId'}
        if kind!='history':fields|={'receivingId','expectedServerRevision','expectedPlanHash'}
        if kind=='rows':fields|={'proofHash','offset','limit'}
        try:
            if (type(data) is not dict or set(data)!=fields or type(expected) is not str
                or not re.fullmatch('[a-f0-9]{32}',expected) or data['sessionId']!=expected
                or type(data['receiptId']) is not str or not re.fullmatch(r'R-[A-Za-z0-9-]{1,100}',data['receiptId'])):raise ValueError()
            _integer(data['revision'],1)
            if kind!='history':
                _uuid(data['receivingId']);_integer(data['expectedServerRevision']);_hash(data['expectedPlanHash'])
            if kind=='rows':
                _hash(data['proofHash']);_integer(data['offset'],0,1000);_integer(data['limit'],1,100)
        except (ValueError,TypeError,KeyError,AttributeError):
            raise _error('Use the saved intake and its exact original plan. Review fields cannot be overridden.',
                         status=400,code='INTAKE_RECEIVING_INPUT_INVALID') from None

    def _anchor(self,data):
        # All ordinary-kind gates precede any cloud, profile or journal access.
        with self.service.store.db() as db:
            row=db.execute('SELECT id,body,deleted_at FROM receipts WHERE id=?',(data['receiptId'],)).fetchone()
        if not row or row['deleted_at'] is not None:raise _error()
        try:
            receipt=json.loads(row['body'])
            if type(receipt) is not dict:raise ValueError()
            require_ordinary_receipt(receipt)
            if (receipt.get('id')!=row['id'] or receipt.get('deleted_at') is not None
                or _integer(receipt.get('revision'),1)!=data['revision']):raise ValueError()
            manifest=required_manifest(receipt);intent=receipt.get('intake_claim')
            if type(intent) is not dict or intent.get('state')!='confirmed':raise ValueError()
            _uuid(intent.get('physical_intake_id'));_hash(intent.get('manifest_hash'));_hash(intent.get('source_manifest_hash'))
            if (intent['source_manifest_hash']!=manifest_digest(manifest)
                or type(intent.get('store')) is not str or not 1<=len(intent['store'])<=200):raise ValueError()
            semantic=semantic_fingerprint(manifest)
        except StationError:raise
        except (ValueError,TypeError,KeyError,AttributeError,ManifestError):raise _error() from None
        if self.service.profile.safe_snapshot()['profileMode']!='hosted':
            raise _error('Use this store’s separate hosted profile to review its original stock.',code='INTAKE_RECEIVING_PROFILE_REQUIRED')
        saved=self.cloud._saved();self.cloud._expected(saved,data['sessionId'])
        if not saved or saved.get('store')!=intent['store']:
            raise _error('Sign in to the original intake’s store before reviewing its stock. Saved work was kept.',code='INTAKE_RECEIVING_STORE_REQUIRED')
        return {'receipt':receipt,'raw':row['body'],'deleted':row['deleted_at'],'manifest':manifest,
                'semantic':semantic,'claimId':intent['physical_intake_id'],'store':intent['store'],'claimHash':intent['manifest_hash']}

    def _binding(self,anchor,bundle,expected,*,preview=False):
        require_ordinary_receipt(anchor['receipt'])
        fields={'sessionId','tenantId','store','role','claim','history'}
        if preview:fields|={'operation','restorationSource'}
        try:
            if (type(bundle) is not dict or set(bundle)!=fields or bundle['sessionId']!=expected
                or bundle['store']!=anchor['store'] or bundle['role'] not in ('owner','manager')):raise ValueError()
            _uuid(bundle['tenantId'])
            claim=bundle['claim'];remote=validate_manifest(claim.get('source_manifest'))
            policy=claim.get('policy');expected_policy={'one_operation_per_claim':True,'new_acceptance_requires_fresh_mfa':True,
                'excluded_rows_reusable':False,'physical_identity_proven_by_hash':False}
            if (type(policy) is not dict or set(policy)!=set(expected_policy)
                or any(policy[key] is not value for key,value in expected_policy.items())):raise ValueError()
            if (claim.get('physical_intake_id')!=anchor['claimId'] or claim.get('manifest_hash')!=anchor['claimHash']
                or manifest_digest(remote)!=claim['manifest_hash'] or semantic_fingerprint(remote)!=claim.get('semantic_hash')
                or claim['semantic_hash']!=anchor['semantic'] or type(claim.get('version')) is not int or claim['version']<0
                or claim.get('state') not in ('open','consumed')
                or type(claim.get('physical_reference')) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._:/-]{2,99}',claim['physical_reference'])
                ):raise ValueError()
            bound=claim.get('bound_trade_id')
            if bound is not None:_uuid(bound)
            if (claim['state']=='open' and bound is not None) or (claim['state']=='consumed' and bound is None):raise ValueError()
            history=bundle['history']
            if (type(history) is not dict or set(history)!={'version','physical_intake_id','operations','lookup_complete'}
                or type(history['version']) is not int or history['version']!=1
                or history['physical_intake_id']!=anchor['claimId'] or history['lookup_complete'] is not True
                or type(history['operations']) is not list or len(history['operations'])>1):raise ValueError()
            summaries=[]
            for operation in history['operations']:
                _uuid(operation.get('receiving_id'));_integer(operation.get('revision'));_hash(operation.get('plan_hash'))
                if (operation.get('physical_intake_id')!=anchor['claimId'] or operation.get('physical_manifest_hash')!=anchor['claimHash']
                    or operation.get('workflow_kind') not in ('inventory_only','accepted_trade')
                    or operation.get('state') not in ('ready','cancelled_before_write','catalog_pending','catalog_confirmed',
                        'inventory_pending','outcome_unknown','needs_review','received')):raise ValueError()
                summaries.append({'receivingId':operation['receiving_id'],'serverRevision':operation['revision'],
                    'planHash':operation['plan_hash'],'workflowKind':operation['workflow_kind'],'state':operation['state']})
        except (ValueError,TypeError,KeyError,AttributeError,ManifestError):raise _error() from None
        if self.service.profile.binding()!={'tenant_id':bundle['tenantId'],'service_origin':ORIGIN}:
            raise _error('Use the original store’s bound hosted profile.',code='INTAKE_RECEIVING_PROFILE_REQUIRED')
        return summaries

    def _finish(self,anchor,bundle,expected,result):
        require_ordinary_receipt(anchor['receipt'])
        # Complete DTO/size work outside both local guards.
        encoded=json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
        if len(encoded)>2_000_000:raise _error('This original review exceeds the supported page size. Keep the saved plan for owner review.')
        current=self.cloud.record_authority(expected_session_id=expected)
        if any(current.get(key)!=bundle[key] for key in ('sessionId','tenantId','store','role')):raise _error()
        if current.get('role') not in ('owner','manager'):raise _error(status=403)
        if self.service.profile.binding()!={'tenant_id':bundle['tenantId'],'service_origin':ORIGIN}:raise _error()
        with self.cloud._session_guard(),self.service.lock:
            saved=self.cloud.assert_recovery_session_unlocked(expected,bundle['tenantId'],bundle['store'])
            if saved.get('role') not in ('owner','manager'):raise _error(status=403)
            with self.service.store.db() as db:
                row=db.execute('SELECT body,deleted_at FROM receipts WHERE id=?',(anchor['receipt']['id'],)).fetchone()
            if not row or tuple(row)!=(anchor['raw'],anchor['deleted']):raise _error()
        return result

    def history(self,data,expected):
        self._input(data,expected,'history')
        with self.cloud.lock:
            anchor=self._anchor(data)
            bundle=self.cloud.read_intake_receiving_history(anchor['claimId'],expected_session_id=expected)
            summaries=self._binding(anchor,bundle,expected)
            result={'version':1,'receiptId':data['receiptId'],'revision':data['revision'],
                'physicalIntakeId':anchor['claimId'],'physicalManifestHash':anchor['claimHash'],
                'physicalReference':bundle['claim']['physical_reference'],'lookupComplete':True,'operations':summaries}
            return self._finish(anchor,bundle,expected,result)

    def _proof(self,data,expected):
        anchor=self._anchor(data)
        bundle=self.cloud.read_intake_receiving_source(anchor['claimId'],data['receivingId'],expected_session_id=expected)
        summaries=self._binding(anchor,bundle,expected,preview=True)
        if (len(summaries)!=1 or summaries[0]['receivingId']!=data['receivingId']
            or summaries[0]['serverRevision']!=data['expectedServerRevision'] or summaries[0]['planHash']!=data['expectedPlanHash']
            or summaries[0]['workflowKind']!='inventory_only'):raise _error()
        try:
            proof=verify_restoration_source(bundle['restorationSource'],bundle['operation'],bundle['claim'],accepted_receipt=None)
            if (proof['workflowKind']!='inventory_only' or proof['receivingId']!=data['receivingId']
                or proof['serverRevision']!=data['expectedServerRevision'] or proof['planHash']!=data['expectedPlanHash']):raise ValueError()
            original={row['source_line_key']:row['quantity'] for row in source_lines(bundle['claim']['source_manifest'])}
            selected={}
            for row in proof['stockRows']:
                key=row['sourceLineKey'];selected[key]=selected.get(key,0)+row['quantity']
            excluded={key:quantity-selected.get(key,0) for key,quantity in original.items()}
            if (any(key not in original or quantity>original[key] for key,quantity in selected.items())
                or fingerprint(selected)!=fingerprint(bundle['operation']['selected_quantities'])
                or fingerprint(excluded)!=fingerprint(bundle['operation']['excluded_quantities'])):raise ValueError()
            counts={'basis':'physical_intake','physicalOriginalCopies':sum(original.values()),
                    'selectedCopies':sum(selected.values()),'excludedCopies':sum(excluded.values())}
        except (ValueError,TypeError,KeyError,AttributeError,ManifestError,RestorationProofError):
            raise _error('The original stock evidence could not be verified. Keep its plan for owner review.',code='INTAKE_RECEIVING_PROOF_INVALID') from None
        return anchor,bundle,proof,counts

    def preview(self,data,expected):
        self._input(data,expected,'preview')
        with self.cloud.lock:
            anchor,bundle,proof,counts=self._proof(data,expected)
            preview={key:copy.deepcopy(proof[key]) for key in ('receivingId','serverRevision','planHash','sourceSnapshotHash',
                'restorationSourceHash','stateBasisHash','proofHash','workflowKind','state','admission','provider','labelProfile',
                'received','terminalReceiptHash','labelsVerified','labelError')}
            preview.update(version=1,sourceKind='physical_intake',receiptId=data['receiptId'],revision=data['revision'],
                physicalIntakeId=anchor['claimId'],physicalManifestHash=anchor['claimHash'],
                physicalReference=bundle['claim']['physical_reference'],counts=counts,rows=copy.deepcopy(proof['stockRows'][:100]),
                rowsOffset=0,rowsLimit=100,totalRows=len(proof['stockRows']),allowedActions=[])
            result={'preview':preview,'readOnly':True,'providerActionPerformed':False,'financialActionPerformed':False}
            return self._finish(anchor,bundle,expected,result)

    def rows(self,data,expected):
        self._input(data,expected,'rows')
        with self.cloud.lock:
            anchor,bundle,proof,_=self._proof(data,expected)
            if proof['proofHash']!=data['proofHash']:raise _error()
            offset=data['offset'];limit=data['limit'];total=len(proof['stockRows'])
            if offset>=total:raise _error('Choose a page within the original plan.',status=400,code='INTAKE_RECEIVING_INPUT_INVALID')
            result={'version':1,'receiptId':data['receiptId'],'revision':data['revision'],'receivingId':proof['receivingId'],
                'serverRevision':proof['serverRevision'],'planHash':proof['planHash'],'proofHash':proof['proofHash'],
                'offset':offset,'limit':limit,'totalRows':total,'rows':copy.deepcopy(proof['stockRows'][offset:offset+limit])}
            return self._finish(anchor,bundle,expected,result)
