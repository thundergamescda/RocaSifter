'use strict';
let csrfToken='',portalEpoch=0,portalMe=null,authTransitionBusy=false;
const byId=id=>document.getElementById(id);
async function api(path,data){
 const requestEpoch=portalEpoch;
 const options={credentials:'same-origin',headers:{},signal:AbortSignal.timeout(20000)};
 if(data!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.headers['X-SS-CSRF']=csrfToken;options.body=JSON.stringify(data)}
 let response,result;
 try{response=await fetch(path,options);result=await response.json()}
 catch{throw Error('The service did not respond. Check your connection, then retry. If account creation may have finished, try signing in first.')}
 if(!response.ok){
  if(response.status===401&&portalMe&&requestEpoch===portalEpoch){sessionStorage.setItem('ss-signed-out','1');signedOutView();byId('loginStatus').textContent='Your session ended. Sign in again.'}
  const error=Error(result.error||'The service could not complete the request.');error.status=response.status;throw error;
 }
 return result;
}
function clearInvitation(){byId('staffInvitationLink').value='';byId('staffInvitationResult').hidden=true}
function notifyAuthTransition(){
 try{localStorage.setItem('ss-auth-epoch',crypto.randomUUID())}catch{}
 try{const channel=new BroadcastChannel('ss-auth-transition');channel.postMessage({changed:true});channel.close()}catch{}
}
function beginAuthTransition(){
 if(authTransitionBusy)return false;
 authTransitionBusy=true;
 notifyAuthTransition();
 for(const id of ['signin','workspace','invitation','pilotSetup','mfaSignin','accountSecurity'])byId(id).inert=true;
 return true;
}
function endAuthTransition(){
 authTransitionBusy=false;
 for(const id of ['signin','workspace','invitation','pilotSetup','mfaSignin','accountSecurity'])byId(id).inert=false;
}
function signedOutView(){
 ++portalEpoch;portalMe=null;csrfToken='';clearInvitation();byId('staffList').replaceChildren();byId('staffPanel').hidden=true;
 if(typeof clearMfaSecrets==='function')clearMfaSecrets();byId('mfaSignin').hidden=true;byId('accountSecurity').hidden=true;
 byId('storeName').textContent='';byId('workspace').hidden=true;byId('signin').hidden=false;
 for(const input of document.querySelectorAll('input[type="password"]'))input.value='';
 byId('inviteStaffForm').reset();byId('staffStatus').textContent='';
 byId('inviteStaffForm').querySelector('button').disabled=false;byId('refreshStaff').disabled=false;
}
function showAccountForm(target){
 if(authTransitionBusy)return;
 if(typeof hasMfaChallenge==='function'&&hasMfaChallenge()){byId('mfaSigninStatus').textContent='Cancel verification before switching accounts.';return}
 ++portalEpoch;
 if(typeof clearMfaSecrets==='function')clearMfaSecrets();byId('mfaSignin').hidden=true;byId('accountSecurity').hidden=true;
 byId('workspace').hidden=true;
 for(const id of ['signin','invitation','pilotSetup'])byId(id).hidden=id!==target;
 for(const id of ['acceptInviteForm','pilotSetupForm']){byId(id).reset();byId(id).querySelector('button').disabled=false}
 byId('inviteStatus').textContent=byId('setupStatus').textContent='';
 byId(target).querySelector('input')?.focus();
}
async function workspace(epoch=portalEpoch){
 const me=await api('/api/me');if(epoch!==portalEpoch)return;
 portalMe=me;csrfToken=me.csrf_token;
 if(typeof updateMfaView==='function')updateMfaView(me);
 byId('storeName').textContent=me.store;byId('signin').hidden=true;byId('invitation').hidden=true;byId('pilotSetup').hidden=true;byId('workspace').hidden=false;
 byId('staffPanel').hidden=me.role!=='owner';
 const square=await api('/api/square/status');if(epoch!==portalEpoch)return;
 byId('squareStatus').textContent=square.message||(square.connected?'Connected to Square '+square.environment+'. Checkout is still in pilot testing.':square.oauth_configured?'The store owner can authorize this store’s Square account.':'Square application setup is required before store authorization can begin.');
 byId('connectSquare').disabled=me.role!=='owner'||!square.oauth_configured||square.connection_status==='environment_mismatch';
 byId('connectSquare').textContent=square.connected?'Reconnect Square':'Connect Square';
 if(me.role==='owner')await refreshStaff(epoch);
}
async function refreshStaff(epoch=portalEpoch){
 if(epoch!==portalEpoch||portalMe?.role!=='owner')return;
 byId('refreshStaff').disabled=true;
 try{
  const result=await api('/api/staff');if(epoch!==portalEpoch)return;
  byId('staffList').replaceChildren();
  for(const staff of result.staff){
   const row=document.createElement('div');row.className='staff-row';
   const description=document.createElement('div'),name=document.createElement('strong'),status=document.createElement('small');
   name.textContent=staff.email;status.textContent=staff.role+' · '+(staff.enabled?'Active':'Disabled');description.append(name,status);row.append(description);
   if(staff.enabled&&staff.role!=='owner'){
    const button=document.createElement('button');button.type='button';button.className='quiet';button.textContent='Disable access';
    button.onclick=async()=>{
     if(epoch!==portalEpoch||!confirm('Disable access for '+staff.email+'? Their active sessions will be revoked.'))return;
     button.disabled=true;
     try{await api('/api/staff/'+encodeURIComponent(staff.id)+'/disable',{});if(epoch===portalEpoch){await refreshStaff(epoch);byId('staffStatus').textContent='Access disabled. Their active sessions have been revoked.'}}
     catch(error){if(epoch===portalEpoch){byId('staffStatus').textContent=error.message;button.disabled=false}}
    };row.append(button);
   }
   byId('staffList').append(row);
  }
 }catch(error){if(epoch===portalEpoch)byId('staffStatus').textContent=error.message}
 finally{if(epoch===portalEpoch)byId('refreshStaff').disabled=false}
}
byId('refreshStaff').onclick=()=>refreshStaff();
byId('loginForm').onsubmit=async event=>{
 event.preventDefault();if(!beginAuthTransition())return;
 const button=event.target.querySelector('button'),epoch=++portalEpoch;button.disabled=true;byId('loginStatus').textContent='Signing in…';
 try{const result=await api('/api/auth/login',{store_slug:byId('storeSlug').value,email:byId('email').value,password:byId('password').value,mfa_supported:true});if(epoch!==portalEpoch)return;byId('password').value='';if(result.mfa_required){beginMfaChallenge(result);notifyAuthTransition();return}csrfToken=result.csrf_token;sessionStorage.removeItem('ss-signed-out');await workspace(epoch);notifyAuthTransition()}
 catch(error){if(epoch===portalEpoch)byId('loginStatus').textContent=error.message}
 finally{byId('password').value='';button.disabled=false;endAuthTransition()}
};
byId('connectSquare').onclick=async()=>{
 const epoch=portalEpoch;byId('connectSquare').disabled=true;
 try{const result=await api('/api/square/connect',{});if(epoch!==portalEpoch)return;const url=new URL(result.authorization_url);if(!['connect.squareup.com','connect.squareupsandbox.com'].includes(url.hostname)||url.protocol!=='https:'||url.username||url.password)throw Error('Authorization destination was rejected.');window.location.assign(url.href)}
 catch(error){if(epoch===portalEpoch){byId('workspaceStatus').textContent=error.message;byId('connectSquare').disabled=portalMe?.role!=='owner'}}
};
byId('signOut').onclick=async()=>{
 if(!beginAuthTransition())return;
 const request=api('/api/auth/logout',{});sessionStorage.setItem('ss-signed-out','1');signedOutView();
 const epoch=portalEpoch;
 try{await request;notifyAuthTransition();if(epoch===portalEpoch)byId('loginStatus').textContent='Signed out.'}
 catch{if(epoch===portalEpoch)byId('loginStatus').textContent='Signed out of this view. The service could not confirm session revocation. Close this browser until you can sign in and sign out online; sessions expire within eight hours.'}
 finally{endAuthTransition()}
};
byId('showInvitation').onclick=()=>showAccountForm('invitation');
byId('showPilotSetup').onclick=()=>showAccountForm('pilotSetup');
for(const button of document.querySelectorAll('[data-return-signin]'))button.onclick=()=>showAccountForm('signin');
byId('acceptInviteForm').onsubmit=async event=>{
 event.preventDefault();const button=event.target.querySelector('button'),epoch=++portalEpoch;button.disabled=true;byId('inviteStatus').textContent='';
 try{
  if(byId('invitePassword').value!==byId('invitePasswordConfirm').value)throw Error('The passwords do not match.');
  const store=byId('inviteStore').value.trim(),email=byId('inviteEmail').value.trim();
  await api('/api/staff/accept-invite',{invite_token:byId('inviteCode').value,email,password:byId('invitePassword').value});
  if(epoch!==portalEpoch)return;
  showAccountForm('signin');byId('storeSlug').value=store;byId('email').value=email;byId('loginStatus').textContent='Your staff account is ready. Sign in with the password you just created.';byId('password').focus();
 }catch(error){if(epoch===portalEpoch)byId('inviteStatus').textContent=error.message}
 finally{if(epoch===portalEpoch){byId('invitePassword').value=byId('invitePasswordConfirm').value='';button.disabled=false}}
};
byId('pilotSetupForm').onsubmit=async event=>{
 event.preventDefault();const button=event.target.querySelector('button'),epoch=++portalEpoch;button.disabled=true;byId('setupStatus').textContent='';
 try{
  if(byId('setupPassword').value!==byId('setupPasswordConfirm').value)throw Error('The passwords do not match.');
  const store=byId('setupStoreSlug').value.trim(),email=byId('setupEmail').value.trim();
  await api('/api/pilot/bootstrap',{bootstrap_token:byId('setupAuthorization').value,store_slug:store,store_name:byId('setupStoreName').value,email,password:byId('setupPassword').value});
  if(epoch!==portalEpoch)return;
  showAccountForm('signin');byId('storeSlug').value=store;byId('email').value=email;byId('loginStatus').textContent='Pilot owner created. Sign in to invite staff and connect Square when application setup is ready.';byId('password').focus();
 }catch(error){if(epoch===portalEpoch)byId('setupStatus').textContent=error.message}
 finally{if(epoch===portalEpoch){byId('setupPassword').value=byId('setupPasswordConfirm').value='';byId('setupAuthorization').value='';button.disabled=false}}
};
byId('inviteStaffForm').onsubmit=async event=>{
 event.preventDefault();if(portalMe?.role!=='owner')return;
 const epoch=portalEpoch,button=event.target.querySelector('button');button.disabled=true;clearInvitation();byId('staffStatus').textContent='';
 try{
  const result=await api('/api/staff/invites',{email:byId('staffInviteEmail').value,role:byId('staffInviteRole').value});if(epoch!==portalEpoch)return;
  const link=new URL('/',location.origin);link.hash=new URLSearchParams({invite:result.invite_token,store:portalMe.store}).toString();
  byId('staffInvitationLink').value=link.href;byId('staffInvitationResult').hidden=false;byId('staffStatus').textContent='Invitation created. Share it privately; no message was sent.';
 }catch(error){if(epoch===portalEpoch)byId('staffStatus').textContent=error.message}
 finally{if(epoch===portalEpoch)button.disabled=false}
};
byId('copyStaffInvitation').onclick=async()=>{
 try{await navigator.clipboard.writeText(byId('staffInvitationLink').value);byId('staffStatus').textContent='Invitation link copied. Share it privately with the intended person.'}
 catch{byId('staffInvitationLink').select();byId('staffStatus').textContent='Select and copy the invitation link above.'}
};
function openAccountFragment(){
 const fragment=new URLSearchParams(location.hash.slice(1)),token=fragment.get('invite'),setup=location.hash==='#pilot-setup';
 if(!token&&!setup)return false;
 history.replaceState(null,'',location.pathname);
 if(authTransitionBusy){byId(portalMe?'workspaceStatus':'loginStatus').textContent='Wait for sign-in or sign-out to finish, then open the private link again.';return true}
 if(typeof hasMfaChallenge==='function'&&hasMfaChallenge()){byId('mfaSigninStatus').textContent='Cancel verification before opening another account link.';return true}
 if(portalMe){byId('workspaceStatus').textContent='Sign out before opening another account invitation or initial setup. Open the private link again after signing out.';return true}
 if(token){showAccountForm('invitation');byId('inviteCode').value=token;byId('inviteStore').value=fragment.get('store')||''}
 else showAccountForm('pilotSetup');
 return true;
}
window.addEventListener('hashchange',openAccountFragment);
if(!openAccountFragment()&&!sessionStorage.getItem('ss-signed-out'))workspace().catch(error=>{if(error.status!==401)byId('loginStatus').textContent=error.message});
