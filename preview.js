'use strict';
// Read-only release status and manager cleanup preview. No merchant writes.
const previewButton=document.createElement('button');
previewButton.className='button secondary';previewButton.textContent='Connections & cleanup';
previewButton.id='previewConnections';
document.querySelector('.heading-actions').append(previewButton);
const previewDialog=document.createElement('dialog');previewDialog.className='modal';previewDialog.id='previewStatus';
previewDialog.setAttribute('aria-labelledby','previewConnectionsTitle');
previewDialog.innerHTML=[
 '<h2 id="previewConnectionsTitle">Connections & cleanup</h2><p><strong>Private pilot</strong></p>',
 '<h3>Shared store workspace</h3><p>Invited staff can sign in to Shared credit. Hosted account history is shared within the selected store. Square checkout and automatic trade issuance remain disabled until acceptance testing is complete.</p><button class="button secondary" id="openSharedWorkspace">Open Shared credit</button>',
 '<h3>Square connection</h3><p>Store owners manage OAuth authorization in the hosted Store connections portal. The existing receiving credential remains available to this installation while OAuth receiving is integrated.</p>',
 '<section class="settings-section" aria-labelledby="cleanupPreviewHeading"><h3 id="cleanupPreviewHeading">Old SKU cleanup</h3>',
 '<p>Saved cleanup observations on this computer are preview-only. This screen does not collect new stock observations or delete Square data. Deletion is disabled.</p>',
 '<p>Saved-history results do not establish current stock freshness, pending-order checks or fresh Square preflight. They do not establish deletion readiness.</p>',
 '<p>Cleanup opt-in is a saved-history eligibility gate. This screen displays the stored setting and cannot change it.</p>',
 '<button id="loadCleanupPreview" class="button secondary">View manager cleanup preview</button>',
 '<p id="cleanupPreviewResult" class="inline-message" role="status" aria-live="polite" aria-atomic="true"></p>',
 '<div id="cleanupPreviewContent" aria-busy="false"><p id="cleanupOptInStatus"></p><p id="cleanupPreviewSummary"></p><p id="cleanupPreviewReason"></p><p id="cleanupPreviewShown"></p><ol id="cleanupPreviewEntries"></ol></div></section>',
 '<form method="dialog"><button class="button primary">Close</button></form>',
].join('');
document.body.append(previewDialog);
const cleanupPreviewLoad=document.getElementById('loadCleanupPreview');
const cleanupPreviewStatus=document.getElementById('cleanupPreviewResult');
const cleanupPreviewContent=document.getElementById('cleanupPreviewContent');
const cleanupPreviewOptIn=document.getElementById('cleanupOptInStatus');
const cleanupPreviewSummary=document.getElementById('cleanupPreviewSummary');
const cleanupPreviewReason=document.getElementById('cleanupPreviewReason');
const cleanupPreviewShown=document.getElementById('cleanupPreviewShown');
const cleanupPreviewEntries=document.getElementById('cleanupPreviewEntries');
let cleanupPreviewGeneration=0;
let cleanupPreviewRequest=null;
let previewReturnFocus=true;

function clearCleanupPreviewOutput(){
 cleanupPreviewOptIn.textContent='';
 cleanupPreviewSummary.textContent='';
 cleanupPreviewReason.textContent='';
 cleanupPreviewShown.textContent='';
 cleanupPreviewEntries.replaceChildren();
}

function resetCleanupPreview(){
 clearCleanupPreviewOutput();
 cleanupPreviewStatus.textContent='Load saved cleanup observations on this computer. Deletion is disabled.';
 cleanupPreviewContent.setAttribute('aria-busy','false');
 cleanupPreviewLoad.disabled=false;
 cleanupPreviewLoad.textContent='View manager cleanup preview';
}

function invalidateCleanupPreview(){
 cleanupPreviewGeneration+=1;
 const pending=cleanupPreviewRequest;
 cleanupPreviewRequest=null;
 if(pending)pending.abort();
}

function cleanupPreviewText(value){
 return typeof value==='string'&&value.trim().length>0;
}

function cleanupPreviewRecordIsUsable(item){
 if(!item||typeof item!=='object'||Array.isArray(item))return false;
 const keys=['id','sku','parent','version','locations','blocked','zero_since','verified_days','candidate'];
 if(Object.keys(item).length!==keys.length||!keys.every(key=>Object.prototype.hasOwnProperty.call(item,key)))return false;
 if(!cleanupPreviewText(item.id)||typeof item.blocked!=='boolean'||typeof item.candidate!=='boolean')return false;
 if(!Array.isArray(item.locations)||!item.locations.every(cleanupPreviewText)||new Set(item.locations).size!==item.locations.length)return false;
 if(item.zero_since!==null&&!cleanupPreviewText(item.zero_since))return false;
 if(typeof item.verified_days!=='number'||!Number.isFinite(item.verified_days)||item.verified_days<0)return false;
 if(item.zero_since===null&&(item.verified_days!==0||item.candidate))return false;
 if(item.candidate&&(item.blocked||item.verified_days<30))return false;
 const complete=cleanupPreviewText(item.sku)&&cleanupPreviewText(item.parent)&&
  typeof item.version==='number'&&Number.isInteger(item.version)&&item.version>0&&item.locations.length>0;
 const incomplete=item.sku===null&&item.parent===null&&item.version===null&&item.locations.length===0&&
  item.blocked===true&&item.zero_since===null&&item.verified_days===0&&item.candidate===false;
 return complete||incomplete;
}

function cleanupPreviewIsUsable(data){
 return data!==null&&typeof data==='object'&&!Array.isArray(data)&&
  data.mode==='dry-run-only'&&data.deletionEnabled===false&&typeof data.optedIn==='boolean'&&
  Array.isArray(data.items)&&data.items.every(cleanupPreviewRecordIsUsable)&&
  (data.optedIn||!data.items.some(item=>item.candidate))&&cleanupPreviewText(data.reason);
}

function showCleanupPreview(data){
 cleanupPreviewOptIn.textContent='Stored cleanup opt-in: '+(data.optedIn?'On':'Off')+'.';
 const blocked=data.items.filter(item=>item.blocked||item.sku===null).length;
 const meetingRules=data.items.filter(item=>item.candidate).length;
 cleanupPreviewSummary.textContent='Saved records: '+data.items.length+'. Blocked or incomplete: '+blocked+
  '. Meeting saved-history rules: '+meetingRules+'.';
 cleanupPreviewReason.textContent='Preview status: '+data.reason;
 const shown=Math.min(data.items.length,50);
 cleanupPreviewShown.textContent='Showing details for '+shown+' of '+data.items.length+' saved records.';
 for(const item of data.items.slice(0,shown)){
  const entry=document.createElement('li');
  const title=document.createElement('strong');
  title.textContent=item.sku===null?'SKU unavailable':'SKU: '+item.sku;
  const detail=document.createElement('p');
  const context=item.sku===null?'Incomplete saved context':item.blocked?'Blocked saved context':'Saved context present';
  detail.textContent='ID: '+item.id+'. Location coverage: '+(item.locations.length?item.locations.join(', '):'Unavailable')+
   '. Status: '+context+'. Recorded zero-history days: '+item.verified_days+
   '. Saved-history rules: '+(item.candidate?'Met in this saved preview':'Not met in this saved preview')+'.';
  entry.append(title,detail);
  cleanupPreviewEntries.append(entry);
 }
 cleanupPreviewStatus.textContent=data.items.length?
  'Saved cleanup preview loaded. Deletion is disabled.':
  'No saved cleanup observations on this computer. Deletion is disabled.';
}

previewButton.onclick=()=>{
 if(previewDialog.open)return;
 invalidateCleanupPreview();
 resetCleanupPreview();
 previewReturnFocus=true;
 previewDialog.showModal();
};

cleanupPreviewLoad.onclick=async()=>{
 if(!previewDialog.open||cleanupPreviewRequest)return;
 const generation=++cleanupPreviewGeneration;
 const request=new AbortController();
 cleanupPreviewRequest=request;
 clearCleanupPreviewOutput();
 cleanupPreviewContent.setAttribute('aria-busy','true');
 cleanupPreviewLoad.disabled=true;
 cleanupPreviewStatus.textContent='Loading saved cleanup observations on this computer. Deletion is disabled.';
 try{
  const data=await api('/api/admin/cleanup',undefined,request.signal);
  if(generation!==cleanupPreviewGeneration||cleanupPreviewRequest!==request||!previewDialog.open)return;
  if(!cleanupPreviewIsUsable(data))throw Error('The saved preview response is unavailable or malformed.');
  showCleanupPreview(data);
 }catch(error){
  if(generation!==cleanupPreviewGeneration||cleanupPreviewRequest!==request||!previewDialog.open)return;
  clearCleanupPreviewOutput();
  const message=error&&typeof error.message==='string'&&error.message.trim()?error.message:'The request could not complete.';
  cleanupPreviewStatus.textContent='Cleanup preview unavailable. '+message+' Use Retry cleanup preview to try again. Deletion is disabled.';
  cleanupPreviewLoad.textContent='Retry cleanup preview';
 }finally{
  if(generation===cleanupPreviewGeneration&&cleanupPreviewRequest===request&&previewDialog.open){
   cleanupPreviewRequest=null;
   cleanupPreviewContent.setAttribute('aria-busy','false');
   cleanupPreviewLoad.disabled=false;
  }
 }
};

previewDialog.addEventListener('cancel',invalidateCleanupPreview);
previewDialog.querySelector('form[method="dialog"]').addEventListener('submit',invalidateCleanupPreview);
previewDialog.addEventListener('close',()=>{
 if(previewDialog.open)return;
 invalidateCleanupPreview();
 resetCleanupPreview();
 if(previewReturnFocus)previewButton.focus();
});
document.getElementById('openSharedWorkspace').onclick=()=>{
 previewReturnFocus=false;
 invalidateCleanupPreview();
 previewDialog.close();
 navigate('cloud');
};
