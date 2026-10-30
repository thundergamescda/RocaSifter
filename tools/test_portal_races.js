'use strict';
/* Deterministic portal async regressions. Node VM with a small DOM fixture;
 * this is not browser, PostgreSQL, Square, or deployed-service acceptance. */
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const sourcePath = path.join(root, 'hosted', 'static', 'portal.js');
const source = fs.readFileSync(sourcePath, 'utf8');
const digest = value => crypto.createHash('sha256').update(value).digest('hex');
const sourceHash = digest(source);
const started = new Date().toISOString();

class Element {
  constructor(document, id) {
    this.document = document;
    this.id = id;
    this.hidden = id !== 'signin';
    this.value = '';
    this.textContent = '';
    this.disabled = false;
    this.children = [];
  }
  querySelector(selector) {
    if (selector === 'button') return this.document.getElementById(`${this.id}-submit`);
    if (selector === 'input') return this.document.getElementById(`${this.id}-first-input`);
    throw Error(`Unsupported fixture selector: ${selector}`);
  }
  reset() {
    const fields = {
      acceptInviteForm: ['inviteStore', 'inviteEmail', 'inviteCode', 'invitePassword', 'invitePasswordConfirm'],
      pilotSetupForm: ['setupAuthorization', 'setupStoreName', 'setupStoreSlug', 'setupEmail', 'setupPassword', 'setupPasswordConfirm'],
      inviteStaffForm: ['staffInviteEmail', 'staffInviteRole']
    };
    for (const id of fields[this.id] || []) this.document.getElementById(id).value = '';
  }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  focus() {}
  select() {}
}

function fixture() {
  const elements = new Map();
  const passwordIds = ['password', 'inviteCode', 'invitePassword', 'invitePasswordConfirm',
    'setupAuthorization', 'setupPassword', 'setupPasswordConfirm'];
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element(document, id));
      return elements.get(id);
    },
    querySelectorAll(selector) {
      if (selector === 'input[type="password"]') return passwordIds.map(id => this.getElementById(id));
      if (selector === '[data-return-signin]') return [];
      throw Error(`Unsupported fixture selector: ${selector}`);
    },
    createElement(tag) { return new Element(document, `created-${tag}`); }
  };
  const storage = new Map([['ss-signed-out', '1']]);
  const requests = [];
  const context = {
    document,
    sessionStorage: {
      getItem: key => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key)
    },
    location: {hash: '', pathname: '/', origin: 'https://fixture.invalid'},
    history: {replaceState() {}},
    window: {addEventListener() {}, location: {assign() { throw Error('Unexpected navigation'); }}},
    navigator: {clipboard: {writeText: async () => {}}},
    URL, URLSearchParams, AbortSignal,
    confirm: () => true,
    fetch(url, options) {
      return new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));
    }
  };
  vm.createContext(context);
  vm.runInContext(source, context, {filename: 'hosted/static/portal.js'});
  const evaluate = code => vm.runInContext(code, context);
  const element = id => document.getElementById(id);
  const answer = (index, status, result) => {
    const request = requests[index];
    assert.ok(request, `Expected deferred request ${index}`);
    request.resolve({ok: status >= 200 && status < 300, status, json: async () => result});
  };
  const signedIn = label => {
    context.fixtureLabel = label;
    evaluate("++portalEpoch;portalMe={role:'owner',store:fixtureLabel};csrfToken=fixtureLabel;byId('workspace').hidden=false;for(const id of ['signin','invitation','pilotSetup'])byId(id).hidden=true;");
  };
  return {evaluate, element, answer, requests, signedIn};
}

const cases = [
  ['a previous session 401 cannot clear a newer signed-in view', async () => {
    const f = fixture();
    f.signedIn('old-fixture');
    const pending = f.evaluate("api('/fixture/stale').catch(error=>error.status)");
    f.signedIn('new-fixture');
    f.answer(0, 401, {error: 'Fixture session expired.'});
    assert.equal(await pending, 401);
    assert.equal(f.evaluate("portalMe.store==='new-fixture'&&csrfToken==='new-fixture'"), true);
    assert.equal(f.element('workspace').hidden, false);
  }],
  ['a current session 401 clears the workspace and password fields', async () => {
    const f = fixture();
    f.signedIn('current-fixture');
    f.element('staffInvitationLink').value = 'fixture-private-invitation';
    for (const id of ['password', 'inviteCode', 'setupAuthorization']) f.element(id).value = 'fixture-secret';
    const pending = f.evaluate("api('/fixture/current').catch(error=>error.status)");
    f.answer(0, 401, {error: 'Fixture session expired.'});
    assert.equal(await pending, 401);
    assert.equal(f.evaluate("portalMe===null&&csrfToken===''"), true);
    assert.equal(f.element('workspace').hidden, true);
    assert.equal(f.element('signin').hidden, false);
    for (const id of ['password', 'inviteCode', 'setupAuthorization', 'staffInvitationLink']) {
      assert.equal(f.element(id).value, '', `Secret field must be cleared: ${id}`);
    }
  }],
  ...['invitation', 'pilotSetup'].map(kind => [
    `delayed ${kind} success cannot replace a newer signed-in view`, async () => {
      const f = fixture();
      const invitation = kind === 'invitation';
      const form = invitation ? 'acceptInviteForm' : 'pilotSetupForm';
      const prefix = invitation ? 'invite' : 'setup';
      f.evaluate(`showAccountForm('${kind}')`);
      f.element(`${prefix}Password`).value = f.element(`${prefix}PasswordConfirm`).value = 'fictional-test-password';
      f.element(invitation ? 'inviteStore' : 'setupStoreSlug').value = 'fixture-store';
      f.element(`${prefix}Email`).value = 'owner@example.invalid';
      const pending = f.evaluate(`byId('${form}').onsubmit({preventDefault(){},target:byId('${form}')})`);
      assert.equal(f.requests.length, 1);
      f.evaluate("showAccountForm('signin')");
      f.signedIn('new-fixture');
      f.element('loginStatus').textContent = 'new-session-status';
      f.answer(0, 201, invitation ? {staff_created: true} : {owner_created: true});
      await pending;
      assert.equal(f.element('signin').hidden, true);
      assert.equal(f.element('workspace').hidden, false);
      assert.equal(f.evaluate("portalMe.store==='new-fixture'&&csrfToken==='new-fixture'"), true);
      assert.equal(f.element('loginStatus').textContent, 'new-session-status');
    }
  ]),
  ...['success', 'failure'].map(outcome => [
    `delayed logout ${outcome} cannot overwrite a newer session status`, async () => {
      const f = fixture();
      f.signedIn('old-fixture');
      const pending = f.evaluate("byId('signOut').onclick()");
      assert.equal(f.requests[0].url, '/api/auth/logout');
      f.signedIn('new-fixture');
      f.element('loginStatus').textContent = 'new-session-status';
      if (outcome === 'success') f.answer(0, 200, {signed_out: true});
      else f.requests[0].reject(Error('Fictional network failure'));
      await pending;
      assert.equal(f.element('loginStatus').textContent, 'new-session-status');
      assert.equal(f.element('workspace').hidden, false);
      assert.equal(f.evaluate("portalMe.store==='new-fixture'&&csrfToken==='new-fixture'"), true);
    }
  ]),
  ['opening an account form hides any previous workspace', async () => {
    const f = fixture();
    f.element('workspace').hidden = false;
    f.evaluate("showAccountForm('invitation')");
    assert.equal(f.element('workspace').hidden, true);
    assert.equal(f.element('invitation').hidden, false);
  }],
  ['a pending initial workspace response cannot replace an account form', async () => {
    const f = fixture();
    const pending = f.evaluate('workspace()');
    f.evaluate("showAccountForm('pilotSetup')");
    f.answer(0, 200, {store: 'old-fixture', role: 'owner', csrf_token: 'fixture-token'});
    await pending;
    assert.equal(f.element('pilotSetup').hidden, false);
    assert.equal(f.element('workspace').hidden, true);
    assert.equal(f.evaluate('portalMe===null'), true);
    assert.equal(f.requests.length, 1);
  }]
];

(async () => {
  const results = [];
  for (const [name, run] of cases) {
    try { await run(); results.push({name, passed: true}); }
    catch (error) { results.push({name, passed: false, failure: error.message}); }
  }
  const endHash = digest(fs.readFileSync(sourcePath));
  const stable = sourceHash === endHash;
  const passed = stable && results.every(result => result.passed);
  console.log(JSON.stringify({
    started_utc: started,
    finished_utc: new Date().toISOString(),
    scope: 'Deterministic Node VM with a small DOM fixture and deferred fictional fetch responses. Not browser or deployed-service acceptance.',
    node: process.version,
    source_sha256: {'hosted/static/portal.js': endHash, 'tools/test_portal_races.js': digest(fs.readFileSync(__filename))},
    source_sha256_at_start: {'hosted/static/portal.js': sourceHash},
    source_snapshot_stable: stable,
    live_square_contacted: false,
    network_contacted: false,
    tests_run: results.length,
    passed,
    results
  }, null, 2));
  process.exitCode = passed ? 0 : 1;
})().catch(error => { console.error(error.message); process.exitCode = 1; });
