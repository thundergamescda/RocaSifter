"""Fixed server-only Square receiving adapter. Never a generic request proxy."""
from datetime import datetime, timezone, timedelta
import hmac
import json
import time
from .receiving_plan import (API_VERSION, READ_SCOPES, WRITE_SCOPES, ReceivingError,
                             canonical, fingerprint, validate_inventory_ack)
from .security import ServiceError


class ReceivingSquare:
    def __init__(self, square):self.square=square

    def access(self, tenant, *, write=False, catalog=False):
        required=WRITE_SCOPES+('ITEMS_WRITE',) if catalog else WRITE_SCOPES if write else READ_SCOPES
        return self.square.verified_access(tenant,required)

    def assert_current(self, db, tenant, access):
        row=db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s FOR SHARE',(tenant,)).fetchone()
        try:
            if not row or row['revoked_at'] or any(row[key]!=access.proof[key] for key in ('merchant_id','environment')):raise ValueError()
            tokens=json.loads(self.square.cipher.decrypt(row['encrypted_tokens'].encode()))
            if not hmac.compare_digest(tokens['access_token'],access.token):raise ValueError()
            if access.proof['application_id']!=self.square.config['SQUARE_APPLICATION_ID']:raise ValueError()
            if datetime.fromisoformat(access.proof['expires_at'])<=datetime.now(timezone.utc)+timedelta(minutes=1):raise ValueError()
        except Exception:raise ReceivingError('Square authorization changed. Recover or reauthorize this exact operation.',code='OAUTH_RECONSENT_REQUIRED') from None

    def _call(self, access, endpoint, payload=None, *, method='POST', body=None, deadline=None):
        response=None
        try:
            headers={'Authorization':'Bearer '+access.token,'Square-Version':API_VERSION,'Content-Type':'application/json'}
            timeout=min(20,max(.1,deadline-time.monotonic())) if deadline else 20
            if deadline and time.monotonic()>=deadline:raise ValueError()
            if method=='GET':
                response=self.square.transport.get(self.square.base+endpoint,headers=headers,timeout=timeout,allow_redirects=False,stream=True)
            else:
                response=self.square.transport.post(self.square.base+endpoint,headers=headers,
                    data=body if body is not None else canonical(payload),timeout=timeout,allow_redirects=False,stream=True)
            if response.status_code!=200:raise ValueError()
            length=response.headers.get('Content-Length')
            if length is not None and (not str(length).isdigit() or int(length)>4*1024*1024):raise ValueError()
            chunks=[];size=0
            for chunk in response.iter_content(chunk_size=8192):
                if deadline and time.monotonic()>=deadline:raise ValueError()
                if not isinstance(chunk,bytes):raise ValueError()
                size+=len(chunk)
                if size>4*1024*1024:raise ValueError()
                chunks.append(chunk)
            result=json.loads(b''.join(chunks).decode('utf-8'))
            if not isinstance(result,dict) or result.get('errors'):raise ValueError()
            return result
        except Exception:
            raise ReceivingError('Square did not provide a complete verified response. Preserve this operation for recovery.',
                                 502,code='PROVIDER_OUTCOME_UNKNOWN') from None
        finally:
            if response is not None:response.close()

    def catalog(self, access, ids):
        ids=sorted(set(ids))
        if not 1<=len(ids)<=200:raise ServiceError('Catalog reads need 1 to 200 server-owned IDs.')
        result=self._call(access,'/v2/catalog/batch-retrieve',{'object_ids':ids,'include_related_objects':True})
        objects={}
        for field in ('objects','related_objects'):
            values=result.get(field,[])
            if not isinstance(values,list) or len(values)>1000:raise ReceivingError('The catalog response exceeds the supported bound.')
            for obj in values:
                if not isinstance(obj,dict) or not isinstance(obj.get('id'),str):raise ReceivingError('Catalog object evidence is invalid.')
                if obj['id'] in objects and objects[obj['id']]!=obj:raise ReceivingError('Catalog object evidence conflicts.')
                objects[obj['id']]=obj
        if not set(ids).issubset(objects):raise ReceivingError('A required current catalog object is missing.')
        return objects

    def assert_skus(self, access, targets):
        expected={};original={}
        for target in targets:
            sku=target.get('target_sku',target.get('sku'));ident=target.get('variation_id')
            if not isinstance(sku,str) or not 1<=len(sku)<=100:raise ReceivingError('The retained SKU needs review.')
            key=sku.casefold()
            if key in expected and expected[key]!=ident:raise ReceivingError('Two targets share an ambiguous SKU.',code='SKU_COLLISION_REVIEW_REQUIRED')
            expected[key]=ident;original[key]=sku
        if not 1<=len(expected)<=100:raise ReceivingError('Use a bounded set of retained SKUs.')
        query={'object_types':['ITEM_VARIATION'],'include_deleted_objects':False,'limit':1000,
               'query':{'set_query':{'attribute_name':'sku','attribute_values':list(original.values())}}}
        found={key:set() for key in expected};seen={};cursors=set();size=0;deadline=time.monotonic()+45
        for _ in range(20):
            result=self._call(access,'/v2/catalog/search',query,deadline=deadline);size+=len(canonical(result))
            if size>8*1024*1024 or time.monotonic()>=deadline:raise ReceivingError('Complete SKU collision proof exceeds the supported bound.',code='SKU_COLLISION_REVIEW_REQUIRED')
            rows=result.get('objects',[])
            if not isinstance(rows,list) or len(rows)>1000:raise ReceivingError('SKU search evidence is unavailable.')
            for row in rows:
                if not isinstance(row,dict) or row.get('type')!='ITEM_VARIATION':raise ReceivingError('SKU search returned an unexpected object.')
                if row.get('is_deleted'):continue
                sku=(row.get('item_variation_data') or {}).get('sku');ident=row.get('id')
                if not isinstance(sku,str) or sku.casefold() not in expected or not isinstance(ident,str) or not ident:
                    raise ReceivingError('SKU search returned inconsistent evidence.')
                if ident in seen and seen[ident]!=row:raise ReceivingError('SKU search changed during pagination.')
                seen[ident]=row;found[sku.casefold()].add(ident)
                if len(found[sku.casefold()])>1:raise ReceivingError('An identical SKU belongs to multiple Square variations.',code='SKU_COLLISION_REVIEW_REQUIRED')
            cursor=result.get('cursor')
            if not cursor:
                if any(found[key] != ({ident} if ident else set()) for key,ident in expected.items()):
                    raise ReceivingError('The retained SKU is missing or belongs to another variation.',code='SKU_COLLISION_REVIEW_REQUIRED')
                return
            if not isinstance(cursor,str) or not 1<=len(cursor)<=4096 or cursor in cursors:raise ReceivingError('SKU pagination is incomplete or inconsistent.')
            cursors.add(cursor);query=dict(query,cursor=cursor)
        raise ReceivingError('Complete SKU uniqueness proof exceeds the page bound.',code='SKU_COLLISION_REVIEW_REQUIRED')

    def location(self, access, location_id):
        result=self._call(access,'/v2/locations',method='GET');values=result.get('locations')
        if not isinstance(values,list) or len(values)>1000:raise ReceivingError('Location evidence is unavailable.')
        found=[row for row in values if isinstance(row,dict) and row.get('id')==location_id]
        if len(found)!=1 or found[0].get('status')!='ACTIVE' or found[0].get('currency')!='USD':
            raise ReceivingError('Select an active USD location belonging to this Square account.')
        return {'id':location_id,'name':found[0].get('name',location_id),'status':'ACTIVE','currency':'USD'}

    def locations(self, access):
        result=self._call(access,'/v2/locations',method='GET');values=result.get('locations')
        if not isinstance(values,list) or len(values)>1000:raise ReceivingError('Location evidence is unavailable.')
        return [{'id':row['id'],'name':row.get('name',''),'status':'ACTIVE','currency':'USD'} for row in values
                if isinstance(row,dict) and isinstance(row.get('id'),str) and row.get('status')=='ACTIVE' and row.get('currency')=='USD']

    def dispatch_inventory(self, access, body_bytes):
        body=json.loads(body_bytes)
        result=self._call(access,'/v2/inventory/changes/batch-create',body=body_bytes)
        return validate_inventory_ack(body,result)

    def dispatch_catalog(self,access,body_bytes,additions):
        from .receiving_catalog import catalog_ack
        body=json.loads(body_bytes)
        result=self._call(access,'/v2/catalog/batch-upsert',body=body_bytes)
        return catalog_ack(body,result,additions)

    def history(self, access, body, first_admitted_at):
        objects=sorted({row['adjustment']['catalog_object_id'] for row in body['changes']})
        locations=sorted({row['adjustment']['to_location_id'] for row in body['changes']})
        query={'catalog_object_ids':objects,'location_ids':locations,'types':['ADJUSTMENT'],'limit':1000,
               'updated_after':(first_admitted_at-timedelta(days=1)).astimezone(timezone.utc).isoformat()}
        expected={row['adjustment']['reference_id'] for row in body['changes']}
        changes={};cursors=set();aggregate_bytes=0;deadline=time.monotonic()+45
        for _ in range(100):
            result=self._call(access,'/v2/inventory/changes/batch-retrieve',query,deadline=deadline)
            aggregate_bytes+=len(canonical(result))
            if aggregate_bytes>8*1024*1024 or time.monotonic()>=deadline:
                raise ReceivingError('Complete history exceeds the recovery resource budget.',code='PROVIDER_OUTCOME_UNKNOWN')
            rows=result.get('changes')
            if not isinstance(rows,list) or len(rows)>1000:raise ReceivingError('Inventory history is incomplete.',code='PROVIDER_OUTCOME_UNKNOWN')
            for row in rows:
                value=row.get('adjustment') if isinstance(row,dict) and row.get('type')=='ADJUSTMENT' else None
                if not isinstance(value,dict) or value.get('reference_id') not in expected:continue
                ident=value.get('id')
                if not isinstance(ident,str) or not ident or (ident in changes and changes[ident]!=row):
                    raise ReceivingError('Matching history evidence is incomplete or conflicting.',code='PROVIDER_OUTCOME_UNKNOWN')
                changes[ident]=row
                if len(changes)>2*len(expected):raise ReceivingError('Duplicate history references require review.',code='PROVIDER_OUTCOME_UNKNOWN')
            cursor=result.get('cursor')
            if not cursor:return list(changes.values())
            if not isinstance(cursor,str) or not 1<=len(cursor)<=4096 or cursor in cursors:
                raise ReceivingError('Inventory history pagination is inconsistent.',code='PROVIDER_OUTCOME_UNKNOWN')
            cursors.add(cursor);query=dict(query,cursor=cursor)
        raise ReceivingError('Inventory history exceeds the supported complete-query bound.',code='PROVIDER_OUTCOME_UNKNOWN')
