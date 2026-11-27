/* Read-only guide. Root supplies the authenticated load/action adapters. */
(()=>{'use strict';
 const ORIGIN='https://stock-and-sleeve-api.onrender.com';
 const URLS=Object.freeze({owner_invite:ORIGIN+'/owner-invite',security:ORIGIN+'/',square_receiving:ORIGIN+'/receiving/catalog'});
 const ACTIONS=new Set(['sign_in','owner_invite','security','square_receiving','review_receiving','refresh']);
 const SECRET=new Set(['token','accesstoken','refreshtoken','csrftoken','challengetoken','password','authorization','cookie','setcookie','clientsecret','squaretoken','bearertoken','sessiontoken']);
 const reasons=new Set(['SIGN_IN_REQUIRED','LEGACY_PROFILE','MFA_ENROLLMENT_REQUIRED','OWNER_ACTION_REQUIRED','SQUARE_SETUP_UNAVAILABLE','SQUARE_NOT_CONNECTED','SQUARE_RECONNECT_REQUIRED','RECEIVING_AUTHORIZATION_REQUIRED','RECEIVING_DISABLED','RECEIVING_REVIEW_REQUIRED']);
 const bool=v=>typeof v==='boolean',nullableBool=v=>v===null||bool(v);
 function keys(v,names){return v&&typeof v==='object'&&!Array.isArray(v)&&Object.keys(v).sort().join(',')===names.slice().sort().join(',');}
 function checkedRenewal(q){
  const v=q.renewal;if(v===undefined||v===null)return null;
  if(!keys(v,['age_status','due','stale','expired','last_obtained_at','application_binding','automatic_renewal_active'])||
   !['fresh','due','stale','unknown'].includes(v.age_status)||!['matched','unknown','mismatch'].includes(v.application_binding)||
   ![v.due,v.stale,v.expired].every(bool)||v.automatic_renewal_active!==false||
   v.stale!==(v.age_status==='stale')||(['due','stale'].includes(v.age_status)&&!v.due)||(v.expired&&!v.due)||
   (v.age_status==='unknown')!==(v.last_obtained_at===null)||
   (v.last_obtained_at!==null&&(typeof v.last_obtained_at!=='string'||v.last_obtained_at.length>64||
    !/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})$/.test(v.last_obtained_at)||!Number.isFinite(Date.parse(v.last_obtained_at))))||
   (v.application_binding==='mismatch'&&q.authorizationSaved===true)||(q.state==='application_mismatch'&&v.application_binding!=='mismatch')||
   ['unknown','access_restricted'].includes(q.state))throw Error('invalid');
  return v;
 }
 function renewalText(q){
  if(!q.state.endsWith('_authorization_saved'))return '';
  const v=q.renewal;
  if(!v)return 'Authorization renewal status is unavailable. Refresh status or ask your store owner to review Square receiving access.';
  let message='Authorization was updated recently.';
  if(v.expired)message='The saved access token has expired. Renewal must succeed before synchronization can continue.';
  else if(v.application_binding==='unknown'||v.age_status==='unknown')message='The previous authorization update is unknown. Renewal is needed before synchronization.';
  else if(v.stale)message='Authorization renewal is overdue.';
  else if(v.due)message='Authorization renewal is due.';
  return message+' Automatic renewal is not active in this pilot. Refresh status after receiving review, or ask your store owner to review Square receiving access.';
 }
 function clean(raw){
  const text=JSON.stringify(raw);if(!text||text.length>65536)throw Error('invalid');const v=JSON.parse(text),stack=[v];let count=0;
  while(stack.length){const n=stack.pop();if(++count>4096)throw Error('invalid');if(n&&typeof n==='object'){for(const [k,x]of Object.entries(n)){if(SECRET.has(k.toLowerCase().replace(/[^a-z]/g,'')))throw Error('private');stack.push(x);}}}
  if(!keys(v,['protocolVersion','sessionId','profile','account','security','square','receiving','checkoutEnabled','commerceReady','reasonCodes'])||v.protocolVersion!==1||v.checkoutEnabled!==false||v.commerceReady!==false)throw Error('invalid');
  const p=v.profile,a=v.account,s=v.security,q=v.square,r=v.receiving;
  if(!keys(p,['profileVersion','profileMode','localCreditEnabled','legacySquareEntryEnabled','requiredReceivingRoute','storeBound'])||p.profileVersion!==1||!['hosted','legacy_compatibility'].includes(p.profileMode)||!bool(p.storeBound))throw Error('invalid');
  const hosted=p.profileMode==='hosted';if(p.localCreditEnabled!==!hosted||p.legacySquareEntryEnabled!==!hosted||p.requiredReceivingRoute!==(hosted?'hosted':'legacy_compatible'))throw Error('invalid');
  if(!keys(a,['state','tenantId','storeSlug','role'])||!['sign_in_required','connected','legacy_compatibility'].includes(a.state))throw Error('invalid');
  const connected=a.state==='connected';if(connected){if(!p.storeBound||!hosted||!(/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/).test(a.tenantId)||!(/^[a-z0-9][a-z0-9-]{2,49}$/).test(a.storeSlug)||!['owner','manager','buyer','cashier'].includes(a.role)||!(/^[a-f0-9]{32}$/).test(v.sessionId))throw Error('invalid');}
  else if(a.tenantId!==null||a.storeSlug!==null||a.role!==null||v.sessionId!==null||(a.state==='legacy_compatibility')===hosted)throw Error('invalid');
  if(!keys(s,['state','mfaEnrolled','mfaAuthenticated','verificationRequiredForNewWrites'])||!['unknown','enrollment_required','enrolled'].includes(s.state)||!nullableBool(s.mfaEnrolled)||!nullableBool(s.mfaAuthenticated)||s.verificationRequiredForNewWrites!==true||(s.mfaAuthenticated===true&&s.mfaEnrolled!==true))throw Error('invalid');
  if(connected&&(s.mfaEnrolled===null||s.mfaAuthenticated===null||s.state!==(s.mfaEnrolled?'enrolled':'enrollment_required')))throw Error('invalid');
  const squareKeys=['state','oauthConfigured','environment','requestedProfile','authorizationSaved','grantsVerified'];if(Object.hasOwn(q,'renewal'))squareKeys.push('renewal');
  if(!keys(q,squareKeys)||!['unknown','setup_unavailable','not_connected','revoked','revocation_pending','environment_mismatch','application_mismatch','standard_authorization_saved','receiving_authorization_saved','access_restricted'].includes(q.state)||!nullableBool(q.oauthConfigured)||!nullableBool(q.authorizationSaved)||![null,'production','sandbox'].includes(q.environment)||![null,'standard','receiving'].includes(q.requestedProfile)||q.grantsVerified!==false)throw Error('invalid');
  if(['revocation_pending','application_mismatch'].includes(q.state)&&(q.authorizationSaved!==false||q.oauthConfigured!==true||q.environment===null||q.requestedProfile===null))throw Error('invalid');
  if(q.state.endsWith('_authorization_saved')&&(q.authorizationSaved!==true||q.oauthConfigured!==true||q.requestedProfile!==(q.state.startsWith('standard')?'standard':'receiving')))throw Error('invalid');
  checkedRenewal(q);
  if(!keys(r,['state','serverEnabled','storeEnabled','newPriceEnabled','locationSelection','locationSelectionRequired','selectedLocation'])||!['unknown','access_restricted','receiving_disabled','review_required'].includes(r.state)||![r.serverEnabled,r.storeEnabled,r.newPriceEnabled].every(nullableBool)||r.locationSelection!=='per_plan'||r.locationSelectionRequired!==true||r.selectedLocation!==null)throw Error('invalid');
  if(r.state==='review_required'&&(r.serverEnabled!==true||r.storeEnabled!==true)||r.state==='receiving_disabled'&&(r.serverEnabled===null||r.storeEnabled===null||r.serverEnabled&&r.storeEnabled)||r.newPriceEnabled===true&&(r.serverEnabled!==true||r.storeEnabled!==true))throw Error('invalid');
  if(!Array.isArray(v.reasonCodes)||v.reasonCodes.length>12||v.reasonCodes.some(x=>!reasons.has(x))||new Set(v.reasonCodes).size!==v.reasonCodes.length)throw Error('invalid');
  if(!connected&&(s.state!=='unknown'||s.mfaEnrolled!==null||s.mfaAuthenticated!==null||q.state!=='unknown'||q.oauthConfigured!==null||q.environment!==null||q.requestedProfile!==null||q.authorizationSaved!==null||r.state!=='unknown'||r.serverEnabled!==null||r.storeEnabled!==null||r.newPriceEnabled!==null))throw Error('invalid');
  return v;
 }
 function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
 function create(container,{loadSnapshot,onAction}={}){
  if(!(container instanceof Element)||typeof loadSnapshot!=='function'||typeof onAction!=='function')throw TypeError('StoreSetup needs a container and load/action adapters.');
  let epoch=0,abort=null,destroyed=false;container.classList.add('store-setup');
  function message(text,error=false){container.replaceChildren();container.removeAttribute('aria-busy');const n=el('p','store-setup-message'+(error?' store-setup-error':''),text);n.setAttribute('role','status');container.append(n);}
  function action(parent,name,label,disabled=false){
   if(!ACTIONS.has(name))throw Error('Unsupported setup action');const url=URLS[name],n=el(url?'a':'button','store-setup-action',label);
   if(url){n.href=url;n.target='_blank';n.rel='noopener noreferrer';}else n.type='button';n.dataset.setupAction=name;
   if(disabled){n.setAttribute('aria-disabled','true');if(!url)n.disabled=true;}
   n.addEventListener('click',event=>{event.preventDefault();if(disabled||destroyed)return;try{Promise.resolve(onAction(name)).catch(()=>{if(!destroyed)message('That setup step could not open. Refresh and try again. No stock or credit changed.',true);});}catch{message('That setup step could not open. Refresh and try again. No stock or credit changed.',true);}});
   parent.append(n);return n;
  }
  function render(v){
   container.replaceChildren();container.removeAttribute('aria-busy');const heading=el('div','store-setup-heading');const title=el('div');title.append(el('p','store-setup-kicker','YOUR STORE CONNECTION'),el('h2','',v.profile.profileMode==='hosted'?'Set up your connected store.':'Existing profile, preserved.'));heading.append(title);action(heading,'refresh','Refresh status');container.append(heading);
   if(v.profile.profileMode!=='hosted'){container.append(el('p','store-setup-note','This profile keeps its existing workflows and saved history. Store setup does not convert its balances, credentials or pending actions.'));return;}
   const cards=el('div','store-setup-grid');container.append(cards);
   function card(number,title,status,detail,done=false){const n=el('section','store-setup-card');const top=el('div','store-setup-card-top');top.append(el('span','store-setup-step'+(done?' is-complete':''),done?'✓':String(number).padStart(2,'0')),el('span','store-setup-state',status));n.append(top,el('h3','',title),el('p','store-setup-detail',detail));cards.append(n);return n;}
   const connected=v.account.state==='connected',owner=v.account.role==='owner',manager=['owner','manager'].includes(v.account.role);
   let n=card(1,'Connect your store',connected?'Signed in':'Sign-in needed',connected?'Signed in to '+v.account.storeSlug+'. Your saved work belongs to this store profile.':'Sign in with your invited staff account. Opening a new store requires a private owner invitation.',connected);
   action(n,'sign_in',connected?'View shared workspace':'Sign in to store');if(!connected)action(n,'owner_invite','Set up an invited owner account');
   n=card(2,'Protect your sign-in',v.security.mfaEnrolled===true?'Authenticator enrolled':connected?'Enrollment needed':'After sign-in',v.security.mfaEnrolled===true?'Your authenticator is enrolled. The server still checks recent verification before each new trade or receiving write.':'Set up an authenticator in your account security page. Enrollment and a recent verification are separate requirements.',v.security.mfaEnrolled===true);
   if(connected)action(n,'security','Open account security');
   const squareText={unknown:['After sign-in','Square status will be checked after you connect your store.'],setup_unavailable:['Setup unavailable','The hosted Square application needs configuration. Keep your saved batches and ask the platform team to review the connection.'],not_connected:['Not connected','The owner must explicitly review Square receiving access. No local access token is needed.'],revoked:['Reconnect needed','Square authorization was revoked. The owner must review receiving access again.'],revocation_pending:['Access paused','Square sent an authorization notice. Your saved work is preserved. The owner must start a new Square connection before access can resume.'],environment_mismatch:['Connection needs review','The saved Square environment differs from this service. Review the existing connection before continuing.'],application_mismatch:['Reconnect Square','This authorization belongs to a different Square application. The owner must reconnect through this service before synchronization or receiving can continue.'],standard_authorization_saved:['Read access saved','The standard connection does not authorize receiving writes. The owner must review the additional receiving permissions.'],receiving_authorization_saved:['Authorization saved','Receiving authorization is saved. Actual permissions and provider identity are checked during each receiving review.'],access_restricted:['Owner action needed','Your owner can review this store’s Square connection.']};
   const q=squareText[v.square.state];n=card(3,'Review Square receiving access',q[0],q[1]);
   const renewal=renewalText(v.square);if(renewal){const note=el('p','store-setup-note',renewal);note.setAttribute('role','status');n.append(note);}
   if(connected&&owner&&v.square.oauthConfigured===true)action(n,'square_receiving',['revocation_pending','application_mismatch'].includes(v.square.state)?'Start a new Square connection':'Review receiving access',v.security.mfaEnrolled!==true);
   else if(connected&&!owner)n.append(el('p','store-setup-note',['revocation_pending','application_mismatch'].includes(v.square.state)?'Ask your store owner to start a new Square connection.':'Ask your store owner to connect Square.'));
   if(connected&&owner&&v.square.oauthConfigured===true&&v.security.mfaEnrolled!==true)n.append(el('p','store-setup-note','Enroll your authenticator first, then sign in with verification.'));
   const r=v.receiving.state,details=r==='access_restricted'?'Your current role cannot prepare stock. An owner or manager reviews receiving.':r==='receiving_disabled'?'Receiving activation is pending for this store. Setup does not turn on stock changes.':'Choose a verified location while reviewing each batch. The saved plan, current permissions and recent verification control any stock change.';
   n=card(4,'Choose location and review stock',r==='receiving_disabled'?'Not activated':r==='access_restricted'?'Role restricted':r==='review_required'?'Per-batch review required':'After sign-in',details);
   if(connected)action(n,'review_receiving','Open receiving review',!manager||r==='access_restricted');
   container.append(el('p','store-setup-footer','Setup does not add stock or change balances. Checkout remains disabled. In the hosted workspace, check the same Store ID before changing access.'));
  }
  const controller={
   async refresh(){if(destroyed)return false;const at=++epoch;abort?.abort();abort=new AbortController();const signal=abort.signal;message('Checking your store setup…');container.setAttribute('aria-busy','true');try{const value=await loadSnapshot({signal});if(destroyed||at!==epoch)return false;render(clean(value));return true;}catch{if(destroyed||at!==epoch)return false;message('Store setup could not be confirmed. Reconnect and refresh. Saved work stays on this computer; no stock or credit changed.',true);return false;}},
   clear(text='Sign-in changed. Refresh store setup to continue.'){if(destroyed)return;++epoch;abort?.abort();message(text);},
   destroy(){if(destroyed)return;destroyed=true;++epoch;abort?.abort();container.replaceChildren();container.classList.remove('store-setup');container.removeAttribute('aria-busy');}
  };return controller;
 }
 window.StoreSetup=Object.freeze({create,actionUrl:action=>Object.hasOwn(URLS,action)?URLS[action]:null});
})();
