"""Real Chrome CSV mapping acceptance; fictional loopback profile, no Square."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright,expect
import roca_to_square as core
from station_service import StationService

REPORT=ROOT/'Testing'/'2.6.0'
SOURCES=('import.js','import.css','scanner_adapters.py','station_service.py','station_server.py',
         'station.js','roca_square_label_app.html','tools/user_acceptance_import.py')


def fingerprints():return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


def run():
    REPORT.mkdir(parents=True,exist_ok=True);steps=[];cases=[];errors=[];external=[];backend=[]
    sources=fingerprints();started=datetime.now(timezone.utc).isoformat()
    def record(message):steps.append(message);print(message,flush=True)
    def forbidden(session,method,url,**kwargs):
        backend.append({'method':str(method),'host':urlsplit(str(url)).hostname})
        raise AssertionError('External backend HTTP is forbidden in mapping acceptance.')
    with tempfile.TemporaryDirectory() as folder,patch.object(core,'SETTINGS_FILE',str(Path(folder)/'settings.json')), \
         patch.object(core,'OUTPUT_DIR',str(Path(folder)/'outputs')),patch.object(core.requests.sessions.Session,'request',forbidden), \
         patch.object(core,'upload_to_square',side_effect=AssertionError('Square upload forbidden')) as square:
        server=core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler);service=server.station=StationService(core)
        service.store.put('preferences',service.preferences()|{'pricingEnabled':False})
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chrome',headless=True)
                context=browser.new_context(viewport={'width':1440,'height':1100})
                def route(request):
                    uri=urlsplit(request.request.url)
                    if uri.scheme=='http' and uri.hostname=='127.0.0.1' and uri.port==server.server_port:request.continue_()
                    else:external.append({'scheme':uri.scheme,'host':uri.hostname});request.abort()
                context.route('**/*',route);page=context.new_page();page.set_default_timeout(10000)
                page.on('pageerror',lambda error:errors.append(str(error)));page.on('dialog',lambda dialog:dialog.accept())
                def reset():
                    page.goto(base,wait_until='domcontentloaded');expect(page.locator('#version')).to_have_text('2.6.0')
                    page.locator('#newReceipt').click();expect(page.locator('#emptyState')).to_be_visible()
                def count():
                    with service.store.db() as db:return db.execute('SELECT count(*) FROM receipts').fetchone()[0]
                def upload(text,name='fictional-export.csv'):
                    # A reopened batch may already have this picker value while
                    # its refresh is still running. Wait for the visible ready
                    # state before the next file-selection interaction.
                    expect(page.locator('#newReceipt')).to_be_enabled()
                    content=text.encode('utf-8') if isinstance(text,str) else text
                    page.locator('#csvFile').set_input_files({'name':name,'mimeType':'text/csv','buffer':content})
                    expect(page.locator('#importDialog')).to_be_visible()
                    expect(page.locator('#importUpdatePreview')).to_be_enabled()
                def update():
                    with page.expect_response(lambda response:response.url.endswith('/api/import/preview')):
                        page.locator('#importUpdatePreview').click()
                    expect(page.locator('#importUpdatePreview')).to_be_enabled()
                def choose(field,column):
                    identifier='importMap'+''.join(word.title() for word in field.split('_'))
                    page.locator('#'+identifier).select_option(column)
                def commit():
                    expect(page.locator('#importCommit')).to_be_enabled()
                    with page.expect_response(lambda response:response.url.endswith('/api/import') and response.request.method=='POST') as info:
                        page.locator('#importCommit').click()
                    response=info.value
                    assert response.status==200,response.text()
                    expect(page.locator('#importDialog')).to_be_hidden()
                    data=response.json();identifier=data['receipt']['id']
                    expect(page.locator('#receiptPicker')).to_have_value(identifier)
                    return data
                def case(name,callback):
                    begin=time.monotonic()
                    try:reset();callback();cases.append({'name':name,'status':'pass','elapsed_seconds':round(time.monotonic()-begin,3)});record('PASS '+name)
                    except Exception:
                        failure=traceback.format_exc();screenshot=f'import-failure-{len(cases)+1}.png'
                        try:page.screenshot(path=str(REPORT/screenshot),full_page=True)
                        except Exception:screenshot=None
                        cases.append({'name':name,'status':'fail','error':failure,'screenshot':screenshot});record('FAIL '+name+'\n'+failure)

                def cancellation_and_mapping():
                    original=count();simple='Name,Price,Quantity\nFictional Cancel,1.13,2\n'
                    upload(simple);expect(page.locator('#importCommit')).to_be_enabled()
                    assert count()==original;page.locator('#importCancel').click();assert count()==original
                    text='Scanner Title,Set Label,Collector,Game Kind,Copies,Ask USD\nFictional A,Fixture Set,001,Riftbound,3,1.13\nFictional B,Fixture Set,002,One Piece,4,2.17\n'
                    upload(text,'fictional-custom.csv');expect(page.locator('#importCommit')).to_be_disabled()
                    expect(page.locator('#importIssues')).to_contain_text('Map a Card Name')
                    assert count()==original
                    for field,column in {'card_name':'Scanner Title','card_set':'Set Label','card_num':'Collector','game':'Game Kind','quantity':'Copies','price':'Ask USD'}.items():choose(field,column)
                    expect(page.locator('#importCommit')).to_be_disabled();update();expect(page.locator('#importCommit')).to_be_enabled()
                    expect(page.locator('#importRows')).to_contain_text('Riftbound');expect(page.locator('#importRows')).to_contain_text('One Piece')
                    page.locator('#importSource summary').click();expect(page.locator('#importSourceRows')).to_contain_text('Fictional A')
                    page.screenshot(path=str(REPORT/'import-column-mapping.png'),full_page=True)
                    result=commit();receipt=service.store.receipt(result['receipt']['id'])
                    assert [card['qty'] for card in receipt['cards']]==[3,4]
                    assert [card['final_price'] for card in receipt['cards']]==[1.13,2.17]
                    assert {card['game'] for card in receipt['cards']}=={'riftbound','onepiece'}
                    assert receipt['mixed_games'] and not any(card['reviewed'] for card in receipt['cards'])
                    assert count()==original+1 and receipt['inventory_status']=='not_received'

                def quantity_selection_and_snapshot():
                    original=count();text='Product Name,Product Line,Add to Quantity,Total Quantity,TCG Marketplace Price\nFictional Delivery,Magic,2,20,1.13\n'
                    upload(text);expect(page.locator('#importCommit')).to_be_disabled()
                    expect(page.locator('#importIssues')).to_contain_text('different quantity meanings')
                    choose('quantity','Add to Quantity');update()
                    expect(page.locator('#importSnapshot')).to_be_hidden();expect(page.locator('#importCommit')).to_be_enabled()
                    page.locator('#importMapping summary').first.click()
                    page.locator('#importOverview').scroll_into_view_if_needed()
                    page.screenshot(path=str(REPORT/'import-quantity-choice.png'),full_page=True)
                    receipt=commit()['receipt'];assert receipt['cards'][0]['qty']==2
                    snapshot=text.replace('Fictional Delivery','Fictional Snapshot')
                    upload(snapshot,'fictional-snapshot.csv');choose('quantity','Total Quantity');update()
                    expect(page.locator('#importSnapshot')).to_be_visible();expect(page.locator('#importCommit')).to_be_disabled()
                    assert count()==original+1
                    page.locator('#importMapping summary').first.click()
                    page.locator('#importSnapshot').scroll_into_view_if_needed()
                    page.screenshot(path=str(REPORT/'import-snapshot-confirmation.png'),full_page=True)
                    page.locator('#importSnapshotAck').check();expect(page.locator('#importCommit')).to_be_enabled()
                    page.locator('#importCancel').click();assert count()==original+1
                    upload(snapshot,'fictional-snapshot.csv');choose('quantity','Total Quantity');update();page.locator('#importSnapshotAck').check()
                    receipt=commit()['receipt'];assert receipt['cards'][0]['qty']==20
                    assert receipt['inventory_status']=='not_received' and count()==original+2

                def invalid_rows_do_not_partially_import():
                    original=count();upload('Name,Price,Quantity\nFictional Good,1,1\nFictional Invalid,-1,1\n','fictional-invalid.csv')
                    expect(page.locator('#importCommit')).to_be_disabled();expect(page.locator('#importIssues')).to_contain_text('nonnegative USD price')
                    assert count()==original
                    page.screenshot(path=str(REPORT/'import-error-review.png'),full_page=True)
                    page.locator('#importCancel').click();assert count()==original

                def duplicate_and_additional_copy_confirmation():
                    original=count();text='Name,Game,Price,Quantity\nFictional Duplicate,Gundam,2.13,2\n'
                    upload(text,'fictional-duplicate.csv');first=commit()['receipt'];assert count()==original+1
                    upload(text,'renamed-duplicate.csv');expect(page.locator('#importExisting')).to_be_visible()
                    page.locator('#importOpenExisting').click();expect(page.locator('#importDialog')).to_be_hidden()
                    expect(page.locator('#receiptPicker')).to_have_value(first['id']);assert count()==original+1
                    upload(text,'fictional-duplicate.csv')
                    page.locator('#importCommit').click();expect(page.locator('#confirmDialog')).to_be_visible()
                    page.locator('#confirmCancel').click();expect(page.locator('#confirmDialog')).to_be_hidden();assert count()==original+1
                    upload(text,'fictional-duplicate.csv');page.locator('#importCommit').click();expect(page.locator('#confirmDialog')).to_be_visible()
                    with page.expect_response(lambda response:response.url.endswith('/api/import') and response.request.method=='POST' and response.request.post_data_json.get('newDelivery') is True) as info:
                        page.locator('#confirmOkay').click()
                    second=info.value.json()['receipt'];expect(page.locator('#receiptPicker')).to_have_value(second['id'])
                    assert second['id']!=first['id'] and count()==original+2
                    assert second['inventory_status']=='not_received'

                def stale_preview_cannot_approve_changed_choices():
                    original=count();entered=threading.Event();release=threading.Event();actual=service.preview_import;first=[True]
                    def held(*args,**kwargs):
                        if first[0]:first[0]=False;entered.set();assert release.wait(5)
                        return actual(*args,**kwargs)
                    with patch.object(service,'preview_import',side_effect=held):
                        page.locator('#csvFile').set_input_files({'name':'fictional-slow.csv','mimeType':'text/csv','buffer':b'Name,Price,Quantity\nFictional Slow,1,1\n'})
                        expect(page.locator('#importDialog')).to_be_visible()
                        deadline=time.monotonic()+5
                        while not entered.is_set() and time.monotonic()<deadline:page.wait_for_timeout(20)
                        assert entered.is_set()
                        page.locator('#importMapping summary').first.click();page.locator('#importDefaultGame').select_option('magic')
                        release.set();expect(page.locator('#importCommit')).to_be_disabled()
                        update();expect(page.locator('#importRows')).to_contain_text('Magic: The Gathering')
                        expect(page.locator('#importCommit')).to_be_enabled();page.locator('#importCancel').click()
                    assert count()==original

                case('Cancel leaves no receipt; generic scanner columns and per-row games map into one unchecked draft',cancellation_and_mapping)
                case('Add-to-quantity and stock snapshots require explicit choices and snapshot acknowledgement',quantity_selection_and_snapshot)
                case('Malformed prices block the whole file without a partial import',invalid_rows_do_not_partially_import)
                case('Saved-file reopen and additional physical-copy confirmation preserve duplicate protection',duplicate_and_additional_copy_confirmation)
                case('A stale preview cannot approve mapping choices changed during a slow response',stale_preview_cannot_approve_changed_choices)
                assert not errors,errors;assert not external,external;assert not backend,backend;assert square.call_count==0
                record('PASS no external browser/backend HTTP, no Square upload and no uncaught JavaScript errors')
                browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(5);end=fingerprints();stable=sources==end
            if not stable:record('EVIDENCE NEEDS RETEST: sources changed during the walkthrough.')
            payload={'started_utc':started,'finished_utc':datetime.now(timezone.utc).isoformat(),'scope':'Real Chrome loopback import preview and draft intake; fictional data only.',
                     'source_stable':stable,'sources_before':sources,'sources_after':end,'cases':cases,'browser_errors':errors,
                     'external_browser_attempts':external,'backend_http_attempts':backend,'square_upload_attempts':square.call_count}
            (REPORT/'import-user-acceptance.json').write_text(json.dumps(payload,indent=2),encoding='utf-8')
            (REPORT/'import-user-acceptance.log').write_text('\n'.join(steps)+'\n',encoding='utf-8')
    if not stable or not cases or any(case['status']!='pass' for case in cases):raise SystemExit(1)


if __name__=='__main__':run()
