"""Authenticated hosted API. Pilot-only until live release gates are satisfied."""
from functools import wraps
import hashlib
import hmac
import logging
import os
from urllib.parse import urlsplit
import uuid
from flask import Flask, g, jsonify, request, make_response, render_template
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from .auth import Auth
from .database import Database
from .ledger import Ledger
from .security import ServiceError
from .square import Square


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
    app.config.update(supplied)
    public=app.config['PUBLIC_BASE_URL'].rstrip('/')
    uri=urlsplit(public)
    if (uri.scheme!='https' and not app.config['TESTING']) or not uri.hostname or uri.path or uri.query or uri.fragment or uri.username:
        raise ServiceError('Configure the public HTTPS service origin before starting.',503)
    app.config['PUBLIC_BASE_URL']=public
    app.config['TRUSTED_HOSTS']=[uri.hostname]
    app.wsgi_app=ProxyFix(app.wsgi_app,x_for=1,x_proto=1,x_host=0,x_port=0)
    db=database or (Database(app.config['DATABASE_URL']) if app.config['DATABASE_URL'] else None)
    auth=Auth(db,app.config['BOOTSTRAP_TOKEN']) if db else None
    ledger=Ledger(db) if db else None
    square=Square(db,app.config,square_transport) if db else None
    app.extensions.update(ss_database=db,ss_auth=auth,ss_ledger=ledger,ss_square=square)

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
    def service_error(error):return jsonify(error=str(error)),error.status

    @app.errorhandler(HTTPException)
    def http_error(error):return jsonify(error=error.name),error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        # Error type only. Never include SQL parameters, credentials or customer payloads.
        app.logger.error('service_operation_failed type=%s path=%s',type(error).__name__,request.path)
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

    @app.post('/api/auth/login')
    def login():
        require_db()
        token,info=auth.login(body(),request.remote_addr or '')
        # JSON bearer token supports desktop OS-vault storage; browser uses HttpOnly cookie.
        response=make_response(jsonify(info|{'access_token':token,'csrf_token':csrf(token)}))
        response.set_cookie('ss_session',token,max_age=28800,secure=not app.config['TESTING'],httponly=True,samesite='Lax',path='/')
        return response

    @app.post('/api/auth/logout')
    @authenticated
    def logout():
        auth.logout(g.token);response=make_response(jsonify(signed_out=True))
        response.delete_cookie('ss_session',path='/',secure=not app.config['TESTING'],httponly=True,samesite='Lax')
        return response

    @app.get('/api/me')
    @authenticated
    def me():return jsonify(store=g.actor['slug'],role=g.actor['role'],csrf_token=csrf(g.token),mode='pilot')

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

    @app.get('/api/square/status')
    @authenticated
    def square_status():return jsonify(square.status(g.actor))

    @app.post('/api/square/connect')
    @authenticated
    def square_connect():
        body();return jsonify(authorization_url=square.begin(g.actor))

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
