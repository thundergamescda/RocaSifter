'use strict';
(() => {
 const get=id=>document.getElementById(id),form=get('staffRecoveryForm'),status=get('recoveryStatus');
 let active=false,busy=false,generation=0;
 const fragment=new URLSearchParams(location.hash.slice(1)),token=fragment.get('recovery');
 // Fragments never reach HTTP. Remove before any request and keep no browser
 // storage, query parameter, telemetry, HTML rendering or cookie token copy.
 history.replaceState(null,'',location.pathname);
 if(typeof token==='string'&&/^[A-Za-z0-9_-]{43}$/.test(token))get('recoveryCode').value=token;
 function clear(){for(const id of ['recoveryCode','recoveryEmail','recoveryPassword','recoveryConfirm'])get(id).value=''}
 function block(){++generation;active=false;clear();form.hidden=true;get('recoveryBlocked').hidden=false;status.textContent='An account session changed. Reopen recovery in a signed-out browser.'}
 window.addEventListener('storage',event=>{if(event.key==='ss-auth-epoch')block()});
 try{const channel=new BroadcastChannel('ss-auth-transition');channel.onmessage=block}catch{}
 window.addEventListener('pagehide',()=>{++generation;active=false;clear()});
 function characters(value){let length=0;for(const character of value){const point=character.codePointAt(0);if(point>=0xd800&&point<=0xdfff)return 0;++length}return length}
 form.addEventListener('submit',async event=>{
  event.preventDefault();if(!active||busy)return;
  const payload={recovery_token:get('recoveryCode').value,email:get('recoveryEmail').value,new_password:get('recoveryPassword').value};
  const matches=get('recoveryConfirm').value===payload.new_password;
  get('recoveryPassword').value=get('recoveryConfirm').value='';
  const length=characters(payload.new_password);
  if(!matches||length<15||length>128){payload.new_password='';status.textContent=matches?'Use a password of 15–128 characters.':'The new passwords do not match.';return}
  const requestGeneration=generation;busy=true;get('recoverySubmit').disabled=true;status.textContent='Resetting password…';
  try{
   const response=await fetch('/api/staff/password-recovery/redeem',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:AbortSignal.timeout(20000)});
   let result;try{result=await response.json()}catch{}
   if(requestGeneration!==generation)return;
   if(response.status===409){block();return}
   if(!response.ok){
    if(response.status===429){status.textContent='Too many recovery attempts. Wait before trying again.';return}
    if(response.status>=500)throw Error();
    clear();status.textContent='Recovery was not accepted. Ask your store owner for a new private link.';return;
   }
   if(!result||Object.keys(result).length!==4||!['password_changed','sign_in_required','all_sessions_revoked','mfa_preserved'].every(key=>result[key]===true))throw Error();
   active=false;clear();form.hidden=true;status.textContent='Password reset. Sign in normally using your new password and your existing authenticator or unused MFA recovery code if enrolled.';
  }catch{
   if(requestGeneration!==generation)return;
   active=false;clear();form.hidden=true;status.textContent='The result could not be confirmed. Try signing in with your new password first, then your previous password. Ask your owner for a new link only if recovery is still needed.';
  }finally{payload.new_password='';payload.recovery_token='';if(requestGeneration===generation){busy=false;get('recoverySubmit').disabled=false}}
 });
 const initialGeneration=generation;
 fetch('/api/me',{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(20000)}).then(response=>{
  if(initialGeneration!==generation)return;
  if(response.status===401){active=true;form.hidden=false;status.textContent='Enter your own staff email and choose a new password.'}
  else if(response.ok)block();
  else{clear();status.textContent='Session state could not be checked. Reopen this page when the service is available.'}
 }).catch(()=>{if(initialGeneration===generation){clear();status.textContent='Session state could not be checked. Reopen this page when the service is available.'}});
})();
