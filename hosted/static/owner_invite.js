/* Private owner invitation: secrets exist only in this page's closure. */
(()=>{'use strict';
 const $=id=>document.getElementById(id), KEY='ss-owner-invite-requests-v1';
 const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
 let token='',invitationId='',epoch=0,controller=null,busy=false,unsigned=false,inspected=null,requestKey='',email='',uncertain=false,complete=false,channel=null;
 let fragment=location.hash;
 history.replaceState(null,'',location.pathname);
 function passwords(){ $('ownerInvitePassword').value='';$('ownerInvitePasswordConfirm').value='';$('ownerStoreConfirmed').checked=false; }
 function status(text){$('ownerInviteStatus').textContent=text;}
 function step(which){for(const id of ['stepInvite','stepAccount','stepSecurity']){$(id).removeAttribute('aria-current');$(id).classList.remove('done');}$(which).setAttribute('aria-current','step');if(which!=='stepInvite')$('stepInvite').classList.add('done');if(which==='stepSecurity')$('stepAccount').classList.add('done');}
 function render(){
  $('inspectOwnerInviteForm').hidden=!token||!unsigned||!!inspected||uncertain||complete;
  $('reviewOwnerInvite').hidden=!inspected||uncertain||complete||!unsigned;
  $('uncertainOwnerInvite').hidden=!uncertain||!token||!unsigned;
  $('ownerInviteComplete').hidden=!complete;
  $('clearOwnerInvite').hidden=!token||complete;
  for(const button of document.querySelectorAll('form button,#checkOwnerSetup,#changeOwnerEmail'))button.disabled=busy;
 }
 function invalidate(message){epoch++;controller?.abort();controller=null;busy=false;token='';invitationId='';requestKey='';email='';inspected=null;uncertain=false;complete=false;unsigned=false;passwords();$('ownerInviteEmail').value='';for(const id of ['invitedStoreName','invitedStoreSlug','invitedOwnerEmail','invitedExpires','createdStoreSlug','createdOwnerEmail'])$(id).textContent='';$('panelTitle').textContent='Let’s find your store.';$('panelEyebrow').textContent='YOUR PRIVATE INVITATION';$('panelHelp').hidden=false;$('panelHelp').textContent='Open the private link from your invitation, then enter the email it was issued to.';step('stepInvite');$('missingLink').hidden=false;render();status(message);}
 function readRecords(){
  let records;try{const raw=localStorage.getItem(KEY);if(raw&&raw.length>4000)throw Error();records=raw?JSON.parse(raw):[];}catch{throw Error('This browser cannot read the saved setup reference. Keep your original private link and contact the platform team before retrying account creation.');}
  if(!Array.isArray(records)||records.length>20||records.some(r=>!r||Object.keys(r).sort().join(',')!=='invitation_id,request_key'||!UUID.test(r.invitation_id)||!UUID.test(r.request_key))||new Set(records.map(r=>r.invitation_id)).size!==records.length)throw Error('The saved setup references need review. Keep your private link and contact the platform team; no new request has been sent.');
  return records;
 }
 async function savedKey(create=false){
  const id=invitationId,at=epoch;const current=()=>at===epoch&&id===invitationId&&UUID.test(id)&&!!token;
  const access=()=>{if(!current())throw Error('stale');const records=readRecords(),old=records.find(r=>r.invitation_id===id);if(old)return old.request_key;if(!create)return '';if(records.length===20)throw Error('This browser has reached its saved setup reference limit. Contact the platform team before starting another store.');const key=crypto.randomUUID();records.push({invitation_id:id,request_key:key});try{if(!current())throw Error('stale');localStorage.setItem(KEY,JSON.stringify(records));if(readRecords().find(r=>r.invitation_id===id)?.request_key!==key)throw Error();}catch{if(!current())throw Error('stale');throw Error('A recovery reference could not be saved. Allow this site to store the nonsecret setup reference before creating the account. No account request has been sent.');}return key;};
  if(create){if(!navigator.locks)throw Error('Use a current browser that supports secure setup coordination. No account request has been sent.');return navigator.locks.request(KEY,access);}return access();
 }
 function safeStore(v){return v&&typeof v.name==='string'&&v.name.length>=1&&v.name.length<=150&&typeof v.slug==='string'&&/^[a-z0-9][a-z0-9-]{2,49}$/.test(v.slug);}
 function asciiEmail(v){return typeof v==='string'&&/^[\x20-\x7e]*$/.test(v);}
 function safeEmail(v){return asciiEmail(v)&&v.length<=254&&/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);}
 function noSession(v){return !['access_token','csrf_token','session','session_token','challenge_token'].some(k=>Object.hasOwn(v,k));}
 function validResult(v){return v&&v.protocol_version===1&&v.owner_created===true&&safeStore(v.store)&&safeEmail(v.owner_email)&&v.owner_email===email&&v.next_action==='sign_in'&&v.mfa_enrollment_required_for_trade_and_square_writes===true&&v.square_connected===false&&v.receiving_enabled===false&&v.checkout_enabled===false&&noSession(v)&&(!inspected||(v.store.slug===inspected.store.slug&&v.store.name===inspected.store.name));}
 const errors={INVITATION_UNAVAILABLE:'This invitation is unavailable for that email. Check the original link and intended email, or contact the issuer.',ENROLLMENT_ALREADY_COMPLETED:'This invitation has already been used. Check the saved setup or sign in with the original account details; contact the platform team if you need help.',SIGN_OUT_REQUIRED:'A store session is active. Sign out in the store workspace, then reopen the original private invitation link.',ENROLLMENT_RATE_LIMITED:'Too many invitation attempts. Wait before trying again; keep the original private link.'};
 async function api(path,data,at){
  const abort=new AbortController();controller=abort;const timer=setTimeout(()=>abort.abort(),20000);
  try{const response=await fetch(path,{method:data?'POST':'GET',credentials:'same-origin',cache:'no-store',redirect:'error',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined,signal:abort.signal});if(at!==epoch)throw Error('stale');const raw=await response.text();if(raw.length>16384)throw Error('The setup reply could not be verified. Check the saved setup before continuing.');let result;try{result=JSON.parse(raw);}catch{throw Error('The setup reply could not be verified. Check the saved setup before continuing.');}if(!response.ok){const error=Error(errors[result?.code]||(response.status===429?'Too many attempts. Wait before retrying.':'The service could not confirm this step. Keep the original private link and check the saved setup.'));error.code=result?.code;error.http=response.status;throw error;}return result;
  }finally{clearTimeout(timer);if(controller===abort)controller=null;}
 }
 async function sessionCheck(){
  const at=epoch;
  try{const response=await fetch('/api/me',{credentials:'same-origin',cache:'no-store',redirect:'error'});if(at!==epoch)return;if(response.status===200){invalidate('Sign out of the current store explicitly, then reopen the private invitation link.');$('sessionBlocked').hidden=false;$('missingLink').hidden=true;return;}if(response.status!==401)throw Error();unsigned=true;render();if(token&&!busy)status('Enter the email your invitation was issued to.');}
  catch{if(at!==epoch)return;unsigned=false;render();status('Your sign-in state could not be checked. Reconnect and return to this page before continuing.');}
 }
 function showComplete(result){
  if(!validResult(result))throw Error('The account reply could not be verified. Keep your original link and check the saved setup.');
  complete=true;uncertain=false;passwords();token='';$('createdStoreSlug').textContent=result.store.slug;$('createdOwnerEmail').textContent=result.owner_email;$('panelTitle').textContent='Welcome to your own workspace.';$('panelHelp').hidden=true;$('panelEyebrow').textContent='OWNER ACCOUNT CREATED';step('stepSecurity');render();status('');
 }
 async function inspectOrRecover(at){
  requestKey=await savedKey();if(at!==epoch)return;
  if(requestKey){const recovered=await api('/api/owner-invitations/status',{invitation_token:token,email,request_key:requestKey},at);if(at!==epoch)return;if(recovered?.found===true&&recovered.state==='completed'){showComplete(recovered.result);return;}if(recovered?.found!==false||recovered.state!=='not_completed'||recovered.result!==null)throw Error('The saved setup reply could not be verified. Keep the original link and contact the platform team.');}
  const v=await api('/api/owner-invitations/inspect',{invitation_token:token,email},at);if(at!==epoch)return;
  if(!v||v.protocol_version!==1||v.invitation_id!==invitationId||!safeStore(v.store)||!safeEmail(v.email)||v.email!==email||!Number.isFinite(Date.parse(v.expires_at))||v.signed_out_required!==true||v.public_signup_enabled!==false||v.delivery_configured!==false||!noSession(v))throw Error('This invitation reply did not match the private link. No account request has been sent.');
  inspected=v;uncertain=false;$('invitedStoreName').textContent=v.store.name;$('invitedStoreSlug').textContent=v.store.slug;$('invitedOwnerEmail').textContent=v.email;$('invitedExpires').textContent=new Date(v.expires_at).toLocaleString();$('panelTitle').textContent='This store will be yours.';$('panelHelp').textContent='Review the fixed invitation details, then choose your owner password.';step('stepAccount');render();status('');$('ownerInvitePassword').focus();
 }
 async function run(action){if(busy)return;const at=epoch;busy=true;render();status('Checking securely…');try{await action(at);}catch(error){if(at!==epoch)return;if(error.code==='SIGN_OUT_REQUIRED'){invalidate(errors.SIGN_OUT_REQUIRED);$('sessionBlocked').hidden=false;$('missingLink').hidden=true;}else status(error.name==='AbortError'?'The connection was interrupted. Keep your original private link and check the saved setup.':error.message);}finally{if(at===epoch){busy=false;render();}}}
 $('inspectOwnerInviteForm').addEventListener('submit',event=>{event.preventDefault();if(!token||!unsigned||busy)return;const raw=$('ownerInviteEmail').value;if(!asciiEmail(raw)){status('This invitation preview supports ASCII email addresses only. Internationalized addresses need a verified delivery path; contact the platform team without changing the invited mailbox.');return;}const normalized=raw.trim().toLowerCase();if(!safeEmail(normalized)){status('Enter the valid email address your invitation was issued to.');return;}email=normalized;run(inspectOrRecover);});
 $('acceptOwnerInviteForm').addEventListener('submit',event=>{event.preventDefault();if(!token||!unsigned||!inspected||busy)return;const password=$('ownerInvitePassword').value;if(password!==$('ownerInvitePasswordConfirm').value){status('The passwords do not match. Enter the same new password twice.');return;}if(password.length<12||password.length>128||!$('ownerStoreConfirmed').checked){status('Choose a 12–128 character password and confirm the store details.');return;}passwords();run(async at=>{requestKey=await savedKey(true);if(at!==epoch)return;uncertain=true;render();const body={invitation_token:token,email,password,request_key:requestKey,store_details_confirmed:true};const result=await api('/api/owner-invitations/accept',body,at);if(at===epoch)showComplete(result);});});
 $('checkOwnerSetup').addEventListener('click',()=>{if(token&&unsigned)run(inspectOrRecover);});
 $('changeOwnerEmail').addEventListener('click',()=>{if(busy)return;inspected=null;passwords();step('stepInvite');$('panelTitle').textContent='Let’s find your store.';$('panelHelp').textContent='Enter the email your private invitation was issued to.';render();status('');$('ownerInviteEmail').focus();});
 $('clearOwnerInvite').addEventListener('click',()=>invalidate('The private link has been cleared from this page. An account request already sent may still finish. Reopen the original private link and use the same email to check the saved setup.'));
 function authChanged(){invalidate('Sign-in changed in another window. Reopen the private invitation link after explicitly signing out of any store session.');$('sessionBlocked').hidden=false;$('missingLink').hidden=true;}
 addEventListener('storage',event=>{if(event.key==='ss-auth-epoch')authChanged();});
 try{channel=new BroadcastChannel('ss-auth-transition');channel.onmessage=event=>{if(event.data?.changed===true)authChanged();};}catch{/* Focus checks and the server also enforce signed-out acceptance. */}
 addEventListener('pagehide',()=>invalidate('For privacy, reopen the original private invitation link to continue.'));
 addEventListener('hashchange',()=>{const next=location.hash;history.replaceState(null,'',location.pathname);invalidate('The private link changed. Checking the new invitation context.');openFragment(next);});
 addEventListener('focus',()=>{if(token&&!busy)sessionCheck();});document.addEventListener('visibilitychange',()=>{if(!document.hidden&&token&&!busy)sessionCheck();});
 function openFragment(value){try{const params=new URLSearchParams(value.slice(1));if([...params.keys()].length!==2||params.getAll('owner_invite').length!==1||params.getAll('invitation_id').length!==1||!(/^[A-Za-z0-9_-]{43}$/).test(params.get('owner_invite')||'')||!UUID.test(params.get('invitation_id')||''))throw Error();token=params.get('owner_invite');invitationId=params.get('invitation_id');$('missingLink').hidden=true;$('sessionBlocked').hidden=true;render();sessionCheck();}catch{token='';invitationId='';$('missingLink').hidden=false;render();status('No valid private invitation was found. No account has been created.');}}
 openFragment(fragment);fragment='';
})();
