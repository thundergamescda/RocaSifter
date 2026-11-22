"""Conservative zero-stock eligibility journal. No Square delete capability."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import json
from station_store import StationError


_CONTEXT_FIELDS = frozenset(('id','sku','parent','version','locations','blocked','continuityVersion'))


def _text(value):
    return type(value) is str and bool(value.strip())


def _aware_datetime(value):
    if type(value) is not datetime:return None
    try:
        if value.tzinfo is None or value.utcoffset() is None:return None
        value.astimezone(timezone.utc)  # Reject instants outside datetime's range.
    except (ValueError,TypeError,OverflowError):return None
    return value


def _observation_time(value):
    value=datetime.now(timezone.utc) if value is None else value
    if _aware_datetime(value) is None:
        raise StationError('A valid timezone-aware observation is required.')
    return value


def _stored_time(value):
    if type(value) is not str or not 1<=len(value)<=64:return None
    try:return _aware_datetime(datetime.fromisoformat(value))
    except (ValueError,TypeError,OverflowError):return None


def _stored_history(row):
    checked=_stored_time(row['checked'])
    raw_zero=row['zero_since']
    zero=_stored_time(raw_zero) if raw_zero is not None else None
    valid=(checked is not None and
           (raw_zero is None or zero is not None and zero<=checked))
    return checked,zero,valid


def _locations(value, *, stored=False):
    if stored:
        if type(value) is not list:return None
    elif type(value) not in (list,tuple,set,frozenset):return None
    if not value or not all(_text(item) for item in value):return None
    if len(value)!=len(set(value)):return None
    ordered=sorted(value)
    if stored and value!=ordered:return None
    return ordered


def _complete_context(value, key):
    if (type(value) is not dict or not all(type(field) is str for field in value)
        or set(value)!=_CONTEXT_FIELDS):return False
    return (_text(key) and _text(value['id']) and value['id']==key
            and _text(value['sku']) and _text(value['parent'])
            and type(value['version']) is int and 1<=value['version']<=9223372036854775807
            and type(value['blocked']) is bool
            and type(value['continuityVersion']) is int and value['continuityVersion']==1
            and _locations(value['locations'],stored=True) is not None)


def _stored_context(body, key):
    try:context=json.loads(body)
    except (ValueError,TypeError,RecursionError):return None
    return context if _complete_context(context,key) else None


def _incomplete_context(key):
    return {'id':key,'sku':None,'parent':None,'version':None,
            'locations':[],'blocked':True,'continuityVersion':1}


def _current_context(variation, locations, blocked):
    data=variation.get('item_variation_data')
    if type(data) is not dict:return None
    ordered=_locations(locations)
    if ordered is None:return None
    context={'id':variation['id'],'sku':data.get('sku'),'parent':data.get('item_id'),
             'version':variation.get('version'),'locations':ordered,
             'blocked':blocked,'continuityVersion':1}
    return context if _complete_context(context,variation['id']) else None


def _public_context(context):
    return {'id':context['id'],'sku':context['sku'],'parent':context['parent'],
            'version':context['version'],'locations':list(context['locations']),
            'blocked':context['blocked']}


class Cleanup:
    def __init__(self,store):
        self.store=store
        with store.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS cleanup_observations (variation TEXT PRIMARY KEY, zero_since TEXT, checked TEXT, body TEXT)')

    def observe(self,variation,counts,expected_locations,*,at=None,blocked=False):
        """Only trusted complete adapter snapshots may advance eligibility.

        Observations older than 24h break continuity; absent counts never mean zero.
        """
        if type(variation) is not dict or not _text(variation.get('id')):
            raise StationError('A valid catalog variation id is required.')
        key=variation['id']
        at=_observation_time(at)
        context=_current_context(variation,expected_locations,blocked)
        body=context if context is not None else _incomplete_context(key)
        eligible=False
        if context is not None:
            data=variation['item_variation_data']
            eligible=variation.get('type')=='ITEM_VARIATION' and not variation.get('is_deleted') and not blocked
            eligible=eligible and data.get('track_inventory') is True and bool(data.get('sku'))
            by_location={}
            for count in counts:
                if count.get('catalog_object_id')!=key or count.get('state')!='IN_STOCK':continue
                loc=count.get('location_id')
                if loc in by_location:eligible=False
                try:by_location[loc]=Decimal(str(count['quantity']))
                except (InvalidOperation,KeyError):eligible=False
            eligible=eligible and set(by_location)==set(context['locations'])
            eligible=eligible and all(q.is_finite() and q==0 for q in by_location.values())
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM cleanup_observations WHERE variation=?',(key,)).fetchone()
            checked,old_zero,valid_history=_stored_history(old) if old else (None,None,False)
            if checked is not None and at<checked:raise StationError('Out-of-order inventory observation.')
            previous=_stored_context(old['body'],key) if old else None
            # Unreadable history proves no elapsed zero-stock time. A fresh
            # trusted observation can restart it, never inherit its age.
            continuous=(context is not None and valid_history and old_zero is not None and previous==context
                        and timedelta(0)<=at-checked<=timedelta(hours=24))
            zero=(old['zero_since'] if continuous else at.isoformat()) if eligible else None
            db.execute('INSERT OR REPLACE INTO cleanup_observations VALUES (?,?,?,?)',(key,zero,at.isoformat(),json.dumps(body)))

    def preview(self,*,at=None,opted_in=False):
        at=_observation_time(at)
        with self.store.db() as db:
            rows=db.execute('SELECT * FROM cleanup_observations ORDER BY variation').fetchall()
        result=[]
        for row in rows:
            context=_stored_context(row['body'],row['variation'])
            checked,zero,valid_history=_stored_history(row)
            if context is None or not valid_history or context['blocked'] and zero is not None:
                result.append(_public_context(_incomplete_context(row['variation']))|
                              {'zero_since':None,'verified_days':0,'candidate':False})
                continue
            age=(at-zero).total_seconds()/86400 if zero is not None else 0
            fresh=timedelta(0)<=at-checked<=timedelta(hours=24)
            result.append(_public_context(context)|{'zero_since':row['zero_since'],'verified_days':max(0,round(age,3)),
                         'candidate':bool(opted_in and not context['blocked'] and fresh and zero is not None and age>=30)})
        return {'mode':'dry-run-only','deletionEnabled':False,'optedIn':opted_in,'items':result,
                'reason':'No live deletion. Confirm complete location/channel/order coverage and perform a fresh Square preflight before enabling deletion.'}
