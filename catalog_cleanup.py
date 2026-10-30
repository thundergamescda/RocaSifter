"""Conservative zero-stock eligibility journal. No Square delete capability."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import json
from station_store import StationError


class Cleanup:
    def __init__(self,store):
        self.store=store
        with store.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS cleanup_observations (variation TEXT PRIMARY KEY, zero_since TEXT, checked TEXT, body TEXT)')

    def observe(self,variation,counts,expected_locations,*,at=None,blocked=False):
        """Only trusted complete adapter snapshots may advance eligibility.

        Observations older than 24h break continuity; absent counts never mean zero.
        """
        at=at or datetime.now(timezone.utc)
        if at.tzinfo is None:raise StationError('A timezone-aware observation is required.')
        eligible=bool(expected_locations) and variation.get('type')=='ITEM_VARIATION' and not variation.get('is_deleted') and not blocked
        data=variation.get('item_variation_data',{})
        eligible=eligible and data.get('track_inventory') is True and bool(data.get('sku'))
        by_location={}
        for count in counts:
            if count.get('catalog_object_id')!=variation.get('id') or count.get('state')!='IN_STOCK':continue
            loc=count.get('location_id')
            if loc in by_location:eligible=False
            try:by_location[loc]=Decimal(str(count['quantity']))
            except (InvalidOperation,KeyError):eligible=False
        eligible=eligible and set(by_location)==set(expected_locations)
        eligible=eligible and all(q.is_finite() and q==0 for q in by_location.values())
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM cleanup_observations WHERE variation=?',(variation['id'],)).fetchone()
            if old and at<datetime.fromisoformat(old['checked']):raise StationError('Out-of-order inventory observation.')
            continuous=old and old['zero_since'] and at-datetime.fromisoformat(old['checked'])<=timedelta(hours=24)
            zero=(old['zero_since'] if continuous else at.isoformat()) if eligible else None
            body={'id':variation['id'],'sku':data.get('sku'),'parent':data.get('item_id'),
                  'version':variation.get('version'),'locations':sorted(expected_locations),'blocked':bool(blocked)}
            db.execute('INSERT OR REPLACE INTO cleanup_observations VALUES (?,?,?,?)',(variation['id'],zero,at.isoformat(),json.dumps(body)))

    def preview(self,*,at=None,opted_in=False):
        at=at or datetime.now(timezone.utc)
        with self.store.db() as db:
            rows=db.execute('SELECT * FROM cleanup_observations ORDER BY variation').fetchall()
        result=[]
        for row in rows:
            age=(at-datetime.fromisoformat(row['zero_since'])).total_seconds()/86400 if row['zero_since'] else 0
            fresh=timedelta(0)<=at-datetime.fromisoformat(row['checked'])<=timedelta(hours=24)
            result.append(json.loads(row['body'])|{'zero_since':row['zero_since'],'verified_days':round(age,3),
                         'candidate':bool(opted_in and fresh and row['zero_since'] and age>=30)})
        return {'mode':'dry-run-only','deletionEnabled':False,'optedIn':opted_in,'items':result,
                'reason':'No live deletion. Confirm complete location/channel/order coverage and perform a fresh Square preflight before enabling deletion.'}
