"""Browser acceptance of the private pilot portal using fictional local responses."""
from pathlib import Path
import json
import hashlib
import traceback
from datetime import datetime,timezone
import sys
import threading
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright, expect
from hosted.app import create_app
from hosted.security import ServiceError

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'Testing'/'2.6.0'


def run():
    REPORT.mkdir(parents=True,exist_ok=True)
    db=Mock();db.ready.return_value=True
    server=make_server('127.0.0.1',0,lambda environ,start_response:[],threaded=True)
    app=create_app({'TESTING':True,'PUBLIC_BASE_URL':f'http://127.0.0.1:{server.server_port}',
                    'APP_ENCRYPTION_KEYS':'fictional-test-key-not-for-deployment'},database=db)
    app.extensions['ss_auth'].resolve=Mock(side_effect=ServiceError('Sign in to your store.',401))
    app.extensions['ss_auth'].login=Mock(side_effect=ServiceError('Store, email or password is incorrect.',401))
    server.app=app
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    results=[]
    outcome={'started_utc':datetime.now(timezone.utc).isoformat(),'results':results,'passed':False,
             'scope':'Local fictional responses; no PostgreSQL or Square calls.',
             'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                             ('hosted/static/portal.js','hosted/static/portal.css','hosted/templates/portal.html')}}
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            page=browser.new_page();errors=[];requests=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('request',lambda request:requests.append(request.url))
            for width,height in ((1440,1000),(1024,768),(390,844)):
                page.set_viewport_size({'width':width,'height':height})
                page.goto(f'http://127.0.0.1:{server.server_port}/')
                expect(page.get_by_role('heading',name='Sign in to your store')).to_be_visible()
                expect(page.locator('#signin').get_by_label('Email',exact=True)).to_be_visible()
                expect(page.locator('#signin').get_by_label('Password',exact=True)).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Horizontal overflow'
                page.screenshot(path=str(REPORT/f'hosted-portal-{width}.png'),full_page=True)
                results.append(f'PASS sign-in layout and labels at {width}x{height}; no horizontal overflow')
            page.locator('#signin').get_by_label('Store ID',exact=True).fill('fictional-store')
            page.locator('#signin').get_by_label('Email',exact=True).fill('cashier@example.invalid')
            page.locator('#signin').get_by_label('Password',exact=True).fill('fictional-password')
            page.get_by_role('button',name='Open store workspace').click()
            expect(page.locator('#loginStatus')).to_contain_text('Store, email or password is incorrect.')
            expect(page.get_by_role('button',name='Open store workspace')).to_be_enabled()
            results.append('PASS failed sign-in shows a safe actionable message and enables retry')
            assert not errors,errors
            assert all(url.startswith(f'http://127.0.0.1:{server.server_port}/') for url in requests), 'Unexpected external request'
            results.append('PASS no JavaScript errors or third-party requests')
            outcome['passed']=True
            browser.close()
    except Exception:
        outcome['failure']=traceback.format_exc()
        raise
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        outcome['finished_utc']=datetime.now(timezone.utc).isoformat()
        (REPORT/'hosted-portal-acceptance.json').write_text(json.dumps(outcome,indent=2),encoding='utf-8')
    print('\n'.join(results))


if __name__=='__main__':run()
