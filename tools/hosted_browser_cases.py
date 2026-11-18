"""Nine source-authored real hosted-HTTPS browser flows; UNRUN until admitted.

Root owns the browser, private trust store, service fixture and evidence capture.
No auth/me fulfilment, cookie injection, traces, secret logging or desktop imports.
Only sanitized booleans/counts/labels leave these cases. Playwright and server
exceptions are converted by the runner to fixed safe failure codes, never reprs.
"""
import time

from hosted.mfa import secret_bytes, totp


DESKTOP = {'width': 1440, 'height': 1000}
MOBILE = {'width': 390, 'height': 844}
EXTRA_MASKS = ('#staffList', '#storeName')


def _visible(ctx, page, selector, code):
    try:
        page.locator(selector).wait_for(state='visible', timeout=10000)
    except Exception:
        ctx.require(False, code)
    ctx.require(page.locator(selector).is_visible(), code)


def _text(ctx, page, selector, value, code):
    try:
        page.wait_for_function('''({selector,text})=>{
            const node=document.querySelector(selector);return !!node&&node.textContent.includes(text);
        }''', arg={'selector': selector, 'text': value}, timeout=10000)
    except Exception:
        ctx.require(False, code)


def _enabled(ctx, page, selector, code):
    try:
        page.wait_for_function('''selector=>{
            const node=document.querySelector(selector);return !!node&&!node.disabled;
        }''', arg=selector, timeout=10000)
    except Exception:
        ctx.require(False, code)
    ctx.require(page.locator(selector).is_enabled(), code)


def _me(page):
    # Project only assertion fields, never the /me CSRF value or full response.
    return page.evaluate('''async()=>{
        const response=await fetch('/api/me',{credentials:'same-origin'});
        const value=await response.json();
        return {status:response.status,role:value.role,tenant_id:value.tenant_id,
            mfa_enabled:value.mfa_enabled,mfa_authenticated:value.mfa_authenticated,mfa_fresh:value.mfa_fresh};
    }''')


def _fill_login(ctx, page, role, password):
    account = ctx.fixture.accounts[role]
    page.locator('#storeSlug').fill(account.slug)
    page.locator('#email').fill(account.email)
    page.locator('#password').fill(password)
    page.locator('#loginForm button[type="submit"]').click()


def _open_trusted(ctx, page, code):
    if page.url == ctx.fixture.origin + '/':
        return None
    with page.expect_response(lambda response: response.url == ctx.fixture.origin + '/api/me'
                              and response.request.method == 'GET', timeout=15000) as boot:
        response = page.goto(ctx.fixture.origin + '/', wait_until='load', timeout=15000)
    ctx.require(boot.value.status == 401, code + '_BOOT_UNAUTHENTICATED')
    boot.value.finished()
    return response


def _observer_ready(ctx, page, code):
    if page.locator('#workspace').is_hidden():
        return
    _enabled(ctx, page, '#refreshSquare', code + '_SQUARE_REQUEST_FINISHED')
    status = page.locator('#squareStatus').text_content() or ''
    ctx.require(bool(status.strip()) and status != 'Checking connection…', code + '_SQUARE_STATUS_SETTLED')
    value = _me(page)
    ctx.require(value['status'] == 200, code + '_CURRENT_SESSION')
    if value['role'] != 'cashier':
        _visible(ctx, page, '#cleanupObserver', code + '_OBSERVER_READY')
        _text(ctx, page, '#cleanupObserverGate', 'Global worker gate: off', code + '_STATUS_SETTLED')


def _sign_in(ctx, page, role, *, password=None, recovery_code=None, code='SIGN_IN'):
    _open_trusted(ctx, page, code)
    _visible(ctx, page, '#signin', code + '_FORM')
    _enabled(ctx, page, '#loginForm button[type=submit]', code + '_LOGIN_READY')
    _fill_login(ctx, page, role, ctx.passwords[role] if password is None else password)
    if recovery_code is not None:
        _visible(ctx, page, '#mfaSignin', code + '_REAL_CHALLENGE')
        ctx.require(_me(page)['status'] == 401, code + '_CHALLENGE_NOT_SESSION')
        ctx.require(not _session_cookies(page.context, ctx.fixture.origin), code + '_NO_PREPROOF_COOKIE')
        page.locator('#mfaSigninCode').fill(recovery_code)
        page.locator('#mfaSigninForm button[type="submit"]').click()
    _visible(ctx, page, '#workspace', code + '_WORKSPACE')
    _enabled(ctx, page, '#passwordChangeSubmit', code + '_AUTH_TRANSITION_ENDED')
    _observer_ready(ctx, page, code)
    value = _me(page)
    ctx.require(value['status'] == 200, code + '_REAL_ME')
    ctx.require(value['role'] == role, code + '_ROLE')
    ctx.require(value['tenant_id'] == str(ctx.fixture.accounts[role].tenant_id), code + '_TENANT')
    return value


def _session_cookies(context, origin):
    return [cookie for cookie in context.cookies(origin) if cookie['name'] == 'ss_session']


def _cookie(ctx, page, code):
    rows = _session_cookies(page.context, ctx.fixture.origin)
    ctx.require(len(rows) == 1, code + '_ONE_SESSION_COOKIE')
    cookie = rows[0]
    ctx.require(cookie.get('secure') is True, code + '_SECURE')
    ctx.require(cookie.get('httpOnly') is True, code + '_HTTPONLY')
    ctx.require(cookie.get('sameSite') == 'Lax', code + '_SAMESITE')
    ctx.require(cookie.get('path') == '/', code + '_PATH')
    ctx.require(not page.evaluate("document.cookie.split(';').some(part=>part.trim().startsWith('ss_session='))"),
                code + '_NOT_SCRIPT_READABLE')
    return cookie['value']  # Private comparison only; never a result/artifact.


def _logout(ctx, page, code):
    _observer_ready(ctx, page, code)
    with page.expect_response(lambda response: response.url == ctx.fixture.origin + '/api/auth/logout'
                              and response.request.method == 'POST', timeout=15000) as reply:
        page.locator('#signOut').click()
    ctx.require(reply.value.status == 200, code + '_ACTUAL_LOGOUT')
    reply.value.finished()
    _visible(ctx, page, '#signin', code + '_SIGNIN')
    ctx.require(page.locator('#workspace').is_hidden(), code + '_WORKSPACE_HIDDEN')
    ctx.require(page.locator('#accountSecurity').is_hidden(), code + '_SECURITY_HIDDEN')
    ctx.require(page.locator('#cleanupObserver').is_hidden(), code + '_OBSERVER_HIDDEN')
    ctx.require(_me(page)['status'] == 401, code + '_SESSION_REVOKED')


def _screen(ctx, page, name):
    _observer_ready(ctx, page, 'SCREEN_STATUS')
    ctx.screenshot(page, name + '.png', mask_selectors=EXTRA_MASKS)


def _password_fields_empty(ctx, page, code):
    for suffix in ('Current', 'New', 'Confirm'):
        ctx.require(page.locator('#passwordChange' + suffix).input_value() == '', code + '_' + suffix.upper())


def _submit_password(ctx, page, role, replacement, *, current=None):
    page.locator('#passwordChangeCurrent').fill(ctx.passwords[role] if current is None else current)
    page.locator('#passwordChangeNew').fill(replacement)
    page.locator('#passwordChangeConfirm').fill(replacement)
    page.locator('#passwordChangeSubmit').click()


def _password_response(ctx, response, status, code):
    ctx.require(response.status == status, code + '_STATUS')
    ctx.require('set-cookie' not in response.all_headers(), code + '_NO_SET_COOKIE')
    return response.json()


def _password_reply(ctx, page):
    return page.expect_response(lambda response: response.url == ctx.fixture.origin + '/api/auth/password/change'
                                and response.request.method == 'POST', timeout=15000)


def _committed(ctx, gate, code):
    meta = gate.wait_committed(timeout=10)
    ctx.require(meta.get('status') == 200, code + '_ACTUAL_200')
    ctx.require(meta.get('has_set_cookie') is False, code + '_NO_SET_COOKIE')
    ctx.require(type(meta.get('body_bytes')) is int and meta['body_bytes'] > 0, code + '_REAL_REPLY')


def trusted_https_security_and_csrf(ctx):
    context = ctx.new_context(viewport=DESKTOP.copy())
    page = context.new_page()
    response = _open_trusted(ctx, page, 'HUI01')
    ctx.require(response is not None and response.status == 200, 'HUI01_TRUSTED_HTTPS')
    headers = response.all_headers()
    ctx.require(headers.get('strict-transport-security') == 'max-age=31536000', 'HUI01_HSTS')
    ctx.require(headers.get('x-content-type-options') == 'nosniff', 'HUI01_NOSNIFF')
    ctx.require(headers.get('x-frame-options') == 'DENY', 'HUI01_FRAME_DENY')
    ctx.require(headers.get('referrer-policy') == 'no-referrer', 'HUI01_REFERRER')
    ctx.require(headers.get('cache-control') == 'no-store', 'HUI01_NO_STORE')
    policy = headers.get('content-security-policy', '')
    ctx.require("default-src 'self'" in policy and "frame-ancestors 'none'" in policy, 'HUI01_CSP')
    _sign_in(ctx, page, 'cashier', code='HUI01')
    _cookie(ctx, page, 'HUI01')
    before = ctx.fixture.read_auth_state('cashier')
    rejected = page.evaluate('''async()=>{
        const response=await fetch('/api/auth/password/change',{method:'POST',credentials:'same-origin',
            headers:{'Content-Type':'application/json'},body:'{}'});
        const value=await response.json();
        return {status:response.status,code:value.code,sign_in_required:value.sign_in_required};
    }''')
    ctx.require(rejected['status'] == 403, 'HUI01_CSRF_REJECTED')
    ctx.require(rejected['code'] == 'PASSWORD_INPUT_INVALID' and rejected['sign_in_required'] is False,
                'HUI01_CSRF_TYPED')
    ctx.require(ctx.fixture.read_auth_state('cashier') == before, 'HUI01_NO_SECURITY_MUTATION')
    ctx.require(_me(page)['status'] == 200, 'HUI01_SESSION_STILL_CURRENT')
    _screen(ctx, page, 'hui01-trusted-workspace')
    _logout(ctx, page, 'HUI01_LOGOUT')
    return {'trusted_https': True, 'secure_cookie': True, 'csrf_rejected': True}


def reject_untrusted_ca_and_hostname(ctx):
    context = ctx.new_context(viewport=DESKTOP.copy())
    expected = ((ctx.fixture.untrusted_origin, 'net::ERR_CERT_AUTHORITY_INVALID', 'HUI02_UNTRUSTED_CA'),
                (ctx.fixture.hostname_mismatch_origin, 'net::ERR_CERT_COMMON_NAME_INVALID', 'HUI02_HOSTNAME'))
    for origin, certificate_error, code in expected:
        page = context.new_page()
        rejected = False
        try:
            page.goto(origin + '/', wait_until='domcontentloaded', timeout=10000)
        except Exception as failure:
            rejected = certificate_error in str(failure)
        ctx.require(rejected, code)
    page = context.new_page()
    response = _open_trusted(ctx, page, 'HUI02')
    ctx.require(response is not None and response.status == 200, 'HUI02_TRUSTED_CONTROL')
    return {'certificate_failures': 2, 'trusted_control': True}


def four_roles_default_off_and_logout(ctx):
    for role in ('owner', 'manager', 'buyer', 'cashier'):
        code = 'HUI03_' + role.upper()
        context = ctx.new_context(viewport=DESKTOP.copy())
        page = context.new_page()
        value = _sign_in(ctx, page, role, code=code)
        ctx.require(value['mfa_enabled'] is False, code + '_INITIAL_UNENROLLED')
        ctx.require(page.locator('#passwordChangeSection').is_visible(), code + '_OWN_PASSWORD_AVAILABLE')
        ctx.require(page.locator('#staffPanel').is_visible() == (role == 'owner'), code + '_STAFF_ROLE')
        if role == 'cashier':
            ctx.require(page.locator('#cleanupObserver').is_hidden(), code + '_OBSERVER_ROLE')
        else:
            _visible(ctx, page, '#cleanupObserver', code + '_OBSERVER_VISIBLE')
            _text(ctx, page, '#cleanupObserverGate', 'Global worker gate: off', code + '_GLOBAL_OFF')
            _text(ctx, page, '#cleanupObserverGate', 'Store consent: off', code + '_OWNER_OFF')
            ctx.require(page.locator('#cleanupObserverRequest').is_disabled(), code + '_NO_COLLECTION')
            ctx.require(page.locator('#cleanupObserverConfig').is_visible() == (role == 'owner'), code + '_CONFIG_ROLE')
            if role == 'owner':
                ctx.require(not page.locator('#cleanupObserverEnabled').is_checked(), code + '_NO_CONSENT')
                ctx.require(not page.locator('#cleanupObserverBackground').is_checked(), code + '_NO_BACKGROUND')
                ctx.require(page.locator('#cleanupObserverSave').is_disabled(), code + '_NO_CONNECTED_GENERATION')
        _screen(ctx, page, 'hui03-' + role + '-default-off')
        _logout(ctx, page, code + '_LOGOUT')
    ctx.require(ctx.fixture.provider_attempts == 0, 'HUI03_NO_PROVIDER')
    return {'roles': 4, 'default_off': True, 'logout_clears_privileged_ui': True}


def desktop_mobile_keyboard_layout(ctx):
    for label, viewport in (('desktop1440', DESKTOP), ('mobile390', MOBILE)):
        context = ctx.new_context(viewport=viewport.copy())
        page = context.new_page()
        _sign_in(ctx, page, 'owner', code='HUI04_' + label.upper())
        form = page.locator('#passwordChangeForm')
        ctx.require(form.get_by_label('Current password', exact=True).count() == 1, 'HUI04_CURRENT_LABEL')
        ctx.require(form.get_by_label('New password · 15–128 characters', exact=True).count() == 1, 'HUI04_NEW_LABEL')
        ctx.require(form.get_by_label('Confirm new password', exact=True).count() == 1, 'HUI04_CONFIRM_LABEL')
        ctx.require(page.locator('#passwordChangeCurrent').get_attribute('autocomplete') == 'current-password',
                    'HUI04_CURRENT_AUTOCOMPLETE')
        for suffix in ('New', 'Confirm'):
            ctx.require(page.locator('#passwordChange' + suffix).get_attribute('autocomplete') == 'new-password',
                        'HUI04_NEW_AUTOCOMPLETE')
        page.locator('#passwordChangeCurrent').focus()
        for target in ('passwordChangeNew', 'passwordChangeConfirm', 'passwordChangeSubmit'):
            page.keyboard.press('Tab')
            ctx.require(page.evaluate('document.activeElement.id') == target, 'HUI04_KEYBOARD_ORDER')
        page.keyboard.press('Shift+Tab')
        ctx.require(page.evaluate('document.activeElement.id') == 'passwordChangeConfirm', 'HUI04_REVERSE_FOCUS')
        ctx.require(page.evaluate("getComputedStyle(document.activeElement).outlineStyle!=='none'"),
                    'HUI04_VISIBLE_FOCUS')
        ctx.require(page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'), 'HUI04_NO_PAGE_OVERFLOW')
        _screen(ctx, page, 'hui04-' + label)
        _logout(ctx, page, 'HUI04_LOGOUT')
    return {'viewports': [[1440, 1000], [390, 844]], 'labels_and_keyboard': True, 'no_overflow': True}


def owner_mfa_enrollment_and_real_challenge(ctx):
    context = ctx.new_context(viewport=DESKTOP.copy())
    page = context.new_page()
    _sign_in(ctx, page, 'owner', code='HUI05_INITIAL')
    page.locator('#mfaEnrollPassword').fill(ctx.passwords['owner'])
    page.locator('#mfaEnrollForm button[type="submit"]').click()
    _visible(ctx, page, '#mfaEnrollment', 'HUI05_REAL_ENROLLMENT')
    seed = page.locator('#mfaSetupKey').input_value()
    ctx.require(bool(seed), 'HUI05_SEED_ISSUED')
    proof = totp(secret_bytes(seed), time.time())
    page.locator('#mfaConfirmCode').fill(proof)
    page.locator('#mfaConfirmForm button[type="submit"]').click()
    seed = proof = ''
    _visible(ctx, page, '#mfaRecoveryResult', 'HUI05_ENROLLED')
    _enabled(ctx, page, '#passwordChangeSubmit', 'HUI05_TRANSITION_ENDED')
    codes = page.locator('#mfaRecoveryCodes').inner_text().splitlines()
    ctx.require(len(codes) == 10 and len(set(codes)) == 10, 'HUI05_REAL_RECOVERY_CODES')
    state = ctx.fixture.read_auth_state('owner')
    ctx.require(state['mfa_enabled'] is True and state['mfa_version'] > 0, 'HUI05_MFA_PERSISTED')
    ctx.require(state['recovery_codes_remaining'] == 10, 'HUI05_RECOVERY_PERSISTED')
    _screen(ctx, page, 'hui05-enrolled')
    page.locator('#dismissMfaRecovery').click()
    ctx.require(page.locator('#mfaRecoveryCodes').text_content() == '', 'HUI05_RECOVERY_DOM_CLEARED')
    _logout(ctx, page, 'HUI05_LOGOUT')
    value = _sign_in(ctx, page, 'owner', recovery_code=codes.pop(0), code='HUI05_SECOND_FACTOR')
    codes.clear()
    ctx.require(value['mfa_authenticated'] is True and value['mfa_fresh'] is True, 'HUI05_REAL_PROOF')
    ctx.require(ctx.fixture.read_auth_state('owner')['recovery_codes_remaining'] == 9, 'HUI05_ONE_CODE_CONSUMED')
    _screen(ctx, page, 'hui05-real-challenge-complete')
    _logout(ctx, page, 'HUI05_FINAL_LOGOUT')
    return {'real_enrollment': True, 'real_challenge': True, 'unused_recovery_code_consumed': True}


def wrong_current_password_clears_secrets(ctx):
    context = ctx.new_context(viewport=DESKTOP.copy())
    page = context.new_page()
    _sign_in(ctx, page, 'buyer', code='HUI06')
    before = ctx.fixture.read_auth_state('buyer')
    wrong = ctx.fixture.next_password('buyer')
    replacement = ctx.fixture.next_password('buyer')
    ctx.require(wrong != ctx.passwords['buyer'], 'HUI06_DISTINCT_WRONG_PASSWORD')
    cookie = _cookie(ctx, page, 'HUI06_BEFORE')
    with _password_reply(ctx, page) as reply:
        _submit_password(ctx, page, 'buyer', replacement, current=wrong)
    value = _password_response(ctx, reply.value, 403, 'HUI06_REPLY')
    wrong = replacement = ''
    ctx.require(value.get('code') == 'CURRENT_PASSWORD_INVALID' and value.get('sign_in_required') is False,
                'HUI06_CONTROLLED_FAILURE')
    _text(ctx, page, '#passwordChangeStatus', 'current password was not accepted', 'HUI06_VISIBLE_ERROR')
    _enabled(ctx, page, '#passwordChangeSubmit', 'HUI06_RETRY_AVAILABLE')
    _password_fields_empty(ctx, page, 'HUI06_CLEARED')
    after = ctx.fixture.read_auth_state('buyer')
    ctx.require(after['sessions'] == before['sessions'] and after['password_changed'] is False, 'HUI06_NOT_REVOKED')
    ctx.require(after['password_change_audits'] == before['password_change_audits'], 'HUI06_NO_CHANGE_AUDIT')
    ctx.require(after['password_rejection_audits'] > before['password_rejection_audits'], 'HUI06_REJECTION_COMMITTED')
    ctx.require(_cookie(ctx, page, 'HUI06_AFTER') == cookie, 'HUI06_COOKIE_UNCHANGED')
    ctx.require(_me(page)['role'] == 'buyer', 'HUI06_STILL_SIGNED_IN')
    _screen(ctx, page, 'hui06-wrong-password-cleared')
    _logout(ctx, page, 'HUI06_LOGOUT')
    return {'wrong_password_rejected': True, 'secrets_cleared': True, 'session_preserved': True}


def password_change_revokes_owned_sessions(ctx):
    context = ctx.new_context(viewport=DESKTOP.copy())
    page = context.new_page()
    _sign_in(ctx, page, 'buyer', code='HUI07_PRIMARY')
    other_context = ctx.new_context(viewport=DESKTOP.copy())
    other = other_context.new_page()
    _sign_in(ctx, other, 'buyer', code='HUI07_SECOND_SESSION')
    unrelated_context = ctx.new_context(viewport=DESKTOP.copy())
    unrelated = unrelated_context.new_page()
    _sign_in(ctx, unrelated, 'manager', code='HUI07_OTHER_ACCOUNT')
    before = ctx.fixture.read_auth_state('buyer')
    manager = ctx.fixture.read_auth_state('manager')
    ctx.require(before['sessions'] >= 2, 'HUI07_MULTIPLE_OWN_SESSIONS')
    old = ctx.passwords['buyer'];replacement = ctx.fixture.next_password('buyer')
    cookie = _cookie(ctx, page, 'HUI07_BEFORE')
    with _password_reply(ctx, page) as reply:
        _submit_password(ctx, page, 'buyer', replacement)
    value = _password_response(ctx, reply.value, 200, 'HUI07_REPLY')
    keys = {'password_changed', 'sign_in_required', 'all_sessions_revoked'}
    ctx.require(set(value) == keys and all(value[key] is True for key in keys), 'HUI07_EXACT_ACK')
    _visible(ctx, page, '#signin', 'HUI07_FORCED_SIGNIN')
    _text(ctx, page, '#loginStatus', 'Password changed.', 'HUI07_CONFIRMED_MESSAGE')
    _password_fields_empty(ctx, page, 'HUI07_CLEARED')
    ctx.require(page.locator('#workspace').is_hidden() and page.locator('#accountSecurity').is_hidden(),
                'HUI07_PRIVILEGED_UI_CLEARED')
    after = ctx.fixture.read_auth_state('buyer')
    ctx.require(after['sessions'] == 0 and after['password_changed'] is True, 'HUI07_ALL_OWN_SESSIONS_REVOKED')
    ctx.require(after['password_change_audits'] == before['password_change_audits'] + 1, 'HUI07_ONE_CHANGE_AUDIT')
    ctx.require(ctx.fixture.read_auth_state('manager')['sessions'] == manager['sessions'], 'HUI07_OTHER_ACCOUNT_PRESERVED')
    ctx.require(_me(other)['status'] == 401 and _me(unrelated)['role'] == 'manager', 'HUI07_REAL_SESSION_BOUNDARY')
    ctx.require(_cookie(ctx, page, 'HUI07_AFTER') == cookie, 'HUI07_NO_COOKIE_REWRITE')
    ctx.passwords['buyer'] = replacement
    _screen(ctx, page, 'hui07-password-changed-signin')
    _fill_login(ctx, page, 'buyer', old)
    _text(ctx, page, '#loginStatus', 'Store, email or password is incorrect.', 'HUI07_OLD_PASSWORD_REJECTED')
    _sign_in(ctx, page, 'buyer', code='HUI07_NEW_PASSWORD')
    old = replacement = ''
    _logout(ctx, page, 'HUI07_LOGOUT')
    return {'all_owned_sessions_revoked': True, 'other_account_preserved': True, 'new_password_signin': True}


def lost_committed_reply_manual_recovery(ctx):
    context = ctx.new_context(viewport=DESKTOP.copy())
    page = context.new_page()
    _sign_in(ctx, page, 'cashier', code='HUI08')
    replacement = ctx.fixture.next_password('cashier')
    submissions = []
    def count(request):
        if request.method == 'POST' and request.url == ctx.fixture.origin + '/api/auth/password/change':
            submissions.append(True)
    page.on('request', count)
    try:
        with ctx.fixture.password_reply_gate() as gate:
            _submit_password(ctx, page, 'cashier', replacement)
            _committed(ctx, gate, 'HUI08_COMMITTED')
            ctx.require(ctx.fixture.read_auth_state('cashier')['sessions'] == 0, 'HUI08_SERVER_REVOKED')
            gate.drop_delivery()
            _visible(ctx, page, '#signin', 'HUI08_LOCAL_SIGNIN')
            _text(ctx, page, '#loginStatus', 'could not be confirmed', 'HUI08_UNCERTAINTY_SHOWN')
        _password_fields_empty(ctx, page, 'HUI08_CLEARED')
        ctx.require(len(submissions) == 1, 'HUI08_ONE_SUBMISSION_NO_RETRY')
        _screen(ctx, page, 'hui08-uncertain-signin')
        _sign_in(ctx, page, 'cashier', password=replacement, code='HUI08_MANUAL_NEW_PASSWORD')
        ctx.passwords['cashier'] = replacement
        replacement = ''
        ctx.require(ctx.fixture.read_auth_state('cashier')['password_changed'] is True, 'HUI08_REAL_RECOVERY')
        ctx.require(len(submissions) == 1, 'HUI08_STILL_ONE_SUBMISSION')
        _screen(ctx, page, 'hui08-manual-recovery')
        _logout(ctx, page, 'HUI08_LOGOUT')
    finally:
        page.remove_listener('request', count)
        replacement = ''
    return {'submissions': 1, 'manual_signin_recovery': True, 'no_auto_retry': True}


def delayed_reply_preserves_newer_login_cookie(ctx):
    context = ctx.new_context(viewport=DESKTOP.copy())
    first = context.new_page()
    _sign_in(ctx, first, 'manager', code='HUI09_OLDER_LOGIN')
    old_cookie = _cookie(ctx, first, 'HUI09_OLDER')
    replacement = ctx.fixture.next_password('manager')
    requests = {'password': 0, 'logout': 0}
    def count(request):
        if request.method == 'POST':
            if request.url == ctx.fixture.origin + '/api/auth/password/change':requests['password'] += 1
            if request.url == ctx.fixture.origin + '/api/auth/logout':requests['logout'] += 1
    context.on('request', count)
    try:
        with ctx.fixture.password_reply_gate() as gate:
            _submit_password(ctx, first, 'manager', replacement)
            _committed(ctx, gate, 'HUI09_COMMITTED')
            newer = context.new_page()
            _sign_in(ctx, newer, 'buyer', code='HUI09_NEWER_REAL_LOGIN')
            new_cookie = _cookie(ctx, newer, 'HUI09_NEWER')
            ctx.require(new_cookie != old_cookie, 'HUI09_NEW_COOKIE_ESTABLISHED')
            ctx.require(_me(newer)['role'] == 'buyer', 'HUI09_NEW_IDENTITY_BEFORE_RELEASE')
            ctx.require(first.locator('#workspace').is_visible() and first.locator('#signin').is_hidden(),
                        'HUI09_OLDER_REPLY_NOT_DELIVERED_YET')
            ctx.require(first.locator('#passwordChangeSubmit').is_disabled(), 'HUI09_OLDER_ACTION_STILL_PENDING')
            gate.release()
            _visible(ctx, first, '#signin', 'HUI09_OLDER_LOCAL_SIGNIN')
            _text(ctx, first, '#loginStatus', 'Password changed.', 'HUI09_REAL_ACK')
            ctx.passwords['manager'] = replacement
            replacement = ''
            value = _me(newer)
            ctx.require(value['status'] == 200 and value['role'] == 'buyer', 'HUI09_NEW_IDENTITY_SURVIVES')
            ctx.require(value['tenant_id'] == str(ctx.fixture.accounts['buyer'].tenant_id), 'HUI09_NEW_TENANT_SURVIVES')
            ctx.require(_cookie(ctx, newer, 'HUI09_AFTER') == new_cookie, 'HUI09_NEW_COOKIE_SURVIVES')
            ctx.require(newer.locator('#workspace').is_visible(), 'HUI09_NEW_WORKSPACE_SURVIVES')
            ctx.require(newer.locator('#accountSecurity').is_visible(), 'HUI09_NEW_SECURITY_VIEW_SURVIVES')
            ctx.require(requests == {'password': 1, 'logout': 0}, 'HUI09_NO_AMBIENT_LOGOUT_OR_RETRY')
            _screen(ctx, newer, 'hui09-newer-login-preserved')
    finally:
        context.remove_listener('request', count)
        replacement = old_cookie = ''
    return {'submissions': 1, 'newer_identity_preserved': True, 'newer_cookie_preserved': True, 'ambient_logouts': 0}


CASE_LABELS = {
    'HUI01': 'Trusted HTTPS, session-cookie protections and real CSRF rejection',
    'HUI02': 'Untrusted CA and hostname mismatch are rejected',
    'HUI03': 'Four actual roles, default-off controls and logout',
    'HUI04': 'Desktop/mobile accessible labels and keyboard layout',
    'HUI05': 'Actual owner MFA enrollment and subsequent challenge',
    'HUI06': 'Wrong current password preserves session and clears secrets',
    'HUI07': 'Password change revokes every owned session',
    'HUI08': 'Lost committed reply recovers through manual sign-in',
    'HUI09': 'Delayed reply preserves a newer real login cookie and identity',
}

CASES = (
    ('HUI01', trusted_https_security_and_csrf),
    ('HUI02', reject_untrusted_ca_and_hostname),
    ('HUI03', four_roles_default_off_and_logout),
    ('HUI04', desktop_mobile_keyboard_layout),
    ('HUI05', owner_mfa_enrollment_and_real_challenge),
    ('HUI06', wrong_current_password_clears_secrets),
    ('HUI07', password_change_revokes_owned_sessions),
    ('HUI08', lost_committed_reply_manual_recovery),
    ('HUI09', delayed_reply_preserves_newer_login_cookie),
)
