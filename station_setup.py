"""Read-only, allowlisted store setup projection. No credentials or mutations."""
from copy import deepcopy
from datetime import datetime
import re
import uuid
from station_cloud import CloudError

PROFILE_KEYS=frozenset(('profileVersion','profileMode','localCreditEnabled','legacySquareEntryEnabled','requiredReceivingRoute','storeBound'))
ME_KEYS=frozenset(('tenant_id','store','role','mfa_enabled','mfa_authenticated'))
ROLES=('owner','manager','buyer','cashier')
NONCE=re.compile(r'[a-f0-9]{32}\Z')
SLUG=re.compile(r'[a-z0-9][a-z0-9-]{2,49}\Z')
SECRET_KEYS=frozenset(('token','accesstoken','refreshtoken','csrftoken','challengetoken','password','authorization','cookie','setcookie','clientsecret','squaretoken','bearertoken','sessiontoken'))
SQUARE_REQUIRED=frozenset(('oauth_configured','connected','connection_status','environment','requested_profile','grants_verified','write_checkout_enabled'))
SQUARE_OPTIONAL=frozenset(('merchant_id','saved_environment','scopes','requested_scopes','message','renewal'))
RECEIVING_REQUIRED=frozenset(('protocol_version','write_enabled','server_write_enabled','tenant_write_enabled','same_price_existing_supported','new_price_supported','new_parent_enabled','fresh_mfa_required','max_targets','location_selection_required','grants_verified','required_scopes'))
RECEIVING_OPTIONAL=frozenset(('new_price_write_enabled',))


def invalid():
    raise CloudError('Store setup metadata could not be verified. Refresh your store connection before continuing.',status=502,code='SETUP_PROTOCOL_UNSUPPORTED')


def _secrets(value):
    pending=[value];count=0
    while pending:
        node=pending.pop();count+=1
        if count>4096:invalid()
        if isinstance(node,dict):
            for key,item in node.items():
                if not isinstance(key,str) or re.sub('[^a-z]','',key.lower()) in SECRET_KEYS:invalid()
                pending.append(item)
        elif isinstance(node,list):pending.extend(node)
        elif node is not None and type(node) not in (str,bool,int,float):invalid()


def _keys(value,required,optional=()):
    if not isinstance(value,dict) or not set(required)<=set(value) or set(value)-set(required)-set(optional):invalid()


def _bool(value):
    if type(value) is not bool:invalid()
    return value


def _scopes(value):
    if (not isinstance(value,list) or len(value)>40
        or any(not isinstance(s,str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,63}',s) for s in value)):invalid()
    if len(set(value))!=len(value):invalid()


def _opaque(value):
    if not isinstance(value,str) or not 1<=len(value)<=255 or any(ord(c)<32 or ord(c)==127 for c in value):invalid()


def _renewal(value,square):
    # Keep the hosted policy's facts; this read-only projection never renews a
    # token, guesses grants or infers provider availability from a local age.
    _keys(value,('age_status','due','stale','expired','last_obtained_at','application_binding','automatic_renewal_active'))
    if value['age_status'] not in ('fresh','due','stale','unknown') or value['application_binding'] not in ('matched','unknown','mismatch'):invalid()
    for key in ('due','stale','expired','automatic_renewal_active'):_bool(value[key])
    if (value['automatic_renewal_active'] or value['stale']!=(value['age_status']=='stale')
        or (value['age_status'] in ('due','stale') and not value['due'])
        or (value['expired'] and not value['due'])
        or (value['application_binding']=='mismatch' and square['connected'])
        or (square['connection_status']=='application_mismatch' and value['application_binding']!='mismatch')):invalid()
    obtained=value['last_obtained_at']
    if (value['age_status']=='unknown')!=(obtained is None):invalid()
    if obtained is not None:
        if not isinstance(obtained,str) or len(obtained)>64 or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})',obtained):invalid()
        try:
            if datetime.fromisoformat(obtained.replace('Z','+00:00')).utcoffset() is None:invalid()
        except ValueError:invalid()
    return deepcopy(value)


def profile_snapshot(value):
    _secrets(value);_keys(value,PROFILE_KEYS)
    if type(value['profileVersion']) is not int or value['profileVersion']!=1:invalid()
    if value['profileMode'] not in ('hosted','legacy_compatibility'):invalid()
    hosted=value['profileMode']=='hosted'
    for key in ('localCreditEnabled','legacySquareEntryEnabled','storeBound'):_bool(value[key])
    if (value['requiredReceivingRoute']!=('hosted' if hosted else 'legacy_compatible')
        or value['localCreditEnabled']!=(not hosted)
        or value['legacySquareEntryEnabled']!=(not hosted)):invalid()
    return deepcopy(value)


def _empty(profile):
    return {'protocolVersion':1,'sessionId':None,'profile':deepcopy(profile),
        'account':{'state':'sign_in_required','tenantId':None,'storeSlug':None,'role':None},
        'security':{'state':'unknown','mfaEnrolled':None,'mfaAuthenticated':None,'verificationRequiredForNewWrites':True},
        'square':{'state':'unknown','oauthConfigured':None,'environment':None,'requestedProfile':None,'authorizationSaved':None,'grantsVerified':False,'renewal':None},
        'receiving':{'state':'unknown','serverEnabled':None,'storeEnabled':None,'newPriceEnabled':None,'locationSelection':'per_plan','locationSelectionRequired':True,'selectedLocation':None},
        'checkoutEnabled':False,'commerceReady':False,'reasonCodes':['SIGN_IN_REQUIRED']}


def _body(wrapper,required,optional):
    _keys(wrapper,('status','body'))
    if type(wrapper['status']) is not int or wrapper['status'] not in (200,403):invalid()
    if wrapper['status']==403:
        if wrapper['body'] is not None:invalid()
        return None
    _keys(wrapper['body'],required,optional)
    return wrapper['body']


def project(profile,bundle=None,*,expected_session_id=''):
    """Pure validator/projector. Returned objects never share mutable input data."""
    profile=profile_snapshot(profile);result=_empty(profile)
    if profile['profileMode']=='legacy_compatibility':
        if bundle is not None:invalid()
        result['account']['state']='legacy_compatibility';result['reasonCodes']=['LEGACY_PROFILE'];return result
    if bundle is None:
        if expected_session_id:invalid()
        return result
    _secrets(bundle);_keys(bundle,('sessionId','me','square','receiving'))
    if (not isinstance(expected_session_id,str) or not NONCE.fullmatch(expected_session_id)
        or bundle['sessionId']!=expected_session_id):
        raise CloudError('The store sign-in changed. Refresh setup without changing saved work.',status=409,session_changed=True,code='SETUP_CONTEXT_CHANGED')
    if not profile['storeBound']:invalid()
    me=bundle['me'];_keys(me,ME_KEYS)
    try:
        if not isinstance(me['tenant_id'],str) or str(uuid.UUID(me['tenant_id']))!=me['tenant_id']:invalid()
    except (ValueError,AttributeError,TypeError):invalid()
    if not isinstance(me['store'],str) or not SLUG.fullmatch(me['store']) or me['role'] not in ROLES:invalid()
    enrolled=_bool(me['mfa_enabled']);authenticated=_bool(me['mfa_authenticated'])
    if authenticated and not enrolled:invalid()
    result['sessionId']=expected_session_id
    result['account']={'state':'connected','tenantId':me['tenant_id'],'storeSlug':me['store'],'role':me['role']}
    result['security']={'state':'enrolled' if enrolled else 'enrollment_required','mfaEnrolled':enrolled,'mfaAuthenticated':authenticated,'verificationRequiredForNewWrites':True}
    reasons=[]
    if not enrolled:reasons.append('MFA_ENROLLMENT_REQUIRED')
    square=_body(bundle['square'],SQUARE_REQUIRED,SQUARE_OPTIONAL)
    if square is None:
        result['square']['state']='access_restricted';reasons.append('OWNER_ACTION_REQUIRED')
    else:
        for key in ('oauth_configured','connected','grants_verified','write_checkout_enabled'):_bool(square[key])
        if square['grants_verified'] or square['write_checkout_enabled']:invalid()
        if square['environment'] not in ('production','sandbox') or square['requested_profile'] not in ('standard','receiving'):invalid()
        states=('not_connected','connected','revoked','revocation_pending','environment_mismatch','application_mismatch')
        if square['connection_status'] not in states or square['connected']!=(square['connection_status']=='connected'):invalid()
        if 'saved_environment' in square:
            if square['saved_environment'] not in (None,'production','sandbox'):invalid()
            if square['connection_status']=='environment_mismatch' and square['saved_environment'] in (None,square['environment']):invalid()
            if square['connected'] and square['saved_environment']!=square['environment']:invalid()
        if 'merchant_id' in square:
            if square['connected']:_opaque(square['merchant_id'])
            elif square['merchant_id'] is not None:invalid()
        for key in ('scopes','requested_scopes'):
            if key in square:_scopes(square[key])
        if 'message' in square and (not isinstance(square['message'],str) or len(square['message'])>1000):invalid()
        state=square['connection_status']
        if not square['oauth_configured']:state='setup_unavailable';reasons.append('SQUARE_SETUP_UNAVAILABLE')
        elif state=='connected':
            state=square['requested_profile']+'_authorization_saved'
            if square['requested_profile']=='standard':reasons.append('RECEIVING_AUTHORIZATION_REQUIRED')
        elif state=='not_connected':reasons.append('SQUARE_NOT_CONNECTED')
        else:reasons.append('SQUARE_RECONNECT_REQUIRED')
        result['square']={'state':state,'oauthConfigured':square['oauth_configured'],'environment':square['environment'],
            'requestedProfile':square['requested_profile'],'authorizationSaved':square['connected'],'grantsVerified':False,
            'renewal':_renewal(square['renewal'],square) if 'renewal' in square else None}
    receiving=_body(bundle['receiving'],RECEIVING_REQUIRED,RECEIVING_OPTIONAL)
    if receiving is None:
        result['receiving']['state']='access_restricted';reasons.append('OWNER_ACTION_REQUIRED')
    else:
        for key in RECEIVING_REQUIRED-{'protocol_version','max_targets','required_scopes'}:_bool(receiving[key])
        if (type(receiving['protocol_version']) is not int or receiving['protocol_version']!=1
            or type(receiving['max_targets']) is not int or receiving['max_targets']!=100
            or receiving['write_enabled']!=(receiving['server_write_enabled'] and receiving['tenant_write_enabled'])
            or not receiving['same_price_existing_supported'] or not receiving['fresh_mfa_required']
            or not receiving['location_selection_required'] or receiving['new_parent_enabled'] or receiving['grants_verified']):invalid()
        _scopes(receiving['required_scopes'])
        if not receiving['required_scopes']:invalid()
        new_price=_bool(receiving.get('new_price_write_enabled',False))
        if new_price and (not receiving['write_enabled'] or not receiving['new_price_supported']):invalid()
        result['receiving'].update(state='review_required' if receiving['write_enabled'] else 'receiving_disabled',
            serverEnabled=receiving['server_write_enabled'],storeEnabled=receiving['tenant_write_enabled'],newPriceEnabled=new_price)
        reasons.append('RECEIVING_REVIEW_REQUIRED' if receiving['write_enabled'] else 'RECEIVING_DISABLED')
    result['reasonCodes']=list(dict.fromkeys(reasons));return result


class Setup:
    def __init__(self,service,policy):self.service,self.policy=service,policy

    def snapshot(self,expected_session_id=''):
        # Profile recovery exceptions intentionally propagate. Setup may not
        # fabricate a hosted policy, binding or replacement recovery context.
        before=profile_snapshot(self.policy.safe_snapshot())
        if not isinstance(expected_session_id,str):
            raise CloudError('Refresh your store sign-in before checking setup.',status=409,session_changed=True,code='SETUP_CONTEXT_CHANGED')
        if before['profileMode']=='legacy_compatibility' or not expected_session_id:return project(before)
        if not NONCE.fullmatch(expected_session_id):
            raise CloudError('Refresh your store sign-in before checking setup.',status=409,session_changed=True,code='SETUP_CONTEXT_CHANGED')
        bundle=self.service.cloud.read_setup_metadata(expected_session_id=expected_session_id)
        after=profile_snapshot(self.policy.safe_snapshot())
        if before!=after:
            raise CloudError('The profile changed while checking setup. Refresh without changing saved work.',status=409,session_changed=True,code='SETUP_CONTEXT_CHANGED')
        return project(after,bundle,expected_session_id=expected_session_id)
