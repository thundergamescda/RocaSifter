"""Seven UNRUN native-current-desktop flows. No direct JS action invocation.

Fixtures seed fictional reviewed receiving through real service methods; these
flows begin at saved work. CSV/manual entry UI and physical devices are separate.
"""
import re

from tools.desktop_label_browser_fixture import DEVICE_PASSWORD, STAFF_PASSWORD


def saved_receipt(ctx, page=None):
    from playwright.sync_api import expect
    page = page or ctx.page
    ctx.bootstrap(page)
    expect(page.locator('#version')).to_have_text('2.6.0')
    page.locator('#openHistory').click()
    expect(page.locator('#historyDialog')).to_be_visible()
    page.locator('#historyList button[data-open="' + ctx.fixture.ident + '"]').click()
    expect(page.locator('#historyDialog')).not_to_be_visible()
    expect(page.locator('#receiptReference')).to_contain_text(ctx.fixture.ident)
    expect(page.locator('#reviewView')).to_be_visible()
    page.locator('#reviewContinue').click()
    expect(page.locator('#finishView')).to_be_visible()
    page.locator('#printOptions > summary').click()
    expect(page.locator('#labelCopyStart')).to_be_visible()
    page.locator('#labelCopyStart').fill('1')
    page.locator('#labelCopyCount').fill('1')
    page.locator('#labelSeparateReprint').check()
    expect(page.locator('#labelsOnly')).to_be_enabled()
    ctx.check(ctx.fixture.durable()['jobs'] == 0, 'NATIVE_SAVED_RECEIPT_WITHOUT_PRIOR_PAIR')


def no_adoption_after_error(ctx, reads_before, text):
    from playwright.sync_api import expect
    expect(ctx.page.locator('#toast')).to_be_visible()
    expect(ctx.page.locator('#toast')).to_contain_text(text)
    expect(ctx.page.locator('#toast')).to_contain_text('retry the same pair or use saved-job recovery')
    expect(ctx.page.locator('#doneView')).not_to_be_visible()
    expect(ctx.page.locator('#labelSeparateReprint')).to_be_checked()
    expect(ctx.page.locator('#labelsOnly')).to_be_enabled()
    # The catch has completed when its visible error appears. Pump real event
    # delivery, including the normal receipt-list/pricing poll, without issuing
    # any test GET or calling an application action function.
    ctx.page.wait_for_timeout(300)
    ctx.check(ctx.generic_reads() == reads_before, 'ZERO_AUTOMATIC_GENERIC_RECEIPT_REFRESH_AFTER_DENIAL')
    ctx.check(ctx.page.locator('#outputFiles a').count() == 0, 'NO_REJECTED_RECEIPT_FILE_ADOPTION')
    pair = ctx.fixture.durable()
    ctx.check(pair['jobs'] == pair['published_events'] == pair['renders'] == 1,
              'DENIAL_PRESERVES_ONE_REAL_COMMITTED_PAIR')
    return pair


def explicit_retry(ctx, original, capture_name):
    from playwright.sync_api import expect
    index = len(ctx.label_requests)
    ctx.page.locator('#labelsOnly').click()
    second = ctx.label_terminal(index, 200)
    ctx.check(second['job_key_sha256'] == original['job_key_sha256'], 'NATIVE_RETRY_RETAINS_ORIGINAL_JOB_KEY')
    ctx.check(second['staff_context_sha256'] == original['staff_context_sha256'], 'SAME_NATIVE_STAFF_CONTEXT_FOR_RETRY')
    expect(ctx.page.locator('#doneView')).to_be_visible()
    expect(ctx.page.locator('#printLabels')).to_be_visible()
    pair = ctx.fixture.durable()
    ctx.check(pair['job_key_sha256'] == original['job_key_sha256'] and pair['renders'] == 1
              and pair['published_events'] == 1, 'EXPLICIT_RETRY_DOES_NOT_DUPLICATE_JOB_RENDER_OR_PUBLICATION')
    ctx.capture(ctx.page, capture_name)
    ctx.download(ctx.page, 'pdf')


def native_pair(ctx):
    from playwright.sync_api import expect
    saved_receipt(ctx)
    before = ctx.generic_reads()
    ctx.page.locator('#labelsOnly').click()
    request = ctx.label_terminal(0, 200)
    expect(ctx.page.locator('#doneView')).to_be_visible()
    expect(ctx.page.locator('#doneSummary')).to_contain_text('No inventory changed.')
    ctx.check(ctx.generic_reads() == before, 'SUCCESS_HAS_NO_GENERIC_RECEIPT_RELOAD')
    pair = ctx.fixture.durable()
    ctx.check(pair['job_key_sha256'] == request['job_key_sha256'], 'NATIVE_REQUEST_MATCHES_DURABLE_JOB')
    ctx.capture(ctx.page, 'DUI01_done')
    ctx.download(ctx.page, 'pdf')
    ctx.download(ctx.page, 'zpl')
    ctx.check(len(ctx.label_requests) == 1, 'ONE_NATIVE_LABEL_REQUEST')


def denial_and_retry(ctx, *, change, status, prefix):
    saved_receipt(ctx)
    hold = ctx.fixture.arm('pair_read')
    before_reads = ctx.generic_reads()
    before_session = ctx.fixture.session_hash()
    ctx.page.locator('#labelsOnly').click()
    ctx.wait(ctx.page, lambda: hold.ready, 'REAL_FINAL_PAIR_READ_HOLD_REQUIRED')
    ctx.check(hold.observation['published_events'] == 1 and hold.observation['renders'] == 1,
              'REAL_PAIR_COMMITTED_BEFORE_REMOTE_CHANGE')
    if change == 'role':
        ctx.fixture.remote_role('cashier')
        message = 'staff role changed'
    else:
        ctx.fixture.source_state('outcome_unknown')
        message = 'hosted receiving plan does not match'
    ctx.check(ctx.fixture.session_hash() == before_session, 'REMOTE_DENIAL_KEEPS_LOCAL_SESSION_ID')
    hold.release()
    first = ctx.label_terminal(0, status)
    original = no_adoption_after_error(ctx, before_reads, message)
    ctx.check(first['job_key_sha256'] == original['job_key_sha256'], 'DENIED_REQUEST_REMAINS_DURABLY_IDENTICAL')
    ctx.capture(ctx.page, prefix + '_denied')
    if change == 'role':
        ctx.fixture.remote_role('manager')
    else:
        ctx.fixture.source_state('received')
    explicit_retry(ctx, first, prefix + '_recovered')


def remote_role_denial(ctx):
    denial_and_retry(ctx, change='role', status=400, prefix='DUI02')


def remote_source_denial(ctx):
    denial_and_retry(ctx, change='source', status=502, prefix='DUI03')


def native_admin_workspace_change(ctx):
    from playwright.sync_api import expect
    saved_receipt(ctx)
    hold = ctx.fixture.arm('completed_reply')
    before_reads = ctx.generic_reads()
    ctx.page.locator('#labelsOnly').click()
    ctx.wait(ctx.page, lambda: hold.ready, 'REAL_SERVER_SUCCESS_COMPLETED_BEFORE_DELIVERY')
    ctx.check(hold.observation['published_events'] == 1, 'SERVER_SUCCESS_IS_REAL_COMMITTED_PUBLICATION')
    # Navigation/history/new-batch handlers deliberately refuse work while busy.
    # Device Admin is a genuinely reachable native control in the current UI.
    # Unlock uses real local auth/cookie handling; no cookie injection or JS state.
    expect(ctx.page.locator('#openAdmin')).to_be_visible()
    expect(ctx.page.locator('#openAdmin')).to_be_enabled()
    ctx.page.locator('#openAdmin').click()
    expect(ctx.page.locator('#adminAuth')).to_be_visible()
    ctx.page.locator('#adminPassword').fill(DEVICE_PASSWORD)
    ctx.page.locator('#authSubmit').click()
    expect(ctx.page.locator('#adminSettings')).to_be_visible()
    ctx.check(ctx.fixture.service.auth.configured(), 'NATIVE_DEVICE_ADMIN_USES_REAL_AUTH_CONFIGURATION')
    ctx.capture(ctx.page, 'DUI04_admin_context')
    hold.release()
    first = ctx.label_terminal(0, 200)
    expect(ctx.page.locator('#toast')).to_contain_text('workspace changed')
    # Observe rejection before closing the native modal resets local admin state.
    expect(ctx.page.locator('#doneView')).not_to_be_visible()
    ctx.page.locator('#closeAdmin').click()
    expect(ctx.page.locator('#adminDialog')).not_to_be_visible()
    no_adoption_after_error(ctx, before_reads, 'workspace changed')
    ctx.capture(ctx.page, 'DUI04_rejected')
    explicit_retry(ctx, first, 'DUI04_recovered')


def native_staff_logout(ctx):
    from playwright.sync_api import expect
    saved_receipt(ctx)
    other = ctx.context.new_page()
    ctx.bootstrap(other)
    other.locator('#cloudNav').click()
    expect(other.locator('#cloudWorkspace')).to_be_visible()
    expect(other.locator('#cloudSignOut')).to_be_enabled()
    hold = ctx.fixture.arm('pair_read')
    before_reads = ctx.generic_reads()
    before_session = ctx.fixture.session_hash()
    ctx.page.locator('#labelsOnly').click()
    ctx.wait(ctx.page, lambda: hold.ready, 'REAL_PAIR_HOLD_BEFORE_NATIVE_LOGOUT')
    other.locator('#cloudSignOut').click()
    expect(other.locator('#cloudSignin')).to_be_visible()
    ctx.wait(other, lambda: ctx.fixture.session_hash() is None, 'REAL_NATIVE_STAFF_SESSION_CLEARED')
    hold.release()
    first = ctx.label_terminal(0, 409)
    no_adoption_after_error(ctx, before_reads, 'workspace changed')
    ctx.capture(ctx.page, 'DUI05_signed_out')
    other.close()
    # Real native re-login and explicit saved-batch reopening. The busy first
    # page cannot natively expose same-page staff sign-out; no control is forced.
    ctx.page.locator('#cloudNav').click()
    expect(ctx.page.locator('#cloudLoginForm')).to_be_visible()
    ctx.page.locator('#cloudStore').fill('fictional-store')
    ctx.page.locator('#cloudEmail').fill('manager@example.invalid')
    ctx.page.locator('#cloudPassword').fill(STAFF_PASSWORD)
    ctx.page.locator('#cloudLoginForm button[type=submit]').click()
    expect(ctx.page.locator('#cloudWorkspace')).to_be_visible()
    ctx.wait(ctx.page, lambda: ctx.fixture.session_hash() not in (None, before_session), 'REAL_NATIVE_NEW_STAFF_SESSION')
    ctx.page.locator('#openHistory').click()
    ctx.page.locator('#historyList button[data-open="' + ctx.fixture.ident + '"]').click()
    expect(ctx.page.locator('#doneView')).to_be_visible()
    pair = ctx.fixture.durable()
    ctx.check(pair['job_key_sha256'] == first['job_key_sha256'] and pair['jobs'] == 1,
              'EXPLICIT_REOPEN_RECOVERS_ORIGINAL_JOB_AFTER_NATIVE_LOGOUT')
    ctx.check(len(ctx.label_requests) == 1 and pair['renders'] == 1 and pair['published_events'] == 1,
              'NATIVE_LOGOUT_RECOVERY_ADDS_NO_LABEL_REQUEST_OR_STOCK')
    ctx.capture(ctx.page, 'DUI05_reopened')
    ctx.download(ctx.page, 'pdf')



def completed_download_workspace_change(ctx, *, change, prefix):
    from playwright.sync_api import expect
    saved_receipt(ctx)
    ctx.page.locator('#labelsOnly').click()
    ctx.label_terminal(0, 200)
    expect(ctx.page.locator('#doneView')).to_be_visible()
    expect(ctx.page.locator('#printLabels')).to_be_visible()
    original = ctx.fixture.durable()
    ctx.check(original['jobs'] == original['published_events'] == original['renders'] == 1,
              'REAL_PAIR_EXISTS_BEFORE_DOWNLOAD_CONTEXT_CHANGE')
    hold = ctx.fixture.arm('completed_download')
    before_events = len(ctx.download_events)
    ctx.page.locator('#printLabels').click()
    ctx.wait(ctx.page, lambda: hold.ready, 'REAL_AUTHORIZED_DOWNLOAD_COMPLETE_BEFORE_DELIVERY')
    completed = hold.observation['completed_download']
    ctx.check(completed['status'] == 200 and completed['kind'] == 'pdf'
              and completed['bytes'] == original['files']['pdf']['bytes']
              and completed['sha256'] == original['files']['pdf']['sha256'],
              'HELD_DOWNLOAD_IS_ORIGINAL_AUTHORIZED_200_BYTES')
    before_session = ctx.fixture.session_hash()
    if change == 'same_receipt':
        expect(ctx.page.locator('#openHistory')).to_be_enabled()
        ctx.page.locator('#openHistory').click()
        expect(ctx.page.locator('#historyDialog')).to_be_visible()
        ctx.page.locator('#historyList button[data-open="' + ctx.fixture.ident + '"]').click()
        expect(ctx.page.locator('#historyDialog')).not_to_be_visible()
        expect(ctx.page.locator('#receiptReference')).to_contain_text(ctx.fixture.ident)
        expect(ctx.page.locator('#doneView')).to_be_visible()
        ctx.check(ctx.fixture.session_hash() == before_session,
                  'SAME_RECEIPT_REOPEN_PRESERVES_STAFF_SESSION')
    else:
        ctx.check(change == 'device_admin', 'FIXED_DOWNLOAD_CONTEXT_CHANGE_KIND')
        expect(ctx.page.locator('#openAdmin')).to_be_enabled()
        ctx.page.locator('#openAdmin').click()
        expect(ctx.page.locator('#adminAuth')).to_be_visible()
        ctx.page.locator('#adminPassword').fill(DEVICE_PASSWORD)
        ctx.page.locator('#authSubmit').click()
        expect(ctx.page.locator('#adminSettings')).to_be_visible()
        ctx.check(ctx.fixture.service.auth.configured(), 'DOWNLOAD_CHANGE_USES_REAL_DEVICE_AUTH')
        ctx.check(ctx.fixture.session_hash() == before_session,
                  'DEVICE_ADMIN_CHANGE_PRESERVES_STAFF_SESSION')
    hold.release()
    expect(ctx.page.locator('#toast')).to_be_visible()
    expect(ctx.page.locator('#toast')).to_contain_text('workspace changed')
    # The client rejection follows response.blob(). Keep observers active through
    # final context/server/browser close as well, so a late anchor cannot escape.
    ctx.page.wait_for_timeout(300)
    ctx.check(len(ctx.download_events) == before_events,
              'COMPLETED_STALE_DOWNLOAD_EMITS_NO_BROWSER_DOWNLOAD_EVENT')
    ctx.check(ctx.fixture.durable() == original,
              'REJECTED_DOWNLOAD_PRESERVES_PAIR_RENDER_PUBLICATION_AND_STOCK')
    ctx.capture(ctx.page, prefix + '_rejected')
    if change == 'device_admin':
        ctx.page.locator('#closeAdmin').click()
        expect(ctx.page.locator('#adminDialog')).not_to_be_visible()
    expect(ctx.page.locator('#doneView')).to_be_visible()
    ctx.download(ctx.page, 'pdf')
    ctx.check(len(ctx.download_events) == before_events + 1,
              'ONE_FRESH_EXPLICIT_DOWNLOAD_AFTER_CONTEXT_REJECTION')
    ctx.check(len(ctx.label_requests) == 1 and ctx.fixture.durable() == original,
              'FRESH_DOWNLOAD_RECOVERY_ADDS_NO_LABEL_JOB_OR_STOCK')
    ctx.capture(ctx.page, prefix + '_recovered')


def same_receipt_download_change(ctx):
    completed_download_workspace_change(ctx, change='same_receipt', prefix='DUI06')


def device_admin_download_change(ctx):
    completed_download_workspace_change(ctx, change='device_admin', prefix='DUI07')


CASES = (
    ('DUI01', native_pair),
    ('DUI02', remote_role_denial),
    ('DUI03', remote_source_denial),
    ('DUI04', native_admin_workspace_change),
    ('DUI05', native_staff_logout),
    ('DUI06', same_receipt_download_change),
    ('DUI07', device_admin_download_change),
)
