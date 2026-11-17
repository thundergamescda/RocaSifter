'use strict';
(() => {
  const panel=document.getElementById('cleanupObserver');
  if (!panel || panel.dataset.bound==='true') return;
  panel.dataset.bound='true';
  const el=id=>document.getElementById(id);
  const states={not_connected:'Not connected',disabled:'Disabled',idle:'No read yet',queued:'Queued',
    reading:'Reading',complete:'Read complete',partial:'Partial read',stale:'Stale',authority_blocked:'Authority blocked'};
  let context=null,snapshot=null,serial=0,busy=false,pending=null,configBlocked=false;
  const integer=value=>Number.isSafeInteger(value)&&value>=0;
  const boundedCount=(count,exact)=>integer(count)&&typeof exact==='boolean'&&
    (exact?count<=1000:count===1001);
  const countText=(count,exact)=>exact?String(count):'At least '+count;
  const timestamp=value=>typeof value==='string'&&value.length<=64&&Number.isFinite(Date.parse(value))&&
    /(?:Z|[+-]\d{2}:\d{2})$/.test(value);
  const interval=value=>value!==null&&typeof value==='object'&&!Array.isArray(value)&&
    timestamp(value.started_at)&&timestamp(value.finished_at)&&Date.parse(value.started_at)<=Date.parse(value.finished_at);

  function currentContext() {
    if (typeof portalMe==='undefined'||!portalMe||typeof portalEpoch==='undefined'||
        authTransitionBusy||el('workspace').hidden||squareLoading||squareConnectBusy) return null;
    if (!['owner','manager','buyer'].includes(portalMe.role)) return null;
    return {key:[portalEpoch,portalMe.tenant_id,portalMe.role,portalMe.mfa_fresh===true].join('|'),
      role:portalMe.role,fresh:portalMe.mfa_fresh===true};
  }
  function stillCurrent(key,ticket) {
    return context&&context.key===key&&currentContext()?.key===key&&serial===ticket;
  }
  function emptyDisplay(message) {
    snapshot=null;
    el('cleanupObserverState').textContent='Status unavailable';
    el('cleanupObserverGate').textContent='Worker gate unknown.';
    el('cleanupObserverCoverage').textContent='No current read evidence is displayed.';
    el('cleanupObserverAttempt').textContent='Unavailable';
    el('cleanupObserverComplete').textContent='Unavailable';
    el('cleanupObserverMissing').textContent='Unknown';
    el('cleanupObserverPending').textContent='Unknown';
    el('cleanupObserverProcessing').textContent='Unknown';
    el('cleanupObserverDue').textContent='Unavailable';
    el('cleanupObserverReasons').replaceChildren();
    el('cleanupObserverStatus').textContent=message;
    el('cleanupObserverEnabled').checked=false;
    el('cleanupObserverBackground').checked=false;
    el('cleanupObserverCadence').value='3600';
    el('cleanupObserverStale').value='7200';
  }
  function controls() {
    const ready=!!context&&!!snapshot&&!busy;
    const owner=ready&&context.role==='owner';
    const canConfigure=owner&&context.fresh&&!configBlocked&&snapshot.generation!==null;
    el('cleanupObserverCheck').disabled=!context||busy;
    for (const id of ['cleanupObserverEnabled','cleanupObserverBackground','cleanupObserverCadence','cleanupObserverStale','cleanupObserverSave']) {
      el(id).disabled=!canConfigure;
    }
    el('cleanupObserverBackground').disabled=!canConfigure||!el('cleanupObserverEnabled').checked;
    el('cleanupObserverConfig').hidden=context?.role!=='owner';
    el('cleanupObserverMfa').hidden=!(context?.role==='owner'&&(!context.fresh||configBlocked));
    el('cleanupObserverRequest').disabled=!(ready&&['owner','manager'].includes(context.role)&&
      snapshot.generation!==null&&snapshot.worker_enabled&&snapshot.enabled&&snapshot.state!=='authority_blocked'&&
      !['queued','reading'].includes(snapshot.state));
    panel.setAttribute('aria-busy',busy?'true':'false');
  }
  function validate(value) {
    if (!value||typeof value!=='object'||Array.isArray(value)||typeof value.state!=='string'||!Object.hasOwn(states,value.state)||
        !(value.generation===null||typeof value.generation==='string'&&/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value.generation))||
        !integer(value.config_version)||!integer(value.cadence_seconds)||value.cadence_seconds<300||value.cadence_seconds>86400||
        !integer(value.stale_after_seconds)||value.stale_after_seconds<value.cadence_seconds||value.stale_after_seconds>604800||
        !['enabled','background_enabled','worker_enabled','coverage_current','missing_truncated'].every(key=>typeof value[key]==='boolean')||
        value.background_enabled&&!value.enabled||
        value.atomic_snapshot!==false||value.continuous_zero_verified!==false||value.deletion_enabled!==false||
        !boundedCount(value.pending_notification_count,value.pending_notification_count_exact)||
        !boundedCount(value.receipt_processing_pending_count,value.receipt_processing_pending_count_exact)||
        !(value.missing_count===null||integer(value.missing_count))||
        !(value.observed_age_seconds===null||integer(value.observed_age_seconds))||
        !(value.next_due_at===null||timestamp(value.next_due_at))||
        !Array.isArray(value.reason_codes)||value.reason_codes.length>32||
        !value.reason_codes.every(code=>typeof code==='string'&&/^[a-z][a-z0-9_]{0,63}$/.test(code))||
        !Array.isArray(value.missing)||value.missing.length>50||!value.missing.every(pair=>pair&&
          typeof pair.object_id==='string'&&typeof pair.location_id==='string')||
        !(value.last_complete===null||interval(value.last_complete))||
        !(value.last_attempt===null||interval(value.last_attempt)&&typeof value.last_attempt.outcome==='string'&&
          value.last_attempt.outcome.length<=64&&integer(value.last_attempt.count_rows)&&
          (value.last_attempt.missing_count===null||integer(value.last_attempt.missing_count)))) {
      throw new Error('The service returned an incomplete status. Check again before taking action.');
    }
    return value;
  }
  function showInterval(value) {
    return value?new Date(value.started_at).toLocaleString()+' to '+new Date(value.finished_at).toLocaleString():'No completed interval';
  }
  function render(value,message) {
    const changed=pending&&(pending.body.expected_generation!==value.generation||pending.body.expected_config_version!==value.config_version);
    if (changed) pending=null;
    snapshot=value;
    el('cleanupObserverState').textContent=states[value.state];
    el('cleanupObserverGate').textContent=(value.worker_enabled?'Global worker gate: enabled.':'Global worker gate: off; no background worker is activated here.')+
      ' Store consent: '+(value.enabled?'enabled':'off')+'. Background consent: '+(value.background_enabled?'enabled':'off')+'.';
    const current=value.coverage_current&&value.pending_notification_count_exact&&value.pending_notification_count===0&&
      value.receipt_processing_pending_count_exact&&value.receipt_processing_pending_count===0&&
      value.state==='complete'&&value.missing_count===0;
    el('cleanupObserverCoverage').textContent=current?
      'At this status check, the latest bounded read met the service checks. Separate reads are not an atomic snapshot.':
      'Current complete coverage is not established. Queued notices, missing evidence or changed authority may require another read.';
    el('cleanupObserverAttempt').textContent=showInterval(value.last_attempt)+(value.last_attempt?' · '+value.last_attempt.outcome+' · '+value.last_attempt.count_rows+' count rows':'');
    el('cleanupObserverComplete').textContent=showInterval(value.last_complete);
    el('cleanupObserverMissing').textContent=value.missing_count===null?'Unknown':String(value.missing_count)+(value.missing_truncated?' (detail list capped)':'');
    el('cleanupObserverPending').textContent=countText(value.pending_notification_count,value.pending_notification_count_exact);
    el('cleanupObserverProcessing').textContent=countText(value.receipt_processing_pending_count,value.receipt_processing_pending_count_exact);
    el('cleanupObserverDue').textContent=value.next_due_at?new Date(value.next_due_at).toLocaleString()+' (due metadata; no scheduler promise)':'Not scheduled';
    const reasons=el('cleanupObserverReasons');reasons.replaceChildren();
    for (const code of value.reason_codes) {const item=document.createElement('li');item.textContent=code.replaceAll('_',' ');reasons.append(item);}
    el('cleanupObserverEnabled').checked=value.enabled;
    el('cleanupObserverBackground').checked=value.background_enabled;
    el('cleanupObserverCadence').value=String(value.cadence_seconds);
    el('cleanupObserverStale').value=String(value.stale_after_seconds);
    el('cleanupObserverStatus').textContent=message;
  }
  async function refreshStatus(message='') {
    if (!context) return;
    if (currentContext()?.key!==context.key) {synchronize();return;}
    const key=context.key,ticket=++serial;
    busy=true;emptyDisplay('Checking current read status…');controls();
    try {
      const value=await api('/api/catalog-cleanup/status');
      if (!stillCurrent(key,ticket)) return;
      render(validate(value),message);
    } catch (error) {
      if (!stillCurrent(key,ticket)) return;
      emptyDisplay(error.message||'Status could not be loaded. Check again.');
    } finally {
      if (stillCurrent(key,ticket)) {busy=false;controls();}
    }
  }
  function synchronize() {
    const next=currentContext();
    if (!next) {
      ++serial;context=null;pending=null;busy=false;configBlocked=false;
      emptyDisplay('Sign in to the current store to check read status.');panel.hidden=true;controls();return;
    }
    if (context?.key===next.key) return;
    ++serial;context=next;pending=null;busy=false;configBlocked=false;
    emptyDisplay('Checking current store…');panel.hidden=false;controls();void refreshStatus();
  }
  async function submit(kind,values) {
    if (!context||!snapshot||busy) return;
    if (currentContext()?.key!==context.key) {synchronize();return;}
    const key=context.key;
    const intent={expected_generation:snapshot.generation,expected_config_version:snapshot.config_version,...values};
    const identity=kind+'|'+JSON.stringify(intent);
    if (!pending||pending.identity!==identity) pending={identity,body:{request_key:crypto.randomUUID(),...intent}};
    const body=pending.body,ticket=++serial;
    busy=true;emptyDisplay(kind==='config'?'Saving owner consent…':'Requesting a read…');controls();
    try {
      const result=await api('/api/catalog-cleanup/'+kind,body);
      if (!stillCurrent(key,ticket)) return;
      if (!result||result.accepted!==true||typeof result.duplicate!=='boolean') throw new Error('The request acknowledgement was incomplete.');
      pending=null;
      await refreshStatus(kind==='config'?'Consent acknowledged. Current status reloaded.':'Read request acknowledged. Current status reloaded; no provider read was performed by this request.');
    } catch (error) {
      if (!stillCurrent(key,ticket)) return;
      if (kind==='config'&&error.status===403) configBlocked=true;
      if (error.status===409) pending=null;
      await refreshStatus((error.message||'The request could not be confirmed.')+' Current status reloaded. Review it before trying again.');
    }
  }
  el('cleanupObserverCheck').addEventListener('click',()=>{if (!busy) void refreshStatus();});
  el('cleanupObserverEnabled').addEventListener('change',()=>{
    if (!el('cleanupObserverEnabled').checked) el('cleanupObserverBackground').checked=false;
    controls();
  });
  el('cleanupObserverConfig').addEventListener('submit',event=>{
    event.preventDefault();
    if (el('cleanupObserverSave').disabled) return;
    const cadence=Number(el('cleanupObserverCadence').value),stale=Number(el('cleanupObserverStale').value);
    if (!Number.isSafeInteger(cadence)||cadence<300||cadence>86400||!Number.isSafeInteger(stale)||stale<cadence||stale>604800) {
      el('cleanupObserverStatus').textContent='Use whole seconds: cadence 300–86400; stale after at least cadence and at most 604800.';return;
    }
    void submit('config',{enabled:el('cleanupObserverEnabled').checked,background_enabled:el('cleanupObserverEnabled').checked&&el('cleanupObserverBackground').checked,
      cadence_seconds:cadence,stale_after_seconds:stale});
  });
  el('cleanupObserverRequest').addEventListener('click',()=>{if (!el('cleanupObserverRequest').disabled) void submit('refresh',{});});
  document.addEventListener('ss-cleanup-observer-context',synchronize);
  synchronize();
})();
