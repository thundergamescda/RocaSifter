"""Fifteen preserved portal/SKU flows plus six actual HTTPS staff stock flows.

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
            const expected=['/stock','#squareConnection','#accountSecurity'];
            return !!nav&&links.length===expected.length&&links.every((link,index)=>link.getAttribute('href')===expected[index])&&
                nav.scrollWidth<=nav.clientWidth&&links.every(link=>{
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


# Six additional actual HTTPS staff-page flows. Source definitions remain UNRUN.
STOCK_CAPTURE_NAMES = {
    'HUI16': ('hui16-stock-desktop.png', 'hui16-stock-mobile.png'),
    'HUI17': ('hui17-stock-unknown.png',),
    'HUI18': ('hui18-stock-unit.png',),
    'HUI19': (), 'HUI20': (), 'HUI21': (),
}


@contextmanager
def _stock_case(parent):
    fixture=parent.fixture.square_sku_fixture()
    ctx=type(parent)(fixture,parent.browser);ctx.case_id=parent.case_id
    parent.require(ctx.case_id in STOCK_CAPTURE_NAMES and
        parent.fixture._application.config['SQUARE_ENVIRONMENT']=='sandbox' and
        fixture._application.config['SQUARE_ENVIRONMENT']=='production','STOCK_SEPARATE_REAL_APPLICATIONS')
    try:yield ctx
    finally:
        ctx.close_contexts()
        parent.cleanup_complete=parent.cleanup_complete and ctx.cleanup_complete
        parent.ledger_overflow=parent.ledger_overflow or ctx.ledger_overflow
        for name in ('network','js_errors','console_events','restore_diagnostics'):
            for row in getattr(ctx,name):parent._append(getattr(parent,name),row)
        if ctx.stock_pagehide_observation is not None:
            parent.require(ctx.case_id==parent.case_id=='HUI21' and parent.stock_pagehide_observation is None,
                'HUI21_ONLY_OWNED_OBSERVATION_PROPAGATION')
            parent.stock_pagehide_observation=ctx.stock_pagehide_snapshot()
        names=tuple(row['name'] for row in ctx.screenshots)
        parent.require(len(set(names))==len(names) and all(name in STOCK_CAPTURE_NAMES[ctx.case_id] for name in names),
            'STOCK_ONLY_FOUR_ADMITTED_CAPTURE_NAMES')
        parent.require(len(parent.screenshots)+len(ctx.screenshots)<=20 and
            parent.screenshot_bytes+ctx.screenshot_bytes<=6*1024*1024,'STOCK_AGGREGATE_CAPTURE_BOUNDS')
        parent.screenshots.extend(ctx.screenshots);parent.screenshot_bytes+=ctx.screenshot_bytes
        summary=fixture.transport.summary()
        parent.require(summary['write_attempts']==0 and summary['denied_attempts']==0 and fixture.provider_attempts==0,
            'STOCK_ZERO_PROVIDER_WRITE_OR_DENIED_ATTEMPTS')
        parent.require(summary['responses_created']==summary['responses_closed'],'STOCK_ALL_READ_RESPONSES_CLOSED')


def _stock_actor(ctx,role,viewport=None):
    if role=='owner':return _sku_owner(ctx)
    context=ctx.new_context(viewport=(viewport or DESKTOP).copy());page=context.new_page()
    _sign_in(ctx,page,role,code=ctx.case_id+'_'+role.upper())
    return context,page


def _stock_open(ctx,page):
    link=page.get_by_role('link',name='Stock lookup',exact=True)
    ctx.require(link.count()==1 and link.get_attribute('href')=='/stock',ctx.case_id+'_REAL_WORKSPACE_STOCK_LINK')
    link.focus()
    ctx.require(page.evaluate("document.activeElement.getAttribute('href')==='/stock'"),ctx.case_id+'_NATIVE_LINK_FOCUS')
    with page.expect_navigation(wait_until='load',timeout=15000) as navigation:
        page.keyboard.press('Enter')
    response=navigation.value
    ctx.require(response is not None and response.status==200 and page.url==ctx.fixture.origin+'/stock',
        ctx.case_id+'_NATIVE_STOCK_NAVIGATION')
    ctx.require(response.all_headers().get('strict-transport-security')=='max-age=31536000' and
        response.all_headers().get('cache-control')=='no-store',ctx.case_id+'_STOCK_HTTPS_NO_STORE')
    _visible(ctx,page,'#stockForm',ctx.case_id+'_REAL_SESSION_AND_LOCATIONS')
    _enabled(ctx,page,'#stockRefreshSession',ctx.case_id+'_STOCK_SESSION_READY')
    ctx.require(page.locator('#stockLocation option').count()==3,ctx.case_id+'_TWO_OWNED_ACTIVE_LOCATIONS')


def _stock_workspace(ctx,page):
    page.get_by_role('link',name='Back to workspace',exact=True).click()
    _visible(ctx,page,'#workspace',ctx.case_id+'_BACK_TO_REAL_WORKSPACE')
    _observer_ready(ctx,page,ctx.case_id+'_BACK_WORKSPACE_READY')


def _stock_lookup(ctx,page,*,catalog='match',count='reported',location=None):
    page.locator('#stockLocation').select_option(location or ctx.fixture.location_a)
    page.locator('#stockSku').fill(ctx.fixture.sku)
    ctx.require(page.locator('#stockSku').input_value()==ctx.fixture.sku and
        page.locator('#stockCheck').text_content()=='Check stock',ctx.case_id+'_EXACT_MANUAL_INPUT')
    with page.expect_response(lambda response:response.request.method=='GET' and
        response.url.startswith(ctx.fixture.origin+'/api/stock/lookup?'),timeout=15000) as reply:
        page.locator('#stockSku').press('Enter')
    response=reply.value
    ctx.require(response.status==200 and 'set-cookie' not in response.all_headers(),ctx.case_id+'_REAL_STOCK_200_NO_COOKIE')
    response.finished();value=response.json()
    ctx.require(value.get('stock_lookup_version')==1 and value.get('requested_sku')==ctx.fixture.sku and
        value.get('location_id')==(location or ctx.fixture.location_a) and value.get('atomic_snapshot') is False and
        value.get('catalog_state')==catalog and value.get('count_state')==count,ctx.case_id+'_ACTUAL_TYPED_STOCK_RESULT')
    _enabled(ctx,page,'#stockCheck',ctx.case_id+'_STOCK_LOOKUP_SETTLED')
    ctx.require(page.locator('#stockCheck').text_content()=='Check again',ctx.case_id+'_EXPLICIT_MANUAL_RETRY')
    return value


def _stock_empty(ctx,page,code):
    ctx.require(page.locator('#stockResult').is_hidden(),code+'_HIDDEN')
    for suffix in ('Sku','Item','Variation','Location','Quantity','Id','Parent'):
        ctx.require(page.locator('#stockResult'+suffix).input_value()=='',code+'_FIELDS_EMPTY')
    for suffix in ('Summary','Unit','Qualifications','Checked','Updated'):
        node=page.locator('#stockResult'+suffix)
        ctx.require(node.text_content()=='' and node.get_attribute('title') is None and
            node.get_attribute('data-timestamp') is None,code+'_TEXT_AND_TIMES_EMPTY')


def _stock_report(ctx,page,value,quantity,availability):
    _visible(ctx,page,'#stockResult',ctx.case_id+'_REPORT_VISIBLE')
    ctx.require(value['availability']==availability and value['inventory']['quantity']==quantity and
        value['inventory']['state']=='IN_STOCK' and page.locator('#stockResultQuantity').input_value()==quantity,
        ctx.case_id+'_RAW_QUANTITY_NOT_ROUNDED')
    for suffix,expected in (('Sku',ctx.fixture.sku),('Item',ctx.fixture.literal_name),('Variation',ctx.fixture.literal_variation),
        ('Id',ctx.fixture.variation_id),('Parent',ctx.fixture.parent_id)):
        ctx.require(page.locator('#stockResult'+suffix).input_value()==expected,ctx.case_id+'_LITERAL_EXACT_REFERENCE')
    ctx.require(page.locator('#stockResult img,#stockResult script').count()==0,ctx.case_id+'_NO_PROVIDER_HTML_EXECUTION')


def _stock_times(ctx,page,value):
    for suffix,prefix,stamp in (('Checked','Checked at: ',value['read_completed_at']),
        ('Updated','Last affecting update: ',value['inventory']['calculated_at'])):
        actual=page.locator('#stockResult'+suffix)
        expected=page.evaluate('''({prefix,stamp})=>prefix+new Intl.DateTimeFormat(undefined,
            {dateStyle:'medium',timeStyle:'short'}).format(new Date(stamp))''',{'prefix':prefix,'stamp':stamp})
        ctx.require(actual.text_content()==expected and actual.get_attribute('title')==stamp and
            actual.get_attribute('data-timestamp')==stamp,ctx.case_id+'_LOCALE_TIME_AND_EXACT_SOURCE')
    ctx.require(page.evaluate("new Intl.DateTimeFormat().resolvedOptions().locale==='en-US'&&new Intl.DateTimeFormat().resolvedOptions().timeZone==='America/Boise'"),
        ctx.case_id+'_UNCHANGED_LOCALE_TIMEZONE')
    ctx.require(value['inventory']['calculated_at']!=value['read_completed_at'],ctx.case_id+'_CHECK_TIME_DISTINCT_FROM_STOCK_UPDATE')


def _stock_capture(ctx,page,name):
    ctx.require(name in STOCK_CAPTURE_NAMES[ctx.case_id],ctx.case_id+'_ADMITTED_STOCK_CAPTURE')
    ctx.screenshot(page,name,mask_selectors=('#stockSession',))


def _stock_no_mutation(ctx,before):
    _sku_no_mutation(ctx,before)
    ctx.require(not any(row['event']=='request' and row['method']!='GET' and row['path'].startswith('/api/stock/')
        for row in ctx.network),ctx.case_id+'_ONLY_STOCK_GET_REQUESTS')


@contextmanager
def _stock_terminal(ctx,page):
    # The first actual post-arm Request is retained across native navigation.
    # Context events can outlive the old document; URL equality alone is not identity.
    terminals={'request':None,'starts':0,'finished':0,'failed':0}
    context=page.context
    def started(request):
        if request.method=='GET' and request.url.startswith(ctx.fixture.origin+'/api/stock/lookup?'):
            terminals['starts']=min(2,terminals['starts']+1)
            if terminals['request'] is None:terminals['request']=request
    def record(kind,request):
        if request is terminals['request']:terminals[kind]=min(2,terminals[kind]+1)
    def finished(request):record('finished',request)
    def failed(request):record('failed',request)
    context.on('request',started);context.on('requestfinished',finished);context.on('requestfailed',failed)
    try:yield terminals
    finally:
        context.remove_listener('request',started)
        context.remove_listener('requestfinished',finished);context.remove_listener('requestfailed',failed)


def _stock_wait_terminal(ctx,page,terminals):
    deadline=time.monotonic()+15
    while terminals['finished']+terminals['failed']==0 and time.monotonic()<deadline:
        page.wait_for_timeout(50)  # Existing bounded synchronous Playwright event pump.
    ctx.require(terminals['request'] is not None and terminals['starts']>0,ctx.case_id+'_NO_ACTUAL_LOOKUP_REQUEST')
    ctx.require(terminals['starts']==1,ctx.case_id+'_MULTIPLE_ACTUAL_LOOKUP_REQUESTS')
    total=terminals['finished']+terminals['failed']
    ctx.require(total<=1,ctx.case_id+'_MULTIPLE_ACTUAL_LOOKUP_TERMINALS')
    if total==0:
        # Finite sanitized failure-state codes only. Do not wait on request.response(),
        # read private headers/body/query, treat response as terminal, or accept a timeout.
        try:
            failure=terminals['request'].failure
            response=terminals['request'].existing_response
            status=None if response is None else response.status
        except Exception:
            ctx.require(False,ctx.case_id+'_LOOKUP_TERMINAL_STATE_UNAVAILABLE')
        if failure=='net::ERR_ABORTED':code='_LOOKUP_ABORT_WITHOUT_TERMINAL'
        elif failure is not None:code='_LOOKUP_FAILURE_WITHOUT_TERMINAL'
        elif status is None:code='_LOOKUP_NO_RESPONSE_OR_TERMINAL'
        elif status==200:code='_LOOKUP_200_WITHOUT_TERMINAL'
        else:code='_LOOKUP_NON200_WITHOUT_TERMINAL'
        ctx.require(False,ctx.case_id+code)
    ctx.require(total==1,ctx.case_id+'_ONE_ACTUAL_LOOKUP_TERMINAL')
    _sku_settle(page)


def stock_four_roles_native_enter_literal_and_layout(ctx):
    with _stock_case(ctx) as stock:
        stock.fixture.transport.set_mode('stock_positive');before=stock.fixture.protected_snapshot()
        for role,viewport in [('owner',DESKTOP),('manager',DESKTOP),('buyer',DESKTOP),('cashier',DESKTOP),('cashier',MOBILE)]:
            context,page=_stock_actor(stock,role,viewport);_stock_open(stock,page)
            location=page.get_by_role('combobox',name='Square location',exact=True)
            sku=page.get_by_role('textbox',name='Exact Square SKU',exact=True)
            stock.require(location.count()==1 and location.get_attribute('id')=='stockLocation' and
                sku.count()==1 and sku.get_attribute('id')=='stockSku','HUI16_ACCESSIBLE_INPUT_LABELS')
            stock.require(page.evaluate('''()=>[
                ['stockLocation','Square location'],['stockSku','Exact Square SKU']
            ].every(([id,text])=>{
                const control=document.getElementById(id),labels=control?Array.from(control.labels||[]):[];
                if(labels.length!==1||labels[0].control!==control||labels[0].htmlFor!==id)return false;
                const directText=Array.from(labels[0].childNodes).filter(node=>node.nodeType===Node.TEXT_NODE)
                    .map(node=>node.textContent).join('').trim();
                return directText===text;
            })'''),'HUI16_NATIVE_LABEL_CONTROL_ASSOCIATION')
            location.focus();page.keyboard.press('Tab')
            stock.require(page.evaluate("document.activeElement.id==='stockSku'"),'HUI16_NATIVE_LOCATION_TO_SKU_FOCUS')
            value=_stock_lookup(stock,page);_stock_report(stock,page,value,'+0007.00000','positive');_stock_times(stock,page,value)
            _text(stock,page,'#stockResultSummary','Square reports positive IN_STOCK quantity.','HUI16_POSITIVE_REPORT')
            stock.require(page.locator('#stockResultLocation').input_value()=='Fictional counter A','HUI16_SELECTED_LOCATION_LABEL')
            stock.require(page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'HUI16_NO_VIEWPORT_OVERFLOW')
            if role=='owner':_stock_capture(stock,page,'hui16-stock-desktop.png')
            if viewport==MOBILE:_stock_capture(stock,page,'hui16-stock-mobile.png')
            _stock_workspace(stock,page);_logout(stock,page,'HUI16_'+role.upper()+'_LOGOUT');context.close()
        _stock_no_mutation(stock,before)
    return {'four_real_roles':True,'native_enter':True,'exact_literal_sku':True,'desktop_mobile_layout':True}


def _stock_quantity_readable_and_keyboard_focus(ctx,page):
    page.locator('#stockCheck').focus()
    reached=False
    for _ in range(16):
        page.keyboard.press('Tab')
        if page.evaluate("document.activeElement===document.getElementById('stockResultQuantity')"):
            reached=True;break
    ctx.require(reached,'HUI17_QUANTITY_REACHED_BY_NATIVE_TAB')
    try:
        result=page.evaluate('''()=>{
            const input=document.getElementById('stockResultQuantity'),style=getComputedStyle(input),rect=input.getBoundingClientRect();
            const px=value=>{const number=parseFloat(value);if(!Number.isFinite(number))throw Error('MEASUREMENT');return number};
            const fonts=['font-family','font-size','font-style','font-weight','font-stretch','font-variant','font-kerning',
                'font-feature-settings','font-variation-settings','font-optical-sizing','font-size-adjust','font-synthesis',
                'font-variant-caps','font-variant-numeric','font-variant-ligatures','font-variant-east-asian',
                'font-variant-position','font-language-override','letter-spacing','word-spacing','text-transform',
                'text-rendering','direction','unicode-bidi','line-height'];
            let probe=null,fits=false;
            try{
                probe=document.createElement('span');
                probe.style.cssText='all:initial;position:fixed;left:0;top:0;display:inline-block;visibility:hidden;pointer-events:none;white-space:pre;width:max-content;max-width:none;min-width:0;margin:0;border:0;padding:0';
                for(const name of fonts)probe.style.setProperty(name,style.getPropertyValue(name));
                probe.textContent=input.value;document.body.append(probe);
                const contentWidth=Math.min(input.clientWidth,rect.width-px(style.borderLeftWidth)-px(style.borderRightWidth))-
                    px(style.paddingLeft)-px(style.paddingRight);
                const measured=probe.getBoundingClientRect().width;
                fits=input.value.length===26&&rect.width>0&&rect.height>0&&contentWidth>1&&
                    measured>0&&measured<=contentWidth-1&&input.scrollLeft===0;
            }finally{
                if(probe){probe.remove();if(probe.isConnected)throw Error('MEASUREMENT_CLEANUP')}
            }
            // Browser color parsing, alpha compositing and unrounded sRGB contrast.
            const canvas=document.createElement('canvas');canvas.width=canvas.height=1;
            const paint=canvas.getContext('2d',{willReadFrequently:true});
            if(!paint)throw Error('COLOR_MEASUREMENT');
            const rgba=color=>{paint.clearRect(0,0,1,1);paint.fillStyle=color;paint.fillRect(0,0,1,1);
                const data=paint.getImageData(0,0,1,1).data;return [data[0],data[1],data[2],data[3]/255]};
            const over=(front,back)=>{const alpha=front[3]+back[3]*(1-front[3]);
                if(!alpha)return [0,0,0,0];return [0,1,2].map(index=>
                    (front[index]*front[3]+back[index]*back[3]*(1-front[3]))/alpha).concat(alpha)};
            const layers=[];let node=input.parentElement,opaque=false,depth=0;
            while(node&&depth++<16){
                const parent=getComputedStyle(node);if(parent.backgroundImage!=='none')throw Error('COLOR_BACKGROUND');
                const color=rgba(parent.backgroundColor);layers.push(color);
                if(color[3]===1){opaque=true;break}node=node.parentElement;
            }
            if(!opaque)throw Error('COLOR_BACKGROUND');
            let background=layers.pop();while(layers.length)background=over(layers.pop(),background);
            let opacity=1;node=input;depth=0;
            while(node&&depth++<16){opacity*=px(getComputedStyle(node).opacity);node=node.parentElement}
            if(node)throw Error('COLOR_DEPTH');
            const ring=rgba(style.outlineColor);ring[3]*=opacity;
            const foreground=over(ring,background);
            const luminance=color=>color.slice(0,3).map(value=>{const channel=value/255;
                return channel<=0.04045?channel/12.92:Math.pow((channel+0.055)/1.055,2.4)})
                .reduce((sum,value,index)=>sum+value*[0.2126,0.7152,0.0722][index],0);
            const light=luminance(foreground),dark=luminance(background);
            const contrast=(Math.max(light,dark)+0.05)/(Math.min(light,dark)+0.05);
            const extent=px(style.outlineWidth)+Math.max(0,px(style.outlineOffset));
            return {fits,readonly:input.readOnly,focused:document.activeElement===input,
                indicator:!['none','hidden'].includes(style.outlineStyle)&&px(style.outlineWidth)>=2&&
                    px(style.outlineOffset)>=0&&contrast>=3&&rect.left-extent>=0&&rect.right+extent<=innerWidth&&
                    rect.top-extent>=0&&rect.bottom+extent<=innerHeight};
        }''')
    except Exception:
        ctx.require(False,'HUI17_QUANTITY_RENDER_MEASUREMENT')
    ctx.require(type(result) is dict and set(result)=={'fits','readonly','focused','indicator'} and
        all(type(value) is bool for value in result.values()),'HUI17_QUANTITY_MEASUREMENT_SHAPE')
    ctx.require(result['readonly'] and result['fits'],'HUI17_ALL_26_QUANTITY_CHARACTERS_FIT')
    ctx.require(result['focused'] and result['indicator'],'HUI17_QUANTITY_FOCUS_VISIBLE_AND_CONTRASTING')


def stock_raw_quantities_and_unknown_outcomes(ctx):
    with _stock_case(ctx) as stock:
        _,page=_stock_actor(stock,'cashier');stock.fixture.transport.set_mode('stock_positive');_stock_open(stock,page)
        before=stock.fixture.protected_snapshot()
        for mode,quantity,availability,summary in (
            ('stock_zero','+000.00000','zero_reported','Square reports zero IN_STOCK quantity.'),
            ('stock_negative','-0002.00000','negative_reported','Square reports a negative IN_STOCK quantity.'),
            ('stock_fractional','+0002.12500','fractional_review','Square reports fractional IN_STOCK quantity.')):
            stock.fixture.transport.set_mode(mode);value=_stock_lookup(stock,page)
            _stock_report(stock,page,value,quantity,availability);_text(stock,page,'#stockResultSummary',summary,'HUI17_EXPLICIT_SIGNED_OUTCOME')
        for mode,state,reason in (('stock_missing','missing','COUNT_NOT_RETURNED'),
            ('stock_estimated','unknown','COMPOSED_COUNT'),('stock_no_time','unknown','COUNT_TIMESTAMP_UNKNOWN')):
            stock.fixture.transport.set_mode(mode);value=_stock_lookup(stock,page,count=state)
            stock.require(value['availability']=='unknown' and value['reason_code']==reason and value['inventory'] is None,
                'HUI17_NO_NUMERIC_FALLBACK')
            _visible(stock,page,'#stockResult','HUI17_UNKNOWN_REFERENCE_VISIBLE')
            stock.require(page.locator('#stockResultQuantity').input_value()=='Unknown' and
                page.locator('#stockResultUpdated').text_content()=='','HUI17_UNKNOWN_IS_NEVER_ZERO')
            _text(stock,page,'#stockResultSummary','Square quantity is unknown.','HUI17_TYPED_UNKNOWN_VISIBLE')
            if mode=='stock_estimated':_stock_capture(stock,page,'hui17-stock-unknown.png')
        stock.fixture.transport.set_mode('stock_malformed');value=_stock_lookup(stock,page,catalog='incomplete',count='incomplete')
        stock.require(value['reason_code']=='COUNTS_READ_INCOMPLETE' and value['reference'] is None and value['inventory'] is None,
            'HUI17_MALFORMED_COUNT_TYPED_INCOMPLETE')
        _stock_empty(stock,page,'HUI17_MALFORMED_CLEARS_NUMBER')
        _text(stock,page,'#stockStatus','The complete Square count could not be checked.','HUI17_MALFORMED_EXPLICIT_RETRY')
        for viewport in (DESKTOP,MOBILE):
            if viewport==DESKTOP:quantity_page=page
            else:
                _,quantity_page=_stock_actor(stock,'cashier',viewport);_stock_open(stock,quantity_page)
            for mode,expected,availability in (
                ('stock_max_quantity','-0000000000000000001.12500','fractional_review'),
                ('stock_max_integer','00000000000000000000000007','positive')):
                stock.require(len(expected)==26,'HUI17_MAXIMUM_QUANTITY_LITERAL_LENGTH')
                stock.fixture.transport.set_mode(mode);value=_stock_lookup(stock,quantity_page)
                _stock_report(stock,quantity_page,value,expected,availability)
                stock.require(value['inventory']['quantity']==expected and
                    quantity_page.locator('#stockResultQuantity').input_value()==expected,'HUI17_EXACT_26_CHARACTER_RAW_QUANTITY')
                _stock_quantity_readable_and_keyboard_focus(stock,quantity_page)
        _stock_no_mutation(stock,before)
    return {'signed_decimal_text_preserved':True,'explicit_zero':True,'unknown_never_zero':True,'malformed_clears_result':True}


def stock_price_options_units_and_qualifications(ctx):
    with _stock_case(ctx) as stock:
        _,page=_stock_actor(stock,'buyer');stock.fixture.transport.set_mode('stock_variable');_stock_open(stock,page)
        before=stock.fixture.protected_snapshot()
        for mode,kind,label,precision,quantity,availability,codes,words in (
            ('stock_variable','whole','Whole quantities in this Square variation',0,'+0007.00000','positive',
                ['STOCKABLE','SELLABLE'],'Square marks this variation stockable. Square marks this variation sellable.'),
            ('stock_options','whole','Whole quantities in this Square variation',0,'+0007.00000','positive',
                ['STOCKABLE','SELLABLE'],'Square marks this variation stockable. Square marks this variation sellable.'),
            ('stock_measured','measured','Fixture length (fl)',5,'0.12500','fractional_review',
                ['STOCKABLE','SELLABLE'],'Square marks this variation stockable. Square marks this variation sellable.'),
            ('stock_conversion','conversion','Whole quantities in this Square variation; Square conversion variation, no conversion applied here',0,
                '3.00000','positive',['SELLABLE','CONVERSION'],
                'Square marks this variation sellable. Quantity is for the requested variation; no unit conversion was applied here.'),
            ('stock_qualified','whole','Whole quantities in this Square variation',0,'+0007.00000','positive',
                ['ARCHIVED','STOCKABLE','NOT_SELLABLE','MARKED_SOLD_OUT'],
                'This Square item is archived. Square marks this variation stockable. This variation is not marked sellable in Square. This variation is marked sold out in Square.')):
            stock.fixture.transport.set_mode(mode);value=_stock_lookup(stock,page)
            _stock_report(stock,page,value,quantity,availability);_stock_times(stock,page,value)
            stock.require(value['reference']['unit']=={'kind':kind,'label':label,'precision':precision} and
                value['reference']['qualifications']==codes,'HUI18_PRECISE_SUPPORTED_REFERENCE')
            stock.require(page.locator('#stockResultUnit').text_content()==label and
                page.locator('#stockResultQualifications').text_content()==words,'HUI18_PRECISE_UNIT_AND_QUALIFICATIONS')
            if mode=='stock_measured':_stock_capture(stock,page,'hui18-stock-unit.png')
        _stock_no_mutation(stock,before)
    return {'price_independent_reference':True,'options_supported':True,'unit_and_conversion_exact':True,'locale_timestamps':True}


def stock_pending_native_edits_abort_and_clear(ctx):
    with _stock_case(ctx) as stock:
        _,page=_stock_actor(stock,'manager');stock.fixture.transport.set_mode('stock_positive');_stock_open(stock,page)
        before=stock.fixture.protected_snapshot()
        for edit in ('sku','location'):
            _stock_lookup(stock,page);_visible(stock,page,'#stockResult','HUI19_PRIOR_REPORT_VISIBLE')
            with _stock_terminal(stock,page) as terminals:
                with stock.fixture.stock_reply_gate() as gate:
                    page.locator('#stockSku').press('Enter');_sku_committed(stock,page,gate,'HUI19_REAL_COMPLETED_LOOKUP')
                    _stock_empty(stock,page,'HUI19_SUBMIT_CLEARS_PRIOR_REPORT')
                    stock.require(page.locator('#stockCheck').is_disabled(),'HUI19_ACTUAL_PENDING_CHECK')
                    if edit=='sku':page.locator('#stockSku').fill('NATIVE-CHANGED-SKU')
                    else:page.locator('#stockLocation').select_option(stock.fixture.location_b)
                    _stock_empty(stock,page,'HUI19_NATIVE_EDIT_CLEARS_PENDING_DATA')
                    stock.require(page.locator('#stockCheck').text_content()=='Check stock','HUI19_EDIT_RESETS_MANUAL_ACTION')
                    gate.release()
                _stock_wait_terminal(stock,page,terminals)
                stock.require(terminals['failed']==1,'HUI19_NATIVE_EDIT_ABORTED_OLD_FETCH')
            _stock_empty(stock,page,'HUI19_HELD_OLD_REPLY_NEVER_RENDERED')
            stock.require(page.locator('#stockSku').input_value()==('NATIVE-CHANGED-SKU' if edit=='sku' else stock.fixture.sku) and
                page.locator('#stockLocation').input_value()==(stock.fixture.location_a if edit=='sku' else stock.fixture.location_b),
                'HUI19_NATIVE_EDIT_VALUE_PRESERVED')
        _stock_lookup(stock,page);_visible(stock,page,'#stockResult','HUI19_NEW_MANUAL_CHECK_WORKS');_stock_no_mutation(stock,before)
    return {'native_input_and_location_edits':True,'old_fetch_aborted':True,'held_real_reply_rejected':True}


def stock_logout_new_principal_on_existing_page(ctx):
    with _stock_case(ctx) as stock:
        context,page=_stock_actor(stock,'owner');stock.fixture.transport.set_mode('stock_positive');_stock_open(stock,page)
        before=stock.fixture.protected_snapshot();_stock_lookup(stock,page)
        document=page.evaluate_handle('document');other=context.new_page()
        other.goto(stock.fixture.origin+'/',wait_until='load',timeout=15000)
        _visible(stock,other,'#workspace','HUI20_OTHER_REAL_OWNER_WORKSPACE');_observer_ready(stock,other,'HUI20_OTHER_READY')
        try:
            with _stock_terminal(stock,page) as terminals:
                with stock.fixture.stock_reply_gate() as gate:
                    page.locator('#stockSku').press('Enter');_sku_committed(stock,page,gate,'HUI20_REAL_SUCCESS_BEFORE_LOGOUT')
                    _logout(stock,other,'HUI20_ACTUAL_OWNER_LOGOUT');page.bring_to_front()
                    _text(stock,page,'#stockSession','Your session changed.','HUI20_LOGOUT_OBSERVED')
                    _stock_empty(stock,page,'HUI20_EXISTING_PAGE_CLEARED_AT_LOGOUT')
                    _sign_in(stock,other,'buyer',code='HUI20_DIFFERENT_REAL_BUYER');page.bring_to_front();_sku_settle(page)
                    _stock_empty(stock,page,'HUI20_NEW_PRINCIPAL_CANNOT_ADOPT_PRIOR_DATA');gate.release()
                _stock_wait_terminal(stock,page,terminals)
            _stock_empty(stock,page,'HUI20_OLD_REPLY_REMAINS_UNUSABLE')
            stock.require(document.evaluate('node=>node===document') is True and page.url==stock.fixture.origin+'/stock' and
                page.locator('#stockForm').is_hidden(),'HUI20_ACTUAL_EXISTING_DOCUMENT_BEFORE_RELOAD')
            page.locator('#stockRefreshSession').click();_visible(stock,page,'#stockForm','HUI20_EXPLICIT_NEW_SESSION_REFRESH')
            _enabled(stock,page,'#stockRefreshSession','HUI20_NEW_SESSION_SETTLED')
            stock.require(_me(page)['role']=='buyer','HUI20_REAL_CURRENT_BUYER')
            _stock_empty(stock,page,'HUI20_REFRESH_DOES_NOT_REUSE_RESULT');read_count=stock.fixture.transport.summary()['count_read']
            value=_stock_lookup(stock,page);_stock_report(stock,page,value,'+0007.00000','positive')
            stock.require(stock.fixture.transport.summary()['count_read']==read_count+1,'HUI20_NEW_MANUAL_PROVIDER_COUNT_READ')
            _stock_no_mutation(stock,before);_logout(stock,other,'HUI20_BUYER_LOGOUT')
        finally:document.dispose()
    return {'real_logout_and_new_principal':True,'existing_document_clears_old_data':True,'new_manual_buyer_check':True}


def stock_native_pagehide_and_network_back(ctx):
    with _stock_case(ctx) as stock:
        branch_flags=('pagehide_seen','pagehide_not_persisted','form_hidden','manual_inputs_empty','result_hidden',
            'result_fields_empty','result_text_empty','result_timestamps_empty','network_back_200','fresh_form_ready',
            'fresh_result_empty','fresh_manual_inputs_empty')
        observation={'version':1,'case':'HUI21','status':'incomplete',
            'post_destruction_transport':'deferred_after_document_destruction','branches':[],
            'pending_transport':{'request_starts':0,'real_response_completed':False,'finished_events':0,'failed_events':0,
                'exposed_response':'unavailable','exposed_failure':'unavailable'}}
        stock.stock_pagehide_observation=observation
        _,page=_stock_actor(stock,'cashier');stock.fixture.transport.set_mode('stock_positive');_stock_open(stock,page)
        before=stock.fixture.protected_snapshot()

        def verify_pagehide_and_back(row):
            marker=page.evaluate("JSON.parse(sessionStorage.getItem('stock-fixture-pagehide')||'{}')")
            names=branch_flags[:8]
            stock.require(type(marker) is dict and set(marker)==set(names) and
                all(type(marker[name]) is bool for name in names),'HUI21_NATIVE_PAGEHIDE_MARKER_SHAPE')
            for name in names:row[name]=marker[name]
            stock.require(all(marker.values()),'HUI21_ACTUAL_PAGEHIDE_CLEARS_ALL_DATA')
            response=page.go_back(wait_until='load',timeout=15000)
            row['network_back_200']=response is not None and response.status==200 and page.url==stock.fixture.origin+'/stock'
            stock.require(row['network_back_200'],'HUI21_REAL_NETWORK_BACK_NAVIGATION')
            _visible(stock,page,'#stockForm','HUI21_FRESH_BACK_STAFF_PAGE')
            _enabled(stock,page,'#stockRefreshSession','HUI21_BACK_REFRESH_SETTLED');row['fresh_form_ready']=True
            _stock_empty(stock,page,'HUI21_BACK_DOCUMENT_HAS_NO_OLD_REPORT');row['fresh_result_empty']=True
            row['fresh_manual_inputs_empty']=(page.locator('#stockSku').input_value()=='' and
                page.locator('#stockLocation').input_value()=='' and page.locator('#stockCheck').is_disabled() and
                page.locator('#stockCheck').text_content()=='Check stock')
            stock.require(row['fresh_manual_inputs_empty'],'HUI21_BACK_REQUIRES_NEW_MANUAL_INPUT')

        def snapshot_transport(terminals):
            # Immediate scalar observation only; never imply terminal/cancellation acceptance.
            target=terminals['request'];record=observation['pending_transport']
            record.update(request_starts=terminals['starts'],finished_events=terminals['finished'],failed_events=terminals['failed'])
            try:
                if target is not None:
                    failure=target.failure;response=target.existing_response
                    record['exposed_failure']='none' if failure is None else 'aborted' if failure=='net::ERR_ABORTED' else 'other'
                    record['exposed_response']='none' if response is None else 'http200' if response.status==200 else 'other'
            except Exception:
                record['exposed_failure']=record['exposed_response']='unavailable'

        for pending in (False,True):
            row=dict.fromkeys(branch_flags,False);row['branch']='pending' if pending else 'visible'
            observation['branches'].append(row)
            value=_stock_lookup(stock,page);_stock_report(stock,page,value,'+0007.00000','positive');_stock_times(stock,page,value)
            page.evaluate('''()=>{sessionStorage.removeItem('stock-fixture-pagehide');addEventListener('pagehide',event=>{
                const result=document.getElementById('stockResult'),form=document.getElementById('stockForm');
                const fields=['Sku','Item','Variation','Location','Quantity','Id','Parent'];
                const texts=['Summary','Unit','Qualifications','Checked','Updated'];
                sessionStorage.setItem('stock-fixture-pagehide',JSON.stringify({
                    pagehide_seen:true,pagehide_not_persisted:event.persisted===false,form_hidden:form.hidden,
                    manual_inputs_empty:document.getElementById('stockSku').value===''&&document.getElementById('stockLocation').value==='',
                    result_hidden:result.hidden,result_fields_empty:fields.every(suffix=>document.getElementById('stockResult'+suffix).value===''),
                    result_text_empty:texts.every(suffix=>document.getElementById('stockResult'+suffix).textContent===''),
                    result_timestamps_empty:texts.every(suffix=>{const node=document.getElementById('stockResult'+suffix);
                        return !node.hasAttribute('title')&&!node.hasAttribute('data-timestamp')})}));
            },{once:true})}''')
            if pending:
                with _stock_terminal(stock,page) as terminals:
                    try:
                        with stock.fixture.stock_reply_gate() as gate:
                            page.locator('#stockSku').press('Enter');_sku_committed(stock,page,gate,'HUI21_REAL_SUCCESS_BEFORE_NAVIGATION')
                            stock.require(terminals['request'] is not None and terminals['starts']==1,'HUI21_ONE_ACTUAL_REQUEST_BEFORE_NAVIGATION')
                            observation['pending_transport']['request_starts']=terminals['starts']
                            observation['pending_transport']['real_response_completed']=True
                            _stock_workspace(stock,page);gate.release()
                        verify_pagehide_and_back(row)
                    finally:snapshot_transport(terminals)
                    record=observation['pending_transport']
                    stock.require(record['request_starts']==1,'HUI21_ONE_ACTUAL_LOOKUP_REQUEST')
                    stock.require(record['finished_events']+record['failed_events']<=1,'HUI21_AT_MOST_ONE_EXPOSED_TERMINAL')
                    stock.require(record['exposed_failure'] in ('none','aborted'),'HUI21_UNEXPECTED_EXPOSED_REQUEST_FAILURE')
                    stock.require(record['exposed_response'] in ('none','http200'),'HUI21_UNEXPECTED_EXPOSED_RESPONSE')
                    # Exact abort/finish after document destruction remains explicitly DEFERRED.
            else:
                _stock_workspace(stock,page);verify_pagehide_and_back(row)
        _stock_no_mutation(stock,before)
        observation['status']='ui_verified_transport_deferred'
        # No timer/timeout establishes UI success. Actual BFCache restoration stays DEFERRED.
    return {'actual_pagehide':True,'network_back_clears_result':True,'post_destruction_transport':'deferred',
        'bfcache_restore':'deferred_under_default_playwright_profile'}


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
    'HUI16': 'Four real stock roles, native Enter, literal SKU and desktop/mobile layout',
    'HUI17': 'Raw zero/negative/fractional and missing/unknown/malformed stock outcomes',
    'HUI18': 'Price/options/units/conversion qualifications and locale stock timestamps',
    'HUI19': 'Native pending SKU/location edits abort and clear completed old replies',
    'HUI20': 'Existing stock page rejects old principal and permits new manual buyer check',
    'HUI21': 'Native stock pagehide/network back clears data; old-document transport and BFCache deferred',
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
    ('HUI16', stock_four_roles_native_enter_literal_and_layout),
    ('HUI17', stock_raw_quantities_and_unknown_outcomes),
    ('HUI18', stock_price_options_units_and_qualifications),
    ('HUI19', stock_pending_native_edits_abort_and_clear),
    ('HUI20', stock_logout_new_principal_on_existing_page),
    ('HUI21', stock_native_pagehide_and_network_back),
)
