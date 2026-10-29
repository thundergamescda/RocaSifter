'use strict';
// Staff credentials are relayed once; session tokens never enter browser storage.
const cloudNav=document.createElement('button');
cloudNav.id='cloudNav';cloudNav.className='nav-item';cloudNav.dataset.mode='cloud';
cloudNav.innerHTML=icon('globe')+'<span>Shared credit</span>';
$('navigation').append(cloudNav);cloudNav.onclick=()=>navigate('cloud');
const cloudStyle=document.createElement('link');cloudStyle.rel='stylesheet';cloudStyle.href='/cloud.css';document.head.append(cloudStyle);
const cloudView=document.createElement('section');cloudView.id='cloudView';cloudView.hidden=true;
cloudView.innerHTML=`
<div class="cloud-pilot"><strong>Private pilot</strong><p>Shared account history is available to invited stores. Shared trade credit requires an owner or manager to confirm the hosted receipt. Square checkout is not enabled. Local credit history stays on this computer.</p></div>
<div class="cloud-toolbar"><p id="cloudConnection" role="status">Checking store connection…</p><div><button class="button secondary small" id="cloudRefresh">Refresh</button><button class="button secondary small" id="cloudSignOut">Sign out here</button></div></div>
<p class="cloud-message" id="cloudMessage" role="status"></p>
<section class="cloud-signin card-table-panel" id="cloudSignin"><span class="section-kicker">STORE SIGN-IN</span><h2 id="cloudSigninTitle">Open your shared workspace.</h2><p id="cloudSigninDescription">Use your invited staff account. Your password is never saved on this computer.</p>
<form id="cloudLoginForm"><label>Store ID<input id="cloudStore" autocomplete="organization" placeholder="your-store" required maxlength="50"></label><label>Staff email<input id="cloudEmail" type="email" autocomplete="username" required maxlength="254"></label><label>Staff password<input id="cloudPassword" type="password" autocomplete="current-password" required maxlength="128"></label><button class="button primary" type="submit">Sign in to store</button><p id="cloudLoginMessage" role="status"></p></form>
<section id="cloudMfa" class="cloud-mfa" hidden><h3>Verify it’s you.</h3><p id="cloudMfaStore"></p><form id="cloudMfaForm"><label>Use<select id="cloudMfaMethod"><option value="totp">Authenticator app</option><option value="recovery_code">An unused recovery code</option></select></label><label><span id="cloudMfaCodeLabel">Authenticator code</span><input id="cloudMfaCode" autocomplete="one-time-code" inputmode="numeric" maxlength="6" required></label><p id="cloudMfaHelp" class="hint">Enter the current six digits from your authenticator. A code can only be used once.</p><button class="button primary" type="submit">Verify & sign in</button><p id="cloudMfaMessage" role="status"></p></form><button class="button secondary small" id="cloudMfaCancel" type="button">Cancel sign-in</button><button class="button secondary small" id="cloudMfaClearLocal" type="button" hidden>Clear this sign-in on this computer</button><p class="hint">Verification expires after five minutes. Your password and verification code are not saved by this app.</p></section>
<p class="hint">Accounts are invitation-only during the pilot. Your store owner supplies your Store ID and invitation.</p></section>
<section id="cloudWorkspace" hidden><div class="cloud-store-bar"><div><span class="section-kicker">SHARED STORE ACCOUNT</span><h2 id="cloudStoreName"></h2><p id="cloudStaffRole"></p></div><a class="button secondary small" href="https://stock-and-sleeve-api.onrender.com/" target="_blank" rel="noopener noreferrer">Store connections ${icon('arrow-right')}</a></div>
<div class="credit-layout"><section class="card-table-panel credit-customers"><div class="table-heading"><h3>Customers</h3><button id="cloudNewCustomer" class="button secondary small">Add customer</button></div><input id="cloudSearch" placeholder="Search name, contact or Square ID…" aria-label="Search shared-credit customers" maxlength="200"><div id="cloudCustomers"></div></section><section id="cloudAccount" class="card-table-panel credit-account"><p class="list-empty">Choose a customer to view their shared balance.</p></section></div></section>`;
$('workflowContent').after(cloudView);
const cloudCustomerDialog=document.createElement('dialog');cloudCustomerDialog.className='modal';cloudCustomerDialog.id='cloudCustomerDialog';
cloudCustomerDialog.innerHTML=`<h2>Add a shared-credit customer</h2><form id="cloudCustomerForm"><label>Customer name<input id="cloudCustomerName" maxlength="200" required></label><label>Contact <small>Optional</small><input id="cloudCustomerContact" maxlength="320"></label><label>Square customer ID <small>Optional; use the exact ID from Square</small><input id="cloudCustomerSquare" maxlength="150"></label><label class="cloud-check"><input id="cloudCustomerPermission" type="checkbox" required>I have authority to record these details for this store’s credit account.</label><div class="modal-actions"><button class="button secondary" type="button" id="cloudCancelCustomer">Cancel</button><button class="button primary" type="submit">Create shared account</button></div><p id="cloudCustomerMessage" role="status"></p></form>`;
document.body.append(cloudCustomerDialog);
const cloudEntryDialog=document.createElement('dialog');cloudEntryDialog.className='modal';cloudEntryDialog.id='cloudEntryDialog';
cloudEntryDialog.innerHTML=`<h2>Opening balance / correction</h2><p id="cloudEntryCustomer"></p><form id="cloudEntryForm"><label>Entry type<select id="cloudEntryKind"><option value="opening">Opening balance</option><option value="adjust_add">Add correction</option><option value="adjust_remove">Remove correction</option></select></label><label>Amount <small>Positive USD amount</small><input id="cloudEntryAmount" type="number" inputmode="decimal" min="0.01" max="1000000" step="0.01" required></label><label>Reference<input id="cloudEntryReference" maxlength="200" required></label><label>Reason<textarea id="cloudEntryReason" minlength="3" maxlength="1000" required></textarea></label><p class="hint">Corrections are audited to your staff account. This does not record a Square sale or redeem credit.</p><div class="modal-actions"><button class="button secondary" type="button" id="cloudCancelEntry">Cancel</button><button class="button primary" type="submit">Save shared entry</button></div><p id="cloudEntryMessage" role="status"></p></form>`;
document.body.append(cloudEntryDialog);
const cloudPending=document.createElement('section');cloudPending.id='cloudPending';cloudPending.className='cloud-pilot';cloudPending.hidden=true;
$('cloudMessage').after(cloudPending);

let cloudSession={signedIn:false},cloudAccount=null,cloudSelected=null,cloudHealthy=false,cloudRevision=0,cloudPendingCount=0;
let cloudCustomerKey='',cloudEntryKey='',cloudEntryVersion=0,cloudEntryCustomerId=null,cloudSearchTimer,cloudRefreshing=null;
let cloudSearchSequence=0,cloudAccountSequence=0;
let cloudMfa=null;
function cloudResetView(message='Choose a customer to view their shared balance.'){
 cloudPendingCount=0;cloudPending.hidden=true;cloudPending.replaceChildren();
 ++cloudSearchSequence;++cloudAccountSequence;clearTimeout(cloudSearchTimer);cloudRefreshing=null;
 cloudSelected=null;cloudAccount=null;cloudEntryCustomerId=null;cloudEntryKey='';cloudCustomerKey='';
 cloudEntryDialog.close();cloudCustomerDialog.close();$('cloudCustomers').replaceChildren();
 $('cloudAccount').innerHTML=`<p class="list-empty">${escapeHTML(message)}</p>`;$('cloudRefresh').disabled=false;
 for(const form of [$('cloudEntryForm'),$('cloudCustomerForm')]){form.cloudOperation=null;form.inert=false}
}
function cloudFormStart(form){const operation=Symbol();form.cloudOperation=operation;form.inert=true;return operation}
function cloudFormEnd(form,operation){if(form.cloudOperation===operation){form.cloudOperation=null;form.inert=false}}
async function cloudApi(path,data){
 const headers={'X-SS-Local-Session':cloudSession.sessionId||''};if(data!==undefined){headers['Content-Type']='application/json';if(path!=='login')data={...data,sessionId:data.sessionId??cloudSession.sessionId??''}}
 let response;try{response=await fetch('/api/cloud/'+path,{method:data===undefined?'GET':'POST',headers,body:data===undefined?undefined:JSON.stringify(data),cache:'no-store'})}catch{const error=Error('The desktop service is not responding. Reopen the app and check saved shared actions before retrying.');error.offline=true;error.pendingRequired=['adjust','customer'].includes(path);throw error}
 let result;try{result=await response.json()}catch{throw Error('The desktop service is not responding. Reopen the app and check the shared history before retrying.')}
 if(!response.ok){const error=Error((result.errors||['The shared-credit action could not complete.']).join('\n'));error.status=response.status;error.offline=result.offline;error.pendingRequired=result.pendingRequired;error.sessionChanged=result.sessionChanged;error.mfaRestartRequired=result.mfaRestartRequired;throw error}return result;
}
function cloudMfaMethod(){
 const recovery=$('cloudMfaMethod').value==='recovery_code';$('cloudMfaCode').value='';$('cloudMfaCode').type=recovery?'password':'text';
 $('cloudMfaCode').inputMode=recovery?'text':'numeric';$('cloudMfaCode').maxLength=recovery?80:6;
 $('cloudMfaCode').autocomplete=recovery?'off':'one-time-code';$('cloudMfaCodeLabel').textContent=recovery?'Recovery code':'Authenticator code';
 $('cloudMfaHelp').textContent=recovery?'Enter one complete unused recovery code. Each recovery code can only be used once.':'Enter the current six digits from your authenticator. A code can only be used once.';
}
function cloudRenderMfa(){
 const next=!cloudSession.signedIn&&cloudSession.mfaRequired?cloudSession:null;
 if(!next||cloudMfa?.challengeId!==next.challengeId){$('cloudMfaCode').value='';$('cloudMfaMessage').textContent='';$('cloudMfaForm').cloudOperation=null;$('cloudMfaForm').inert=false}
 cloudMfa=next;$('cloudMfa').hidden=!next;$('cloudLoginForm').hidden=!!next;
 for(const node of [$('cloudMfaCode'),$('cloudMfaMethod'),$('cloudMfaForm').querySelector('button[type=submit]')])node.disabled=!!(next?.verificationExpired||next?.cancelRequired);
 $('cloudMfaForm').hidden=!!next?.cancelRequired;$('cloudMfaClearLocal').hidden=!next?.cancelRequired;$('cloudMfaCancel').textContent=next?.cancelRequired?'Retry server cancellation':'Cancel sign-in';
 $('cloudSigninTitle').textContent=next?'Verify it’s you.':'Open your shared workspace.';
 $('cloudSigninDescription').textContent=next?'Complete verification to open your shared workspace.':'Use your invited staff account. Your password is never saved on this computer.';
 if(next){$('cloudMfaStore').textContent=next.cancelRequired?'Server cancellation still needs confirmation. Retry when connected, or explicitly clear this sign-in here. Any issued server session expires within eight hours.':next.verificationExpired?'Verification expired. Cancel sign-in to clear the previous attempt before starting again.':'Continue signing in to '+next.store+'.';
  for(const option of $('cloudMfaMethod').options)option.disabled=!next.methods.includes(option.value);
  if(!next.methods.includes($('cloudMfaMethod').value)){$('cloudMfaMethod').value=next.methods[0];cloudMfaMethod()}
 }
}
function cloudRenderSession(){
 cloudRenderMfa();
 $('cloudSignin').hidden=cloudSession.signedIn;$('cloudWorkspace').hidden=!cloudSession.signedIn;
 $('cloudConnection').textContent=cloudSession.signedIn?`Signed in · ${cloudSession.store}`:'Sign in to your store to use shared credit.';
 $('cloudStoreName').textContent=cloudSession.store||'';$('cloudStaffRole').textContent=cloudSession.signedIn?'Staff role: '+cloudSession.role:'';
 $('cloudNewCustomer').disabled=!cloudHealthy||cloudPendingCount>0;
}
async function cloudLoadPending(){
 const revision=cloudRevision;if(!cloudSession.signedIn){cloudPending.hidden=true;return}
 const result=await cloudApi('pending');if(revision!==cloudRevision||!cloudSession.signedIn)return;
 cloudPendingCount=result.pending.length;cloudPending.hidden=!cloudPendingCount&&!result.hasOtherPending;
 cloudPending.innerHTML='<div><strong>Saved action needs confirmation</strong><p>Check the server before adding another entry. A saved action can be finished once, including after the app has restarted.</p>'+result.pending.map(row=>window.sharedTradePendingMarkup?.(row)||`<article class="cloud-pending-action"><p>${escapeHTML(row.action==='customer'?'Create customer account':({opening:'Opening balance',adjust_add:'Add correction',adjust_remove:'Remove correction'}[row.entryKind]||'Credit entry'))}${row.amount_cents?' · '+usd(row.amount_cents/100):''}${row.reference?' · '+escapeHTML(row.reference):''}</p><div><button class="button secondary small" data-cloud-check="${escapeHTML(row.requestKey)}">Check server</button><button class="button secondary small" data-cloud-retry="${escapeHTML(row.requestKey)}">Finish saved action</button></div></article>`).join('')+(result.hasOtherPending?'<p>This computer also has a saved action for another store. Sign in to that store to review it.</p>':'')+'</div>';
 $('cloudNewCustomer').disabled=!cloudHealthy||cloudPendingCount>0;if($('cloudOpenEntry'))$('cloudOpenEntry').disabled=cloudPendingCount>0;
}
cloudPending.onclick=async event=>{
 const check=event.target.closest('[data-cloud-check]'),retry=event.target.closest('[data-cloud-retry]');if(!check&&!retry)return;
 const key=(check||retry).dataset[check?'cloudCheck':'cloudRetry'],revision=cloudRevision;
 if(retry&&!await confirmAction('Finish this saved action?','The app will check whether this action already completed, then finish the exact saved details once.','Check and finish'))return;
 if(revision!==cloudRevision)return;
 cloudPending.inert=true;
 try{const result=await cloudApi('recover',{requestKey:key,retry:!!retry});if(revision!==cloudRevision)return;await cloudRefresh();if(revision===cloudRevision)$('cloudMessage').textContent=result.resolved?'Saved action confirmed. Shared history has been refreshed.':result.message}
 catch(error){if(revision===cloudRevision)$('cloudMessage').textContent=error.message}finally{cloudPending.inert=false}
};
function cloudUnavailable(error){
 cloudHealthy=false;cloudResetView('The shared balance cannot be confirmed. Reconnect and refresh before making a credit decision.');
 $('cloudNewCustomer').disabled=true;$('cloudMessage').textContent=error.message;
 if(error.status===401){++cloudRevision;cloudSession={signedIn:false};cloudRenderSession()}
}
async function cloudLoadCustomers(){
 if(!cloudSession.signedIn||!cloudHealthy)return false;
 const revision=cloudRevision,sequence=++cloudSearchSequence,query=$('cloudSearch').value;
 let result;try{result=await cloudApi('search',{query})}catch(error){if(revision!==cloudRevision||sequence!==cloudSearchSequence)return false;throw error}
 if(revision!==cloudRevision||sequence!==cloudSearchSequence||!cloudSession.signedIn||!cloudHealthy)return false;
 $('cloudCustomers').innerHTML=result.customers.length?result.customers.map(c=>`<button class="credit-customer" data-cloud-customer="${escapeHTML(c.id)}"><span><strong>${escapeHTML(c.display_name)}</strong><small>${escapeHTML(c.contact||c.square_customer_id||'No contact supplied')}</small></span><b>${usd(c.available_cents/100)}</b></button>`).join(''):'<p class="list-empty">No matching shared customers.</p>';
 return true;
}
function cloudRenderAccount(){
 if(!cloudAccount)return;const c=cloudAccount.customer;const manager=['owner','manager'].includes(cloudSession.role);
 $('cloudAccount').innerHTML=`<div class="credit-balance-header"><div><span class="section-kicker">AVAILABLE SHARED CREDIT</span><h2>${escapeHTML(c.display_name)}</h2><p>${escapeHTML(c.contact)}</p></div><strong>${usd(c.available_cents/100)}</strong></div><div class="cloud-balance-facts"><span>Total balance <b>${usd(c.balance_cents/100)}</b></span><span>Held for checkout <b>${usd(c.reserved_cents/100)}</b></span></div><div class="credit-account-actions">${manager?'<button class="button secondary" id="cloudOpenEntry">Opening balance / correction</button>':'<p class="hint">An owner or manager can correct balances.</p>'}<span class="cloud-checkout-gate">Checkout is not enabled in this pilot.</span></div><div class="table-scroll"><table><thead><tr><th>Date / entry</th><th>Change</th><th>Balance</th><th>Reference / reason</th></tr></thead><tbody>${cloudAccount.entries.map(e=>`<tr><td>${escapeHTML(new Date(e.at).toLocaleString())}<small class="intake-value">${escapeHTML({opening:'Opening balance',adjust_add:'Correction added',adjust_remove:'Correction removed',trade_credit:'Trade credit',discount_redeem:'Square credit used',refund:'Credit restored'}[e.kind]||e.kind)}</small></td><td class="${e.amount_cents<0?'credit-debit':'credit-add'}">${e.amount_cents>0?'+':''}${usd(e.amount_cents/100)}</td><td>${usd(e.balance_after_cents/100)}</td><td>${escapeHTML(e.reference)}<small class="intake-value">${escapeHTML(e.reason)}</small></td></tr>`).join('')||'<tr><td colspan="4">No shared credit entries yet.</td></tr>'}</tbody></table></div><p class="credit-history-note">100 most recent entries · Confirmed with your store at ${escapeHTML(new Date().toLocaleTimeString())}.</p>`;
 if($('cloudOpenEntry')){$('cloudOpenEntry').disabled=cloudPendingCount>0;$('cloudOpenEntry').onclick=()=>{if(!cloudHealthy||cloudPendingCount||cloudAccount?.customer.id!==c.id)return;cloudEntryKey=crypto.randomUUID();cloudEntryCustomerId=c.id;cloudEntryVersion=c.version;$('cloudEntryForm').reset();$('cloudEntryKind').value=cloudAccount.entries.length?'adjust_add':'opening';$('cloudEntryCustomer').textContent=c.display_name;$('cloudEntryMessage').textContent='';cloudEntryDialog.showModal()}};
}
async function cloudLoadAccount(id){
 if(!cloudSession.signedIn||!cloudHealthy)return false;
 const revision=cloudRevision,sequence=++cloudAccountSequence;cloudSelected=id;cloudAccount=null;
 $('cloudAccount').innerHTML='<p class="list-empty">Checking this customer’s shared balance…</p>';
 let result;try{result=await cloudApi('account',{customerId:id})}catch(error){if(revision!==cloudRevision||sequence!==cloudAccountSequence)return false;throw error}
 if(revision!==cloudRevision||sequence!==cloudAccountSequence||cloudSelected!==id||!cloudSession.signedIn||!cloudHealthy)return false;
 if(result.customer?.id!==id)throw Error('The shared customer response did not match your selection. Refresh before continuing.');
 cloudAccount=result;cloudRenderAccount();return true;
}
async function cloudRefresh(){
 if(cloudRefreshing===cloudRevision)return;let revision=cloudRevision;cloudRefreshing=revision;
 $('cloudRefresh').disabled=true;$('cloudConnection').textContent='Checking store connection…';
 try{
  const session=await cloudApi('session');if(revision!==cloudRevision)return;
  if(cloudSession.signedIn&&session.signedIn&&(cloudSession.store!==session.store||cloudSession.role!==session.role||cloudSession.sessionId!==session.sessionId)){
   revision=++cloudRevision;cloudResetView();cloudRefreshing=revision;$('cloudRefresh').disabled=true;
  }
  cloudSession=session;cloudHealthy=session.signedIn;cloudRenderSession();$('cloudMessage').textContent=session.message||'';
  if(!session.signedIn){++cloudRevision;cloudResetView();return}
  await cloudLoadPending();if(revision!==cloudRevision)return;
  await cloudLoadCustomers();if(revision!==cloudRevision)return;if(cloudSelected)await cloudLoadAccount(cloudSelected);
 }catch(error){if(revision===cloudRevision)cloudUnavailable(error)}finally{if(cloudRefreshing===revision){cloudRefreshing=null;$('cloudRefresh').disabled=false}}
}
window.enterCloudWorkspace=cloudRefresh;
$('cloudRefresh').onclick=cloudRefresh;
$('cloudLoginForm').onsubmit=async event=>{
 event.preventDefault();const form=event.target,operation=cloudFormStart(form),revision=++cloudRevision;
 const credentials={storeSlug:$('cloudStore').value,email:$('cloudEmail').value,password:$('cloudPassword').value};$('cloudPassword').value='';
 cloudHealthy=false;cloudSession={signedIn:false};cloudResetView();cloudRenderSession();$('cloudMessage').textContent='';$('cloudLoginMessage').textContent='Signing in…';
 try{const session=await cloudApi('login',credentials);if(revision!==cloudRevision)return;cloudSession=session;$('cloudLoginMessage').textContent='';cloudHealthy=session.signedIn;cloudRenderSession();if(session.signedIn)await cloudRefresh();else if(session.mfaRequired)$('cloudMfaCode').focus()}
 catch(error){if(revision===cloudRevision)$('cloudLoginMessage').textContent=error.message}finally{credentials.password='';cloudFormEnd(form,operation)}
};
$('cloudMfaMethod').onchange=cloudMfaMethod;
$('cloudMfaForm').onsubmit=async event=>{
 event.preventDefault();const form=event.target,operation=cloudFormStart(form),revision=cloudRevision,challengeId=cloudMfa?.challengeId;
 let code=$('cloudMfaCode').value;$('cloudMfaCode').value='';$('cloudMfaMessage').textContent='Verifying…';
 try{if(!challengeId)throw Error('Sign in again to request verification.');const session=await cloudApi('mfa-verify',{challengeId,code});if(revision!==cloudRevision)return;cloudSession=session;cloudHealthy=session.signedIn;cloudRenderSession();await cloudRefresh()}
 catch(error){if(revision!==cloudRevision)return;if(error.mfaRestartRequired){cloudSession={signedIn:false};cloudHealthy=false;cloudRenderSession();$('cloudLoginMessage').textContent=error.message}else $('cloudMfaMessage').textContent=error.message}
 finally{code='';cloudFormEnd(form,operation)}
};
$('cloudMfaCancel').onclick=async()=>{
 const challengeId=cloudMfa?.challengeId;if(!challengeId)return;const revision=++cloudRevision;
 cloudSession={signedIn:false};cloudHealthy=false;cloudResetView();cloudRenderSession();$('cloudLoginMessage').textContent='Cancelling sign-in…';
 const form=$('cloudLoginForm'),operation=cloudFormStart(form);
 try{const result=await cloudApi('mfa-cancel',{challengeId});if(revision===cloudRevision){cloudSession=result;cloudRenderSession();$(result.mfaRequired?'cloudMfaMessage':'cloudLoginMessage').textContent=result.message}}
 catch(error){if(revision===cloudRevision)$('cloudLoginMessage').textContent=error.message+' Verification has been cleared from this page.'}
 finally{cloudFormEnd(form,operation)}
};
$('cloudMfaClearLocal').onclick=async()=>{
 const challengeId=cloudMfa?.challengeId;if(!challengeId||!cloudMfa.cancelRequired)return;const revision=++cloudRevision,form=$('cloudMfaForm'),operation=cloudFormStart(form);
 $('cloudMfaClearLocal').disabled=$('cloudMfaCancel').disabled=true;
 try{const result=await cloudApi('mfa-clear-local',{challengeId});if(revision!==cloudRevision)return;cloudSession=result;cloudHealthy=false;cloudRenderSession();$('cloudLoginMessage').textContent=result.message}
 catch(error){if(revision===cloudRevision)$('cloudMfaMessage').textContent=error.message}finally{$('cloudMfaClearLocal').disabled=$('cloudMfaCancel').disabled=false;cloudFormEnd(form,operation)}
};
$('cloudSignOut').onclick=async()=>{
 const button=$('cloudSignOut'),sessionId=cloudSession.sessionId||'',challengeId=cloudMfa?.challengeId,revision=++cloudRevision;button.disabled=true;
 cloudSession={signedIn:false};cloudHealthy=false;cloudResetView();$('cloudPassword').value='';cloudRenderSession();$('cloudConnection').textContent='Signing out on this computer…';
 const login=$('cloudLoginForm'),operation=cloudFormStart(login);
 try{const result=await cloudApi('logout',{sessionId,challengeId});if(revision!==cloudRevision)return;if(result.mfaRequired)cloudSession=result;cloudRenderSession();$('cloudMessage').textContent=result.message}
 catch(error){if(revision===cloudRevision)$('cloudMessage').textContent=error.message+' Sign-out could not be confirmed. Reopen the app and sign out before sharing this computer.'}finally{if(revision===cloudRevision)button.disabled=false;cloudFormEnd(login,operation)}
};
$('cloudSearch').oninput=()=>{clearTimeout(cloudSearchTimer);cloudSearchTimer=setTimeout(()=>cloudLoadCustomers().catch(cloudUnavailable),300)};
$('cloudCustomers').onclick=event=>{const button=event.target.closest('[data-cloud-customer]');if(button)cloudLoadAccount(button.dataset.cloudCustomer).catch(cloudUnavailable)};
$('cloudNewCustomer').onclick=()=>{cloudCustomerKey=crypto.randomUUID();$('cloudCustomerForm').reset();$('cloudCustomerMessage').textContent='';cloudCustomerDialog.showModal()};
$('cloudCancelCustomer').onclick=()=>cloudCustomerDialog.close();$('cloudCancelEntry').onclick=()=>cloudEntryDialog.close();
$('cloudCustomerForm').onsubmit=async event=>{
 event.preventDefault();const form=event.target,operation=cloudFormStart(form),revision=cloudRevision;$('cloudCustomerMessage').textContent='Saving shared account…';
 try{const result=await cloudApi('customer',{request_key:cloudCustomerKey,display_name:$('cloudCustomerName').value,contact:$('cloudCustomerContact').value,square_customer_id:$('cloudCustomerSquare').value,permission_confirmed:$('cloudCustomerPermission').checked});if(revision!==cloudRevision)return;await cloudLoadAccount(result.customer_id);if(revision!==cloudRevision)return;await cloudLoadCustomers();if(revision!==cloudRevision)return;cloudCustomerDialog.close();toast('Shared customer account saved.')}
 catch(error){if(revision!==cloudRevision)return;$('cloudCustomerMessage').textContent=error.message+' Retry with the same details, or close and check shared history.';if(error.status===401||error.offline||error.sessionChanged)cloudUnavailable(error);if(error.pendingRequired){cloudCustomerDialog.close();await cloudLoadPending().catch(pendingError=>{$('cloudMessage').textContent=pendingError.message})}}finally{cloudFormEnd(form,operation)}
};
function cloudCents(value){if(!/^\d+(\.\d{1,2})?$/.test(value))throw Error('Enter a positive amount with at most two decimal places.');const parts=value.split('.');const cents=Number(parts[0])*100+Number((parts[1]||'').padEnd(2,'0'));if(!Number.isSafeInteger(cents)||cents<=0||cents>100000000)throw Error('Enter an amount between $0.01 and $1,000,000.');return cents}
$('cloudEntryForm').onsubmit=async event=>{
 event.preventDefault();const form=event.target,operation=cloudFormStart(form),revision=cloudRevision,customerId=cloudEntryCustomerId,requestKey=cloudEntryKey,version=cloudEntryVersion;$('cloudEntryMessage').textContent='Saving shared entry…';
 try{if(!customerId||!cloudHealthy||!cloudSession.signedIn)throw Error('Refresh and reopen this customer before saving a correction.');await cloudApi('adjust',{customerId,request_key:requestKey,version,kind:$('cloudEntryKind').value,amount_cents:cloudCents($('cloudEntryAmount').value),reference:$('cloudEntryReference').value,reason:$('cloudEntryReason').value});if(revision!==cloudRevision)return;await cloudLoadAccount(customerId);if(revision!==cloudRevision)return;await cloudLoadCustomers();if(revision!==cloudRevision)return;cloudEntryDialog.close();toast('Shared entry saved and balance refreshed.')}
 catch(error){if(revision!==cloudRevision)return;$('cloudEntryMessage').textContent=error.message+' Retry with the same details, or close and check shared history.';if(error.status===401||error.offline||error.sessionChanged)cloudUnavailable(error);if(error.pendingRequired){cloudEntryDialog.close();await cloudLoadPending().catch(pendingError=>{$('cloudMessage').textContent=pendingError.message})}}finally{cloudFormEnd(form,operation)}
};
setInterval(()=>{if(!window.stationStopping&&state.mode==='cloud'&&cloudSession.signedIn&&!document.querySelector('dialog[open]'))cloudRefresh()},15000);
