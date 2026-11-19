"""Authenticated hosted API. Pilot-only until live release gates are satisfied."""
from functools import wraps
import hashlib
import hmac
import json
import logging
import os
from urllib.parse import urlsplit
import uuid
from flask import Flask, g, jsonify, request, make_response, render_template, Response, stream_with_context
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from .auth import Auth,PasswordChangeError
from .database import Database
from .ledger import Ledger
from .credit_authority import CreditAuthorityError
from .staff_authority import StaffAuthorityError
from .staff_password_recovery import register_staff_password_recovery
from .security import ServiceError
from .square import Square
from .trades import Trades
from .privacy import Privacy, Limits as PrivacyLimits
from .intake import IntakeClaims, IntakeError
from .receiving import Receiving,restoration_source_bytes
from .receiving_pipeline import CatalogReceiving
from .receiving_square import ReceivingSquare
from .receiving_plan import ReceivingError,public_response_bytes
from .owner_onboarding import OwnerOnboarding,EnrollmentError
from .cleanup_observer import CleanupObserver,CleanupObserverError


def create_app(config=None,*,database=None,square_transport=None):
    supplied=dict(config or {})
    app=Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=512*1024,TESTING=False,
                      PUBLIC_BASE_URL=os.environ.get('PUBLIC_BASE_URL') or os.environ.get('RENDER_EXTERNAL_URL',''),
                      DATABASE_URL=os.environ.get('DATABASE_URL',''),
                      APP_ENCRYPTION_KEYS=os.environ.get('APP_ENCRYPTION_KEYS',''),
                      BOOTSTRAP_TOKEN=os.environ.get('BOOTSTRAP_TOKEN',''),
                      SQUARE_APPLICATION_ID=os.environ.get('SQUARE_APPLICATION_ID',''),
                      SQUARE_APPLICATION_SECRET=os.environ.get('SQUARE_APPLICATION_SECRET',''),
                      SQUARE_ENVIRONMENT=os.environ.get('SQUARE_ENVIRONMENT','sandbox'),
                      SQUARE_WEBHOOK_SIGNATURE_KEY=os.environ.get('SQUARE_WEBHOOK_SIGNATURE_KEY',''))
    app.config['RECEIVING_WRITES_ENABLED']=os.environ.get('RECEIVING_WRITES_ENABLED','').lower()=='true'
    app.config.update(CLEANUP_OBSERVER_ENABLED=False,CLEANUP_OBSERVER_ALLOWED_ENVIRONMENTS=('production',))
    app.config.update(supplied)
    public=app.config['PUBLIC_BASE_URL'].rstrip('/')
    uri=urlsplit(public)
    if (uri.scheme!='https' and not app.config['TESTING']) or not uri.hostname or uri.path or uri.query or uri.fragment or uri.username:
        raise ServiceError('Configure the public HTTPS service origin before starting.',503)
    app.config['PUBLIC_BASE_URL']=public
    app.config['TRUSTED_HOSTS']=[uri.hostname]
    app.wsgi_app=ProxyFix(app.wsgi_app,x_for=1,x_proto=1,x_host=0,x_port=0)
    db=database or (Database(app.config['DATABASE_URL']) if app.config['DATABASE_URL'] else None)
    auth=Auth(db,app.config['BOOTSTRAP_TOKEN'],encryption_keys=app.config['APP_ENCRYPTION_KEYS']) if db else None
    ledger=Ledger(db) if db else None
    square=Square(db,app.config,square_transport) if db else None
    trades=Trades(db) if db else None
    intake=IntakeClaims(db) if db else None
    owner_onboarding=OwnerOnboarding(db) if db else None
    receiving=CatalogReceiving(db,ReceivingSquare(square),square.cipher,writes_enabled=app.config.get('RECEIVING_WRITES_ENABLED') is True) if db else None
    cleanup_observer=CleanupObserver(db,square,enabled=app.config.get('CLEANUP_OBSERVER_ENABLED') is True,
        allowed_environments=app.config.get('CLEANUP_OBSERVER_ALLOWED_ENVIRONMENTS',('production',))) if db else None
    privacy=Privacy(db,authorize_session=lambda token:auth.resolve(token),
                    proof_key=app.config['APP_ENCRYPTION_KEYS'].split(',')[0],
                    limits=app.config.get('PRIVACY_EXPORT_LIMITS') or PrivacyLimits()) if db else None
    app.extensions.update(ss_database=db,ss_auth=auth,ss_ledger=ledger,ss_square=square,ss_trades=trades,ss_privacy=privacy,ss_intake=intake,ss_receiving=receiving)
    app.extensions['ss_owner_onboarding']=owner_onboarding
    app.extensions['ss_cleanup_observer']=cleanup_observer

    def require_db():
        if db is None:raise ServiceError('Database configuration is required.',503)
        if not app.config['APP_ENCRYPTION_KEYS']:
            raise ServiceError('Server encryption configuration is required.',503)

    def csrf(token):
        key=app.config['APP_ENCRYPTION_KEYS'].split(',')[0]
        if not key:raise ServiceError('Server encryption configuration is required.',503)
        return hmac.new(key.encode(),('csrf:'+token).encode(),hashlib.sha256).hexdigest()

    @app.before_request
    def boundary():
        if not app.config['TESTING'] and not request.is_secure:
            raise ServiceError('Use the secure HTTPS service address.',400)
        if request.method in ('POST','PUT','PATCH','DELETE'):
            if request.path!='/webhooks/square' and request.mimetype!='application/json':
                raise ServiceError('Send an application/json request.',415)
            origin=request.headers.get('Origin')
            if origin and origin!=public:raise ServiceError('Request origin is not permitted.',403)

    @app.after_request
    def security_headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if not app.config['TESTING']:response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

    @app.errorhandler(ServiceError)
    def service_error(error):
        result={'error':str(error)}
        if isinstance(error,IntakeError):
            result['code']=error.code
            if error.physical_intake_id:result['physical_intake_id']=error.physical_intake_id
        if isinstance(error,ReceivingError):
            result['code']=error.code
            if error.receiving_id:result['receiving_id']=error.receiving_id
        if isinstance(error,(CreditAuthorityError,StaffAuthorityError)):
            result['code']=error.code
            result['sign_in_required']=error.sign_in_required
        if isinstance(error,(EnrollmentError,CleanupObserverError)):result['code']=error.code
        if request.path=='/api/auth/password/change':
            result['code']=getattr(error,'code','PASSWORD_SESSION_CHANGED' if error.status==401 else
                'PASSWORD_INPUT_INVALID' if error.status<500 else 'PASSWORD_CHANGE_UNCONFIRMED')
            result['sign_in_required']=getattr(error,'sign_in_required',error.status==401 or error.status>=500)
        return jsonify(result),error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        result={'error':error.name}
        if request.path=='/api/auth/password/change':
            result.update(code='PASSWORD_INPUT_INVALID' if error.code<500 else 'PASSWORD_CHANGE_UNCONFIRMED',sign_in_required=error.code>=500)
        return jsonify(result),error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        # Error type only. Never include SQL parameters, credentials or customer payloads.
        app.logger.error('service_operation_failed type=%s path=%s',type(error).__name__,request.path)
        if request.path=='/api/auth/password/change':
            return jsonify(error='The password-change result could not be confirmed. Sign in with your new password. If it is not accepted, try your previous password before starting another change.',
                code='PASSWORD_CHANGE_UNCONFIRMED',sign_in_required=True),500
        return jsonify(error='The service could not complete this operation. Retry with the same request key.'),500

    def authenticated(function):
        @wraps(function)
        def wrapper(*args,**kwargs):
            require_db()
            bearer=request.headers.get('Authorization','')
            if bearer:
                if not bearer.startswith('Bearer '):raise ServiceError('Use Bearer authentication.',401)
                token=bearer[7:];cookie_auth=False
            else:token=request.cookies.get('ss_session','');cookie_auth=True
            g.actor=auth.resolve(token);g.token=token
            if cookie_auth and request.method not in ('GET','HEAD','OPTIONS'):
                expected=csrf(token);given=request.headers.get('X-SS-CSRF','')
                if not hmac.compare_digest(given,expected):raise ServiceError('Refresh your session before making changes.',403)
            return function(*args,**kwargs)
        return wrapper

    def body():
        value=request.get_json()
        if not isinstance(value,dict):raise ServiceError('Send a JSON object.')
        if 'tenant_id' in value:raise ServiceError('Store scope comes from the signed-in session, not the request body.')
        return value

    def receiving_response(value,status=200):
        # Only typed operation/plan/request DTOs receive the larger transport
        # budget. Match admission's compact UTF-8 measurement exactly.
        return Response(public_response_bytes(value),status=status,mimetype='application/json')

    @app.get('/healthz')
    def health():return jsonify(service='Stock & Sleeve hosted service',version='0.1.0',mode='pilot',status='running')

    @app.get('/readyz')
    def readiness():
        ready=False
        if db:
            try:ready=db.ready()
            except Exception as error:
                # Provider logs may show error class; never log DSN or SQL parameters.
                app.logger.warning('readiness_database_failed type=%s',type(error).__name__)
        configured=bool(ready and app.config['APP_ENCRYPTION_KEYS'])
        if not configured:
            app.logger.warning('service_not_ready database_configured=%s database_ready=%s encryption_configured=%s',
                               db is not None,ready,bool(app.config['APP_ENCRYPTION_KEYS']))
        return jsonify(database_ready=ready,service_ready=configured,mode='pilot',commercial_release=False),200 if configured else 503

    @app.get('/')
    def portal():return render_template('portal.html')

    @app.post('/api/pilot/bootstrap')
    def bootstrap():
        require_db();return jsonify(auth.bootstrap(body())),201

    @app.get('/owner-invite')
    def owner_invite():return render_template('owner_invite.html')

    @app.post('/api/owner-invitations/inspect')
    def owner_invitation_inspect():
        require_db();return jsonify(owner_onboarding.inspect(body(),request.remote_addr or ''))

    @app.post('/api/owner-invitations/status')
    def owner_invitation_status():
        require_db();return jsonify(owner_onboarding.status(body(),request.remote_addr or ''))

    @app.post('/api/owner-invitations/accept')
    def owner_invitation_accept():
        require_db()
        supplied=request.headers.get('Authorization','')
        tokens=[request.cookies.get('ss_session','')]
        if supplied.startswith('Bearer '):tokens.append(supplied[7:])
        # An invalid bearer must not hide a different valid browser cookie (or
        # vice versa). This route never replaces/deletes either session.
        for token in dict.fromkeys(value for value in tokens if value):
            try:auth.resolve(token)
            except ServiceError as error:
                if error.status!=401:raise
            else:raise EnrollmentError('Sign out of the current store before creating another store owner. No session was changed.',409,code='SIGN_OUT_REQUIRED')
        return jsonify(owner_onboarding.accept(body(),request.remote_addr or '')),201

    @app.post('/api/auth/login')
    def login():
        require_db()
        token,info=auth.login(body(),request.remote_addr or '')
        return session_response(token,info)

    @app.post('/api/auth/password/change')
    @authenticated
    def change_password():
        def invalid(*unused):
            raise PasswordChangeError('Send a small JSON object with only current_password and new_password.')
        def pairs(items):
            value={}
            for key,item in items:
                if key in value:invalid()
                value[key]=item
            return value
        if request.content_length is not None and request.content_length>4096:invalid()
        raw=request.stream.read(4097)
        if len(raw)>4096:invalid()
        try:values=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=invalid,parse_float=invalid)
        except (ValueError,UnicodeError,RecursionError):invalid()
        # Deliberately no cookie mutation on any outcome. Revoked cookies fail
        # normal authentication; a later sign-in may already own a newer cookie.
        return jsonify(auth.change_password(g.actor,values,request.remote_addr or ''))

    def session_response(token,info):
        if token is None:
            # A password-only MFA challenge is not a signed-in session.
            response=make_response(jsonify(info))
            response.delete_cookie('ss_session',path='/',secure=not app.config['TESTING'],httponly=True,samesite='Lax')
            return response
        # JSON bearer token supports desktop OS-vault storage; browser uses HttpOnly cookie.
        response=make_response(jsonify(info|{'access_token':token,'csrf_token':csrf(token)}))
        response.set_cookie('ss_session',token,max_age=28800,secure=not app.config['TESTING'],httponly=True,samesite='Lax',path='/')
        return response

    @app.post('/api/auth/mfa/challenge')
    def verify_mfa_challenge():
        require_db()
        token,info=auth.mfa.verify_challenge(body(),request.remote_addr or '')
        return session_response(token,info)

    @app.post('/api/auth/mfa/cancel')
    def cancel_mfa_challenge():
        require_db();return jsonify(auth.mfa.cancel_challenge(body(),request.remote_addr or ''))

    @app.get('/api/auth/mfa')
    @authenticated
    def mfa_status():return jsonify(auth.mfa.status(g.actor))

    @app.post('/api/auth/mfa/enroll')
    @authenticated
    def mfa_begin():return jsonify(auth.mfa.begin(g.actor,body(),request.remote_addr or ''))

    @app.post('/api/auth/mfa/enroll/cancel')
    @authenticated
    def mfa_cancel_enrollment():
        body();return jsonify(auth.mfa.cancel_enrollment(g.actor))

    @app.post('/api/auth/mfa/confirm')
    @authenticated
    def mfa_confirm():return jsonify(auth.mfa.confirm(g.actor,body(),request.remote_addr or ''))

    @app.post('/api/auth/mfa/recovery-codes')
    @authenticated
    def mfa_recovery():return jsonify(auth.mfa.change(g.actor,body(),request.remote_addr or ''))

    @app.post('/api/auth/mfa/disable')
    @authenticated
    def mfa_disable():
        result=auth.mfa.change(g.actor,body(),request.remote_addr or '',disable=True)
        response=make_response(jsonify(result))
        response.delete_cookie('ss_session',path='/',secure=not app.config['TESTING'],httponly=True,samesite='Lax')
        return response

    @app.post('/api/auth/logout')
    @authenticated
    def logout():
        auth.logout(g.token);response=make_response(jsonify(signed_out=True))
        response.delete_cookie('ss_session',path='/',secure=not app.config['TESTING'],httponly=True,samesite='Lax')
        return response

    @app.get('/api/me')
    @authenticated
    def me():return jsonify(tenant_id=str(g.actor['tenant_id']),store=g.actor['slug'],role=g.actor['role'],csrf_token=csrf(g.token),mode='pilot',
                           mfa_enabled=bool(g.actor.get('mfa_enabled')),mfa_authenticated=bool(g.actor.get('mfa_authenticated')),
                           mfa_fresh=bool(g.actor.get('mfa_fresh')),
                           capabilities={'physical_intake_claims':1,'trade_claim_required':True,
                                         'new_trade_acceptance_requires_fresh_mfa':True,'hosted_receiving':False,'receiving_plans':1})

    register_staff_password_recovery(app,auth,authenticated,body,require_db)

    @app.get('/api/staff')
    @authenticated
    def staff_list():return jsonify(staff=auth.list_staff(g.actor))

    @app.post('/api/staff/invites')
    @authenticated
    def invite():return jsonify(auth.invite(g.actor,body())),201

    @app.post('/api/staff/accept-invite')
    def accept_invite():
        require_db();return jsonify(auth.accept_invite(body(),request.remote_addr or '')),201

    @app.post('/api/staff/<uuid:staff_id>/disable')
    @authenticated
    def disable_staff(staff_id):
        body();auth.disable_staff(g.actor,staff_id);return jsonify(disabled=True)

    @app.get('/api/credit/customers')
    @authenticated
    def customers():return jsonify(customers=ledger.customers(g.actor,request.args.get('q','')))

    @app.get('/privacy')
    def privacy_portal():
        return render_template('privacy.html',browser_max_bytes=privacy.limits.browser_max_bytes if privacy else PrivacyLimits().browser_max_bytes)

    @app.post('/api/privacy/customers/<uuid:customer_id>/export')
    @authenticated
    def customer_privacy_export(customer_id):
        output=privacy.begin(g.actor,customer_id,body(),g.token)
        response=Response(stream_with_context(output.iter_bytes()),mimetype='application/x-ndjson')
        response.headers['Content-Disposition']='attachment; filename="Stock-and-Sleeve-customer-export-'+str(output.id)+'.ndjson"'
        response.headers['X-SS-Export-Id']=str(output.id)
        response.call_on_close(output.close)
        return response

    @app.get('/api/privacy/context')
    @authenticated
    def customer_privacy_context():
        privacy.authorize(g.actor,g.token)
        return jsonify(store=g.actor['slug'],role=g.actor['role'],csrf_token=csrf(g.token),
                       recent_sign_in_seconds=privacy.limits.recent_sign_in_seconds)

    @app.get('/api/privacy/customers')
    @authenticated
    def customer_privacy_search():
        privacy.authorize(g.actor,g.token)
        selected=ledger.customers(g.actor,request.args.get('q',''))
        privacy.authorize(g.actor,g.token)
        return jsonify(customers=[{'id':row['id'],'display_name':row['display_name']} for row in selected])

    @app.post('/api/privacy/exports/<uuid:export_id>/confirm')
    @authenticated
    def customer_privacy_confirm(export_id):return jsonify(privacy.confirm(g.actor,export_id,body(),g.token))

    @app.post('/api/credit/customers')
    @authenticated
    def create_customer():return jsonify(ledger.create_customer(g.actor,body())),201

    @app.get('/api/credit/customers/<uuid:customer_id>')
    @authenticated
    def account(customer_id):return jsonify(ledger.account(g.actor,customer_id))

    @app.get('/api/credit/requests/<key>')
    @authenticated
    def credit_request_status(key):return jsonify(ledger.request_status(g.actor,key))

    @app.post('/api/credit/customers/<uuid:customer_id>/adjust')
    @authenticated
    def adjust(customer_id):return jsonify(ledger.adjust(g.actor,customer_id,body()))

    @app.post('/api/credit/customers/<uuid:customer_id>/reserve')
    @authenticated
    def reserve(customer_id):return jsonify(ledger.reserve(g.actor,customer_id,body()))

    def observer_body(fields):
        # Keep this boundary local: legacy routes retain their existing decoder.
        def invalid(*unused):
            raise CleanupObserverError('Send a small JSON object with the exact observer fields.',400,code='OBSERVER_INPUT_INVALID')
        def pairs(items):
            value={}
            for key,item in items:
                if key in value:invalid()
                value[key]=item
            return value
        if request.content_length is not None and request.content_length>4096:invalid()
        raw=request.stream.read(4097)
        if len(raw)>4096:invalid()
        try:
            value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=invalid,parse_float=invalid)
        except (ValueError,UnicodeError,RecursionError):invalid()
        if type(value) is not dict or set(value)!=set(fields):invalid()
        for name,kind in fields.items():
            if type(value[name]) is not kind:invalid()
        return value

    observer_cas={'request_key':str,'expected_generation':str,'expected_config_version':int}

    @app.get('/api/catalog-cleanup/status')
    @authenticated
    def cleanup_observer_status():return jsonify(cleanup_observer.status(g.actor))

    @app.post('/api/catalog-cleanup/config')
    @authenticated
    def cleanup_observer_config():
        values=observer_body(observer_cas|{'enabled':bool,'background_enabled':bool,
            'cadence_seconds':int,'stale_after_seconds':int})
        return jsonify(cleanup_observer.configure(g.actor,values))

    @app.post('/api/catalog-cleanup/refresh')
    @authenticated
    def cleanup_observer_refresh():return jsonify(cleanup_observer.request_manual(g.actor,observer_body(observer_cas)))

    @app.get('/api/square/status')
    @authenticated
    def square_status():return jsonify(square.status(g.actor))

    @app.post('/api/intake-claims')
    @authenticated
    def intake_create():return jsonify(intake.create(g.actor,body())),201

    @app.get('/api/intake-claims/requests/<request_key>')
    @authenticated
    def intake_request_status(request_key):return jsonify(intake.request_status(g.actor,request_key))

    @app.get('/api/intake-claims/<uuid:claim_id>')
    @authenticated
    def intake_get(claim_id):return jsonify(intake.get(g.actor,claim_id))

    @app.get('/api/receiving/readiness')
    @authenticated
    def receiving_readiness():return jsonify(receiving.readiness(g.actor))

    @app.get('/receiving/catalog')
    def receiving_catalog_page():return render_template('receiving_admin.html')

    @app.get('/receiving/price-templates')
    def receiving_price_templates_page():return render_template('receiving_templates.html')

    @app.get('/api/receiving/catalog-bindings')
    @authenticated
    def receiving_bindings():
        try:limit=int(request.args.get('limit','50'))
        except ValueError:raise ServiceError('Catalog binding page size is invalid.') from None
        return jsonify(receiving.templates.bindings(g.actor,request.args.get('q',''),limit=limit,cursor=request.args.get('cursor')))

    @app.get('/api/receiving/price-templates')
    @authenticated
    def receiving_price_templates_list():
        try:limit=int(request.args.get('limit','50'))
        except ValueError:raise ServiceError('Template page size is invalid.') from None
        return jsonify(receiving.templates.list(g.actor,limit=limit,cursor=request.args.get('cursor')))

    @app.post('/api/receiving/price-templates/previews')
    @authenticated
    def receiving_price_template_preview():return jsonify(receiving.templates.preview(g.actor,body())),201

    @app.post('/api/receiving/price-templates/<uuid:preview_id>/approve')
    @authenticated
    def receiving_price_template_approve(preview_id):return jsonify(receiving.templates.approve(g.actor,preview_id,body()))

    @app.post('/api/receiving/price-templates/<uuid:template_id>/revoke')
    @authenticated
    def receiving_price_template_revoke(template_id):return jsonify(receiving.templates.revoke(g.actor,template_id,body()))

    @app.get('/api/receiving/locations')
    @authenticated
    def receiving_locations():return jsonify(receiving.locations(g.actor))

    @app.get('/api/receiving/catalog-games')
    @authenticated
    def receiving_catalog_games():
        if set(request.args)-{'name'} or len(request.args.getlist('name'))>1:
            raise ServiceError('Choose one game name for catalog review.')
        return jsonify(receiving.catalog_games(g.actor,request.args.get('name')))

    @app.post('/api/receiving/catalog-bindings/previews')
    @authenticated
    def receiving_binding_preview():return jsonify(receiving.binding_preview(g.actor,body())),201

    @app.get('/api/receiving/catalog-lookup')
    @authenticated
    def receiving_catalog_lookup():
        if (set(request.args)!={'sku','location_id'}
            or any(len(request.args.getlist(key))!=1 for key in ('sku','location_id'))):
            raise ServiceError('Supply one exact Square SKU and one receiving location.')
        return jsonify(receiving.catalog_lookup(g.actor,request.args['sku'],request.args['location_id']))

    @app.get('/api/receiving/catalog-bindings/previews/<uuid:preview_id>/review')
    @authenticated
    def receiving_binding_review(preview_id):
        try:limit=int(request.args.get('limit','25'))
        except ValueError:raise ServiceError('Catalog review page size is invalid.') from None
        return jsonify(receiving.binding_review(g.actor,preview_id,limit=limit,cursor=request.args.get('cursor')))

    @app.post('/api/receiving/catalog-bindings/<uuid:preview_id>/approve')
    @authenticated
    def receiving_binding_approve(preview_id):return jsonify(receiving.approve_binding(g.actor,preview_id,body()))

    @app.post('/api/receiving/catalog-bindings/<uuid:binding_id>/revoke')
    @authenticated
    def receiving_binding_revoke(binding_id):return jsonify(receiving.revoke_binding(g.actor,binding_id,body()))

    @app.post('/api/receiving/plans')
    @authenticated
    def receiving_plan():return receiving_response(receiving.create_plan(g.actor,body()),201)

    @app.get('/api/receiving/requests/<request_key>')
    @authenticated
    def receiving_request_status(request_key):return receiving_response(receiving.request_status(g.actor,request_key))

    @app.get('/api/receiving/intakes/<uuid:intake_id>/history')
    @authenticated
    def receiving_intake_history(intake_id):return receiving_response(receiving.intake_history(g.actor,intake_id))

    @app.post('/api/receiving/requests/<request_key>/retire')
    @authenticated
    def receiving_plan_retire(request_key):return receiving_response(receiving.retire_plan_request(g.actor,request_key,body()))

    @app.get('/api/receiving/<uuid:operation_id>')
    @authenticated
    def receiving_get(operation_id):return receiving_response(receiving.get(g.actor,operation_id))

    @app.get('/api/receiving/<uuid:operation_id>/restoration-source')
    @authenticated
    def receiving_restoration_source(operation_id):
        return Response(restoration_source_bytes(receiving.restoration_source(g.actor,operation_id)),mimetype='application/json')

    @app.put('/api/receiving/<uuid:operation_id>/plan')
    @authenticated
    def receiving_revise(operation_id):return receiving_response(receiving.revise(g.actor,operation_id,body()))

    @app.post('/api/receiving/<uuid:operation_id>/commit')
    @authenticated
    def receiving_commit(operation_id):return receiving_response(receiving.commit(g.actor,operation_id,body()))

    @app.post('/api/receiving/<uuid:operation_id>/recover')
    @authenticated
    def receiving_recover(operation_id):return receiving_response(receiving.recover(g.actor,operation_id,body()))

    @app.post('/api/receiving/<uuid:operation_id>/authorize')
    @authenticated
    def receiving_authorize(operation_id):return receiving_response(receiving.authorize(g.actor,operation_id,body()))

    @app.post('/api/receiving/<uuid:operation_id>/cancel')
    @authenticated
    def receiving_cancel(operation_id):return receiving_response(receiving.cancel(g.actor,operation_id,body()))

    @app.get('/api/trades')
    @authenticated
    def trade_list():
        try:limit=int(request.args.get('limit','50'))
        except ValueError:raise ServiceError('Trade page size is invalid.') from None
        return jsonify(trades.list(g.actor,request.args.get('q',''),limit=limit,cursor=request.args.get('cursor')))

    @app.post('/api/trades')
    @authenticated
    def trade_create():return jsonify(trades.create(g.actor,body())),201

    @app.get('/api/trades/<uuid:trade_id>')
    @authenticated
    def trade_get(trade_id):return jsonify(trades.get(g.actor,trade_id))

    @app.put('/api/trades/<uuid:trade_id>')
    @authenticated
    def trade_update(trade_id):return jsonify(trades.update(g.actor,trade_id,body()))

    @app.post('/api/trades/<uuid:trade_id>/accept')
    @authenticated
    def trade_accept(trade_id):return jsonify(trades.accept(g.actor,trade_id,body()))

    @app.post('/api/trades/<uuid:trade_id>/void')
    @authenticated
    def trade_void(trade_id):return jsonify(trades.void(g.actor,trade_id,body()))

    @app.post('/api/square/connect')
    @authenticated
    def square_connect():
        data=body()
        if set(data)-{'profile','receiving_permissions_confirmed'}:raise ServiceError('Unsupported Square authorization fields.')
        profile=data.get('profile','standard')
        if profile=='receiving':
            if data.get('receiving_permissions_confirmed') is not True:raise ServiceError('Explicitly confirm the additional catalog and inventory permissions.')
            return jsonify(authorization_url=square.begin(g.actor,profile='receiving'),requested_profile='receiving',grants_verified=False,receiving_enabled=False)
        if profile!='standard' or 'receiving_permissions_confirmed' in data:raise ServiceError('Select a supported Square authorization purpose.')
        return jsonify(authorization_url=square.begin(g.actor))

    @app.get('/oauth/square/callback')
    @authenticated
    def square_callback():
        result=square.complete(g.actor,request.args.get('state',''),request.args.get('code',''),request.args.get('error'))
        # Do not put access tokens, codes or provider error text in the page/redirect.
        return render_template('connected.html',connected=result['connected'])

    @app.get('/api/square/locations')
    @authenticated
    def square_locations():return jsonify(square.retrieve(g.actor['tenant_id'],'locations',''))

    @app.post('/webhooks/square')
    def square_webhook():
        require_db();return jsonify(square.ingest(request.get_data(cache=False),request.headers.get('x-square-hmacsha256-signature',''))),202

    return app
