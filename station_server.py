"""Loopback-only HTTP interface. Admin credentials are never returned to operators."""
import json
import mimetypes
import socket
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit, unquote
from station_service import StationService
from station_store import StationError
from station_cloud import CloudError
from station_profile import ProfilePolicyError


def make_handler(core):
    from http.server import BaseHTTPRequestHandler

    class StationHandler(BaseHTTPRequestHandler):
        _CLOSE_DRAIN_BYTES = 64 * 1024
        _CLOSE_DRAIN_SECONDS = 0.05

        def handle_one_request(self):
            self._request_body_bytes = 0
            self._request_headers_ready = False
            return super().handle_one_request()

        def parse_request(self):
            result = super().parse_request()
            self._request_headers_ready = bool(result and getattr(self, 'headers', None) is not None)
            return result

        def _framed_request_length(self):
            if not getattr(self, '_request_headers_ready', False):
                return None
            if self.headers.get_all('Transfer-Encoding'):
                return None
            values = self.headers.get_all('Content-Length', [])
            if not values:
                return 0
            if len(values) != 1:
                return None
            value = values[0].strip(' \t')
            if not 1 <= len(value) <= 20 or any(char < '0' or char > '9' for char in value):
                return None
            return int(value)

        def _drain_closing_input(self):
            length = self._framed_request_length()
            remaining = (self._CLOSE_DRAIN_BYTES if length is None else
                         min(self._CLOSE_DRAIN_BYTES, max(0, length - self._request_body_bytes)))
            if remaining == 0:
                return
            try:
                previous_timeout = self.connection.gettimeout()
            except OSError:
                return
            try:
                deadline = time.monotonic() + self._CLOSE_DRAIN_SECONDS
                self.connection.settimeout(self._CLOSE_DRAIN_SECONDS)
                self.wfile.flush()
                try:
                    self.connection.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        break
                    self.connection.settimeout(timeout)
                    chunk = self.rfile.read1(min(8192, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    self.connection.settimeout(previous_timeout)
                except OSError:
                    pass

        def finish(self):
            try:
                if self.close_connection:
                    self._drain_closing_input()
            finally:
                super().finish()

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
            # Serialize before the I/O boundary. Application/serialization errors
            # must still reach the existing error response and diagnostics.
            content=json.dumps(data).encode()
            return self.send_bytes(content,status,'application/json',cookie=cookie)

        def send_bytes(self,content,status=200,content_type='application/octet-stream',*,cookie=None,disposition=None):
            self.send_response(status)
            self.security_headers()
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(content)))
            if cookie:
                self.send_header('Set-Cookie',cookie)
            if disposition:
                self.send_header('Content-Disposition',disposition)
            try:
                # A reload/closed tab can discard an already completed action's
                # response. End this connection, without a second response or
                # repeating/undoing the completed application operation.
                self.end_headers()
                self.wfile.write(content)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
                self.close_connection=True
                return False
            return True

        def local_request(self):
            host=f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host or self.headers.get('Origin',f'http://{host}') != f'http://{host}':
                self.send_json({'errors':['Open Stock & Sleeve from its desktop icon.']},403)
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

        def clear_cloud_previews(self):
            self.station.receiving_restoration.clear()
            labels=getattr(self.station,'receiving_labels',None)
            if labels is not None:labels.clear()
            actions=getattr(self.station,'receiving_actions',None)
            if actions is not None:actions.clear()
            references=getattr(self.station,'receiving_reference_retirement',None)
            if references is not None:references.clear()
            stock=getattr(self.station,'intake_stock_projection',None)
            if stock is not None:stock.clear()
            intake_actions=getattr(self.station,'intake_stock_actions',None)
            if intake_actions is not None:intake_actions.clear()

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
                if path=='/api/cloud/setup-status':
                    control=getattr(self.server,'runtime_control',None)
                    if control and control.stopping.is_set():
                        return self.send_json({'errors':['The app is stopping. Reopen it before checking store setup.']},503)
                    return self.send_json(self.station.setup.snapshot(self.headers.get('X-SS-Local-Session','')))
                if path.startswith('/api/receipt-label-publications/'):
                    segments=path.split('/')
                    if len(segments)!=6 or urlsplit(self.path).query:
                        raise StationError('Use the saved receipt label-pair download link.')
                    from station_label_publication import OrdinaryLabels
                    result=OrdinaryLabels(self.station).download(segments[3],segments[4],segments[5],self.headers.get('X-SS-Local-Session',''))
                    return self.send_bytes(result['content'],content_type=result['contentType'],
                                           disposition='attachment; filename="'+result['filename']+'"')
                if path.startswith('/api/cloud/receiving-label-publications/'):
                    segments=path.split('/')
                    if len(segments)!=6 or urlsplit(self.path).query:
                        raise StationError('Use the original-stock publication download link.')
                    result=self.station.receiving_labels.download(segments[4],segments[5],self.headers.get('X-SS-Local-Session',''))
                    return self.send_bytes(result['content'],content_type=result['contentType'],
                                           disposition='attachment; filename="'+result['filename']+'"')
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
                    if name.casefold().startswith(('ss-stock-label-','ss-receipt-label-')):
                        raise StationError('Open this original-stock label publication through its current signed-in stock record.')
                    if name!=Path(name).name or '/' in name or '\\' in name or not name.endswith(('.pdf','.zpl','.csv')):
                        raise StationError('Invalid output filename.')
                    filename=Path(core.OUTPUT_DIR)/name
                elif path=='/':
                    filename=Path(core.HTML_APP_FILE)
                elif path in ('/station.js','/station.css','/preview.js','/cloud.js','/cloud.css','/intake.js','/import.js','/import.css','/shared-trade.js','/shared-trade.css','/intake-claim.js','/intake-claim.css','/receiving.js','/receiving.css','/setup.js','/setup.css','/trade-recovery.js','/trade-recovery.css','/receiving-restore.js','/receiving-restore.css','/receiving-labels.js','/receiving-labels.css','/receiving-actions.js','/receiving-actions.css','/receiving-intake-actions.js','/receiving-intake-actions.css'):
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
                mime=mimetypes.guess_type(str(filename))[0] or 'text/plain'
                disposition=f'attachment; filename="{filename.name}"' if filename.suffix in ('.csv','.zpl') else None
                return self.send_bytes(content,content_type=mime+('' if mime=='application/pdf' else '; charset=utf-8'),disposition=disposition)
            except CloudError as exc:
                self.send_json({'errors':[str(exc)],'cloudRequired':exc.status==401,'offline':exc.offline,'pendingRequired':exc.pending,'sessionChanged':exc.session_changed,'mfaRestartRequired':exc.mfa_restart,'code':exc.code,'physicalIntakeId':exc.physical_intake_id,'receivingId':exc.receiving_id,'originalTradeId':exc.original_trade_id},exc.status)
            except ProfilePolicyError as exc:
                self.send_json({'errors':[str(exc)],'code':exc.code,'profileRecoveryRequired':exc.code!='HOSTED_WORKFLOW_REQUIRED'},409)
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
                size=self._framed_request_length()
                if size is None:
                    self.close_connection=True
                    raise StationError('Use one valid Content-Length without Transfer-Encoding.')
                if not 0<size<=20_000_000:
                    self.close_connection=True
                    raise StationError('Uploads must be between 1 byte and 20 MB.')
                raw=self.rfile.read(size)
                self._request_body_bytes += len(raw)
                data=json.loads(raw.decode())
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
                # Only ordinary label rendering detaches from the blanket local
                # lock. Its coordinator owns short profile/session/receipt CAS
                # fences. All original request framing/origin/stop gates above
                # remain in force; every other action retains its old locking.
                segments=self.path.split('/')
                if len(segments)==5 and segments[1:3]==['api','receipts'] and segments[4]=='labels':
                    return self.dispatch_post(data)
                with self.station.lock:
                    if control and control.stopping.is_set():
                        return self.send_json({'errors':['Roca is stopping. Start it again before continuing.']},503)
                    return self.dispatch_post(data)
            except CloudError as exc:
                self.send_json({'errors':[str(exc)],'cloudRequired':exc.status==401,'offline':exc.offline,'pendingRequired':exc.pending,'sessionChanged':exc.session_changed,'mfaRestartRequired':exc.mfa_restart,'code':exc.code,'physicalIntakeId':exc.physical_intake_id,'receivingId':exc.receiving_id,'originalTradeId':exc.original_trade_id},exc.status)
            except ProfilePolicyError as exc:
                self.send_json({'errors':[str(exc)],'code':exc.code,'profileRecoveryRequired':exc.code!='HOSTED_WORKFLOW_REQUIRED'},409)
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
                if path=='/api/cloud/login':
                    result=service.cloud.login(data);self.clear_cloud_previews();return self.send_json(result)
                if path=='/api/cloud/mfa-verify':
                    result=service.cloud.verify_mfa(data);self.clear_cloud_previews();return self.send_json(result)
                if path=='/api/cloud/mfa-cancel':
                    result=service.cloud.cancel_mfa(data);self.clear_cloud_previews();return self.send_json(result)
                if path=='/api/cloud/mfa-clear-local':
                    result=service.cloud.clear_mfa_local(data);self.clear_cloud_previews();return self.send_json(result)
                if path=='/api/cloud/logout':
                    result=service.cloud.logout(expected_session_id=expected,expected_challenge_id=data.get('challengeId'));self.clear_cloud_previews();return self.send_json(result)
                if path=='/api/cloud/search':return self.send_json(service.cloud.customers(data.get('query',''),expected_session_id=expected))
                if path=='/api/cloud/account':return self.send_json(service.cloud.account(data.get('customerId'),expected_session_id=expected))
                if path=='/api/cloud/customer':return self.send_json(service.cloud.create_customer(data,expected_session_id=expected))
                if path=='/api/cloud/adjust':return self.send_json(service.cloud.adjust(data.get('customerId'),data,expected_session_id=expected))
                if path=='/api/cloud/recover':return self.send_json(service.cloud.recover(data.get('requestKey'),retry=data.get('retry') is True,expected_session_id=expected))
                if path=='/api/cloud/intake-prepare':return self.send_json(service.intake_claims.prepare(str(data.get('receiptId','')),data,expected))
                if path=='/api/cloud/intake-status':return self.send_json(service.intake_claims.status(str(data.get('receiptId','')),expected))
                if path=='/api/cloud/intake-select':return self.send_json(service.intake_claims.select_existing(str(data.get('receiptId','')),data,expected))
                if path=='/api/cloud/intake-additional':return self.send_json(service.intake_claims.additional(str(data.get('receiptId','')),data,expected))
                if path=='/api/cloud/receiving-context':return self.send_json(service.hosted_receiving.context(str(data.get('receiptId','')),expected))
                if path=='/api/cloud/receiving-intake-history':return self.send_json(service.intake_receiving_review.history(data,expected))
                if path=='/api/cloud/receiving-intake-preview':return self.send_json(service.intake_receiving_review.preview(data,expected))
                if path=='/api/cloud/receiving-intake-rows':return self.send_json(service.intake_receiving_review.rows(data,expected))
                if path=='/api/cloud/receiving-intake-projection-activate':
                    try:result=service.intake_stock_projection.activate(data,expected)
                    finally:self.clear_cloud_previews()
                    return self.send_json(result)
                if path=='/api/cloud/receiving-intake-projection-preview':return self.send_json(service.intake_stock_projection.preview(data,expected))
                if path=='/api/cloud/receiving-intake-projection-adopt':return self.send_json(service.intake_stock_projection.adopt(data,expected))
                if path=='/api/cloud/receiving-intake-projection-view':return self.send_json(service.intake_stock_projection.view(data,expected))
                if path=='/api/cloud/receiving-intake-projection-rows':return self.send_json(service.intake_stock_projection.rows(data,expected))
                if path=='/api/cloud/receiving-intake-action-activate':
                    try:result=service.intake_stock_actions.activate(data,expected)
                    finally:self.clear_cloud_previews()
                    return self.send_json(result)
                if path=='/api/cloud/receiving-intake-action-preview':return self.send_json(service.intake_stock_actions.preview(data,expected))
                if path=='/api/cloud/receiving-intake-action-status':return self.send_json(service.intake_stock_actions.status(data,expected))
                if path=='/api/cloud/receiving-intake-action-perform':return self.send_json(service.intake_stock_actions.perform(data,expected))
                if path=='/api/cloud/receiving-intake-action-apply':return self.send_json(service.intake_stock_actions.apply_saved_result(data,expected))
                if path=='/api/cloud/receiving-restoration-preview':return self.send_json(service.receiving_restoration.preview(data,expected))
                if path=='/api/cloud/receiving-restoration-history':return self.send_json(service.receiving_restoration.history(data,expected))
                if path=='/api/cloud/receiving-restoration-adopt':return self.send_json(service.receiving_restoration.adopt(data,expected))
                if path=='/api/cloud/receiving-restoration-rows':return self.send_json(service.receiving_restoration.rows(data,expected))
                if path=='/api/cloud/receiving-restoration-labels-preview':return self.send_json(service.receiving_labels.preview(data,expected))
                if path=='/api/cloud/receiving-restoration-labels':return self.send_json(service.receiving_labels.generate(data,expected))
                if path=='/api/cloud/receiving-restoration-label-publication':return self.send_json(service.receiving_labels.status(data,expected))
                if path=='/api/cloud/receiving-restoration-labels-list':return self.send_json(service.receiving_labels.list(data,expected))
                if path=='/api/cloud/receiving-original-action-preview':return self.send_json(service.receiving_actions.preview(data,expected))
                if path=='/api/cloud/receiving-original-action-perform':return self.send_json(service.receiving_actions.perform(data,expected))
                if path=='/api/cloud/receiving-original-action-status':return self.send_json(service.receiving_actions.status(data,expected))
                if path=='/api/cloud/receiving-original-reference-preview':return self.send_json(service.receiving_reference_retirement.preview(data,expected))
                if path=='/api/cloud/receiving-original-reference-retire':return self.send_json(service.receiving_reference_retirement.retire(data,expected))
                if path=='/api/cloud/receiving-original-reference-status':return self.send_json(service.receiving_reference_retirement.status(data,expected))
                if path=='/api/cloud/receiving-prepare':return self.send_json(service.hosted_receiving.prepare(str(data.get('receiptId','')),data,expected))
                if path=='/api/cloud/receiving-status':return self.send_json(service.hosted_receiving.status(str(data.get('receiptId','')),expected))
                if path=='/api/cloud/receiving-existing':return self.send_json(service.hosted_receiving.recover_existing(str(data.get('receiptId','')),data,expected))
                if path=='/api/cloud/receiving-revise':return self.send_json(service.hosted_receiving.revise(str(data.get('receiptId','')),data,expected))
                if path=='/api/cloud/receiving-retire':return self.send_json(service.hosted_receiving.retire(str(data.get('receiptId','')),data,expected))
                if path in ('/api/cloud/receiving-commit','/api/cloud/receiving-recover','/api/cloud/receiving-authorize','/api/cloud/receiving-cancel'):
                    return self.send_json(service.hosted_receiving.act(str(data.get('receiptId','')),data,expected,path.rsplit('-',1)[1]))
                if path=='/api/cloud/trade-prepare':return self.send_json(service.shared_trades.prepare(str(data.get('receiptId','')),data,expected))
                if path=='/api/cloud/trade-recovery-preview':return self.send_json(service.original_trade_recovery.preview(data,expected))
                if path=='/api/cloud/trade-recover-original':return self.send_json(service.original_trade_recovery.recover(data,expected))
                if path=='/api/cloud/trade-recovery-inventory':return self.send_json(service.recovery_review.inventory(data,expected))
                if path=='/api/cloud/trade-recovery-retail-review':return self.send_json(service.recovery_review.retail(data,expected))
                if path=='/api/cloud/trade-status':return self.send_json(service.shared_trades.status(str(data.get('receiptId','')),expected))
                if path=='/api/cloud/trade-accept':return self.send_json(service.shared_trades.accept(str(data.get('receiptId','')),data,expected))
                if path=='/api/cloud/trade-print':return self.send_json(service.shared_trade_record(str(data.get('receiptId','')),expected))
                if path=='/api/cloud/trade-record-view':return self.send_json(service.shared_trades.record(str(data.get('receiptId','')),expected))
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
                    service.profile.require_legacy('local Square location lookup')
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
                service.profile.require_legacy('local customer credit')
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
                elif action=='review-cards':
                    if len(pieces)!=5:
                        raise StationError('Use the saved-card review action without extra path segments.')
                    receipt=service.review_cards(receipt_id,data)
                elif action=='approve-price':
                    receipt=service.approve_price(receipt_id,data)
                elif action=='apply-safe-prices':
                    return self.send_json(service.apply_safe_prices(receipt_id,data.get('revision'),data.get('rowIds')))
                elif action=='research':
                    receipt=service.research_receipt(receipt_id,data.get('revision'))
                elif action=='accept-buy':
                    receipt=service.accept_buy(receipt_id,data.get('offerType'))
                elif action=='receive':
                    receipt=service.receive(receipt_id)
                elif action=='labels':
                    if len(pieces)!=5 or urlsplit(self.path).query:
                        raise StationError('Use the saved receipt label action without extra path segments.')
                    receipt=service.labels(receipt_id,data.get('layout'),data.get('dpi'),
                        copy_offset=data.get('copyOffset',0),copy_count=data.get('copyCount'),
                        job_id=data.get('jobId'),expected_session_id=self.headers.get('X-SS-Local-Session',''))
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
                return self.send_json({'errors':['This older interface is no longer supported. Use Quit app, then reopen Stock & Sleeve from its desktop icon.']},410)
            return self.send_json({'errors':['Not found.']},404)

    return StationHandler
