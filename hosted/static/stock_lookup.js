(() => {
  'use strict';
  const $=id=>document.getElementById(id),form=$('stockForm'),roles=new Set(['owner','manager','buyer','cashier']);
  const catalogStates=new Set(['match','no_match','ambiguous','unsupported','incomplete','mismatch']);
  const countStates=new Set(['not_requested','reported','missing','unknown','incomplete','mismatch']);
  const availabilities=new Set(['positive','zero_reported','negative_reported','fractional_review','unknown']);
  const unitKinds=new Set(['whole','measured','conversion','measured_conversion','component']);
  const qualifications={ARCHIVED:'This Square item is archived.',STOCKABLE:'Square marks this variation stockable.',
    SELLABLE:'Square marks this variation sellable.',NOT_SELLABLE:'This variation is not marked sellable in Square.',
    MARKED_SOLD_OUT:'This variation is marked sold out in Square.',CONVERSION:'Quantity is for the requested variation; no unit conversion was applied here.',
    COMPONENT_BASED:'Square identifies this variation as component-based.'};
  const reasons={NONE:'',NO_EXACT_SKU:'No exact Square SKU was found.',CASE_VARIANT:'A different capitalization exists; no exact SKU was found.',
    SKU_AMBIGUOUS:'Multiple matching Square variations exist. Ask your owner to review the SKU.',
    CATALOG_READ_INCOMPLETE:'The complete Square reference could not be checked. Choose Check again.',
    COUNTS_READ_INCOMPLETE:'The complete Square count could not be checked. Choose Check again.',
    COUNT_NOT_RETURNED:'No IN_STOCK count was returned for this variation at this location.',
    COUNT_TIMESTAMP_UNKNOWN:'Square did not provide a verified time for this count. Choose Check again.',
    COMPOSED_COUNT:'This composed quantity could not be confirmed by the stock count endpoint.',
    CONTEXT_CHANGED:'The Square reference or location changed during the check. Choose Check again.',
    READ_DEADLINE:'The stock check took too long to verify. Choose Check again.',
    CATALOG_TEXT_UNSUPPORTED:'The Square reference text could not be verified.',
    CATALOG_REFERENCE_UNSUPPORTED:'This Square reference could not be verified for stock lookup.',
    LOCATION_SETTINGS_UNSUPPORTED:'The Square location settings could not be verified.',
    TRACKING_SETTINGS_UNSUPPORTED:'The Square tracking settings could not be verified.',
    INVENTORY_NOT_TRACKED:'Square inventory tracking is off for this variation at this location.',
    MEASUREMENT_UNIT_UNSUPPORTED:'This Square measurement unit could not be verified.',
    ITEM_OPTIONS_UNSUPPORTED:'The Square variation options could not be verified.',
    NOT_PRESENT_AT_LOCATION:'This variation is not present at the selected Square location.',
    CONVERSION_UNSUPPORTED:'This Square conversion reference could not be verified.',
    LOCATION_UNAVAILABLE:'Choose an active location in your connected Square account.'};
  let generation=0,controller=new AbortController(),principal=null,locations=new Map(),busy=false,ageTimer=null;
  const object=value=>!!value&&typeof value==='object'&&!Array.isArray(value);
  const keys=(value,names)=>object(value)&&Object.keys(value).length===names.length&&names.every(name=>Object.hasOwn(value,name));
  const text=(value,max)=>typeof value==='string'&&value.length<=max*2&&[...value].length<=max&&!/[\p{Cc}\p{Cf}\p{Cs}]/u.test(value);
  const ident=value=>text(value,255)&&value.length>0&&value===value.trim()&&!['.','..'].includes(value);
  const timestamp=value=>typeof value==='string'&&value.length<=64&&/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)&&Number.isFinite(Date.parse(value));
  const quantity=value=>typeof value==='string'&&value.length<=26&&/^[+-]?\d+(?:\.\d{1,5})?$/.test(value);
  const localTime=new Intl.DateTimeFormat(undefined,{dateStyle:'medium',timeStyle:'short'});
  function showTime(id,prefix,value){
    const node=$(id);node.textContent=prefix+localTime.format(new Date(value));
    node.title=value;node.dataset.timestamp=value;
  }
  function quantityClass(value){
    const unsigned=value.replace(/^[+-]/,''),parts=unsigned.split('.'),fraction=parts[1]||'';
    if(!/[1-9]/.test(parts[0]+fraction))return 'zero_reported';
    if(/[1-9]/.test(fraction))return 'fractional_review';
    return value.startsWith('-')?'negative_reported':'positive';
  }
  function current(seq,sku,location,who){return seq===generation&&principal===who&&$('stockSku').value===sku&&$('stockLocation').value===location}
  function controls(){
    $('stockCheck').disabled=busy||!principal||!locations.has($('stockLocation').value)||!$('stockSku').value.trim();
    $('stockRefreshSession').disabled=busy;
  }
  function clearResult(){
    clearTimeout(ageTimer);ageTimer=null;$('stockResult').hidden=true;
    for(const id of ['Sku','Item','Variation','Location','Quantity','Id','Parent'])$('stockResult'+id).value='';
    for(const id of ['Summary','Unit','Qualifications','Checked','Updated']){
      const node=$('stockResult'+id);node.textContent='';node.removeAttribute('title');node.removeAttribute('data-timestamp');
    }
  }
  function invalidate(){generation++;controller.abort();controller=new AbortController();busy=false;clearResult();$('stockStatus').textContent='';$('stockCheck').textContent='Check stock';controls()}
  function reset(message='Your session changed. Refresh your staff session to continue.'){
    invalidate();principal=null;locations.clear();form.reset();$('stockLocation').replaceChildren(new Option('Choose a location',''));
    form.hidden=true;$('stockSession').textContent=message;controls();
  }
  function checkedMe(value){
    if(!object(value)||!roles.has(value.role)||!ident(value.tenant_id)||!text(value.store,200)||
      typeof value.csrf_token!=='string'||!/^[0-9a-f]{64}$/.test(value.csrf_token)||
      typeof value.mfa_enabled!=='boolean'||typeof value.mfa_authenticated!=='boolean'||
      (value.mfa_enabled&&!value.mfa_authenticated))throw Error('SESSION');
    return JSON.stringify([value.tenant_id,value.role,value.store,value.csrf_token]);
  }
  async function get(path,signal){
    const response=await fetch(path,{credentials:'same-origin',signal});
    const raw=await response.text();if(raw.length>65536)throw Error('RESPONSE');
    if(!response.ok){const error=Error('HTTP');error.status=response.status;throw error}
    return JSON.parse(raw);
  }
  function checkedLocations(value){
    if(!keys(value,['stock_locations_version','locations'])||value.stock_locations_version!==1||
      !Array.isArray(value.locations)||value.locations.length>100)throw Error('LOCATIONS');
    const rows=new Map();
    for(const row of value.locations){
      if(!keys(row,['id','name'])||!ident(row.id)||!text(row.name,512)||rows.has(row.id))throw Error('LOCATIONS');
      rows.set(row.id,row.name);
    }
    return rows;
  }
  function checkedResult(value,sku,location){
    const fields=['stock_lookup_version','requested_sku','location_id','catalog_state','count_state','availability','reason_code',
      'reference','inventory','read_started_at','read_completed_at','atomic_snapshot'];
    if(!keys(value,fields)||value.stock_lookup_version!==1||value.requested_sku!==sku||value.location_id!==location||
      !catalogStates.has(value.catalog_state)||!countStates.has(value.count_state)||!availabilities.has(value.availability)||
      !Object.hasOwn(reasons,value.reason_code)||value.atomic_snapshot!==false||!timestamp(value.read_started_at)||
      !timestamp(value.read_completed_at)||Date.parse(value.read_completed_at)<Date.parse(value.read_started_at))throw Error('RESULT');
    if(value.catalog_state!=='match'){
      if(value.reference!==null||value.inventory!==null||value.availability!=='unknown'||
        !['not_requested','incomplete','mismatch'].includes(value.count_state))throw Error('RESULT');
      return value;
    }
    const ref=value.reference;
    if(!keys(ref,['variation_id','parent_id','sku','item_name','variation_name','unit','qualifications'])||
      !ident(ref.variation_id)||!ident(ref.parent_id)||ref.sku!==sku||!text(ref.item_name,512)||!text(ref.variation_name,512)||
      !keys(ref.unit,['kind','label','precision'])||!unitKinds.has(ref.unit.kind)||!text(ref.unit.label,256)||
      !Number.isInteger(ref.unit.precision)||ref.unit.precision<0||ref.unit.precision>5||
      !Array.isArray(ref.qualifications)||ref.qualifications.length>7||new Set(ref.qualifications).size!==ref.qualifications.length||
      ref.qualifications.some(code=>!Object.hasOwn(qualifications,code)))throw Error('REFERENCE');
    if(value.count_state!=='reported'){
      if(!['missing','unknown'].includes(value.count_state)||value.inventory!==null||value.availability!=='unknown')throw Error('RESULT');
      return value;
    }
    const row=value.inventory;
    if(!keys(row,['state','quantity','calculated_at','is_estimated'])||row.state!=='IN_STOCK'||!quantity(row.quantity)||
      !timestamp(row.calculated_at)||![null,false].includes(row.is_estimated)||quantityClass(row.quantity)!==value.availability||
      value.reason_code!=='NONE')throw Error('COUNT');
    return value;
  }
  async function refresh(){
    reset('Checking your staff session…');const seq=generation,signal=controller.signal;
    const timer=setTimeout(()=>{if(seq===generation)controller.abort()},65000);
    busy=true;controls();
    try{
      const me=await get('/api/me',signal),who=checkedMe(me);
      const rows=checkedLocations(await get('/api/stock/locations',signal));
      const after=checkedMe(await get('/api/me',signal));
      if(seq!==generation)return;
      if(after!==who){reset();return}
      principal=who;locations=rows;
      $('stockLocation').replaceChildren(new Option('Choose a location',''));
      for(const [id,name] of rows)$('stockLocation').append(new Option(name||id,id));
      form.hidden=false;$('stockSession').textContent='Signed in to '+me.store+'.';
      if(!rows.size)$('stockStatus').textContent='No active Square locations were returned.';
    }catch(error){
      if(seq!==generation)return;
      reset(error.status===401?'Sign in through your store workspace, then refresh your staff session.':
        'The staff session or Square connection could not be checked. Open your workspace or refresh to try again.');
    }finally{clearTimeout(timer);if(seq===generation){busy=false;controls()}}
  }
  function render(value){
    $('stockStatus').textContent=reasons[value.reason_code];
    if(!value.reference)return;
    const ref=value.reference,row=value.inventory;
    $('stockResultSku').value=ref.sku;$('stockResultItem').value=ref.item_name;$('stockResultVariation').value=ref.variation_name;
    $('stockResultLocation').value=locations.get(value.location_id)||value.location_id;
    $('stockResultId').value=ref.variation_id;$('stockResultParent').value=ref.parent_id;
    $('stockResultQuantity').value=row?row.quantity:'Unknown';$('stockResultUnit').textContent=ref.unit.label;
    $('stockResultQualifications').textContent=ref.qualifications.map(code=>qualifications[code]).join(' ');
    const summaries={positive:'Square reports positive IN_STOCK quantity.',zero_reported:'Square reports zero IN_STOCK quantity.',
      negative_reported:'Square reports a negative IN_STOCK quantity. Verify the stock record in Square.',
      fractional_review:'Square reports fractional IN_STOCK quantity. Use the quantity unit shown below.',unknown:'Square quantity is unknown.'};
    $('stockResultSummary').textContent=summaries[value.availability];
    showTime('stockResultChecked','Checked at: ',value.read_completed_at);
    if(row)showTime('stockResultUpdated','Last affecting update: ',row.calculated_at);
    $('stockResult').hidden=false;
    ageTimer=setTimeout(()=>{showTime('stockResultChecked','Previous check: ',value.read_completed_at);$('stockResultChecked').textContent+='. Choose Check again for another report.'},60000);
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||!principal)return;
    const sku=$('stockSku').value,location=$('stockLocation').value;
    if(!text(sku,100)||!sku.trim()||!locations.has(location)){
      invalidate();$('stockStatus').textContent='Choose a location and enter an exact Square SKU.';return;
    }
    invalidate();const seq=generation,who=principal,signal=controller.signal;
    busy=true;$('stockStatus').textContent='Checking Square stock…';controls();
    const timer=setTimeout(()=>{if(seq===generation)controller.abort()},65000);
    try{
      const value=await get('/api/stock/lookup?'+new URLSearchParams({sku,location_id:location}),signal);
      if(!current(seq,sku,location,who))return;
      const after=checkedMe(await get('/api/me',signal));
      if(!current(seq,sku,location,who))return;
      if(after!==who){reset();return}
      clearResult();render(checkedResult(value,sku,location));
    }catch(error){
      if(!current(seq,sku,location,who))return;
      clearResult();
      if(error.status===401||error.status===403){reset();return}
      $('stockStatus').textContent=error.status===409?'Square authorization needs attention. Open your workspace to check the connection.':
        'The complete stock report could not be verified. Choose Check again.';
    }finally{clearTimeout(timer);if(seq===generation){busy=false;$('stockCheck').textContent='Check again';controls()}}
  });
  for(const node of [$('stockSku'),$('stockLocation')]){
    node.addEventListener('input',invalidate);node.addEventListener('change',invalidate);
  }
  $('stockRefreshSession').addEventListener('click',refresh);
  window.addEventListener('storage',event=>{if(event.key==='ss-auth-epoch')reset()});
  try{const channel=new BroadcastChannel('ss-auth-transition');channel.onmessage=()=>reset()}catch(_){/* Current-session checks remain. */}
  window.addEventListener('pagehide',()=>reset());
  window.addEventListener('pageshow',event=>{if(event.persisted)reset()});
  refresh();
})();
