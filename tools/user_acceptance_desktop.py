"""Isolated Chrome lifecycle/research acceptance; never uses store credentials.

With --program, starts the actual frozen executable (which opens its normal
Chrome app window). Without it, suppresses only the source launcher's extra
window; the acceptance browser is still actual Chrome.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch
from urllib.parse import unquote_plus, urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright, expect
import roca_to_square as core
from station_service import StationService


def run(program=None):
    report=ROOT/'Testing'/'2.6.0';report.mkdir(parents=True,exist_ok=True)
    label='packaged' if program else 'source'
    steps=[];errors=[];external=[];result={'started_utc':datetime.now(timezone.utc).isoformat(),
        'scope':'Actual Chrome and isolated fictional local profile; no live Square, hardware or tax acceptance.',
        'mode':label,'program_sha256':hashlib.sha256(program.read_bytes()).hexdigest() if program else None,
        'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                        ('station.js','station_server.py','station_runtime.py','station_desktop.py','roca_to_square.py')},
        'steps':steps,'browser_errors':errors,'external_browser_attempts':external,'passed':False}
    def record(value):steps.append(value);print(value,flush=True)
    process=None
    try:
        with tempfile.TemporaryDirectory(prefix='stock-sleeve-desktop-') as directory:
            profile=Path(directory)
            with patch.object(core,'SETTINGS_FILE',str(profile/'settings.json')):
                service=StationService(core)
                service.store.put('preferences',service.preferences()|{'pricingEnabled':False})
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            base=f'http://127.0.0.1:{port}'
            env=os.environ.copy();env.update(ROCA_DATA_HOME=str(profile),ROCA_STATION_PORT=str(port))
            child="""import roca_to_square as core
core.open_station_browser=lambda url: True
def forbidden(*args,**kwargs):
    raise AssertionError('External HTTP is forbidden in source lifecycle acceptance')
core.requests.sessions.Session.request=forbidden
core.launch_desktop_station(reuse_existing=True)
"""
            command=[str(program)] if program else [sys.executable,'-X','utf8','-c',child]
            process=subprocess.Popen(command,cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                     text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            import requests
            client=requests.Session();client.trust_env=False
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                if process.poll() is not None:raise AssertionError('Desktop process exited before readiness')
                try:
                    response=client.get(base+'/api/runtime',timeout=.5)
                    if response.status_code==200:break
                except requests.RequestException:pass
                time.sleep(.1)
            else:raise AssertionError('Desktop did not become ready')
            instance=response.json()['instance'];record('Desktop launcher started with its isolated profile and port.')
            reopened=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,timeout=12,
                                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            assert reopened.returncode==0,reopened.stdout+reopened.stderr
            assert client.get(base+'/api/runtime',timeout=3).json()['instance']==instance
            record('A second launch reopened the same instance without starting another service.')
            csv='Name,Game,Set,Number,Condition,Printing,Language,Market Price,Quantity\nFictional Research,Riftbound,Fictional Set,001,NM,Normal,English,9.99,1\n'
            response=client.post(base+'/api/import',json={'csv':csv,'fileName':'fictional-research.csv','mode':'labels'},timeout=5)
            assert response.status_code==200,response.text
            receipt=response.json()['receipt'];identifier=receipt['id']
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chrome',headless=True)
                context=browser.new_context(viewport={'width':1440,'height':1000})
                def route_handler(route):
                    uri=urlsplit(route.request.url)
                    if uri.scheme=='http' and uri.hostname=='127.0.0.1' and uri.port==port:route.continue_()
                    else:external.append({'host':uri.hostname,'scheme':uri.scheme});route.abort()
                context.route('**/*',route_handler)
                page=context.new_page();page.set_default_timeout(12_000);page.on('pageerror',lambda error:errors.append(str(error)))
                try:
                    page.goto(base,wait_until='domcontentloaded')
                    expect(page.locator('#version')).to_have_text(core.APP_VERSION)
                    page.locator('#openHistory').click()
                    page.locator('#historyList .history-item').first.click()
                    page.locator('#cardRows [data-edit]').first.click()
                    expect(page.locator('#editPrice')).to_have_value('')
                    page.locator('#compareCardPrices').click()
                    expect(page.locator('#priceResearchDialog')).to_be_visible()
                    links=page.locator('#priceResearchLinks a[target="_blank"]')
                    expect(links).to_have_count(4)
                    for anchor in links.all():
                        href=anchor.get_attribute('href');assert 'riftbound' in unquote_plus(href).lower(),href
                        assert 'noopener' in anchor.get_attribute('rel')
                    saved=client.get(base+'/api/receipts/'+identifier,timeout=3).json()['receipt']
                    assert saved['cards'][0]['price_missing'] is True
                    assert saved['cards'][0]['reviewed'] is False
                    page.screenshot(path=str(report/(label+'-research-unpriced.png')),full_page=True)
                    record('An unpriced Riftbound card opens research and four exact-game searches while remaining unpriced and unchecked.')
                    page.locator('#returnToItem').click();page.locator('#closeDrawer').click()
                    expect(page.locator('#reviewContinue')).to_be_disabled()
                    page.locator('#quitApp').click();expect(page.locator('#confirmDialog')).to_be_visible()
                    page.locator('#confirmCancel').click()
                    assert client.get(base+'/api/runtime',timeout=3).json()['instance']==instance
                    record('Canceling Quit leaves the same app running.')
                    page.locator('#receiptTitle').fill('Fictional lifecycle saved before quit')
                    page.locator('#quitApp').click();page.locator('#confirmOkay').click()
                    expect(page.locator('#quitStatusDialog')).to_contain_text('Stock & Sleeve has stopped.',timeout=20_000)
                    process.wait(timeout=8);assert process.returncode==0
                    assert not (profile/'station_data'/'runtime.json').exists()
                    saved=service.store.receipt(identifier)
                    assert saved['title']=='Fictional lifecycle saved before quit'
                    assert saved['cards'][0]['price_missing'] is True
                    assert saved['inventory_status']=='not_received'
                    page.screenshot(path=str(report/(label+'-quit-stopped.png')),full_page=True)
                    record('Save & quit persisted the last edit, stopped the background process and removed only its runtime record.')
                    assert errors==[],errors
                    assert external==[],external
                    result['passed']=True
                except Exception:
                    page.screenshot(path=str(report/(label+'-desktop-failure.png')),full_page=True)
                    raise
                finally:browser.close()
            client.close()
    except Exception:
        result['failure']=traceback.format_exc();print(result['failure'],flush=True)
    finally:
        if process and process.poll() is None:process.kill()
        if process:
            try:process.communicate(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.communicate(timeout=5)
        result['finished_utc']=datetime.now(timezone.utc).isoformat()
        (report/(label+'-desktop-acceptance.json')).write_text(json.dumps(result,indent=2),encoding='utf-8')
        (report/(label+'-desktop-acceptance.log')).write_text('\n'.join(steps)+'\n'+result.get('failure','PASS\n'),encoding='utf-8')
    return 0 if result['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--program',type=Path)
    args=parser.parse_args();sys.exit(run(args.program.resolve() if args.program else None))
