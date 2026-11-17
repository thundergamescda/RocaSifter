'use strict';
(() => {
  const panel=document.getElementById('passwordChangeSection');
  if (!panel||panel.dataset.bound==='true') return;
  panel.dataset.bound='true';
  const form=document.getElementById('passwordChangeForm');
  const current=document.getElementById('passwordChangeCurrent');
  const next=document.getElementById('passwordChangeNew');
  const confirm=document.getElementById('passwordChangeConfirm');
  const button=document.getElementById('passwordChangeSubmit');
  const status=document.getElementById('passwordChangeStatus');
  const controlled={
    CURRENT_PASSWORD_INVALID:{status:403,message:'The current password was not accepted. Check it and try again.',focus:current},
    PASSWORD_INPUT_INVALID:{status:400,message:'Enter your current password and a new password containing 15–128 characters.',focus:current},
    PASSWORD_UNCHANGED:{status:400,message:'Choose a new password different from your current password.',focus:next},
    PASSWORD_CHANGE_RATE_LIMITED:{status:429,message:'Too many attempts. Wait before trying again.',focus:current}
  };
  const uncertain='The password change could not be confirmed. Sign in with your new password first. If it is not accepted, try your previous password.';
  let boundMe=null,boundEpoch=-1,wasActive=false,serial=0,operation=null;

  function clearSecrets() {current.value='';next.value='';confirm.value='';}
  function view() {
    const me=typeof portalMe==='undefined'?null:portalMe;
    const epoch=typeof portalEpoch==='undefined'?-1:portalEpoch;
    const signedIn=!!me&&!document.getElementById('workspace').hidden&&!document.getElementById('accountSecurity').hidden;
    return {me,epoch,signedIn,active:signedIn&&!authTransitionBusy};
  }
  function synchronize() {
    const value=view();
    if (boundMe!==value.me||boundEpoch!==value.epoch) {
      ++serial;boundMe=value.me;boundEpoch=value.epoch;clearSecrets();status.textContent='';
    }
    if (!value.active&&wasActive) {
      clearSecrets();if (!operation) status.textContent='';
    }
    wasActive=value.active;panel.hidden=!value.signedIn;
    for (const field of [current,next,confirm,button]) field.disabled=!value.active||operation!==null;
    panel.setAttribute('aria-busy',operation?'true':'false');
  }
  function isCurrent(request) {
    return operation===request&&request.ticket===serial&&portalEpoch===request.epoch&&portalMe===request.me;
  }
  function characters(value) {
    let count=0;
    for (const character of value) {
      const point=character.codePointAt(0);
      if (point>=0xd800&&point<=0xdfff) return null;
      ++count;
    }
    return count;
  }
  function confirmed(value) {
    const keys=['password_changed','sign_in_required','all_sessions_revoked'];
    return value!==null&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).length===keys.length&&
      keys.every(key=>Object.hasOwn(value,key)&&value[key]===true);
  }
  function returnToSignIn(request,message) {
    if (!isCurrent(request)) return;
    // The service revokes sessions; this changes this view only, never cookies
    // or an ambient session that another tab may have established meanwhile.
    try {sessionStorage.setItem('ss-signed-out','1');} catch {}
    signedOutView();
    notifyAuthTransition();
    return {epoch:portalEpoch,me:portalMe,ticket:serial,target:document.getElementById('loginStatus'),
      message,focus:document.getElementById('password')};
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    const value=view();
    if (!value.active||operation) {synchronize();return;}
    const payload={current_password:current.value,new_password:next.value};
    let confirmation=confirm.value;
    clearSecrets();status.textContent='';
    const currentLength=characters(payload.current_password),newLength=characters(payload.new_password);
    let invalid='',focus=current;
    if (currentLength===null||currentLength<1||currentLength>128) invalid='Enter your current password using 1–128 characters.';
    else if (newLength===null||newLength<15||newLength>128) {invalid='Use 15–128 characters for your new password.';focus=next;}
    else if (payload.current_password===payload.new_password) {invalid='Choose a new password different from your current password.';focus=next;}
    else if (confirmation!==payload.new_password) {invalid='The new passwords do not match.';focus=confirm;}
    confirmation='';
    if (invalid) {payload.current_password='';payload.new_password='';status.textContent=invalid;focus.focus();return;}
    const request={ticket:++serial,epoch:value.epoch,me:value.me};
    operation=request;let locked=false,completion=null;
    try {
      if (!beginAuthTransition()) return;
      locked=true;
      if (!isCurrent(request)) return;
      status.textContent='Changing your password…';synchronize();
      const result=await api('/api/auth/password/change',payload);
      if (!isCurrent(request)) return;
      if (confirmed(result)) completion=returnToSignIn(request,'Password changed. All sessions for this store account ended. Sign in with your new password.');
      else completion=returnToSignIn(request,uncertain);
    } catch (error) {
      if (!isCurrent(request)) return;
      const known=typeof error.code==='string'&&Object.hasOwn(controlled,error.code)?controlled[error.code]:null;
      if (known&&error.status===known.status&&error.sign_in_required===false) {
        completion={epoch:request.epoch,me:request.me,ticket:request.ticket,target:status,message:known.message,focus:known.focus};
      } else if (error.code==='MFA_STEP_UP_REQUIRED'&&error.status===403&&error.sign_in_required===true) {
        completion=returnToSignIn(request,'Sign in again with a fresh two-step proof before changing your password.');
      } else completion=returnToSignIn(request,uncertain);
    } finally {
      payload.current_password='';payload.new_password='';
      if (portalEpoch===request.epoch&&portalMe===request.me&&serial===request.ticket) clearSecrets();
      if (operation===request) operation=null;
      if (locked) endAuthTransition();
      synchronize();
      if (completion&&completion.epoch===portalEpoch&&completion.me===portalMe&&completion.ticket===serial) {
        completion.target.textContent=completion.message;completion.focus.focus();
      }
    }
  });
  document.addEventListener('ss-portal-auth-context',synchronize);
  synchronize();
})();
