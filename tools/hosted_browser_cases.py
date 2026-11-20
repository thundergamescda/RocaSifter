"""Ten preserved portal flows plus five owned fictional-Square HTTPS flows.

Root owns the browser, private trust store, service fixture and evidence capture.
No auth/me fulfilment, cookie injection, traces, secret logging or desktop imports.
Only sanitized booleans/counts/labels leave these cases. Playwright and server
exceptions are converted by the runner to fixed safe failure codes, never reprs.
"""
import time
from urllib.parse import urlsplit

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
            page.locator('#cleanupObserverDetails > summary').click()
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
        # Usability acceptance: native keyboard section navigation.
        ctx.require(page.evaluate('''()=>{
            const nav=document.querySelector('.workspace-links'),links=nav?Array.from(nav.querySelectorAll('a')):[];
            return !!nav&&links.length===2&&nav.scrollWidth<=nav.clientWidth&&links.every(link=>{
                const rect=link.getBoundingClientRect();
                return rect.width>0&&rect.height>0&&rect.left>=0&&rect.right<=window.innerWidth;
            });
        }'''), 'HUI04_SECTION_LINKS_FIT_VIEWPORT')
        square_link = page.locator('.workspace-links a[href="#squareConnection"]')
        for target, code in (('squareConnection', 'HUI04_SQUARE'), ('accountSecurity', 'HUI04_SECURITY')):
            square_link.focus()
            page.keyboard.press('Tab')
            if target == 'squareConnection':
                page.keyboard.press('Shift+Tab')
            ctx.require(page.evaluate('''target=>{
                const link=document.querySelector('.workspace-links a[href="#'+target+'"]');
                if(!link||document.activeElement!==link)return false;
                const style=getComputedStyle(link);
                return style.outlineStyle!=='none'&&parseFloat(style.outlineWidth)>0;
            }''', target), code + '_NATIVE_LINK_KEYBOARD_FOCUS')
            page.keyboard.press('Enter')
            try:
                page.wait_for_function('''target=>{
                    const node=document.getElementById(target);
                    if(!node||location.hash!=='#'+target||document.activeElement!==node)return false;
                    const rect=node.getBoundingClientRect(),style=getComputedStyle(node);
                    return rect.width>0&&rect.height>0&&rect.left>=0&&rect.right<=window.innerWidth&&
                        rect.top>=0&&rect.top<window.innerHeight&&rect.bottom>0&&
                        style.outlineStyle!=='none'&&parseFloat(style.outlineWidth)>0;
                }''', arg=target, timeout=10000)
            except Exception:
                ctx.require(False, code + '_NATIVE_VISIBLE_FOCUS_DESTINATION')
            _visible(ctx, page, '#' + target, code + '_DESTINATION_VISIBLE')
        # End usability keyboard navigation.
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
        no_overflow = page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
        if not no_overflow:
            ctx.record_layout_overflow(page)
        _screen(ctx, page, 'hui04-' + label)
        ctx.require(no_overflow, 'HUI04_NO_PAGE_OVERFLOW')
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
    # Usability acceptance: committed MFA feedback is visible and readable.
    _visible(ctx, page, '#mfaSecurityStatus', 'HUI05_COMMITTED_SUCCESS_VISIBLE')
    _text(ctx, page, '#mfaSecurityStatus', 'Authenticator enabled.', 'HUI05_COMMITTED_SUCCESS_TEXT')
    ctx.require(page.locator('#mfaSecurityStatus').get_attribute('data-tone') == 'success',
                'HUI05_COMMITTED_SUCCESS_TONE')
    ctx.require(page.evaluate(r'''()=>{
        const node=document.getElementById('mfaSecurityStatus'),panel=document.getElementById('accountSecurity');
        if(!node||!panel)return false;
        const style=getComputedStyle(node);
        const rgb=value=>{
            if(!/^rgba?\(/.test(value))return null;
            const parts=value.match(/\d+(?:\.\d+)?/g).map(Number);
            return (parts.length===3||(parts.length===4&&parts[3]===1))&&
                parts.slice(0,3).every(part=>part>=0&&part<=255)?parts.slice(0,3):null;
        };
        const foreground=rgb(style.color),background=rgb(getComputedStyle(panel).backgroundColor);
        if(!foreground||!background)return false;
        const luminance=parts=>{
            const linear=parts.map(part=>{const value=part/255;return value<=0.04045?value/12.92:((value+0.055)/1.055)**2.4});
            return 0.2126*linear[0]+0.7152*linear[1]+0.0722*linear[2];
        };
        const first=luminance(foreground),second=luminance(background);
        return node.textContent.trim().length>0&&parseFloat(style.fontSize)>=12&&
            (Math.max(first,second)+0.05)/(Math.min(first,second)+0.05)>=4.5;
    }'''), 'HUI05_COMMITTED_SUCCESS_READABLE_CONTRAST')
    # End usability committed feedback.
    _screen(ctx, page, 'hui05-enrolled')
    page.locator('#dismissMfaRecovery').click()
    ctx.require(page.locator('#mfaRecoveryCodes').text_content() == '', 'HUI05_RECOVERY_DOM_CLEARED')
    _logout(ctx, page, 'HUI05_LOGOUT')
    # Usability acceptance: real same-page account switch clears MFA feedback.
    ctx.require(page.locator('#mfaSecurityStatus').text_content() == '' and
                page.locator('#mfaSecurityStatus').get_attribute('data-tone') is None,
                'HUI05_LOGOUT_CLEARS_SUCCESS_TEXT_AND_TONE')
    buyer = _sign_in(ctx, page, 'buyer', code='HUI05_SAME_PAGE_BUYER')
    ctx.require(buyer['role'] == 'buyer' and buyer['mfa_enabled'] is False and
                buyer['mfa_authenticated'] is False, 'HUI05_BUYER_ACTUAL_UNENROLLED_ACCOUNT')
    ctx.require(page.locator('#mfaSecurityStatus').text_content() == '' and
                page.locator('#mfaSecurityStatus').get_attribute('data-tone') is None,
                'HUI05_BUYER_NO_PREVIOUS_ACCOUNT_FEEDBACK')
    for field in ('mfaSigninCode', 'mfaSetupKey', 'mfaConfirmCode', 'mfaEnrollPassword',
                  'mfaManagePassword', 'mfaManageCode'):
        ctx.require(page.locator('#' + field).input_value() == '', 'HUI05_BUYER_' + field.upper() + '_EMPTY')
    ctx.require(page.locator('#mfaRecoveryCodes').text_content() == '' and
                page.locator('#mfaRecoveryResult').is_hidden() and page.locator('#mfaEnrollment').is_hidden(),
                'HUI05_BUYER_NO_PREVIOUS_ACCOUNT_RECOVERY_MATERIAL')
    ctx.require(page.locator('#mfaEnrollForm').is_visible() and page.locator('#mfaManageForm').is_hidden() and
                page.locator('#staffPanel').is_hidden(), 'HUI05_BUYER_OWN_UNENROLLED_CONTROLS')
    _logout(ctx, page, 'HUI05_BUYER_LOGOUT')
    # End usability same-page account switch.
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


def owner_mediated_staff_password_recovery(ctx):
    # The shared fixture owner was enrolled by HUI05. Every session and proof
    # below goes through the real HTTPS UI/Auth/MFA boundary; none is seeded.
    cashier_context = ctx.new_context(viewport=DESKTOP.copy())
    cashier = cashier_context.new_page()
    _sign_in(ctx, cashier, 'cashier', code='HUI10_CASHIER_INITIAL')
    cashier.locator('#mfaEnrollPassword').fill(ctx.passwords['cashier'])
    cashier.locator('#mfaEnrollForm button[type="submit"]').click()
    _visible(ctx, cashier, '#mfaEnrollment', 'HUI10_CASHIER_REAL_ENROLLMENT')
    seed = cashier.locator('#mfaSetupKey').input_value()
    ctx.require(bool(seed), 'HUI10_CASHIER_SEED_ISSUED')
    proof = totp(secret_bytes(seed), time.time())
    cashier.locator('#mfaConfirmCode').fill(proof)
    cashier.locator('#mfaConfirmForm button[type="submit"]').click()
    seed = proof = ''
    _visible(ctx, cashier, '#mfaRecoveryResult', 'HUI10_CASHIER_ENROLLED')
    _enabled(ctx, cashier, '#passwordChangeSubmit', 'HUI10_CASHIER_TRANSITION_ENDED')
    codes = cashier.locator('#mfaRecoveryCodes').inner_text().splitlines()
    ctx.require(len(codes) == 10 and len(set(codes)) == 10, 'HUI10_CASHIER_REAL_CODES')
    cashier.locator('#dismissMfaRecovery').click()
    ctx.require(cashier.locator('#mfaRecoveryCodes').text_content() == '', 'HUI10_CASHIER_CODES_CLEARED')
    cashier_before = ctx.fixture.read_auth_state('cashier')
    ctx.require(cashier_before['staff_enabled'] is True and cashier_before['mfa_enabled'] is True and
                cashier_before['sessions'] >= 1 and cashier_before['recovery_codes_remaining'] == 10,
                'HUI10_ENROLLED_ENABLED_TARGET')

    owner_context = ctx.new_context(viewport=DESKTOP.copy())
    owner = owner_context.new_page()
    owner_proof = ctx.fixture.current_mfa_code('owner')
    owner_me = _sign_in(ctx, owner, 'owner', recovery_code=owner_proof, code='HUI10_OWNER_REAL_MFA')
    owner_proof = ''
    ctx.require(owner_me['mfa_enabled'] is True and owner_me['mfa_authenticated'] is True and
                owner_me['mfa_fresh'] is True, 'HUI10_OWNER_FRESH_ENROLLED_MFA')
    owner_cookie = _cookie(ctx, owner, 'HUI10_OWNER_BEFORE')
    owner_before = ctx.fixture.read_auth_state('owner')
    target = ctx.fixture.accounts['cashier']
    row = owner.locator('#staffList .staff-row').filter(has_text=target.email)
    ctx.require(row.count() == 1, 'HUI10_OWNER_SAME_STORE_TARGET_ROW')
    issue_url = ctx.fixture.origin + '/api/staff/' + str(target.staff_id) + '/password-recovery'
    owner.once('dialog', lambda dialog: dialog.accept())
    with owner.expect_response(lambda response: response.url == issue_url and response.request.method == 'POST',
                               timeout=15000) as issued:
        row.get_by_role('button', name='Reset password', exact=True).click()
    ctx.require(issued.value.status == 201 and 'set-cookie' not in issued.value.all_headers(),
                'HUI10_REAL_OWNER_ISSUANCE_NO_COOKIE')
    issued.value.finished()
    _visible(ctx, owner, '#staffRecoveryResult', 'HUI10_PRIVATE_LINK_READY')
    ctx.require(owner.locator('#staffRecoveryLink').get_attribute('readonly') is not None,
                'HUI10_PRIVATE_LINK_READONLY')
    private_link = owner.locator('#staffRecoveryLink').input_value()
    parsed = urlsplit(private_link)
    ctx.require(parsed.scheme + '://' + parsed.netloc == ctx.fixture.origin and
                parsed.path == '/staff-password-recovery' and not parsed.query and
                parsed.fragment.startswith('recovery=') and len(parsed.fragment) == 52,
                'HUI10_PRIVATE_FRAGMENT_LINK')
    owner.locator('#dismissStaffRecovery').click()
    ctx.require(owner.locator('#staffRecoveryLink').input_value() == '' and
                owner.locator('#staffRecoveryResult').is_hidden(), 'HUI10_OWNER_LINK_CLEARED')

    recovery_context = ctx.new_context(viewport=DESKTOP.copy())
    recovery = recovery_context.new_page()
    old = ctx.passwords['cashier']; replacement = ctx.fixture.next_password('cashier')
    try:
        ctx.require(not _session_cookies(recovery_context, ctx.fixture.origin), 'HUI10_GENUINELY_SIGNED_OUT_CONTEXT')
        response = recovery.goto(private_link, wait_until='load', timeout=15000)
        private_link = ''; parsed = None
        ctx.require(response is not None and response.status == 200 and
                    'set-cookie' not in response.all_headers(), 'HUI10_REAL_RECOVERY_PAGE')
        _visible(ctx, recovery, '#staffRecoveryForm', 'HUI10_SIGNED_OUT_RECOVERY_READY')
        ctx.require(recovery.url == ctx.fixture.origin + '/staff-password-recovery', 'HUI10_FRAGMENT_REMOVED')
        ctx.require(bool(recovery.locator('#recoveryCode').input_value()), 'HUI10_FRAGMENT_IN_PRIVATE_INPUT_ONLY')
        recovery.locator('#recoveryEmail').fill(target.email)
        recovery.locator('#recoveryPassword').fill(replacement)
        recovery.locator('#recoveryConfirm').fill(replacement)
        ctx.screenshot(recovery, 'hui10-private-recovery-form.png', mask_selectors=EXTRA_MASKS)
        with recovery.expect_response(lambda response: response.url == ctx.fixture.origin + '/api/staff/password-recovery/redeem'
                                      and response.request.method == 'POST', timeout=15000) as redeemed:
            recovery.locator('#recoverySubmit').click()
        result = _password_response(ctx, redeemed.value, 200, 'HUI10_REDEEM')
        keys = {'password_changed', 'sign_in_required', 'all_sessions_revoked', 'mfa_preserved'}
        ctx.require(type(result) is dict and set(result) == keys and all(result[key] is True for key in keys),
                    'HUI10_EXACT_PASSWORD_ONLY_ACK')
        _text(ctx, recovery, '#recoveryStatus', 'Password reset. Sign in normally', 'HUI10_REAL_RESET_CONFIRMED')
        for field in ('recoveryCode', 'recoveryEmail', 'recoveryPassword', 'recoveryConfirm'):
            ctx.require(recovery.locator('#' + field).input_value() == '', 'HUI10_' + field.upper() + '_CLEARED')
        ctx.require(recovery.locator('#staffRecoveryForm').is_hidden() and
                    not _session_cookies(recovery_context, ctx.fixture.origin) and _me(recovery)['status'] == 401,
                    'HUI10_REDEEM_ISSUED_NO_SESSION')
        after = ctx.fixture.read_auth_state('cashier')
        ctx.require(after['sessions'] == 0 and _me(cashier)['status'] == 401, 'HUI10_TARGET_SESSIONS_REVOKED')
        ctx.require(after['mfa_enabled'] is True and after['mfa_version'] == cashier_before['mfa_version'] and
                    after['recovery_codes_remaining'] == cashier_before['recovery_codes_remaining'],
                    'HUI10_EXISTING_FACTOR_AND_CODES_PRESERVED')
        ctx.require(ctx.fixture.read_auth_state('owner') == owner_before and _me(owner)['role'] == 'owner' and
                    _cookie(ctx, owner, 'HUI10_OWNER_AFTER_RESET') == owner_cookie,
                    'HUI10_LIVE_OWNER_UNCHANGED')
        ctx.screenshot(recovery, 'hui10-password-reset-confirmed.png', mask_selectors=EXTRA_MASKS)

        _open_trusted(ctx, recovery, 'HUI10_RETURN_SIGNIN')
        with recovery.expect_response(lambda response: response.url == ctx.fixture.origin + '/api/auth/login'
                                      and response.request.method == 'POST', timeout=15000) as rejected:
            _fill_login(ctx, recovery, 'cashier', old)
        ctx.require(rejected.value.status == 401, 'HUI10_OLD_PASSWORD_HTTP_REJECTED')
        rejected.value.finished()
        _text(ctx, recovery, '#loginStatus', 'Store, email or password is incorrect.', 'HUI10_OLD_PASSWORD_REJECTED')
        ctx.require(not _session_cookies(recovery_context, ctx.fixture.origin), 'HUI10_REJECTED_LOGIN_NO_SESSION')
        value = _sign_in(ctx, recovery, 'cashier', password=replacement, recovery_code=codes.pop(0),
                         code='HUI10_NEW_PASSWORD_EXISTING_MFA')
        ctx.require(value['mfa_authenticated'] is True and value['mfa_fresh'] is True and
                    ctx.fixture.read_auth_state('cashier')['recovery_codes_remaining'] == 9,
                    'HUI10_NEW_PASSWORD_REQUIRES_PRESERVED_MFA')
        ctx.passwords['cashier'] = replacement
        ctx.require(_me(owner)['status'] == 200 and _cookie(ctx, owner, 'HUI10_OWNER_FINAL') == owner_cookie,
                    'HUI10_OWNER_SESSION_HELD_THROUGH_STAFF_SIGNIN')
        ctx.require(ctx.fixture.provider_attempts == 0, 'HUI10_NO_PROVIDER_REQUESTS')
        _logout(ctx, recovery, 'HUI10_STAFF_LOGOUT')
        _logout(ctx, owner, 'HUI10_OWNER_LOGOUT')
    finally:
        private_link = old = replacement = owner_cookie = owner_proof = ''
        parsed = None
        codes.clear()
    return {'owner_actual_mfa': True, 'password_only_recovery': True, 'signed_out_redemption': True,
            'no_redemption_session_or_cookie': True, 'target_sessions_revoked': True,
            'existing_mfa_preserved': True, 'new_password_real_mfa_signin': True, 'owner_session_preserved': True}


from contextlib import contextmanager


@contextmanager
def _sku_case(parent):
    """A case-local context permits only the owned production child origin."""
    fixture=parent.fixture.square_sku_fixture()
    ctx=type(parent)(fixture,parent.browser);ctx.case_id=parent.case_id
    parent.require(parent.fixture._application.config['SQUARE_ENVIRONMENT']=='sandbox' and
        fixture._application.config['SQUARE_ENVIRONMENT']=='production','SKU_EXPLICIT_SEPARATE_APP_ENVIRONMENTS')
    try:yield ctx
    finally:
        ctx.close_contexts()
        parent.cleanup_complete=parent.cleanup_complete and ctx.cleanup_complete
        parent.ledger_overflow=parent.ledger_overflow or ctx.ledger_overflow
        for name in ('network','js_errors','console_events'):
            for row in getattr(ctx,name):parent._append(getattr(parent,name),row)
        parent.require(not ctx.screenshots and ctx.screenshot_bytes==0,'SKU_NO_ADDITIONAL_CAPTURES')
        summary=fixture.transport.summary()
        parent.require(summary['write_attempts']==0 and fixture.provider_attempts==0,'SKU_ZERO_PROVIDER_WRITE_OR_DENIED_ATTEMPTS')
        parent.require(summary['responses_created']==summary['responses_closed'],'SKU_ALL_FICTIONAL_READ_RESPONSES_CLOSED')


def _sku_owner(ctx):
    context=ctx.new_context(viewport=DESKTOP.copy());page=context.new_page()
    state=ctx.fixture.read_auth_state('owner')
    proof=ctx.fixture.current_mfa_code('owner') if state['mfa_enabled'] else None
    try:_sign_in(ctx,page,'owner',recovery_code=proof,code=ctx.case_id+'_OWNER')
    finally:proof=''
    if not state['mfa_enabled']:
        seed=code=''
        try:
            page.locator('#mfaEnrollPassword').fill(ctx.passwords['owner'])
            page.locator('#mfaEnrollForm button[type=submit]').click()
            _visible(ctx,page,'#mfaEnrollment',ctx.case_id+'_REAL_ENROLLMENT')
            seed=page.locator('#mfaSetupKey').input_value();code=totp(secret_bytes(seed),time.time())
            page.locator('#mfaConfirmCode').fill(code);page.locator('#mfaConfirmForm button[type=submit]').click()
            _visible(ctx,page,'#mfaRecoveryResult',ctx.case_id+'_REAL_ENROLLED')
            _enabled(ctx,page,'#passwordChangeSubmit',ctx.case_id+'_ENROLL_TRANSITION_COMPLETE')
            page.locator('#dismissMfaRecovery').click()
        finally:seed=code=''
        _logout(ctx,page,ctx.case_id+'_ENROLL_LOGOUT')
        proof=ctx.fixture.current_mfa_code('owner')
        try:_sign_in(ctx,page,'owner',recovery_code=proof,code=ctx.case_id+'_REAL_MFA')
        finally:proof=''
    value=_me(page)
    ctx.require(value['role']=='owner' and value['mfa_enabled'] and value['mfa_authenticated'] and value['mfa_fresh'],
                ctx.case_id+'_ACTUAL_FRESH_OWNER_MFA')
    return context,page


def _sku_open(ctx,page,location=None):
    response=page.goto(ctx.fixture.origin+'/receiving/catalog',wait_until='load',timeout=15000)
    ctx.require(response is not None and response.status==200,ctx.case_id+'_ACTUAL_TRUSTED_SKU_PAGE')
    ctx.require(response.all_headers().get('strict-transport-security')=='max-age=31536000',ctx.case_id+'_SKU_HSTS')
    _visible(ctx,page,'#receivingAdminWorkspace',ctx.case_id+'_OWNER_WORKSPACE')
    page.locator('#receivingAdminForm [name=location_id]').select_option(location or ctx.fixture.location_a)
    _enabled(ctx,page,'#receivingAdminLookupButton',ctx.case_id+'_LOOKUP_READY')


def _sku_lookup(ctx,page,expected='match'):
    page.locator('#receivingAdminLookupSku').fill(ctx.fixture.sku)
    with page.expect_response(lambda response:response.url.startswith(ctx.fixture.origin+'/api/receiving/catalog-lookup?')
        and response.request.method=='GET',timeout=15000) as reply:
        page.locator('#receivingAdminLookupSku').press('Enter')
    response=reply.value;ctx.require(response.status==200,ctx.case_id+'_LOOKUP_REAL_200');response.finished()
    value=response.json()
    ctx.require(value.get('state')==expected and value.get('requested_sku')==ctx.fixture.sku,ctx.case_id+'_TYPED_EXACT_LOOKUP')
    _enabled(ctx,page,'#receivingAdminLookupButton',ctx.case_id+'_LOOKUP_SETTLED')
    return value


def _sku_settle(page):
    page.evaluate('''async()=>{await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame)}''')


def _sku_committed(ctx,page,gate,code):
    # Keep sync Playwright routes live until the real completed reply reaches the gate.
    deadline=time.monotonic()+10
    while not gate.committed and time.monotonic()<deadline:
        page.wait_for_timeout(50)
    ctx.require(gate.committed,code+'_GATE_COMMITTED')
    _committed(ctx,gate,code)


def _sku_no_mutation(ctx,before):
    ctx.require(ctx.fixture.protected_snapshot()==before,ctx.case_id+'_READ_ONLY_PRODUCT_RECORDS')
    ctx.require(all(value==0 for value in ctx.fixture.counts().values()),ctx.case_id+'_NO_APPROVAL_STOCK_OR_CREDIT')
    ctx.require(not any(row['event']=='request' and row['method']!='GET' and row['path'].startswith('/api/receiving/')
        for row in ctx.network),ctx.case_id+'_NO_RECEIVING_MUTATION_REQUEST')


def exact_sku_enter_selection_and_explicit_preview(ctx):
    with _sku_case(ctx) as sku:
        _,page=_sku_owner(sku);sku.fixture.transport.set_mode('match');_sku_open(sku,page)
        form=page.locator('#receivingAdminForm')
        form.locator('summary').filter(has_text='Set, printing and card identity').click()
        for name,value in (('name','Fictional owned card'),('set','Fixture set'),('number','001'),
            ('condition','Near Mint'),('printing','Normal'),('language','English')):form.locator('[name='+name+']').fill(value)
        form.locator('[name=game]').select_option('pokemon')
        form.locator('[name=variation_id]').fill(sku.fixture.variation_id)
        before=sku.fixture.protected_snapshot();value=_sku_lookup(sku,page)
        sku.require(value['lookup_complete'] is True and value['exact_match_count']==1, 'HUI11_COMPLETE_SINGLE_RAW_MATCH')
        results=page.locator('#receivingAdminLookupResults')
        sku.require(results.locator('img,script').count()==0 and
            sku.fixture.literal_name in results.text_content() and sku.fixture.literal_variation in results.text_content(),
            'HUI11_PROVIDER_STRINGS_ARE_LITERAL')
        sku.require(results.locator('input[readonly]').input_value()==sku.fixture.sku,'HUI11_EXACT_READONLY_SKU_SPACES_CASE_ZEROES')
        sku.require(form.locator('[name=variation_id]').input_value()=='','HUI11_ENTER_DID_NOT_SELECT_OR_PREVIEW')
        results.get_by_role('button',name='Use this variation reference',exact=True).click()
        _text(sku,page,'#receivingAdminProgress','Variation reference selected.','HUI11_EXPLICIT_REFERENCE_SELECTED')
        sku.require(form.locator('[name=variation_id]').input_value()==sku.fixture.variation_id and
            form.locator('[name=name]').input_value()=='Fictional owned card' and page.locator('#receivingAdminReview').is_hidden(),
            'HUI11_SELECTION_ONLY_PRESERVES_PHYSICAL_FIELDS')
        _sku_no_mutation(sku,before)
        with page.expect_response(lambda response:response.url==sku.fixture.origin+'/api/receiving/catalog-bindings/previews'
            and response.request.method=='POST',timeout=15000) as preview:
            page.locator('#receivingAdminPreview').click()
        sku.require(preview.value.status==201,'HUI11_SEPARATE_REAL_PHYSICAL_PREVIEW');preview.value.finished()
        _visible(sku,page,'#receivingAdminReview','HUI11_EXPLICIT_PREVIEW_VISIBLE')
        _text(sku,page,'#receivingAdminMappingStatus','No conflicting card identity mappings','HUI11_ACTUAL_MAPPING_REVIEW')
        sku.require(all(value==0 for value in sku.fixture.counts().values()),'HUI11_PREVIEW_IS_NOT_APPROVAL_OR_STOCK')


def exact_sku_typed_nonselectable_outcomes(ctx):
    with _sku_case(ctx) as sku:
        _,page=_sku_owner(sku);_sku_open(sku,page);before=sku.fixture.protected_snapshot()
        for mode,state,text in (('empty','no_match','No exact Square SKU'),('case_variant','no_match','different capitalization'),
            ('ambiguous','ambiguous','multiple exact or case-insensitive'),('incomplete','incomplete','read was incomplete'),
            ('unavailable','unavailable','cannot be used for receiving')):
            sku.fixture.transport.set_mode(mode);value=_sku_lookup(sku,page,state)
            _text(sku,page,'#receivingAdminLookupStatus',text,'HUI12_EXPLICIT_'+mode.upper())
            sku.require(not value['candidates'] and page.locator('#receivingAdminLookupResults button').count()==0 and
                page.locator('#receivingAdminForm [name=variation_id]').input_value()=='','HUI12_NONMATCH_HAS_NO_SELECTION')
        _sku_no_mutation(sku,before);sku.fixture.transport.set_mode('match')


def exact_sku_native_edits_and_dom_event_races(ctx):
    with _sku_case(ctx) as sku:
        _,page=_sku_owner(sku);sku.fixture.transport.set_mode('match');_sku_open(sku,page)
        before=sku.fixture.protected_snapshot();_sku_lookup(sku,page)
        page.locator('#receivingAdminLookupResults button').click()
        _text(sku,page,'#receivingAdminProgress','Variation reference selected.','HUI13_FIRST_NATIVE_SELECTION')
        page.locator('#receivingAdminLookupSku').fill('CHANGED-SKU')
        sku.require(page.locator('#receivingAdminForm [name=variation_id]').input_value()=='','HUI13_NATIVE_SKU_EDIT_CLEARS_REFERENCE')
        _sku_lookup(sku,page);page.locator('#receivingAdminLookupResults button').click()
        _text(sku,page,'#receivingAdminProgress','Variation reference selected.','HUI13_SECOND_NATIVE_SELECTION')
        page.locator('#receivingAdminForm [name=location_id]').select_option(sku.fixture.location_b)
        sku.require(page.locator('#receivingAdminForm [name=variation_id]').input_value()=='','HUI13_NATIVE_LOCATION_EDIT_CLEARS_REFERENCE')
        for field,value in (('receivingAdminLookupSku','DOM-RACE-SKU'),('location',sku.fixture.location_a)):
            with sku.fixture.lookup_reply_gate() as gate:
                page.locator('#receivingAdminLookupSku').fill(sku.fixture.sku)
                with page.expect_response(lambda response:response.url.startswith(sku.fixture.origin+'/api/receiving/catalog-lookup?'),timeout=15000) as old:
                    page.locator('#receivingAdminLookupSku').press('Enter')
                    _sku_committed(sku,page,gate,'HUI13_ACTUAL_LOOKUP_COMPLETED')
                    sku.require(page.locator('#receivingAdminForm').get_attribute('inert') is not None,'HUI13_NATIVE_BUSY_FORM_INERT')
                    # Explicit DOM-event probe, not a native edit of an inert form.
                    page.evaluate('''({field,value})=>{const node=field==='location'?document.querySelector('#receivingAdminForm [name=location_id]'):document.getElementById(field);node.value=value;node.dispatchEvent(new Event('input',{bubbles:true}))}''',{'field':field,'value':value})
                    with page.expect_response(lambda response:response.url==sku.fixture.origin+'/api/me' and response.status==200,timeout=15000) as current:
                        gate.release()
                    current.value.finished()
                sku.require(old.value.status==200,'HUI13_OLD_REAL_REPLY_UNMODIFIED');old.value.finished()
                _sku_settle(page)
                sku.require(page.locator('#receivingAdminLookupResults button').count()==0 and
                    page.locator('#receivingAdminForm [name=variation_id]').input_value()=='','HUI13_STALE_DOM_EVENT_RESULT_NOT_PUBLISHED')
        _sku_no_mutation(sku,before)


def exact_sku_completed_reply_after_real_logout(ctx):
    with _sku_case(ctx) as sku:
        context,page=_sku_owner(sku);sku.fixture.transport.set_mode('match');_sku_open(sku,page)
        before=sku.fixture.protected_snapshot();other=context.new_page()
        other.goto(sku.fixture.origin+'/',wait_until='load',timeout=15000)
        _visible(sku,other,'#workspace','HUI14_OTHER_ACTUAL_OWNER_WORKSPACE');_observer_ready(sku,other,'HUI14_OTHER_REAL_OWNER_TAB')
        terminals={'finished':0,'failed':0}
        def record_terminal(kind,request):
            if request.method=='GET' and request.url.startswith(sku.fixture.origin+'/api/receiving/catalog-lookup?'):
                terminals[kind]=min(2,terminals[kind]+1)
        def finished(request):record_terminal('finished',request)
        def failed(request):record_terminal('failed',request)
        # Observe before Enter: native logout may abort before the held reply is released.
        page.on('requestfinished',finished);page.on('requestfailed',failed)
        try:
            with sku.fixture.lookup_reply_gate() as gate:
                page.locator('#receivingAdminLookupSku').fill(sku.fixture.sku);page.locator('#receivingAdminLookupSku').press('Enter')
                _sku_committed(sku,page,gate,'HUI14_REAL_SUCCESS_BEFORE_LOGOUT')
                _logout(sku,other,'HUI14_ACTUAL_LOGOUT')
                page.bring_to_front();_text(sku,page,'#receivingAdminSession','Session changed.','HUI14_OLD_OWNER_CONTEXT_CLEARED')
                gate.release()
            deadline=time.monotonic()+15
            while not any(terminals.values()) and time.monotonic()<deadline:
                # Pump real Playwright events while waiting for either bounded terminal event.
                page.wait_for_timeout(50)
            sku.require(sum(terminals.values())==1,'HUI14_ACTUAL_LOOKUP_TERMINATED')
            _sku_settle(page)
            sku.require(page.locator('#receivingAdminLookupResults').text_content()=='' and
                page.locator('#receivingAdminLookupResults button').count()==0 and
                page.locator('#receivingAdminForm [name=variation_id]').input_value()=='','HUI14_EXISTING_PAGE_EMPTY_AFTER_TERMINAL')
            _sign_in(sku,other,'buyer',code='HUI14_DIFFERENT_REAL_PRINCIPAL')
            _sku_settle(page)
            sku.require(page.locator('#receivingAdminLookupResults').text_content()=='' and
                page.locator('#receivingAdminLookupResults button').count()==0 and
                page.locator('#receivingAdminForm [name=variation_id]').input_value()=='','HUI14_EXISTING_PAGE_EMPTY_AFTER_BUYER_LOGIN')
            page.reload(wait_until='load',timeout=15000)
            _text(sku,page,'#receivingAdminSession','Only the store owner','HUI14_DIFFERENT_PRINCIPAL_CANNOT_USE_OWNER_RESULT')
            sku.require(page.locator('#receivingAdminLookupResults button').count()==0 and
                page.locator('#receivingAdminForm [name=variation_id]').input_value()=='','HUI14_OLD_REPLY_NOT_ADOPTED')
            _sku_no_mutation(sku,before);_logout(sku,other,'HUI14_BUYER_LOGOUT')
        finally:
            page.remove_listener('requestfinished',finished);page.remove_listener('requestfailed',failed)


def exact_sku_actual_pagehide_and_network_back_navigation(ctx):
    with _sku_case(ctx) as sku:
        _,page=_sku_owner(sku);sku.fixture.transport.set_mode('match');_sku_open(sku,page)
        before=sku.fixture.protected_snapshot()
        page.evaluate('''()=>{sessionStorage.removeItem('sku-fixture-pagehide');addEventListener('pagehide',event=>sessionStorage.setItem('sku-fixture-pagehide',JSON.stringify({seen:true,persisted:event.persisted})),{once:true})}''')
        with sku.fixture.lookup_reply_gate() as gate:
            page.locator('#receivingAdminLookupSku').fill(sku.fixture.sku);page.locator('#receivingAdminLookupSku').press('Enter')
            _sku_committed(sku,page,gate,'HUI15_REAL_SUCCESS_BEFORE_NAVIGATION')
            page.goto(sku.fixture.origin+'/',wait_until='load',timeout=15000);gate.release()
            _visible(sku,page,'#workspace','HUI15_NATIVE_NAVIGATION_OWNER_WORKSPACE');_observer_ready(sku,page,'HUI15_NATIVE_NAVIGATION_READY')
            marker=page.evaluate("JSON.parse(sessionStorage.getItem('sku-fixture-pagehide')||'{}')")
            sku.require(marker.get('seen') is True and marker.get('persisted') is False,'HUI15_ACTUAL_PAGEHIDE_NOT_BFCACHE')
        response=page.go_back(wait_until='load',timeout=15000)
        sku.require(response is not None and response.status==200,'HUI15_NETWORK_BACK_NAVIGATION')
        _visible(sku,page,'#receivingAdminWorkspace','HUI15_FRESH_BACK_OWNER_WORKSPACE')
        sku.require(page.locator('#receivingAdminLookupResults button').count()==0 and
            page.locator('#receivingAdminForm [name=variation_id]').input_value()=='','HUI15_STALE_REPLY_CANNOT_FILL_FRESH_DOCUMENT')
        _sku_no_mutation(sku,before)
        # Actual BFCache restoration remains deferred; no synthetic pageshow.


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
    'HUI10': 'Owner-mediated staff password recovery preserves MFA and owner session',
    'HUI11': 'Exact SKU Enter/selection/literal strings and separate physical preview',
    'HUI12': 'Real typed SKU no-match/ambiguity/incomplete/unavailable outcomes',
    'HUI13': 'Native SKU/location clearing and separately labeled DOM-event races',
    'HUI14': 'Completed real SKU reply cannot survive actual logout/new principal',
    'HUI15': 'Actual pagehide/network back navigation; BFCache restore deferred',
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
    ('HUI10', owner_mediated_staff_password_recovery),
    ('HUI11', exact_sku_enter_selection_and_explicit_preview),
    ('HUI12', exact_sku_typed_nonselectable_outcomes),
    ('HUI13', exact_sku_native_edits_and_dom_event_races),
    ('HUI14', exact_sku_completed_reply_after_real_logout),
    ('HUI15', exact_sku_actual_pagehide_and_network_back_navigation),
)
