"""Manual staff stock reads; Square quantities never authorize business writes."""
from datetime import datetime, timezone
from decimal import Decimal
import re
import time
import unicodedata

from .cleanup_square import CleanupSquare
from .intake import current_authority
from .receiving_plan import READ_SCOPES, ReceivingError, canonical, fingerprint
from .receiving_square import ReceivingSquare
from .security import ServiceError, provider_id


ROLES = ('owner', 'manager', 'buyer', 'cashier')
_QUANTITY = re.compile(r'[+-]?[0-9]+(?:\.[0-9]{1,5})?\Z')
_RATIO = re.compile(r'[0-9]{1,10}(?:\.[0-9]{1,5})?\Z')
_TIME = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
                   r'(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z')
_UNIT_TYPES={'custom_unit':'TYPE_CUSTOM','area_unit':'TYPE_AREA','length_unit':'TYPE_LENGTH',
             'volume_unit':'TYPE_VOLUME','weight_unit':'TYPE_WEIGHT','generic_unit':'TYPE_GENERIC'}
_STANDARD_UNITS={
    'area_unit':frozenset(('IMPERIAL_ACRE','IMPERIAL_SQUARE_INCH','IMPERIAL_SQUARE_FOOT','IMPERIAL_SQUARE_YARD',
                          'IMPERIAL_SQUARE_MILE','METRIC_SQUARE_CENTIMETER','METRIC_SQUARE_METER','METRIC_SQUARE_KILOMETER')),
    'length_unit':frozenset(('IMPERIAL_INCH','IMPERIAL_FOOT','IMPERIAL_YARD','IMPERIAL_MILE',
                            'METRIC_MILLIMETER','METRIC_CENTIMETER','METRIC_METER','METRIC_KILOMETER')),
    'volume_unit':frozenset(('GENERIC_FLUID_OUNCE','GENERIC_SHOT','GENERIC_CUP','GENERIC_PINT','GENERIC_QUART',
                            'GENERIC_GALLON','IMPERIAL_CUBIC_INCH','IMPERIAL_CUBIC_FOOT','IMPERIAL_CUBIC_YARD',
                            'METRIC_MILLILITER','METRIC_LITER')),
    'weight_unit':frozenset(('IMPERIAL_WEIGHT_OUNCE','IMPERIAL_POUND','IMPERIAL_STONE',
                            'METRIC_MILLIGRAM','METRIC_GRAM','METRIC_KILOGRAM')),
    'generic_unit':frozenset(('UNIT',)),
    'time_unit':frozenset(('GENERIC_MILLISECOND','GENERIC_SECOND','GENERIC_MINUTE','GENERIC_HOUR','GENERIC_DAY')),
}


class _Incomplete(Exception):
    def __init__(self, reason='CATALOG_READ_INCOMPLETE'):self.reason=reason


class _Unsupported(Exception):
    def __init__(self, reason):self.reason=reason


class _Mismatch(Exception):pass


def _text(value, limit=512):
    if (type(value) is not str or len(value)>limit or
            any(unicodedata.category(c) in ('Cc','Cf','Cs') for c in value)):
        raise _Unsupported('CATALOG_TEXT_UNSUPPORTED')
    return value


def _id(value):
    try:
        result=provider_id(value)
        if result.startswith('#'):raise ValueError()
        return result
    except (ServiceError, ValueError):raise _Unsupported('CATALOG_REFERENCE_UNSUPPORTED') from None


def _object(value, kind, ident):
    if (type(value) is not dict or value.get('type')!=kind or value.get('id')!=ident
            or ('is_deleted' in value and value['is_deleted'] is not False)):
        raise _Unsupported('CATALOG_REFERENCE_UNSUPPORTED')
    field={'ITEM':'item_data','ITEM_VARIATION':'item_variation_data',
           'MEASUREMENT_UNIT':'measurement_unit_data'}[kind]
    data=value.get(field)
    if type(data) is not dict:raise _Unsupported('CATALOG_REFERENCE_UNSUPPORTED')
    return data


def _present(value, location):
    # Square defaults this field to true; the unused location list is ignored.
    all_locations=value.get('present_at_all_locations',True)
    if type(all_locations) is not bool:raise _Unsupported('LOCATION_SETTINGS_UNSUPPORTED')
    fields={}
    for name in ('present_at_location_ids','absent_at_location_ids'):
        rows=value.get(name,[])
        if type(rows) is not list or len(rows)>1000:raise _Unsupported('LOCATION_SETTINGS_UNSUPPORTED')
        ids=[_id(row) for row in rows]
        if len(set(ids))!=len(ids):raise _Unsupported('LOCATION_SETTINGS_UNSUPPORTED')
        fields[name]=ids
    return location not in fields['absent_at_location_ids'] if all_locations else location in fields['present_at_location_ids']


def _settings(data, location):
    tracking=data.get('track_inventory',False)
    if type(tracking) is not bool:raise _Unsupported('TRACKING_SETTINGS_UNSUPPORTED')
    overrides=data.get('location_overrides',[])
    if type(overrides) is not list or len(overrides)>1000:raise _Unsupported('TRACKING_SETTINGS_UNSUPPORTED')
    seen=set();selected={}
    for row in overrides:
        if type(row) is not dict:raise _Unsupported('TRACKING_SETTINGS_UNSUPPORTED')
        ident=_id(row.get('location_id'))
        if ident in seen:raise _Unsupported('TRACKING_SETTINGS_UNSUPPORTED')
        seen.add(ident)
        for name in ('track_inventory','sold_out'):
            if name in row and type(row[name]) is not bool:raise _Unsupported('TRACKING_SETTINGS_UNSUPPORTED')
        if ident==location:selected=row
    if 'track_inventory' in selected:tracking=selected['track_inventory']
    if tracking is not True:raise _Unsupported('INVENTORY_NOT_TRACKED')
    return selected


def _measurement(obj, ident):
    data=_object(obj,'MEASUREMENT_UNIT',ident)
    precision=data.get('precision',3)
    if type(precision) is not int or not 0<=precision<=5:raise _Unsupported('MEASUREMENT_UNIT_UNSUPPORTED')
    unit=data.get('measurement_unit')
    if type(unit) is not dict:raise _Unsupported('MEASUREMENT_UNIT_UNSUPPORTED')
    kinds=('custom_unit','area_unit','length_unit','volume_unit','weight_unit','generic_unit','time_unit')
    if set(unit)-set(kinds)-{'type'}:raise _Unsupported('MEASUREMENT_UNIT_UNSUPPORTED')
    selected=[name for name in kinds if name in unit and unit[name] is not None]
    if len(selected)!=1:raise _Unsupported('MEASUREMENT_UNIT_UNSUPPORTED')
    name=selected[0];value=unit[name]
    # The optional declared type must agree; an unknown/open future type is not inferred.
    if 'type' in unit and (name not in _UNIT_TYPES or unit['type']!=_UNIT_TYPES[name]):
        raise _Unsupported('MEASUREMENT_UNIT_UNSUPPORTED')
    if name=='custom_unit':
        if type(value) is not dict:raise _Unsupported('MEASUREMENT_UNIT_UNSUPPORTED')
        label=_text(value.get('name'),100);abbreviation=_text(value.get('abbreviation'),100)
        if not label or not abbreviation:raise _Unsupported('MEASUREMENT_UNIT_UNSUPPORTED')
        label=label+' ('+abbreviation+')'
    else:
        if type(value) is not str or value not in _STANDARD_UNITS[name]:
            raise _Unsupported('MEASUREMENT_UNIT_UNSUPPORTED')
        label=value.replace('_',' ').lower()
    return {'kind':'measured','label':label,'precision':precision}


def catalog_reference(objects, variation_id, parent_id, sku, location):
    """Price-independent read reference, never receiving or physical authority."""
    variation=objects.get(variation_id);parent=objects.get(parent_id)
    data=_object(variation,'ITEM_VARIATION',variation_id);item=_object(parent,'ITEM',parent_id)
    if data.get('item_id')!=parent_id or data.get('sku')!=sku:raise _Mismatch()
    if not _present(variation,location) or not _present(parent,location):
        raise _Unsupported('NOT_PRESENT_AT_LOCATION')
    selected=_settings(data,location)
    for name in ('stockable','sellable'):
        if name in data and type(data[name]) is not bool:raise _Unsupported('CATALOG_REFERENCE_UNSUPPORTED')
    if 'is_archived' in item and type(item['is_archived']) is not bool:
        raise _Unsupported('CATALOG_REFERENCE_UNSUPPORTED')
    options=data.get('item_option_values',[])
    if type(options) is not list or len(options)>1000:raise _Unsupported('ITEM_OPTIONS_UNSUPPORTED')
    seen=set()
    for option in options:
        if type(option) is not dict:raise _Unsupported('ITEM_OPTIONS_UNSUPPORTED')
        ident=_id(option.get('item_option_id'));_id(option.get('item_option_value_id'))
        if ident in seen:raise _Unsupported('ITEM_OPTIONS_UNSUPPORTED')
        seen.add(ident)
    unit={'kind':'whole','label':'Whole quantities in this Square variation','precision':0}
    measurement=data.get('measurement_unit_id')
    if measurement is not None:
        measurement=_id(measurement)
        unit=_measurement(objects.get(measurement),measurement)
    qualifications=[]
    if item.get('is_archived') is True:qualifications.append('ARCHIVED')
    if data.get('stockable') is True:qualifications.append('STOCKABLE')
    if data.get('sellable') is True:qualifications.append('SELLABLE')
    if data.get('sellable') is False:qualifications.append('NOT_SELLABLE')
    if selected.get('sold_out') is True:qualifications.append('MARKED_SOLD_OUT')
    conversion=data.get('stockable_conversion')
    if conversion is not None:
        if type(conversion) is not dict or data.get('stockable') is True:
            raise _Unsupported('CONVERSION_UNSUPPORTED')
        stock_id=_id(conversion.get('stockable_item_variation_id'))
        stock=_object(objects.get(stock_id),'ITEM_VARIATION',stock_id)
        if stock_id==variation_id or stock.get('item_id')!=parent_id or stock.get('stockable') is not True:
            raise _Unsupported('CONVERSION_UNSUPPORTED')
        if not _present(objects[stock_id],location):raise _Unsupported('CONVERSION_UNSUPPORTED')
        _settings(stock,location)
        for name in ('stockable_quantity','nonstockable_quantity'):
            value=conversion.get(name)
            if type(value) is not str or not _RATIO.fullmatch(value) or Decimal(value)<=0:
                raise _Unsupported('CONVERSION_UNSUPPORTED')
        unit=dict(unit,kind='measured_conversion' if measurement else 'conversion',
                  label=unit['label']+'; Square conversion variation, no conversion applied here')
        qualifications.append('CONVERSION')
    elif data.get('stockable') is False:
        unit=dict(unit,kind='component',label=unit['label']+'; Square component-based variation')
        qualifications.append('COMPONENT_BASED')
    if len(unit['label'])>256:raise _Unsupported('MEASUREMENT_UNIT_UNSUPPORTED')
    return {'variation_id':variation_id,'parent_id':parent_id,'sku':sku,
            'item_name':_text(item.get('name','')),'variation_name':_text(data.get('name','')),
            'unit':unit,'qualifications':qualifications}


class StockLookup:
    def __init__(self,database,square):
        self.database=database;self.square=square;self.reader=ReceivingSquare(square)
        self.location_reader=CleanupSquare(square,max_pages=1,max_records=1000,
            max_bytes=1024*1024,deadline_seconds=10)
        self.count_reader=CleanupSquare(square,max_pages=3,max_records=20,
            max_bytes=262144,deadline_seconds=10)

    def _staff(self,actor):
        with self.database.transaction() as db:current_authority(db,actor,allowed_roles=ROLES)

    def _access(self,actor):
        self._staff(actor)
        try:return self.reader.access(actor['tenant_id'])
        except ServiceError:
            self._staff(actor)
            raise

    def _context(self,access):
        return (access.proof.get('merchant_id'),access.proof.get('application_id'),
                access.proof.get('environment'),access.generation,self.square.environment,self.square.base,
                self.square.config.get('SQUARE_APPLICATION_ID'),self.square.config.get('SQUARE_ENVIRONMENT'))

    def _live(self,actor,access,context):
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=ROLES)
            self.reader.assert_current(db,actor['tenant_id'],access)
            if (self._context(access)!=context or access.proof.get('environment')!=self.square.environment
                    or set(access.proof.get('required_scopes',()))!=set(READ_SCOPES)
                    or not set(READ_SCOPES)<=set(access.proof.get('scopes',()))):
                raise ReceivingError('Square authorization changed. Check the connection before checking stock.',
                                     code='OAUTH_RECONSENT_REQUIRED')

    def _read(self,actor,access,context,deadline,call,reason='CATALOG_READ_INCOMPLETE'):
        self._live(actor,access,context)
        if time.monotonic()>=deadline:raise _Incomplete('READ_DEADLINE')
        try:result=call()
        except (ServiceError,ValueError,TypeError,KeyError,AttributeError,RecursionError):
            self._live(actor,access,context)
            raise _Incomplete(reason) from None
        self._live(actor,access,context)
        if time.monotonic()>=deadline:raise _Incomplete('READ_DEADLINE')
        return result

    @staticmethod
    def _locations(rows):
        active=[]
        if type(rows) is not list:raise _Incomplete()
        for row in rows:
            if row['status']=='ACTIVE':
                active.append({'id':_id(row['id']),'name':_text(row.get('name',row['id']))})
        if len(active)>100:raise _Incomplete()
        result={'stock_locations_version':1,'locations':active}
        if len(canonical(result))>65536:raise _Incomplete()
        return result

    def locations(self,actor):
        access=self._access(actor);context=self._context(access);deadline=time.monotonic()+60
        try:
            rows=self._read(actor,access,context,deadline,lambda:self.location_reader.locations(access))
            result=self._locations(rows)
        except (_Incomplete,_Unsupported):
            self._live(actor,access,context)
            raise ServiceError('Square locations could not be verified. Check again.',502) from None
        self._live(actor,access,context)
        return result

    def _selected(self,rows,location,*,after=False):
        found=[row for row in rows if row['id']==location and row['status']=='ACTIVE']
        if len(found)!=1:
            if after:raise _Mismatch()
            raise _Unsupported('LOCATION_UNAVAILABLE')
        _text(found[0].get('name',location))
        return found[0]

    def _catalog(self,actor,access,context,deadline,row,sku,location):
        ident=_id(row.get('id'));parent=_id(row['item_variation_data'].get('item_id'))
        read=lambda ids:self._read(actor,access,context,deadline,lambda:self.reader.catalog(access,ids))
        objects=read([ident,parent])
        if objects.get(ident)!=row:raise _Mismatch()
        data=row['item_variation_data'];dependencies=[]
        if data.get('measurement_unit_id') is not None:dependencies.append(_id(data['measurement_unit_id']))
        if data.get('stockable_conversion') is not None:
            conversion=data['stockable_conversion']
            if type(conversion) is not dict:raise _Unsupported('CONVERSION_UNSUPPORTED')
            dependencies.append(_id(conversion.get('stockable_item_variation_id')))
        if dependencies:
            if ident in dependencies or parent in dependencies or len(set(dependencies))!=len(dependencies):
                raise _Unsupported('CATALOG_REFERENCE_UNSUPPORTED')
            extra=read(dependencies)
            for key,value in extra.items():
                if key in objects and objects[key]!=value:raise _Mismatch()
                objects[key]=value
        reference=catalog_reference(objects,ident,parent,sku,location)
        if any('merchant_id' in objects[key] and objects[key]['merchant_id']!=access.proof['merchant_id']
               for key in set([ident,parent]+dependencies)):
            raise _Incomplete()
        return reference,fingerprint({key:objects[key] for key in sorted(set([ident,parent]+dependencies))})

    @staticmethod
    def _inventory(result,variation,location):
        rows=result.get('counts');missing=result.get('missing')
        if type(rows) is not list or len(rows)>1 or type(missing) is not list:raise _Incomplete('COUNTS_READ_INCOMPLETE')
        if not rows:
            if missing!=[(variation,location)]:raise _Incomplete('COUNTS_READ_INCOMPLETE')
            return 'missing','unknown','COUNT_NOT_RETURNED',None
        if missing:raise _Incomplete('COUNTS_READ_INCOMPLETE')
        row=rows[0]
        if (type(row) is not dict or row.get('catalog_object_id')!=variation or row.get('location_id')!=location
                or row.get('state')!='IN_STOCK' or row.get('catalog_object_type','ITEM_VARIATION')!='ITEM_VARIATION'):
            raise _Incomplete('COUNTS_READ_INCOMPLETE')
        value=row.get('quantity')
        if type(value) is not str or not 1<=len(value)<=26 or not _QUANTITY.fullmatch(value):raise _Incomplete('COUNTS_READ_INCOMPLETE')
        quantity=Decimal(value)
        if not quantity.is_finite():raise _Incomplete('COUNTS_READ_INCOMPLETE')
        estimated=row.get('is_estimated')
        if estimated is not None and type(estimated) is not bool:raise _Incomplete('COUNTS_READ_INCOMPLETE')
        if estimated is True:return 'unknown','unknown','COMPOSED_COUNT',None
        at=row.get('calculated_at')
        try:
            if type(at) is not str or len(at)>64 or not _TIME.fullmatch(at):raise ValueError()
            datetime.fromisoformat(at.replace('Z','+00:00'))
        except (ValueError,OverflowError):return 'unknown','unknown','COUNT_TIMESTAMP_UNKNOWN',None
        availability=('fractional_review' if quantity!=quantity.to_integral_value() else
                      'positive' if quantity>0 else 'negative_reported' if quantity<0 else 'zero_reported')
        return 'reported',availability,'NONE',{'state':'IN_STOCK','quantity':value,'calculated_at':at,'is_estimated':estimated}

    def lookup(self,actor,sku,location_id):
        if (type(sku) is not str or not 1<=len(sku)<=100 or not sku.strip()
                or any(unicodedata.category(c) in ('Cc','Cf','Cs') for c in sku)):
            raise ServiceError('Enter an exact Square SKU of 1 to 100 supported characters.')
        location=provider_id(location_id,'Location ID')
        started=datetime.now(timezone.utc).isoformat();deadline=time.monotonic()+60
        access=self._access(actor);context=self._context(access)
        result={'stock_lookup_version':1,'requested_sku':sku,'location_id':location,
                'catalog_state':'incomplete','count_state':'not_requested','availability':'unknown',
                'reason_code':'CATALOG_READ_INCOMPLETE','reference':None,'inventory':None,
                'read_started_at':started,'read_completed_at':started,'atomic_snapshot':False}
        read=lambda call,reason='CATALOG_READ_INCOMPLETE':self._read(actor,access,context,deadline,call,reason)
        try:
            selected=self._selected(read(lambda:self.location_reader.locations(access)),location)
            rows=read(lambda:self.reader.lookup_sku(access,sku))
            if type(rows) is not list or len(rows)>20:raise _Incomplete()
            if any('merchant_id' in row and row['merchant_id']!=access.proof['merchant_id'] for row in rows):
                raise _Incomplete()
            exact=[row for row in rows if row['item_variation_data']['sku']==sku]
            if not exact:
                result.update(catalog_state='no_match',reason_code='CASE_VARIANT' if rows else 'NO_EXACT_SKU')
            elif len(rows)!=1:
                result.update(catalog_state='ambiguous',reason_code='SKU_AMBIGUOUS')
            else:
                reference,before=self._catalog(actor,access,context,deadline,exact[0],sku,location)
                result.update(count_state='incomplete')
                counts=read(lambda:self.count_reader.counts(access,[reference['variation_id']],[location]),'COUNTS_READ_INCOMPLETE')
                after_rows=read(lambda:self.reader.lookup_sku(access,sku))
                if after_rows!=rows:raise _Mismatch()
                after_location=self._selected(read(lambda:self.location_reader.locations(access)),location,after=True)
                try:after_reference,after=self._catalog(actor,access,context,deadline,after_rows[0],sku,location)
                except _Unsupported:raise _Mismatch() from None
                if before!=after or reference!=after_reference or selected!=after_location:raise _Mismatch()
                state,availability,reason,inventory=self._inventory(counts,reference['variation_id'],location)
                result.update(catalog_state='match',count_state=state,availability=availability,
                              reason_code=reason,reference=reference,inventory=inventory)
            if result['catalog_state'] in ('no_match','ambiguous'):
                after_location=self._selected(read(lambda:self.location_reader.locations(access)),location,after=True)
                if selected!=after_location:raise _Mismatch()
        except _Unsupported as error:
            result.update(catalog_state='unsupported',count_state='not_requested',reason_code=error.reason,
                          reference=None,inventory=None,availability='unknown')
        except _Mismatch:
            result.update(catalog_state='mismatch',count_state='mismatch',reason_code='CONTEXT_CHANGED',
                          reference=None,inventory=None,availability='unknown')
        except _Incomplete as error:
            result.update(catalog_state='incomplete',reason_code=error.reason,reference=None,inventory=None,availability='unknown')
            if result['count_state']!='not_requested':result['count_state']='incomplete'
        self._live(actor,access,context)
        if time.monotonic()>=deadline:
            result.update(catalog_state='incomplete',count_state='incomplete',reason_code='READ_DEADLINE',
                          reference=None,inventory=None,availability='unknown')
        result['read_completed_at']=datetime.now(timezone.utc).isoformat()
        if len(canonical(result))>16384:raise ServiceError('The complete stock result could not be verified. Check again.',502)
        return result
