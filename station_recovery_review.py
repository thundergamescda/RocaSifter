"""Explicit retail review and read-only receiving history for original projections."""
import copy

from station_cloud import CloudError, ORIGIN
from station_store import StationError
from square_variations import price_cents


def require_recovery_retail(receipt):
    if 'original_trade_recovery' not in receipt:return
    from station_trade_recovery import recovered_marker,validate_recovered_projection
    marker=recovered_marker(receipt);validate_recovered_projection(receipt)
    if receipt.get('retail_review_required') is not False:
        raise StationError('Explicitly review today’s retail prices for this original trade before labels, listings or new receiving.')
    if marker['inventoryVerification']=='not_checked':
        raise StationError('Check the original hosted inventory history before labels, listings or new receiving.')
    if marker['inventoryVerification']=='existing_operation':
        intent=receipt.get('hosted_receive') or {}
        if intent.get('receiving_id')!=marker['receivingId'] or intent.get('state')!='confirmed':
            raise StationError('Recover the existing hosted receiving operation before producing stock labels or listings. Starting another plan is locked.')


def require_recovery_outputs(receipt):
    require_recovery_retail(receipt)
    if 'original_trade_recovery' in receipt and receipt.get('hosted_receive',{}).get('state')!='confirmed':
        raise StationError('Recover or complete the exact hosted receiving operation before generating retail labels or listings for this original trade. A previous empty history check is not SKU confirmation.')


class OriginalTradeReview:
    def __init__(self,service):self.service=service

    def _local(self,ident,revision):
        from station_trade_recovery import recovered_marker,validate_recovered_projection
        receipt=self.service.active_receipt(ident)
        if receipt['revision']!=revision:raise StationError('This original trade changed. Reload it before reviewing.')
        marker=recovered_marker(receipt)
        if not marker:raise StationError('Choose a recovered original trade.')
        validate_recovered_projection(receipt)
        return receipt,marker

    def _authority(self,receipt,marker,expected,*,inventory=False):
        cloud=self.service.cloud
        evidence=(cloud.original_trade_inventory if inventory else cloud.read_original_trade)(marker['tradeId'],expected_session_id=expected)
        if (marker['serviceOrigin']!=ORIGIN or marker['tenantId']!=evidence['tenantId']
            or marker['receiptHash']!=evidence['trade'].get('receipt_hash')
            or receipt['intake_claim']['physical_intake_id']!=evidence['claim'].get('physical_intake_id')):
            raise CloudError('This original trade no longer matches its authorized hosted evidence. Keep the original record for owner review.',status=409)
        from intake_manifest import validate_manifest,manifest_digest,semantic_fingerprint,ManifestError
        from station_intake_claim import required_manifest
        try:
            manifest=required_manifest(receipt);remote=validate_manifest(evidence['claim'].get('source_manifest'))
            if (remote!=manifest or evidence['claim'].get('manifest_hash')!=manifest_digest(manifest)
                or evidence['claim'].get('semantic_hash')!=semantic_fingerprint(manifest)):
                raise ManifestError('Original source binding changed.')
        except ManifestError:
            raise CloudError('The original physical-source evidence changed. Keep the original trade for owner review.',status=502) from None
        self.service.shared_trades._terminal(receipt,evidence['trade'])
        self.service.shared_trades._confirmed_claim(receipt,evidence['trade'],expected,evidence['claim'])
        return evidence

    @staticmethod
    def verify_history(evidence):
        value=evidence.get('history');claim=evidence['claim'];trade=evidence['trade']
        if (not isinstance(value,dict) or set(value)!={'version','physical_intake_id','operations','lookup_complete'}
            or type(value.get('version')) is not int or value['version']!=1
            or value.get('physical_intake_id')!=claim['physical_intake_id'] or value.get('lookup_complete') is not True
            or not isinstance(value.get('operations'),list) or len(value['operations'])>1):
            raise CloudError('The hosted inventory history could not be verified. New receiving stays locked.',status=502)
        from station_hosted_receive import uuid_value,STATES,HASH
        for operation in value['operations']:
            if (not isinstance(operation,dict) or operation.get('physical_intake_id')!=claim['physical_intake_id']
                or operation.get('physical_manifest_hash')!=claim['manifest_hash']
                or operation.get('state') not in STATES or type(operation.get('revision')) is not int or operation['revision']<0
                or not isinstance(operation.get('plan_hash'),str) or not HASH.fullmatch(operation['plan_hash'])):
                raise CloudError('The original inventory operation has inconsistent source evidence. New receiving stays locked.',status=502)
            ident=uuid_value(operation.get('receiving_id'))
            if ident!=operation.get('receiving_id'):raise CloudError('The original inventory operation ID is invalid.',status=502)
            if (operation.get('workflow_kind')!='accepted_trade' or operation.get('accepted_trade_id')!=trade['id']
                or operation.get('accepted_receipt_hash')!=trade.get('receipt_hash')):
                # Historical cancelled inventory-only plans are still occupancy,
                # never authority to reuse this intake for a new plan.
                if operation.get('workflow_kind')!='inventory_only' or operation.get('state')!='cancelled_before_write':
                    raise CloudError('The original inventory operation does not match this accepted trade.',status=502)
        return value['operations'][0] if value['operations'] else None

    def inventory(self,data,expected):
        with self.service.cloud.lock:
            with self.service.lock:receipt,marker=self._local(str(data.get('receiptId','')),data.get('revision'))
            evidence=self._authority(receipt,marker,expected,inventory=True);operation=self.verify_history(evidence)
            state='existing_operation' if operation else 'no_operation'
            with self.service.cloud._session_guard():
                self.service.cloud.assert_recovery_session_unlocked(expected,evidence['tenantId'],evidence['store'])
                with self.service.lock:
                    current,current_marker=self._local(receipt['id'],receipt['revision'])
                    if current_marker!=marker:raise StationError('The recovered record changed. Check its inventory history again.')
                    updated=copy.deepcopy(marker);updated.update(inventoryVerification=state,receivingId=operation['receiving_id'] if operation else None)
                    if updated!=marker:
                        current['original_trade_recovery']=updated
                        self.service.store.save_receipt(current,('original_trade_inventory_history','Authorized original receiving history checked; no provider, financial or journal write.'))
            return {'receipt':current,'inventory':{'verification':state,'receivingId':updated['receivingId'],'operation':operation,'lookupComplete':True}}

    def retail(self,data,expected):
        if data.get('retailReviewed') is not True:raise StationError('Explicitly confirm review of today’s retail prices.')
        with self.service.cloud.lock:
            with self.service.lock:
                receipt,marker=self._local(str(data.get('receiptId','')),data.get('revision'))
                cards=[self.service.validate_card(card) for card in receipt['cards'] if card.get('include',True)]
                if (not cards or any(card.get('reviewed') is not True or card.get('price_missing')
                    or price_cents(card)==0 and card.get('price_confirmed') is not True for card in cards)):
                    raise StationError('Review every included card, enter today’s retail price and explicitly confirm any zero price.')
            evidence=self._authority(receipt,marker,expected)
            with self.service.cloud._session_guard():
                self.service.cloud.assert_recovery_session_unlocked(expected,evidence['tenantId'],evidence['store'])
                with self.service.lock:
                    current,current_marker=self._local(receipt['id'],receipt['revision'])
                    if current_marker!=marker:raise StationError('The recovered record changed. Review it again.')
                    if current.get('retail_review_required') is not False:
                        current['retail_review_required']=False
                        self.service.store.save_receipt(current,('original_trade_retail_review','Operator explicitly reviewed current selling prices; original accepted financial terms kept.'))
            return {'receipt':current}
