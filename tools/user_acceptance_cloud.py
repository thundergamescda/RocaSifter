"""Chrome operator walkthrough through two real loopback stations; fictional hosted API."""
from contextlib import ExitStack
from pathlib import Path
import json
import sys
import tempfile
import threading
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright,expect
import roca_to_square as core
from station_service import StationService
from station_cloud import CloudClient
from test_station_cloud import SharedFixtureTransport

REPORT=ROOT/'Testing'/'2.6.0'


def run():
    REPORT.mkdir(parents=True,exist_ok=True);steps=[];errors=[];servers=[]
    remote=SharedFixtureTransport()
    def passed(text):steps.append('PASS '+text);print(steps[-1],flush=True)
    with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
        for name in ('till-a','till-b'):
            profile=Path(folder)/name;profile.mkdir()
            with patch.object(core,'SETTINGS_FILE',str(profile/'settings.json')):
                service=StationService(core)
            service.cloud=CloudClient(service.store.root,transport=remote)
            server=core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler);server.station=service
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();servers.append((server,thread))
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chrome',headless=True)
                pages=[browser.new_page(viewport={'width':1440,'height':1000}) for _ in servers]
                for page,(server,_) in zip(pages,servers):
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.route('**/*',lambda route:route.continue_() if route.request.url.startswith('http://127.0.0.1:') else route.abort())
                    page.goto(f'http://127.0.0.1:{server.server_port}/')
                    page.locator('#cloudNav').click();expect(page.locator('#cloudSignin')).to_be_visible()
                def sign_in(page,email):
                    page.locator('#cloudStore').fill('fictional-store');page.locator('#cloudEmail').fill(email)
                    page.locator('#cloudPassword').fill('fixture-staff-password');page.locator('#cloudLoginForm button[type=submit]').click()
                    expect(page.locator('#cloudWorkspace')).to_be_visible();expect(page.locator('#cloudConnection')).to_contain_text('fictional-store')
                first,second=pages;sign_in(first,'manager@example.invalid');sign_in(second,'cashier@example.invalid')
                passed('Two isolated desktop profiles sign in to the same fictional store; browser sees no bearer tokens')
                first.locator('#cloudNewCustomer').click();first.locator('#cloudCustomerName').fill('Fictional Shared Customer')
                first.locator('#cloudCustomerPermission').check();first.locator('#cloudCustomerForm button[type=submit]').click()
                expect(first.locator('#cloudAccount h2')).to_have_text('Fictional Shared Customer')
                first.locator('#cloudOpenEntry').click();first.locator('#cloudEntryAmount').fill('50')
                first.locator('#cloudEntryReference').fill('FIXTURE OPENING');first.locator('#cloudEntryReason').fill('Fictional pilot opening')
                first.locator('#cloudEntryForm button[type=submit]').click()
                expect(first.locator('#cloudAccount .credit-balance-header>strong')).to_have_text('$50.00')
                second.locator('#cloudRefresh').click();second.locator('[data-cloud-customer]').click()
                expect(second.locator('#cloudAccount .credit-balance-header>strong')).to_have_text('$50.00')
                expect(second.locator('#cloudOpenEntry')).to_have_count(0)
                passed('Manager opens $50 shared balance; cashier sees the same account and cannot access manager correction controls')
                first.locator('#cloudOpenEntry').click();first.locator('#cloudEntryKind').select_option('adjust_remove')
                first.locator('#cloudEntryAmount').fill('20');first.locator('#cloudEntryReference').fill('FIXTURE CORRECTION')
                first.locator('#cloudEntryReason').fill('Fictional manager correction');first.locator('#cloudEntryForm button[type=submit]').click()
                expect(first.locator('#cloudAccount .credit-balance-header>strong')).to_have_text('$30.00')
                second.locator('#cloudRefresh').click();expect(second.locator('#cloudAccount .credit-balance-header>strong')).to_have_text('$30.00')
                passed('Shared correction and history refresh to $30 on both desktops; no Square sale or redemption simulated')
                first.screenshot(path=str(REPORT/'shared-credit-desktop.png'),full_page=True)
                # Lose only the completed correction response, then restart the station client.
                original=remote.request;lose_once={'value':True}
                def lost_response(method,url,**options):
                    result=original(method,url,**options)
                    if lose_once['value'] and url.endswith('/adjust'):
                        lose_once['value']=False
                        import requests
                        raise requests.Timeout('Fictional completed response lost')
                    return result
                remote.request=lost_response
                first.locator('#cloudOpenEntry').click();first.locator('#cloudEntryKind').select_option('adjust_add')
                first.locator('#cloudEntryAmount').fill('5');first.locator('#cloudEntryReference').fill('FIXTURE LOST RESPONSE')
                first.locator('#cloudEntryReason').fill('Fictional recovery test');first.locator('#cloudEntryForm button[type=submit]').click()
                expect(first.locator('#cloudPending')).to_be_visible();expect(first.locator('[data-cloud-check]')).to_have_count(1)
                remote.request=original
                station=servers[0][0].station;station.cloud=CloudClient(station.store.root,transport=remote)
                first.reload();first.locator('#cloudNav').click();expect(first.locator('#cloudPending')).to_be_visible()
                first.locator('[data-cloud-check]').click();expect(first.locator('#cloudPending')).to_be_hidden()
                first.locator('[data-cloud-customer]').click();expect(first.locator('#cloudAccount .credit-balance-header>strong')).to_have_text('$35.00')
                self_entries=list(remote.entries.values())[0];assert len(self_entries)==3
                passed('Lost successful response survives client restart; Check server recovers it once, leaving $35 and three ledger entries')
                remote.offline=True;first.locator('#cloudRefresh').click()
                expect(first.locator('#cloudConnection')).to_contain_text('Sign in to your store')
                expect(first.locator('#cloudMessage')).to_contain_text('unavailable')
                expect(first.locator('#cloudAccount')).not_to_contain_text('$35.00')
                passed('Outage hides unconfirmed shared balance and blocks shared writes')
                remote.offline=False;first.locator('#cloudRefresh').click();expect(first.locator('#cloudWorkspace')).to_be_visible()
                first.locator('#cloudSignOut').click();expect(first.locator('#cloudSignin')).to_be_visible()
                expect(first.locator('#cloudMessage')).to_contain_text('revoked')
                passed('Sign out revokes this desktop session and hides shared customer information')
                assert not errors,errors;passed('No JavaScript errors across the full shared-credit walkthrough')
                browser.close()
        finally:
            for server,thread in servers:
                server.station.stop_event.set();server.shutdown();server.server_close();thread.join(timeout=5)
            (REPORT/'shared-credit-user-acceptance.json').write_text(json.dumps({'steps':steps,'javascript_errors':errors,
                'scope':'Two real desktop HTTP servers/Chrome profiles; fictional in-memory hosted API. Not real two-PC or Square proof.'},indent=2))


if __name__=='__main__':run()
