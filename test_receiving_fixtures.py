"""Fictional injected provider only. Application code never imports this file."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
import uuid
from hosted.square import VerifiedSquareAccess
from hosted.receiving_plan import API_VERSION, READ_SCOPES, WRITE_SCOPES, ReceivingError, validate_inventory_ack


def catalog_fixture(cents=1000):
    variation={'id':'fixture-variation','type':'ITEM_VARIATION','version':17,'present_at_all_locations':True,
        'item_variation_data':{'item_id':'fixture-parent','name':'Retained old stock','sku':'000000001234',
            'pricing_type':'FIXED_PRICING','price_money':{'amount':cents,'currency':'USD'},'track_inventory':True,
            'stockable':True,'sellable':True,'user_data':'{"fictional":"unchanged"}'}}
    parent={'id':'fixture-parent','type':'ITEM','version':17,'present_at_all_locations':True,
        'item_data':{'name':'Fictional parent','product_type':'REGULAR','tax_ids':['fixture-tax'],
                     'variations':[deepcopy(variation)],'description':'Preserve existing description'}}
    return {variation['id']:variation,parent['id']:parent,'fixture-tax':{'id':'fixture-tax','type':'TAX','version':17,
            'tax_data':{'name':'Fictional existing tax setting','percentage':'6','inclusion_type':'ADDITIVE','calculation_phase':'TAX_SUBTOTAL_PHASE','enabled':True}}}


class FictionalReceivingProvider:
    def __init__(self, database, tenant):
        self.database,self.tenant=database,tenant;self.objects=catalog_fixture();self.calls=[];self.history_rows=[]
        self.access_calls=[];self.behavior='success';self.after_dispatch=None;self.revoked=False
        self.catalog_calls=[];self.catalog_behavior='success';self.after_catalog=None
        self.proof={'merchant_id':'fixture-merchant-'+tenant.hex,'environment':'sandbox','application_id':'fixture-application',
            'scopes':list(WRITE_SCOPES)+['ITEMS_WRITE'],'required_scopes':list(WRITE_SCOPES),'expires_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
            'checked_at':datetime.now(timezone.utc).isoformat(),'square_version':API_VERSION,'method':'fictional-explicit-test-fixture','grants_verified':True}

    def access(self, tenant, *, write=False, catalog=False):
        if tenant!=self.tenant or self.revoked:raise ReceivingError('Fictional provider authority unavailable.')
        self.access_calls.append(write)
        return VerifiedSquareAccess('fictional-server-token-never-to-client',deepcopy(self.proof))

    def assert_current(self, db, tenant, access):
        if tenant!=self.tenant or self.revoked or access.proof['merchant_id']!=self.proof['merchant_id']:
            raise ReceivingError('Fictional provider principal changed.')

    def locations(self, access):return [{'id':'fixture-location','name':'Fictional till','status':'ACTIVE','currency':'USD'}]
    def location(self, access, ident):
        if ident!='fixture-location':raise ReceivingError('Fictional location does not belong to this provider.')
        return self.locations(access)[0]

    def catalog(self, access, ids):
        if any(ident not in self.objects for ident in ids):raise ReceivingError('Fictional catalog target missing.')
        return deepcopy(self.objects)

    def assert_skus(self, access, targets):
        for target in targets:
            sku=target.get('target_sku',target.get('sku'));expected=target.get('variation_id')
            found={value['id'] for value in self.objects.values() if value.get('type')=='ITEM_VARIATION' and
                   not value.get('is_deleted') and (value.get('item_variation_data') or {}).get('sku','').casefold()==sku.casefold()}
            if found!=({expected} if expected else set()):raise ReceivingError('Fictional SKU collision.',code='SKU_COLLISION_REVIEW_REQUIRED')

    def dispatch_inventory(self, access, body_bytes):
        body=json.loads(body_bytes);self.calls.append(bytes(body_bytes))
        if self.behavior=='before_effect':raise ReceivingError('Fictional lost request.',code='PROVIDER_OUTCOME_UNKNOWN')
        counts=[]
        for row in body['changes']:
            value=deepcopy(row['adjustment'])|{'id':'fixture-adjustment-'+uuid.uuid4().hex,
                'source':{'application_id':self.proof['application_id']}}
            self.history_rows.append({'type':'ADJUSTMENT','adjustment':value})
            counts.append({'catalog_object_id':value['catalog_object_id'],'catalog_object_type':'ITEM_VARIATION',
                           'location_id':value['to_location_id'],'state':'IN_STOCK','quantity':'27'})
        if self.after_dispatch:self.after_dispatch()
        if self.behavior=='lost_response':raise ReceivingError('Fictional response lost after application.',code='PROVIDER_OUTCOME_UNKNOWN')
        return validate_inventory_ack(body,{'counts':counts,'errors':[]})

    def history(self, access, body, first_admitted_at):return deepcopy(self.history_rows)

    def dispatch_catalog(self,access,body_bytes,additions):
        from hosted.receiving_catalog import catalog_ack
        body=json.loads(body_bytes);self.catalog_calls.append(bytes(body_bytes))
        if self.catalog_behavior=='before_effect':raise ReceivingError('Fictional lost catalog request.')
        parents=deepcopy(body['batches'][0]['objects']);mappings=[]
        for parent in parents:
            parent['version']+=1
            for child in parent['item_data']['variations']:
                if child['id'].startswith('#'):
                    actual='fixture-price-'+uuid.uuid4().hex;mappings.append({'client_object_id':child['id'],'object_id':actual});child['id']=actual
                    child['version']=parent['version']
                self.objects[child['id']]=deepcopy(child)
            self.objects[parent['id']]=deepcopy(parent)
        if self.after_catalog:self.after_catalog()
        if self.catalog_behavior=='lost_response':raise ReceivingError('Fictional catalog response lost after effect.')
        return catalog_ack(body,{'objects':parents,'id_mappings':mappings},additions)
