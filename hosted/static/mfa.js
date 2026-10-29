'use strict';
let mfaChallenge=null,mfaEnrollmentToken=null,mfaActionBusy=false;
function hasMfaChallenge(){return mfaChallenge!==null}
function clearMfaSecrets(){
 mfaChallenge=null;mfaEnrollmentToken=null;
 for(const id of ['mfaSigninCode','mfaSetupKey','mfaConfirmCode','mfaEnrollPassword','mfaManagePassword','mfaManageCode'])byId(id).value='';
 byId('mfaRecoveryCodes').textContent='';byId('mfaRecoveryResult').hidden=true;byId('mfaEnrollment').hidden=true;
}
function updateMfaView(me){
 byId('mfaSignin').hidden=true;byId('accountSecurity').hidden=false;
 byId('mfaEnrollForm').hidden=!!me.mfa_enabled;byId('mfaManageForm').hidden=!me.mfa_enabled;
 byId('mfaSecuritySummary').textContent=me.mfa_enabled?'An authenticator protects this account. Keep unused recovery codes in a private place.':'You can add an authenticator to protect this account. Setup is optional in this development pilot.';
}
function beginMfaChallenge(result){
 clearMfaSecrets();mfaChallenge={token:result.challenge_token,deadline:Date.now()+result.expires_in*1000};
 csrfToken='';portalMe=null;
 for(const id of ['signin','workspace','invitation','pilotSetup','accountSecurity'])byId(id).hidden=true;
 byId('mfaSignin').hidden=false;byId('mfaSigninStatus').textContent='';byId('mfaSigninCode').focus();
}
byId('mfaSigninForm').onsubmit=async event=>{
 event.preventDefault();if(!mfaChallenge||!beginAuthTransition())return;
 const challenge=mfaChallenge,epoch=portalEpoch,button=event.target.querySelector('button');button.disabled=true;
 try{
  if(Date.now()>=challenge.deadline)throw Error('Verification expired. Cancel and start sign-in again.');
  const result=await api('/api/auth/mfa/challenge',{challenge_token:challenge.token,code:byId('mfaSigninCode').value});
  if(epoch!==portalEpoch||mfaChallenge!==challenge)return;
  clearMfaSecrets();csrfToken=result.csrf_token;sessionStorage.removeItem('ss-signed-out');await workspace(epoch);notifyAuthTransition();
 }catch(error){if(epoch===portalEpoch)byId('mfaSigninStatus').textContent=error.message}
 finally{byId('mfaSigninCode').value='';button.disabled=false;endAuthTransition()}
};
byId('cancelMfaSignin').onclick=async()=>{
 if(!mfaChallenge||!beginAuthTransition())return;
 const challenge=mfaChallenge;
 try{await api('/api/auth/mfa/cancel',{challenge_token:challenge.token});clearMfaSecrets();sessionStorage.setItem('ss-signed-out','1');signedOutView();byId('loginStatus').textContent='Verification cancelled. You can sign in again.';notifyAuthTransition()}
 catch(error){byId('mfaSigninStatus').textContent='Cancellation could not be confirmed. Retry cancellation before switching accounts. '+error.message}
 finally{endAuthTransition()}
};
function finishMfaAction(){mfaActionBusy=false;endAuthTransition()}
function startMfaAction(){if(mfaActionBusy||!portalMe||!beginAuthTransition())return false;mfaActionBusy=true;byId('mfaSecurityStatus').textContent='';return true}
function showRecoveryCodes(result){
 mfaEnrollmentToken=null;byId('mfaSetupKey').value='';byId('mfaEnrollment').hidden=true;
 byId('mfaRecoveryCodes').textContent=result.recovery_codes.join('\n');byId('mfaRecoveryResult').hidden=false;
 portalMe.mfa_enabled=true;portalMe.mfa_authenticated=true;updateMfaView(portalMe);
}
byId('mfaEnrollForm').onsubmit=async event=>{
 event.preventDefault();if(!startMfaAction())return;const epoch=portalEpoch;
 try{const result=await api('/api/auth/mfa/enroll',{password:byId('mfaEnrollPassword').value});if(epoch!==portalEpoch)return;mfaEnrollmentToken=result.enrollment_token;byId('mfaSetupKey').value=result.secret;byId('mfaEnrollment').hidden=false;byId('mfaConfirmCode').focus()}
 catch(error){if(epoch===portalEpoch)byId('mfaSecurityStatus').textContent=error.message}
 finally{byId('mfaEnrollPassword').value='';finishMfaAction()}
};
byId('mfaConfirmForm').onsubmit=async event=>{
 event.preventDefault();if(!mfaEnrollmentToken||!startMfaAction())return;const epoch=portalEpoch;
 try{const result=await api('/api/auth/mfa/confirm',{enrollment_token:mfaEnrollmentToken,code:byId('mfaConfirmCode').value});if(epoch!==portalEpoch)return;showRecoveryCodes(result);byId('mfaSecurityStatus').textContent='Authenticator enabled. Other sessions have been signed out.';notifyAuthTransition()}
 catch(error){if(epoch===portalEpoch)byId('mfaSecurityStatus').textContent=error.message}
 finally{byId('mfaConfirmCode').value='';finishMfaAction()}
};
byId('cancelMfaEnrollment').onclick=async()=>{
 if(!startMfaAction())return;
 try{await api('/api/auth/mfa/enroll/cancel',{});byId('mfaSecurityStatus').textContent='Pending authenticator setup cancelled.'}
 catch{byId('mfaSecurityStatus').textContent='Pending setup cancellation was not confirmed. It expires after ten minutes; starting setup again replaces it.'}
 finally{mfaEnrollmentToken=null;byId('mfaSetupKey').value='';byId('mfaConfirmCode').value='';byId('mfaEnrollment').hidden=true;finishMfaAction()}
};
byId('dismissMfaRecovery').onclick=()=>{byId('mfaRecoveryCodes').textContent='';byId('mfaRecoveryResult').hidden=true};
async function changeMfa(disable){
 if(!startMfaAction())return;const epoch=portalEpoch;
 try{
  const result=await api('/api/auth/mfa/'+(disable?'disable':'recovery-codes'),{password:byId('mfaManagePassword').value,code:byId('mfaManageCode').value});
  if(epoch!==portalEpoch)return;
  if(disable){notifyAuthTransition();sessionStorage.setItem('ss-signed-out','1');signedOutView();byId('loginStatus').textContent='Authenticator disabled. All sessions were revoked. Sign in again.'}
  else{showRecoveryCodes(result);byId('mfaSecurityStatus').textContent='Previous recovery codes are no longer valid.'}
 }catch(error){if(epoch===portalEpoch)byId('mfaSecurityStatus').textContent=error.message}
 finally{byId('mfaManagePassword').value=byId('mfaManageCode').value='';finishMfaAction()}
}
byId('mfaManageForm').onsubmit=event=>{event.preventDefault();changeMfa(false)};
byId('disableMfa').onclick=()=>{if(byId('mfaManageForm').reportValidity()&&confirm('Disable the authenticator for this account and revoke all active sessions?'))changeMfa(true)};
