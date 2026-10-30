'use strict';
(()=>{
const $=id=>document.getElementById(id),encoder=new TextEncoder();
const names=['customer','credit_entries','credit_lots','lot_mutations','reservations','lot_reservations','reservation_terms','square_settlements','credit_returns','trade_receipts','trade_lines','physical_intakes','physical_intake_requests','idempotency_records','reconciliation_exceptions','origin_policies','policy_revocations','audit'];
const maximum=Math.min(64*1024*1024,Number($('privacyApp').dataset.browserMaxBytes)||32*1024*1024);
const limitLabel=maximum>=1024*1024?(maximum/1024/1024).toFixed(0)+' MiB':Math.ceil(maximum/1024)+' KiB';
$('privacyLimit').textContent=limitLabel;
let generation=0,searchSequence=0,runSequence=0,me=null,selected=null,busy=false,controller=null,searchController=null,chunks=null,blobUrl=null,searchTimer=null;
function stamp(){try{return localStorage.getItem('ss-auth-epoch')||''}catch{return ''}}
function message(id,text,error=false){$(id).textContent=text;$(id).classList.toggle('error',error)}
function release(){if(chunks)chunks.length=0;chunks=null;if(blobUrl){URL.revokeObjectURL(blobUrl);blobUrl=null}}
function controls(active){busy=active;for(const field of $('privacyForm').querySelectorAll('input,select,button'))field.disabled=active;for(const button of $('privacyCustomers').querySelectorAll('button'))button.disabled=active;$('privacySearch').disabled=active;$('privacyRefresh').disabled=active;$('privacyCancel').hidden=!active;$('privacyCancel').disabled=false}
function resetChecks(){for(const id of ['privacyIdentity','privacyAuthority','privacyScope','privacyCopies','privacyAgent'])$(id).checked=false}
function cancel(reason='Export canceled. No completed copy was downloaded.'){
 ++runSequence;controller?.abort();controller=null;release();controls(false);resetChecks();message('privacyProgress',reason);
}
function invalidate(reason='Your sign-in changed. Refresh the owner session before continuing.'){
 ++generation;++searchSequence;cancel(reason);searchController?.abort();me=null;selected=null;
 $('privacyForm').reset();$('privacyForm').hidden=true;$('privacyWorkspace').hidden=true;
 $('privacyCustomers').replaceChildren();$('privacyCustomerName').textContent=$('privacyCustomerId').textContent=$('privacyStore').textContent='';
 $('privacySearch').value='';message('privacySearchStatus','');message('privacyProgress','');message('privacySession',reason,true);
}
window.addEventListener('storage',event=>{if(event.key==='ss-auth-epoch')invalidate()});
let channel=null;try{channel=new BroadcastChannel('ss-auth-transition');channel.onmessage=()=>invalidate()}catch{}
window.addEventListener('pagehide',()=>{invalidate('This export view closed.');channel?.close()});
async function jsonRequest(path,data,signal,isCurrent=()=>true){
 const requestEpoch=generation,requestStamp=stamp(),requestSession=me?.csrf_token||null;
 const options={credentials:'same-origin',signal:signal||AbortSignal.timeout(20000),headers:{}};
 if(data!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-SS-CSRF':me?.csrf_token||''};options.body=JSON.stringify(data)}
 const response=await fetch(path,options);let result;try{result=await response.json()}catch{throw Error('The service returned an invalid response. No completed copy was downloaded.')}
 if(!response.ok){if((response.status===401||response.status===403)&&requestEpoch===generation&&requestStamp===stamp()&&(me?.csrf_token||null)===requestSession&&isCurrent())invalidate(result.error||'Your owner session ended. Sign in again in the workspace.');throw Error(result.error||'This request could not be completed.')}
 return result;
}
async function refresh(){
 invalidate('Checking your owner session…');const epoch=generation,authStamp=stamp();
 try{const current=await jsonRequest('/api/privacy/context',undefined,undefined,()=>epoch===generation&&authStamp===stamp());if(epoch!==generation||authStamp!==stamp())return;
  if(current.role!=='owner')throw Error('Only the store owner can prepare a customer data export.');
  me=Object.freeze(current);$('privacyStore').textContent=current.store;$('privacyWorkspace').hidden=false;
  message('privacySession','Signed in as the owner of '+current.store+'. Verify the customer before preparing a copy.');
 }catch(error){if(epoch===generation)message('privacySession',error.message,true)}
}
function choose(customer){
 if(busy||!me)return;selected=Object.freeze({id:String(customer.id),display_name:String(customer.display_name)});
 $('privacyForm').reset();$('privacyAgentRow').hidden=true;$('privacyAgent').required=false;
 $('privacyCustomerName').textContent=selected.display_name;$('privacyCustomerId').textContent=selected.id;
 $('privacyForm').hidden=false;message('privacyProgress','');$('privacyConfirmId').focus();
}
async function search(){
 if(busy||!me)return;const epoch=generation,sequence=++searchSequence,authStamp=stamp();searchController?.abort();searchController=new AbortController();
 $('privacyCustomers').replaceChildren();message('privacySearchStatus','Searching this store…');
 try{const result=await jsonRequest('/api/privacy/customers?q='+encodeURIComponent($('privacySearch').value),undefined,searchController.signal,()=>epoch===generation&&sequence===searchSequence&&authStamp===stamp());
  if(epoch!==generation||sequence!==searchSequence||authStamp!==stamp())return;
  for(const customer of result.customers){const button=document.createElement('button');button.type='button';button.className='customer';
   const text=document.createElement('span'),name=document.createElement('strong'),id=document.createElement('small');name.textContent=customer.display_name;id.textContent=customer.id;text.append(name,id);button.append(text);button.onclick=()=>choose(customer);$('privacyCustomers').append(button)}
  message('privacySearchStatus',result.customers.length?result.customers.length+' matching accounts. Select the independently verified person.':'No matching accounts in this store.');
 }catch(error){if(epoch===generation&&sequence===searchSequence&&error.name!=='AbortError')message('privacySearchStatus',error.message,true)}
}
function counts(value){return value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).length===names.length&&names.every(name=>Number.isSafeInteger(value[name])&&value[name]>=0)}
function sameCounts(one,two){return counts(one)&&counts(two)&&names.every(name=>one[name]===two[name])}
function hex(bytes){return Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('')}
async function nextHash(previous,line){const raw=encoder.encode(line+'\n'),combined=new Uint8Array(previous.length+raw.length);combined.set(previous);combined.set(raw,previous.length);return new Uint8Array(await crypto.subtle.digest('SHA-256',combined))}
function guard(epoch,run,authStamp,context,customer){if(epoch!==generation||run!==runSequence||authStamp!==stamp()||me?.csrf_token!==context.csrf_token||selected?.id!==customer.id)throw Error('The selected customer or owner session changed. No completed copy was downloaded.')}
async function receive(response,epoch,run,authStamp,context,customer){
 if(!response.body)throw Error('Streaming is unavailable in this browser. No copy was downloaded.');
 const reader=response.body.getReader(),decoder=new TextDecoder('utf-8',{fatal:true});let carry='',total=0,priorBytes=0,chain=new Uint8Array(32),manifest=null,footer=null,pages=0;
 const observed=Object.fromEntries(names.map(name=>[name,0]));chunks=[];
 async function line(raw){
  guard(epoch,run,authStamp,context,customer);if(!raw||footer)throw Error('The export contained unexpected trailing or empty data.');
  const value=JSON.parse(raw),length=encoder.encode(raw+'\n').length;
  if(value.kind==='error'){if(value.code==='authorization_changed')invalidate(value.error);throw Error(value.error||'The export stopped before completion.')}
  if(value.kind==='manifest'){
   if(manifest||priorBytes||value.schema!=='stock-and-sleeve.hosted-customer-export'||value.version!==2||value.customer_id!==customer.id||value.store!==context.store||!counts(value.record_counts)||!Number.isSafeInteger(value.start_audit_id)||value.start_audit_id<1||!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(value.export_id))throw Error('The export identity or manifest is invalid.');
   manifest=value;
  }else if(value.kind==='page'){
   if(!manifest||!names.includes(value.dataset)||value.page!==pages+1||!Array.isArray(value.records)||value.records.length!==value.count||!Number.isSafeInteger(value.count)||value.count<1||value.count>100)throw Error('The export page sequence is invalid.');
   observed[value.dataset]+=value.count;if(observed[value.dataset]>manifest.record_counts[value.dataset])throw Error('The export contains unexpected records.');pages++;
  }else if(value.kind==='complete'){
   if(!manifest||value.schema!==manifest.schema||value.version!==2||value.export_id!==manifest.export_id||value.start_audit_id!==manifest.start_audit_id||value.pages!==pages||value.bytes_before_footer!==priorBytes||!sameCounts(value.record_counts,observed)||!sameCounts(value.record_counts,manifest.record_counts)||value.content_chain_sha256!==hex(chain))throw Error('Export footer counts or hash did not verify. No completed copy was downloaded.');
   footer=value;return;
  }else throw Error('The export format is not supported.');
  chain=await nextHash(chain,raw);guard(epoch,run,authStamp,context,customer);priorBytes+=length;
  message('privacyProgress','Checking '+Object.values(observed).reduce((sum,n)=>sum+n,0).toLocaleString()+' linked records…');
 }
 try{
  while(true){guard(epoch,run,authStamp,context,customer);const part=await reader.read();guard(epoch,run,authStamp,context,customer);if(part.done)break;
   total+=part.value.byteLength;if(total>maximum)throw Error('This export exceeds the '+limitLabel+' browser limit. It was canceled without a completed download; arrange an operator-assisted export instead.');
   chunks.push(part.value);carry+=decoder.decode(part.value,{stream:true});let end;
   while((end=carry.indexOf('\n'))>=0){const raw=carry.slice(0,end);carry=carry.slice(end+1);await line(raw)}
  }
  carry+=decoder.decode();if(carry||!footer)throw Error('The export ended without a complete verified footer. No copy was downloaded.');
  return {manifest,footer};
 }catch(error){await reader.cancel().catch(()=>{});throw error}
 finally{reader.releaseLock()}
}
$('privacyForm').onsubmit=async event=>{
 event.preventDefault();if(busy||!me||!selected)return;
 const context=me,customer=selected,epoch=generation,authStamp=stamp(),run=++runSequence;
 const data={expected_store:context.store,confirmed_customer_id:$('privacyConfirmId').value,purpose:$('privacyPurpose').value,
  verification_method:$('privacyMethod').value,case_reference:$('privacyCase').value,
  identity_verified:$('privacyIdentity').checked,authority_confirmed:$('privacyAuthority').checked,
  scope_reviewed:$('privacyScope').checked,copy_handling_confirmed:$('privacyCopies').checked,agent_authority_verified:$('privacyAgent').checked};
 controller=new AbortController();const active=controller,timeout=setTimeout(()=>active.abort(),150000);controls(true);release();message('privacyProgress','Opening a consistent hosted snapshot…');
 try{
  const response=await fetch('/api/privacy/customers/'+encodeURIComponent(customer.id)+'/export',{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json','X-SS-CSRF':context.csrf_token},body:JSON.stringify(data),signal:active.signal});
  guard(epoch,run,authStamp,context,customer);
  if(!response.ok){let problem;try{problem=await response.json()}catch{};guard(epoch,run,authStamp,context,customer);if(response.status===401||response.status===403)invalidate(problem?.error);throw Error(problem?.error||'The export could not be authorized.')}
  if(!response.headers.get('Content-Type')?.startsWith('application/x-ndjson'))throw Error('The service returned an unsupported export format.');
  const {manifest,footer}=await receive(response,epoch,run,authStamp,context,customer);guard(epoch,run,authStamp,context,customer);
  const current=await jsonRequest('/api/privacy/context',undefined,active.signal,()=>epoch===generation&&run===runSequence&&authStamp===stamp());guard(epoch,run,authStamp,context,customer);
  if(current.store!==context.store||current.role!=='owner'||current.csrf_token!==context.csrf_token){invalidate();throw Error('The owner session changed before download.')}
  const verified=await jsonRequest('/api/privacy/exports/'+encodeURIComponent(manifest.export_id)+'/confirm',{
   start_audit_id:footer.start_audit_id,content_chain_sha256:footer.content_chain_sha256,record_counts:footer.record_counts},active.signal,()=>epoch===generation&&run===runSequence&&authStamp===stamp());
  guard(epoch,run,authStamp,context,customer);if(verified.verified!==true)throw Error('The export verification audit did not complete.');
  const blob=new Blob(chunks,{type:'application/x-ndjson'});blobUrl=URL.createObjectURL(blob);
  const link=document.createElement('a');link.href=blobUrl;link.download='Stock-and-Sleeve-customer-export-'+manifest.export_id+'.ndjson';document.body.append(link);
  try{guard(epoch,run,authStamp,context,customer);link.click()}finally{link.remove()}
  message('privacyProgress','Verified hosted copy prepared for download. Review its scope and third-party details before private delivery. Delivery to the customer is not confirmed.');
  resetChecks();const finishedUrl=blobUrl;blobUrl=null;setTimeout(()=>URL.revokeObjectURL(finishedUrl),1000);
 }catch(error){if(epoch===generation&&run===runSequence)message('privacyProgress',error.name==='AbortError'?'Export canceled or timed out. No completed copy was downloaded.':error.message,true)}
 finally{clearTimeout(timeout);if(run===runSequence){controller=null;release();controls(false)}}
};
$('privacyCancel').onclick=()=>cancel();$('privacyRefresh').onclick=refresh;
$('privacySearch').oninput=()=>{clearTimeout(searchTimer);++searchSequence;searchController?.abort();$('privacyCustomers').replaceChildren();searchTimer=setTimeout(search,250)};
$('privacyMethod').onchange=()=>{const agent=$('privacyMethod').value==='authorized_representative';$('privacyAgentRow').hidden=!agent;$('privacyAgent').required=agent;if(!agent)$('privacyAgent').checked=false};
refresh();
})();
