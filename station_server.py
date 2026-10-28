"""Loopback-only HTTP interface. Admin credentials are never returned to operators."""
import json
import mimetypes
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit, unquote
from station_service import StationService
from station_store import StationError
from station_cloud import CloudError


def make_handler(core):
    from http.server import BaseHTTPRequestHandler

    class StationHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        @property
        def station(self):
            # Normally initialized before serving. This fallback is useful for embedding/tests.
            if not hasattr(self.server, 'station'):
                self.server.station = StationService(core)
            return self.server.station

        def security_headers(self):
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

        def send_json(self, data, status=200, cookie=None):
            content=json.dumps(data).encode()
            self.send_response(status)
            self.security_headers()
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(content)))
            if cookie:
                self.send_header('Set-Cookie',cookie)
            self.end_headers();self.wfile.write(content)

        def local_request(self):
            host=f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host or self.headers.get('Origin',f'http://{host}') != f'http://{host}':
                self.send_json({'errors':['Open Roca Station through its local launcher.']},403)
                return False
            return True

        def session(self):
            cookie=SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie',''))
                return cookie['roca_admin'].value if 'roca_admin' in cookie else ''
            except Exception:
                return ''

        def admin_required(self):
            if not self.station.auth.authorized(self.session()):
                self.send_json({'errors':['Unlock Admin to change these settings.'],'adminRequired':True},403)
                return False
            return True

        def do_GET(self):
            if not self.local_request():
                return
            try:
                path=urlsplit(self.path).path
                if path=='/api/version':
                    return self.send_json({'version':core.APP_VERSION})
                if path=='/api/runtime':
                    control=getattr(self.server,'runtime_control',None)
                    return self.send_json(control.status() if control else {'errors':['No background launcher.']},200 if control else 404)
                if path=='/api/settings':
                    return self.send_json(self.station.public_settings())
                if path in ('/api/cloud/session','/api/cloud/pending'):
                    with self.station.cloud.lock:
                        control=getattr(self.server,'runtime_control',None)
                        if control and control.stopping.is_set():return self.send_json({'errors':['The app is stopping. Reopen it before using shared credit.']},503)
                        if path=='/api/cloud/session':return self.send_json(self.station.cloud.status())
                        return self.send_json(self.station.cloud.pending(expected_session_id=self.headers.get('X-SS-Local-Session','')))
                if path=='/api/auth':
                    return self.send_json({'configured':self.station.auth.configured(),'authenticated':self.station.auth.authorized(self.session())})
                if path=='/api/admin/settings':
                    if self.admin_required():
                        self.send_json(self.station.admin_settings())
                    return
                if path=='/api/admin/cleanup':
                    if self.admin_required():
                        self.send_json(self.station.cleanup.preview(opted_in=self.station.store.get('cleanup_opt_in',False)))
                    return
                if path=='/api/receipts':
                    return self.send_json({'receipts':self.station.store.summaries(),'watcher':self.station.watch_status,
                                           'pickupQueue':self.station.store.pickup_queue()})
                if path.startswith('/api/receipts/'):
                    if path.endswith('/pricing'):
                        return self.send_json(self.station.pricing.receipt_status(self.station.active_receipt(path.split('/')[3])))
                    return self.send_json({'receipt':self.station.store.receipt(path.rsplit('/',1)[1])})
                if path.startswith('/api/pricing/jobs/'):
                    key = path.rsplit('/', 1)[1]
                    if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
                        raise StationError('Invalid pricing request.')
                    return self.send_json(self.station.pricing.get(key))
                if path=='/api/activity':
                    return self.send_json({'events':self.station.store.activity(),'tasks':self.station.tasks()})
                if path.startswith('/files/'):
                    name=unquote(path[7:])
                    if name!=Path(name).name or '/' in name or '\\' in name or not name.endswith(('.pdf','.zpl','.csv')):
                        raise StationError('Invalid output filename.')
                    filename=Path(core.OUTPUT_DIR)/name
                elif path=='/':
                    filename=Path(core.HTML_APP_FILE)
                elif path in ('/station.js','/station.css','/preview.js','/cloud.js','/cloud.css','/intake.js','/import.js','/import.css','/shared-trade.js','/shared-trade.css'):
                    filename=Path(core.APP_DIR)/path[1:]
                elif path in ('/assets/stock-and-sleeve.png','/assets/stock-and-sleeve.ico'):
                    filename=Path(core.APP_DIR)/path[1:]
                else:
                    return self.send_json({'errors':['Not found.']},404)
                if not filename.is_file() or filename.is_symlink():
                    return self.send_json({'errors':['File missing. Extract the complete application ZIP.']},404)
                content=filename.read_bytes()
                if path=='/':
                    from html import escape
                    from product_brand import display_name
                    content=content.decode('utf-8').replace('{{PRODUCT_NAME}}',escape(display_name())).encode('utf-8')
                self.send_response(200);self.security_headers()
                mime=mimetypes.guess_type(str(filename))[0] or 'text/plain'
                self.send_header('Content-Type',mime+('' if mime=='application/pdf' else '; charset=utf-8'))
                if filename.suffix in ('.csv','.zpl'):
                    self.send_header('Content-Disposition',f'attachment; filename="{filename.name}"')
                self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
            except CloudError as exc:
                self.send_json({'errors':[str(exc)],'cloudRequired':exc.status==401,'offline':exc.offline,'pendingRequired':exc.pending,'sessionChanged':exc.session_changed},exc.status)
            except StationError as exc:
                self.send_json({'errors':[str(exc)]},400)
            except Exception:
                self.send_json({'errors':['The local station could not read this resource. Check station_data/station.log.']},500)

        def do_POST(self):
            if not self.local_request():
                return
            try:
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                    return self.send_json({'errors':['Expected application/json.']},415)
                size=int(self.headers.get('Content-Length',0))
                if not 0<size<=20_000_000:
                    raise StationError('Uploads must be between 1 byte and 20 MB.')
                data=json.loads(self.rfile.read(size).decode())
                if not isinstance(data,dict):
                    raise StationError('Expected a JSON object.')
                control=getattr(self.server,'runtime_control',None)
                if self.path=='/api/quit':
                    # An in-app quit is a deliberate same-origin action for this
                    # instance. A stale window must not stop a replacement app.
                    origin=f'http://127.0.0.1:{self.server.server_port}'
                    if self.headers.get('Origin')!=origin or self.headers.get('X-SS-User-Action')!='quit':
                        return self.send_json({'errors':['Use Quit app inside Stock & Sleeve.']},403)
                    if not control:
                        return self.send_json({'errors':['This session has no desktop launcher. Close its console to stop it.']},409)
                    if data.get('instance')!=control.record['instance']:
                        return self.send_json({'errors':['This window belongs to an earlier session. Reopen the app from its desktop icon.']},409)
                    control.request_stop()
                    return self.send_json({'status':'stopping'},202)
                if self.path=='/api/shutdown':
                    if not control or not control.authorized(data.get('token')):
                        return self.send_json({'errors':['Use the Stop Roca shortcut in this application folder.']},403)
                    control.request_stop()
                    return self.send_json({'status':'stopping'},202)
                if control and control.stopping.is_set():
                    return self.send_json({'errors':['Roca is stopping. Start it again before continuing.']},503)
                if self.path.startswith('/api/cloud/'):
                    # The cloud client serializes its own staff session. A provider
                    # timeout must not hold the lock used to save local receipts.
                    with self.station.cloud.lock:
                        if control and control.stopping.is_set():return self.send_json({'errors':['The app is stopping. Reopen it before using shared credit.']},503)
                        return self.dispatch_post(data)
                with self.station.lock:
                    if control and control.stopping.is_set():
                        return self.send_json({'errors':['Roca is stopping. Start it again before continuing.']},503)
                    return self.dispatch_post(data)
            except CloudError as exc:
                self.send_json({'errors':[str(exc)],'cloudRequired':exc.status==401,'offline':exc.offline,'pendingRequired':exc.pending,'sessionChanged':exc.session_changed},exc.status)
            except (StationError, ValueError, TypeError, KeyError) as exc:
                self.send_json({'errors':[str(exc)]},400)
            except Exception:
                # Never echo credential handling errors or arbitrary request payloads.
                self.send_json({'errors':['The operation could not complete. Your receipt is saved; check Activity before retrying.']},500)

        def dispatch_post(self,data):
            service=self.station;path=self.path
            if path=='/api/auth/setup':
                with service.lock:
                    if service.auth.configured():
                        raise StationError('Admin is already set up. Enter the existing password.')
                    service.auth.set_password(str(data.get('password','')))
                token=service.auth.login(str(data['password']),self.client_address[0])
                return self.send_json({'status':'ok'},cookie=f'roca_admin={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=1800')
            if path=='/api/auth/login':
                token=service.auth.login(str(data.get('password','')),self.client_address[0])
                return self.send_json({'status':'ok'},cookie=f'roca_admin={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=1800')
            if path=='/api/auth/logout':
                service.auth.logout(self.session())
                return self.send_json({'status':'ok'},cookie='roca_admin=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            if path.startswith('/api/cloud/'):
                expected=data.get('sessionId','')
                if not isinstance(expected,str):expected=''
                if path=='/api/cloud/login':return self.send_json(service.cloud.login(data))
                if path=='/api/cloud/logout':return self.send_json(service.cloud.logout(expected_session_id=expected))
                if path=='/api/cloud/search':return self.send_json(service.cloud.customers(data.get('query',''),expected_session_id=expected))
                if path=='/api/cloud/account':return self.send_json(service.cloud.account(data.get('customerId'),expected_session_id=expected))
                if path=='/api/cloud/customer':return self.send_json(service.cloud.create_customer(data,expected_session_id=expected))
                if path=='/api/cloud/adjust':return self.send_json(service.cloud.adjust(data.get('customerId'),data,expected_session_id=expected))
                if path=='/api/cloud/recover':return self.send_json(service.cloud.recover(data.get('requestKey'),retry=data.get('retry') is True,expected_session_id=expected))
                if path=='/api/cloud/trade-prepare':return self.send_json(service.shared_trades.prepare(str(data.get('receiptId','')),data,expected))
                if path=='/api/cloud/trade-status':return self.send_json(service.shared_trades.status(str(data.get('receiptId','')),expected))
                if path=='/api/cloud/trade-accept':return self.send_json(service.shared_trades.accept(str(data.get('receiptId','')),data,expected))
                if path=='/api/cloud/trade-cancel':return self.send_json(service.shared_trades.cancel(str(data.get('receiptId','')),expected))
                return self.send_json({'errors':['Cloud action not found.']},404)
            if path.startswith('/api/admin/'):
                if not self.admin_required():
                    return
                if path=='/api/admin/settings':
                    return self.send_json(service.save_preferences(data))
                if path=='/api/admin/cleanup-policy':
                    if not isinstance(data.get('optedIn'),bool):raise StationError('Choose whether to enable cleanup observation.')
                    service.store.put('cleanup_opt_in',data['optedIn'])
                    return self.send_json(service.cleanup.preview(opted_in=data['optedIn']))
                if path=='/api/admin/downloads-folder':
                    return self.send_json({'folder':service.downloads_folder()})
                if path=='/api/admin/locations':
                    settings=service.preferences()
                    token=data.get('squareToken','').strip() or service.vault.read()
                    environment=data.get('squareEnvironment',settings['squareEnvironment'])
                    locations=core.square_locations(token,environment)
                    return self.send_json({'locations':locations,'environment':environment})
                if path=='/api/admin/password':
                    # The active session is required and changing the password revokes all sessions.
                    service.auth.set_password(str(data.get('password','')))
                    return self.send_json({'status':'ok'},cookie='roca_admin=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            if path=='/api/import/preview':
                return self.send_json(service.preview_import(data.get('csv'),data.get('intakeOptions')))
            if path=='/api/import':
                return self.send_json(service.import_csv(str(data.get('csv','')),str(data.get('fileName','Roca export.csv')),
                    str(data.get('mode','bulk')),data.get('newDelivery') is True,intake_options=data.get('intakeOptions')))
            if path=='/api/manual-items':
                return self.send_json({'receipt':service.add_manual_item(data)})
            if path.startswith('/api/credit/'):
                if service.credit is None:return self.send_json({'errors':['Credit tracking is not included in this edition.']},404)
                if path=='/api/credit/search':return self.send_json({'customers':service.credit.search(data.get('query',''))})
                if path=='/api/credit/customer':return self.send_json({'customer':service.credit.add_customer(data)})
                if path=='/api/credit/export':return self.send_json({'csv':service.credit.export(str(data.get('customerId','')))})
                if path=='/api/credit/account':return self.send_json(service.credit.account(str(data.get('customerId',''))))
                if path=='/api/credit/transaction':
                    temporary=None
                    try:
                        if data.get('kind') in ('opening','adjust_add','adjust_remove') and not service.auth.authorized(self.session()):
                            temporary=service.auth.login(str(data.get('adminPassword','')),self.client_address[0])
                        return self.send_json(service.credit.transaction(data))
                    finally:
                        if temporary:service.auth.logout(temporary)
            if path=='/api/pricing/research':
                if not service.preferences().get('pricingEnabled', True):
                    raise StationError('Price research is paused. Enable it in Admin to continue.')
                card = {key:str(data.get(key, '')).strip()[:500] for key in
                        ('game', 'game_label', 'card_name', 'card_set', 'card_num', 'printing', 'condition', 'language',
                         'grader', 'grade', 'grade_qualifier', 'cert_number', 'serial_number', 'year', 'manufacturer', 'team', 'player')}
                if not card['card_name'] or not card['game']:
                    raise StationError('Enter a name and choose the game before researching.')
                return self.send_json({'key': service.pricing.enqueue(card, force=data.get('force') is True)})
            if path=='/api/pricing/links':
                from pricing_review import lookup_links, grading
                card = {key:str(data.get(key,'')).strip()[:500] for key in
                        ('game','game_label','card_name','card_set','card_num','printing','condition','language',
                         'grader','grade','grade_qualifier','cert_number','serial_number','year','manufacturer','team','player')}
                if not card['card_name']:
                    raise StationError('Enter the item name before comparing prices.')
                if not card['game']:
                    raise StationError('Choose the game before researching this item.')
                tier = grading(card)
                return self.send_json({'links':lookup_links(card), 'title':card['card_name'],
                                       'details':' · '.join(card[k] for k in ('card_set','card_num','condition','printing','language') if card[k]),
                                       'gradeNeedsReview':tier[0]=='?', 'livePricesFetched':False})
            if path.startswith('/api/receipts/'):
                pieces=path.split('/');receipt_id=pieces[3];action=pieces[4] if len(pieces)>4 else 'save'
                if action=='save':
                    receipt=service.save_receipt(receipt_id,data)
                elif action=='approve-price':
                    receipt=service.approve_price(receipt_id,data)
                elif action=='apply-safe-prices':
                    return self.send_json(service.apply_safe_prices(receipt_id,data.get('revision'),data.get('rowIds')))
                elif action=='research':
                    if not service.preferences().get('pricingEnabled', True):
                        raise StationError('Price research is paused. Enable it in Admin to continue.')
                    receipt=service.active_receipt(receipt_id)
                    if data.get('revision') != receipt['revision']:
                        raise StationError('This batch changed. Reload it before researching.')
                    service.pricing.enqueue_receipt(receipt, force=True)
                elif action=='accept-buy':
                    receipt=service.accept_buy(receipt_id,data.get('offerType'))
                elif action=='receive':
                    receipt=service.receive(receipt_id)
                elif action=='labels':
                    receipt=service.labels(receipt_id,data.get('layout'),data.get('dpi'))
                elif action=='prepare':
                    receipt=service.prepare_online(receipt_id)
                elif action=='delete':
                    receipt=service.delete_receipt(receipt_id,data.get('revision'))
                elif action=='restore':
                    receipt=service.restore_receipt(receipt_id)
                elif action=='pickup-opened':
                    receipt=service.acknowledge_pickup(receipt_id)
                else:
                    raise StationError('Unknown receipt action.')
                return self.send_json({'receipt':receipt})
            if path=='/api/tasks':
                return self.send_json({'task':service.create_task(data)})
            if path.startswith('/api/tasks/') and path.endswith('/complete'):
                return self.send_json({'task':service.complete_task(path.split('/')[3])})
            if path in ('/api/process','/api/settings','/api/square/locations'):
                return self.send_json({'errors':['This older interface is no longer supported. Reload the complete Roca Station 2 app.']},410)
            return self.send_json({'errors':['Not found.']},404)

    return StationHandler
