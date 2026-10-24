'use strict';
const $=id=>document.getElementById(id);
const paths={layers:'M12 3 2 8l10 5 10-5-10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5',exchange:'M3 7h16l-4-4m4 14H3l4 4M19 7l-4 4M3 17l4-4',tag:'M3 3h7l11 11-7 7L3 10V3Zm4 4h.01',globe:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18Z',activity:'M3 12h4l3-8 4 16 3-8h4',lock:'M6 10h12v11H6V10Zm3 0V6a3 3 0 0 1 6 0v4m-3 5v2',unlock:'M6 10h12v11H6V10Zm3 0V6a3 3 0 0 1 6 0m-3 9v2',help:'M12 17h.01M9 9a3 3 0 0 1 6 0c0 2-3 2-3 4m9-1a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',plus:'M12 5v14M5 12h14',scan:'M3 8V3h5m8 0h5v5m0 8v5h-5M8 21H3v-5m4-8h1v8H7m4-8h2v8h-2m5-8h1v8h-1',upload:'M12 15V3m-4 4 4-4 4 4M4 14v6h16v-6',shield:'M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6l-9-4Zm-4 9 3 3 5-5',qr:'M3 3h6v6H3V3Zm12 0h6v6h-6V3ZM3 15h6v6H3v-6Zm12 0h2v2h4v4h-6v-6Zm6-3h-3M12 3v3m0 6h3M3 12h5m4 5v4',check:'m5 12 4 4L19 6','check-circle':'m8 12 3 3 5-6m5 3a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',save:'M4 3h13l4 4v14H4V3Zm4 0v6h8V3m-8 18v-7h9v7',dollar:'M12 2v20m5-16H9a4 4 0 0 0 0 8h6a4 4 0 0 1 0 8H6',search:'m16 16 5 5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z','chevron-left':'m14 5-7 7 7 7','chevron-right':'m10 5 7 7-7 7','arrow-right':'M4 12h16m-6-6 6 6-6 6',printer:'M6 9V3h12v6M6 17H3V9h18v8h-3m-12-3h12v7H6v-7m11-2h.01',x:'m6 6 12 12M6 18 18 6',plug:'M8 3v5m8-5v5M6 8h12v4a6 6 0 0 1-12 0V8Zm6 10v4',edit:'m16 3 5 5L8 21H3v-5L16 3Zm-2 2 5 5'};
function icon(name){return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name]||paths.layers}"/></svg>`}
function icons(root=document){root.querySelectorAll('i[data-icon]').forEach(el=>{el.innerHTML=icon(el.dataset.icon)})}
function escapeHTML(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
const usd=n=>Number(n||0).toLocaleString('en-US',{style:'currency',currency:'USD'});
const modes={labels:{title:'Process cards',eyebrow:'FROM SCAN TO SHELF',description:'Check your cards. Create labels. Choose whether to add stock.',empty:'Let your exports come to you.',copy:'Choose your export folder once. Then press Export in Roca,<br>and your cards will arrive here for review.',foot:'Your work is saved automatically.'},trade:{title:'Customer trade-in',eyebrow:'BUY WITH CONFIDENCE',description:'Check the cards, agree on an offer, then create labels.',empty:'Start with the customer’s cards.',copy:'Import their Roca export and check the cards first.<br>You’ll agree on the buy before adding stock or creating labels.',foot:'Payment and store credit are handled separately.'},bulk:{title:'Find value in bulk',eyebrow:'MORE TOOLS',description:'Filter a collection, then continue with the cards worth processing.',empty:'Find the cards worth keeping.',copy:'Import the full Roca export, then use the value filter.<br>Continue with the selected cards when you’re ready.',foot:'Finding value does not add inventory.'},online:{title:'Online preparation',eyebrow:'MORE TOOLS',description:'Check cards, assign storage, and prepare your listing files.',empty:'Get a batch ready for online listing.',copy:'Import your cards and check their details.<br>Storage and inventory choices come at the finish.',foot:'Creates files for manual listing. Nothing is published automatically.'}};
let state={mode:'labels',stage:'review',labelLayout:'side',printerDpi:203,receipt:null,receipts:[],settings:{},dirty:false,busy:false,page:1,pageSize:40,offer:'cash',editing:null,admin:false,configured:false,pickupQueue:[],watcher:{state:'off',folder:''}};
let saveTimer,toastTimer,pollTimer;let adminClosing=Promise.resolve();let polling=false,adminFocusPickup=false;let manualRequestId=null;let priceResearchOrigin='manual',priceResearchRequest=0;
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});let data;try{data=await r.json()}catch{throw Error('The station is not responding. Use Start Roca to reopen the station.')}if(!r.ok){if(data.adminRequired){state.admin=false}throw Error((data.errors||['The request could not complete.']).join('\n'))}return data}
function toast(message,error=false){clearTimeout(toastTimer);$('toast').textContent=message;$('toast').className='toast'+(error?' error':'');$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,error?8000:4500)}
function notice(message,error=false){$('notice').textContent=message;$('notice').className='notice'+(error?' error':'');$('notice').hidden=!message}
async function confirmAction(title,text,label='Confirm'){const d=$('confirmDialog');$('confirmTitle').textContent=title;$('confirmText').textContent=text;$('confirmOkay').textContent=label;d.showModal();return new Promise(resolve=>{let settled=false;const finish=value=>{if(settled)return;settled=true;d.close();resolve(value)};$('confirmOkay').onclick=()=>finish(true);$('confirmCancel').onclick=()=>finish(false);d.oncancel=e=>{e.preventDefault();finish(false)}})}
function included(){return(state.receipt?.cards||[]).filter(c=>c.include)}
function frozen(){return !!(state.receipt?.purchase||state.receipt?.inventory_status==='received'||['needs_attention','committing'].includes(state.receipt?.inventory_status))}
function locked(){return ['needs_attention','committing'].includes(state.receipt?.inventory_status)}
function markDirty(){if(!state.receipt)return;state.dirty=true;$('saveState').textContent='Unsaved changes';clearTimeout(saveTimer);saveTimer=setTimeout(()=>saveReceipt().catch(e=>toast(e.message,true)),1200)}
let saving=null;
async function saveReceipt(){clearTimeout(saveTimer);if(saving){await saving;if(state.dirty)return saveReceipt();return}if(!state.dirty||!state.receipt)return;const receipt=structuredClone(state.receipt);state.dirty=false;$('saveState').textContent='Saving…';saving=(async()=>{try{const data=await api('/api/receipts/'+receipt.id,receipt);if(state.receipt?.id===receipt.id){if(!state.dirty)state.receipt=data.receipt;else state.receipt.revision=data.receipt.revision}$('saveState').textContent=state.dirty?'Unsaved changes':'All changes saved'}catch(e){state.dirty=true;$('saveState').textContent='Save needs attention';throw e}finally{saving=null}})();await saving}
async function busyWork(work){if(state.busy)return;state.busy=true;renderActions();document.body.style.cursor='progress';try{await work()}catch(e){toast(e.message,true)}finally{state.busy=false;document.body.style.cursor='';renderActions()}}
function setMode(mode){
 state.mode=mode;document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));
 $('moreToolsNav').open=['bulk','online','activity'].includes(mode);
 const m=modes[mode];$('activityView').hidden=!!m;$('workflowContent').hidden=!m;
 $('pageTitle').textContent=m?.title||'Orders & activity';$('breadcrumb').textContent=m?.title||'Orders & activity';
 $('pageEyebrow').textContent=m?.eyebrow||'MORE TOOLS';$('pageDescription').textContent=m?.description||'Pull online orders and look up batch activity.';
 $('newReceipt').hidden=!m;$('addManualItem').hidden=!m;
 if(m){$('emptyTitle').textContent=m.empty;$('emptyDescription').innerHTML=m.copy;$('workflowFootnote').textContent=m.foot}
 else loadActivity();renderReceipt();renderPickup();
}
async function navigate(mode){if(state.busy)return;try{await saveReceipt();clearReceipt(mode)}catch(e){toast(e.message,true)}}
function clearReceipt(mode=state.mode){
 clearTimeout(saveTimer);closeCard();state.receipt=null;state.dirty=false;state.page=1;state.offer='cash';state.stage='review';state.editing=null;
 state.labelLayout=state.settings.labelLayout||'side';state.printerDpi=state.settings.printerDpi||203;
 $('receiptPicker').value='';$('cardSearch').value='';$('reviewFilter').value='all';$('csvFile').value='';$('minPrice').value=state.settings.minPrice??.7;
 for(const id of ['batchOptions','reviewOptions','printOptions'])$(id).open=false;
 $('historyDialog').close();$('outputFiles').replaceChildren();setMode(mode);$('saveState').textContent='Ready for a new batch';window.scrollTo(0,0);
}
async function startNewBatch(){if(state.busy)return;try{await saveReceipt();clearReceipt();if(!await openNextPickup())toast('Ready for the next export. Previous batches stay saved.')}catch(e){toast(e.message,true)}}


function adoptReceipt(r){
 state.receipt=r;state.dirty=false;state.page=1;state.offer=r.purchase?.type||'cash';
 state.stage=locked()?'finish':(r.files?.pdf||r.prepared)?'done':'review';
 state.labelLayout=state.settings.labelLayout||'side';state.printerDpi=state.settings.printerDpi||203;
 $('cardSearch').value='';$('reviewFilter').value='all';$('batchOptions').open=false;$('printOptions').open=false;$('reviewOptions').open=r.mode==='bulk';
 $('historyDialog').close();setMode(r.mode);$('receiptPicker').value=r.id;renderReceipt();
}
async function restoreIfDeleted(r){if(!r.deleted_at)return r;if(!await confirmAction('Restore this deleted batch?','Restore its saved cards and original inventory status. Any stock already received stays received, so reopening or reprinting will not add it again.','Restore batch'))return null;return(await api('/api/receipts/'+r.id+'/restore',{})).receipt}
async function deleteCurrentReceipt(){if(!state.receipt||state.busy)return;await busyWork(async()=>{await saveReceipt();const r=state.receipt;if(!await confirmAction('Delete this batch?',`Remove “${r.title}” from batch history? Square inventory will not change. Receiving history is kept to prevent duplicate uploads. Reimport the same CSV or open its Activity entry to restore it.`,'Delete batch'))return;await api('/api/receipts/'+r.id+'/delete',{revision:r.revision});clearReceipt();await refreshReceipts();toast('Batch removed from history. Square stock was not changed.')})}
async function refreshReceipts(){
 const data=await api('/api/receipts');state.receipts=data.receipts;state.pickupQueue=data.pickupQueue||[];state.watcher=data.watcher;
 for(const mode of Object.keys(modes))$('count-'+mode).textContent=data.receipts.filter(r=>r.mode===mode).length;
 const picker=$('receiptPicker'),old=state.receipt?.id||'';picker.replaceChildren(new Option('Open a saved batch…',''));
 for(const r of data.receipts)picker.add(new Option(`${r.title} · ${modes[r.mode]?.title||r.mode}`,r.id));picker.value=old;
 const watch=data.watcher;$('watchText').textContent=watch.state==='error'?'Folder needs attention':watch.folder?'Automatic pickup on':'Pickup not set up';
 $('watchStatus').title=watch.message;$('watchStatus').querySelector('.status-dot').style.background=watch.state==='watching'?'#6ebc9a':watch.state==='error'?'#d9a05f':'';
 renderHistory();renderPickup();
}
function renderHistory(){
 const query=$('historySearch').value.trim().toLowerCase();const rows=state.receipts.filter(r=>[r.title,r.id,r.source_name].join(' ').toLowerCase().includes(query));
 $('historyList').innerHTML=rows.length?rows.map(r=>`<button class="history-item" data-open="${escapeHTML(r.id)}"><span><strong>${escapeHTML(r.title)}</strong><small>${escapeHTML(modes[r.mode]?.title||r.mode)} · ${Number(r.units||0).toLocaleString()} copies · ${escapeHTML(r.id)}</small></span><span class="pill">${receiptStatus(r)}</span>${icon('chevron-right')}</button>`).join(''):'<p class="list-empty">No batches found.</p>';
}
async function openHistory(){if(state.busy)return;try{await saveReceipt();await refreshReceipts();$('historySearch').value='';renderHistory();$('historyDialog').showModal();$('historySearch').focus()}catch(e){toast(e.message,true)}}
async function acknowledgePickup(r){return r.pickup_pending?(await api('/api/receipts/'+r.id+'/pickup-opened',{})).receipt:r}
async function openReceipt(id){if(state.busy)return;await busyWork(async()=>{await saveReceipt();let r=(await api('/api/receipts/'+id)).receipt;r=await restoreIfDeleted(r);if(!r){$('receiptPicker').value=state.receipt?.id||'';return}r=await acknowledgePickup(r);adoptReceipt(r);await refreshReceipts()})}
function renderPickup(){
 const watch=state.watcher||{},ready=!!watch.folder,pending=state.pickupQueue.filter(r=>r.id!==state.receipt?.id);
 $('setupPickup').hidden=ready;$('pickupReady').hidden=!ready;$('pickupFolderHint').hidden=!ready;
 $('pickupReadyText').textContent=watch.state==='error'?'Export folder needs attention':'Automatic pickup is on';
 $('pickupReady').classList.toggle('attention',watch.state==='error');
 $('pickupFolderHint').textContent=watch.state==='error'?watch.message:'Export folder: '+watch.folder;
 $('emptyImport').className=ready?'button secondary':'button quiet';
 if(ready&&modes[state.mode]){
  $('emptyTitle').textContent='Ready for your next export.';
  $('emptyDescription').textContent='Press Export in Roca and save the CSV to the folder below. Your cards will appear here for review.';
 }else if(modes[state.mode]){
  $('emptyTitle').textContent=modes[state.mode].empty;
  $('emptyDescription').innerHTML=modes[state.mode].copy;
 }
 $('pickupNotice').hidden=!pending.length;
 $('pickupNoticeText').textContent=`${pending.length} ${pending.length===1?'export is':'exports are'} ready to review.`;
 $('openNextPickup').disabled=state.busy;
}
async function openNextPickup(){
 if(state.busy||!state.pickupQueue.length)return false;
 const next=state.pickupQueue.find(r=>r.id!==state.receipt?.id);if(!next)return false;
 await openReceipt(next.id);return true;
}
async function pollStation(){
 if(polling||state.busy||saving)return;polling=true;
 try{
  await refreshReceipts();
  if(!state.receipt&&!state.dirty&&modes[state.mode]&&!document.querySelector('dialog[open]')&&$('cardDrawer').hidden)await openNextPickup();
  if(state.mode==='activity')await loadActivity();
 }catch{}finally{polling=false}
}
async function setupPickup(){adminFocusPickup=true;await openAdmin()}

async function importFile(file){
 if(!file||state.busy)return;if(!file.name.toLowerCase().endsWith('.csv'))return toast('Choose a CSV export from Roca.',true);if(file.size>20_000_000)return toast('The CSV exceeds the 20 MB limit.',true);
 await busyWork(async()=>{try{
  await saveReceipt();const csv=await file.text();const payload={csv,fileName:file.name,mode:modes[state.mode]?state.mode:'labels'};let data=await api('/api/import',payload);
  if(data.receipt.deleted_at){const restored=await restoreIfDeleted(data.receipt);if(!restored)return;data.receipt=restored}
  else if(data.duplicate){adoptReceipt(data.receipt);const newDelivery=await confirmAction('This batch is already saved.','The saved batch has been opened. Only start another batch if these are a separate physical delivery of additional cards.','These are additional copies');if(newDelivery)data=await api('/api/import',{...payload,newDelivery:true})}
  data.receipt=await acknowledgePickup(data.receipt);adoptReceipt(data.receipt);await refreshReceipts();toast(data.duplicate?'Opened the saved batch. No stock changed.':'Cards imported. Check them before continuing.');
 }finally{$('csvFile').value=''}});
}
function receiptStatus(r){return r.inventory_status==='received'?'IN SQUARE':r.inventory_status==='needs_attention'?'NEEDS ATTENTION':r.inventory_status==='committing'?'ADDING TO SQUARE':r.prepared?'FILES READY':r.files?.pdf?'LABELS READY':r.purchase?'BUY RECORDED':r.cards?.some(c=>c.include)&&r.cards.filter(c=>c.include).every(c=>c.reviewed)?'CHECKED':'IN PROGRESS'}
function renderReceipt(){
 $('addManualItem').disabled=state.busy||frozen();
 const r=state.receipt;document.body.classList.toggle('has-batch',!!r);$('emptyState').hidden=!!r;$('receiptWorkspace').hidden=!r;
 if(!r){$('reviewStep').classList.remove('active');$('finishStep').classList.remove('active');$('importStep').classList.add('active');$('finishStepText').textContent='Finish';return;}
 $('receiptReference').textContent=`${r.source_name} · ${r.id}`;$('receiptTitle').value=r.title;$('receiptStatus').textContent=receiptStatus(r);
 $('receiptError').hidden=!r.error;$('receiptError').textContent=r.error||'';$('receiptNotes').value=r.notes||'';$('saveState').textContent=state.dirty?'Unsaved changes':'All changes saved';
 const buying=state.mode==='trade'&&!r.purchase;
 $('tradePanel').hidden=!buying;$('acceptedBuy').hidden=!r.purchase;
 if(r.purchase)$('acceptedBuy').textContent=`Buy recorded: ${usd(r.purchase.amount)} ${r.purchase.type==='cash'?'cash':'store credit'}.`;
 $('labelsPanel').hidden=state.mode==='bulk'||buying;$('onlinePanel').hidden=state.mode!=='online';
 $('customerReference').value=r.customer_reference||'';$('cashPercent').value=r.cash_percent;$('creditPercent').value=r.credit_percent;
 $('cashOffer').classList.toggle('selected',state.offer==='cash');$('creditOffer').classList.toggle('selected',state.offer==='credit');
 $('onlineChannel').value=r.channel;$('storageStrategy').value=r.storage_strategy;$('storageLocation').value=r.storage_location||'';
 document.querySelectorAll('[data-intent]').forEach(b=>{b.classList.toggle('selected',b.dataset.intent===r.inventory_intent);b.setAttribute('aria-pressed',String(b.dataset.intent===r.inventory_intent))});
 $('inventoryIntent').hidden=r.inventory_status==='received';
 $('labelsPanel').querySelector('h2').textContent=r.inventory_status==='received'?'These copies are already in Square.':'How would you like to finish?';
 $('newChoiceTitle').textContent=state.mode==='online'?'Add to Square & create files':'Add to Square & create labels';
 $('existingChoiceTitle').textContent=state.mode==='online'?'Create listing files only':'Create labels only';
 $('inventoryChoiceHint').textContent=r.inventory_status==='received'?'Reprints and prepared files add no more copies.':r.inventory_intent==='new'?'You will confirm the number of copies before Square is updated.':r.inventory_intent==='existing'?'No copies will be added to Square.':'Choose for this batch. Nothing has been added yet.';
 $('labelLayout').value=state.labelLayout;$('printerDpi').value=state.printerDpi;
 $('markReviewed').disabled=frozen();$('includePage').disabled=frozen();$('applyThreshold').disabled=frozen();
 $('splitCopy').hidden=frozen();renderStats();renderTable();renderFiles();renderStage();renderActions();
}
function renderStage(){
 $('reviewView').hidden=state.stage!=='review';$('finishView').hidden=state.stage!=='finish';$('doneView').hidden=state.stage!=='done';
 $('importStep').classList.remove('active');$('reviewStep').classList.toggle('active',state.stage==='review');$('finishStep').classList.toggle('active',state.stage!=='review');
 $('finishStepText').textContent=state.stage==='done'?'Done':'Finish';
 if(state.stage==='done')renderDone();
}
async function continueReview(){
 if(state.busy)return;await busyWork(async()=>{await saveReceipt();
  if(state.mode==='bulk'){await moveReceipt('labels');toast('Selected cards are ready. Check them before creating labels.');return}
  if(!included().length||!included().every(c=>c.reviewed))throw Error('Check every included card before continuing.');
  state.stage='finish';renderReceipt();window.scrollTo(0,0);
 });
}
async function showCards(){if(state.busy)return;try{await saveReceipt();state.stage='review';renderReceipt();window.scrollTo(0,0)}catch(e){toast(e.message,true)}}
function renderDone(){
 const r=state.receipt,files=r.files||{},copies=included().reduce((n,c)=>n+c.qty,0),online=state.mode==='online'&&r.prepared;
 $('doneTitle').textContent=online?'Your listing files are ready.':'Your labels are ready.';
 $('doneSummary').textContent=r.inventory_status==='received'?`${copies.toLocaleString()} copies confirmed in Square. Reprints add no more stock.`:`${copies.toLocaleString()} copies in this batch. No inventory was added to Square.`;
 $('donePrintHint').textContent=online?'Download the review sheet and publish the listings in your seller account.':'Print at Actual size / 100% on your 2″ × 1″ labels.';
 const file=online?files.listing:files.pdf;$('printLabels').hidden=!file;$('printLabels').href=file?'/files/'+encodeURIComponent(file):'#';
 $('printLabels').innerHTML=icon(online?'globe':'printer')+(online?'Download listing files':'Open labels to print');
}
function renderStats(){$('receiptStatus').textContent=receiptStatus(state.receipt);const cards=included(),copies=cards.reduce((n,c)=>n+c.qty,0),retail=cards.reduce((n,c)=>n+c.qty*c.final_price,0),reviewed=cards.filter(c=>c.reviewed).length;$('statCopies').textContent=copies.toLocaleString();$('statRows').textContent=`${cards.length.toLocaleString()} card rows`;$('statRetail').textContent=usd(retail);$('statReviewed').textContent=`${reviewed} / ${cards.length}`;$('reviewProgress').style.width=(cards.length?reviewed/cards.length*100:0)+'%';$('reviewHint').textContent=reviewed===cards.length&&cards.length?'Ready to continue':'Check condition, finish and price';$('cashAmount').textContent=usd(retail*(state.receipt.cash_percent||0)/100);$('creditAmount').textContent=usd(retail*(state.receipt.credit_percent||0)/100)}

function filteredCards(){if(!state.receipt)return[];const q=$('cardSearch').value.trim().toLowerCase(),filter=$('reviewFilter').value;return state.receipt.cards.filter(c=>(!q||[c.card_name,c.card_set,c.card_num,c.condition,c.printing,c.sku].join(' ').toLowerCase().includes(q))&&(filter!=='pending'||!c.reviewed)&&(filter!=='included'||c.include)&&(filter!=='excluded'||!c.include))}
function renderTable(){if(!state.receipt)return;const all=filteredCards(),pages=Math.max(1,Math.ceil(all.length/state.pageSize));state.page=Math.min(state.page,pages);const start=(state.page-1)*state.pageSize,cards=all.slice(start,start+state.pageSize);$('tableCount').textContent=state.receipt.cards.length.toLocaleString();$('cardRows').innerHTML=cards.map(c=>`<tr class="${c.include?'':'excluded'}"><td class="check-cell"><input type="checkbox" data-include="${c.row_id}" ${c.include?'checked':''} ${frozen()?'disabled':''} aria-label="Include ${escapeHTML(c.card_name)}"></td><td><div class="card-name" title="${escapeHTML(c.card_name)}">${escapeHTML(c.card_name)}</div><div class="card-meta">${escapeHTML(c.card_set||'Set not supplied')} · #${escapeHTML(c.card_num||'—')}${c.storage_location?' · '+escapeHTML(c.storage_location):''}</div></td><td><span class="condition-tag">${escapeHTML(c.condition)}</span><div class="finish-text">${escapeHTML(c.printing)}</div></td><td class="price-cell">${usd(c.final_price)}</td><td>${c.qty}</td><td><label class="row-checked"><input type="checkbox" data-review="${c.row_id}" ${c.reviewed?'checked':''} ${frozen()?'disabled':''} aria-label="Checked ${escapeHTML(c.card_name)}"><span>${c.reviewed?'Checked':'Check'}</span></label></td><td><button class="table-edit" data-edit="${c.row_id}" aria-label="Edit ${escapeHTML(c.card_name)}">${icon('edit')}<span>Edit</span></button></td></tr>`).join('');$('noMatches').hidden=!!cards.length;$('pageSummary').textContent=all.length?`Showing ${start+1}–${Math.min(start+state.pageSize,all.length)} of ${all.length.toLocaleString()} rows`:'No matching rows';$('pageNumber').textContent=`${state.page} / ${pages}`;$('previousPage').disabled=state.page<=1;$('nextPage').disabled=state.page>=pages;$('includePage').checked=cards.length>0&&cards.every(c=>c.include)}
function renderActions(){
 const r=state.receipt;$('newReceipt').disabled=state.busy;$('addManualItem').disabled=state.busy||frozen();$('openNextPickup').disabled=state.busy;if(!r)return;
 const reviewed=included().length>0&&included().every(c=>c.reviewed),buying=state.mode==='trade'&&!r.purchase,received=r.inventory_status==='received';
 document.querySelectorAll('[data-intent]').forEach(b=>b.disabled=state.busy||received||locked());
 for(const id of ['saveReceipt','deleteReceipt','moveReceipt','receiptTitle','receiptNotes'])$(id).disabled=state.busy||locked();
 $('backToReview').disabled=$('doneReview').disabled=$('nextBatch').disabled=state.busy;
 $('cashPercent').disabled=$('creditPercent').disabled=$('cashOffer').disabled=$('creditOffer').disabled=state.busy||!!r.purchase;
 $('reviewContinue').disabled=state.busy||!included().length||(state.mode!=='bulk'&&!reviewed)||locked();
 $('reviewContinue').innerHTML=(state.mode==='bulk'?'Continue with selected cards':'Continue')+icon('arrow-right');
 $('reviewNextHint').textContent=state.mode==='bulk'?'Continue with the included cards. Nothing is added to Square.':reviewed?'All included cards checked. Choose how to finish next.':'Check the included cards to continue.';
 $('printControls').hidden=state.mode==='bulk';$('labelsOnly').hidden=state.mode==='bulk'||buying;$('prepareOnly').hidden=state.mode!=='online';
 $('labelsOnly').disabled=$('prepareOnly').disabled=state.busy||!reviewed||locked();
 const main=$('primaryAction');main.disabled=state.busy||!reviewed||locked();
 if(r.inventory_status==='needs_attention'){
  main.textContent='Retry saved Square update';main.disabled=state.busy;$('actionTitle').textContent='Square needs attention.';$('actionHint').textContent='Use this batch’s saved retry. Keep the same batch so stock is not added twice.';
 }else if(buying){
  main.textContent=`Record ${state.offer==='cash'?'cash':'store-credit'} purchase`;$('actionTitle').textContent='Record the agreed offer.';$('actionHint').textContent='This records the buy. Payment is separate, and no stock is added yet.';
 }else if(!received&&!r.inventory_intent){
  main.textContent='Choose an option above';main.disabled=true;$('actionTitle').textContent='One choice for this batch.';$('actionHint').textContent='Choose whether these copies should be added to Square.';
 }else{
  const adding=!received&&r.inventory_intent==='new';
  main.innerHTML=(state.mode==='online'?(adding?'Add to Square & create files':'Create listing files'):(adding?'Add to Square & create labels':'Create labels'))+icon(adding?'arrow-right':'printer');
  $('actionTitle').textContent=adding?'Ready to add these copies?':state.mode==='online'?'Ready to prepare the files?':'Ready to create your labels?';
  $('actionHint').textContent=adding?'The next confirmation shows how many copies will be added.':'This action will not add any copies to Square.';
  if(adding&&!state.settings.squareConfigured){main.disabled=true;$('actionHint').textContent='Ask an admin to connect Square, or choose labels / files only.'}
 }
 if(state.busy)main.textContent='Working…';
}
function renderFiles(){const files=state.receipt?.files||{},labels={pdf:'Open label PDF',zpl:'Download Zebra ZPL',listing:'Download listing review CSV',ticket:'Open storage ticket',buy:'Open accepted buy receipt'};const items=Object.entries(files).filter(([key])=>labels[key]);$('outputFiles').hidden=!items.length;$('outputFiles').innerHTML=items.map(([key,file])=>`<a href="/files/${encodeURIComponent(file)}" target="_blank" rel="noopener">${icon(key==='listing'?'globe':'printer')}${labels[key]}</a>`).join('')}
async function moveReceipt(mode){if(!state.receipt||mode===state.receipt.mode)return;await saveReceipt();state.receipt.mode=mode;markDirty();await saveReceipt();state.stage='review';$('batchOptions').open=false;$('reviewOptions').open=mode==='bulk';setMode(mode);await refreshReceipts();toast('Same batch, next workflow. No stock changed.')}
async function labelsWithoutStock(){await busyWork(async()=>{if(state.receipt.inventory_status!=='received'){state.receipt.inventory_intent='existing';markDirty();await saveReceipt()}await createLabels()})}
async function prepareWithoutStock(){await busyWork(async()=>{if(state.receipt.inventory_status!=='received'){state.receipt.inventory_intent='existing';markDirty();await saveReceipt()}await action('prepare');state.stage='done';renderReceipt()})}
async function action(name,data={}){await saveReceipt();const result=await api(`/api/receipts/${state.receipt.id}/${name}`,data);state.receipt=result.receipt;state.dirty=false;renderReceipt();await refreshReceipts();return result.receipt}
async function createLabels(){const receipt=await action('labels',{layout:state.labelLayout,dpi:Number(state.printerDpi)});state.stage='done';renderReceipt();window.scrollTo(0,0);toast('Labels ready. Open them to print.');return receipt}
async function primaryAction(){
 if(!state.receipt||state.stage!=='finish')return;
 await busyWork(async()=>{
  const r=state.receipt;
  if(r.inventory_status==='needs_attention'){
   if(!await confirmAction('Retry the saved Square update?','The original saved requests will be reused. Keep the Square connection used for this batch.','Retry saved update'))return;
   const result=await action('receive');if(result.inventory_status!=='received')return toast(result.error||'Square needs attention.',true);
   if(state.mode==='online'){toast('Square confirmed the copies. Choose Create listing files to continue.');return}
   await createLabels();return;
  }
  if(!included().length||!included().every(c=>c.reviewed))throw Error('Check every included card before finishing.');
  if(state.mode==='trade'&&!r.purchase){
   const amount=state.offer==='cash'?$('cashAmount').textContent:$('creditAmount').textContent;
   if(await confirmAction('Record the agreed purchase?',`Record ${amount} ${state.offer==='cash'?'cash':'store credit'} as the agreed buy. Card details will be locked. Handle payment separately; no stock is added yet.`,'Record purchase')){await action('accept-buy',{offerType:state.offer});toast('Buy recorded. Choose how to finish this batch.')}return;
  }
  if(r.inventory_status!=='received'&&!['new','existing'].includes(r.inventory_intent))throw Error('Choose whether this batch should add stock.');
  if(state.mode==='online'&&!r.storage_location.trim()&&included().some(c=>!c.storage_location.trim())){$('storageLocation').focus();throw Error('Enter a storage location before creating listing files.');}
  if(r.inventory_intent==='new'&&r.inventory_status!=='received'){
   const qty=included().reduce((n,c)=>n+c.qty,0);
   if(!await confirmAction('Add these copies to Square?',`Add ${qty.toLocaleString()} copies from this batch to the saved Square store (${state.settings.squareEnvironment||'production'}). ${state.mode==='online'?'Listing files':'Labels'} will be created after Square confirms.`,'Add copies to Square'))return;
   const result=await action('receive');if(result.inventory_status!=='received')return toast(result.error||'Square needs attention.',true);
  }
  if(state.mode==='online'){await action('prepare');state.stage='done';renderReceipt();window.scrollTo(0,0);toast('Listing files are ready. Nothing was published.');return;}
  await createLabels();
 });
}
function openCard(id){const c=state.receipt.cards.find(c=>c.row_id===id);if(!c)return;state.editing=id;const map={editName:'card_name',editSet:'card_set',editNumber:'card_num',editCondition:'condition',editLanguage:'language',editFinish:'printing',editQty:'qty',editPrice:'final_price',editLocation:'storage_location',editSku:'sku_override'};for(const [id,key]of Object.entries(map)){$(id).value=c[key]??'';$(id).disabled=frozen()&&id!=='editLocation'}$('editReviewed').checked=c.reviewed;$('editReviewed').disabled=frozen();$('splitCopy').disabled=frozen()||c.qty<=1;$('splitCopy').hidden=frozen()||c.qty<=1;$('drawerMessage').textContent=frozen()?'Card identity is locked after purchase or receiving. Storage can still be updated.':'';$('cardDrawer').hidden=$('drawerBackdrop').hidden=false;$('editName').focus()}
async function openPriceResearch(origin){
 priceResearchOrigin=origin;
 const prefix=origin==='manual'?'manual':'edit';
 if(!$(prefix+'Name').value.trim())return toast('Enter the item name before comparing prices.',true);
 $('priceResearchDialog').showModal();await renderPriceResearch();
}
async function renderPriceResearch(){
 const request=++priceResearchRequest,p=priceResearchOrigin==='manual'?'manual':'edit';
 $('priceResearchMessage').textContent='Preparing source searches…';$('priceResearchLinks').replaceChildren();
 try{
  const data=await api('/api/pricing/links',{game:$('priceResearchGame').value,card_name:$(p+'Name').value,
   card_set:$(p+'Set').value,card_num:$(p+'Number').value,condition:$(p+'Condition').value,
   printing:$(p+'Finish').value,language:$(p+'Language').value});
  if(request!==priceResearchRequest)return;
  $('priceResearchName').textContent=data.title;$('priceResearchDetails').textContent=data.details;
  $('priceResearchMessage').textContent=data.gradeNeedsReview?'Confirm the exact grader and grade tier; an ambiguous grade needs review.':'';
  const slab=data.links.tcgplayer_role==='raw_card_reference';
  $('priceResearchHint').textContent=slab?'Match the same grader, grade, premium tier and printing. TCGplayer is a raw-card reference; its raw price does not establish slab value.':'Match set, number, finish, language and condition. Sold prices and current asking prices are separate comparisons.';
  const sources=[['ebay_sold','eBay sold prices','Check completed sales and actual accepted prices.'],
   ['ebay_listings','eBay current listings','Compare what sellers are asking today.'],
   ['pricecharting','PriceCharting guide','Check the exact printing and the matching grade column.'],
   ['tcgplayer',slab?'TCGplayer · raw reference':'TCGplayer values','Inspect the exact product’s market, sales and listings.']];
  $('priceResearchLinks').innerHTML=sources.map(([key,title,copy])=>`<a href="${escapeHTML(data.links[key])}" target="_blank" rel="noopener noreferrer"><span><strong>${title}</strong><small>${copy}</small></span>${icon('arrow-right')}</a>`).join('');
 }catch(e){if(request===priceResearchRequest)$('priceResearchMessage').textContent=e.message}
}
async function openManualItem(){
 if(state.busy||frozen())return;
 try{
  await saveReceipt();$('manualItemForm').reset();manualRequestId=crypto.randomUUID();
  $('manualItemMessage').textContent='';
  $('manualItemDescription').textContent=state.receipt?'Add to this batch: '+state.receipt.title:'Create a batch without a scan or CSV. Enter a graded card or any item.';
  if(state.receipt?.requires_purchase||state.mode==='trade')$('manualItemDescription').textContent+=' Trade offers use your normal cash and store-credit percentages.';
  $('manualItemDialog').showModal();$('manualName').focus();
 }catch(e){toast(e.message,true)}
}
async function saveManualItem(e){
 e.preventDefault();if(state.busy)return;
 $('manualItemForm').inert=true;$('closeManualItem').disabled=true;
 await busyWork(async()=>{
  try{
   await saveReceipt();const r=state.receipt;
   const item={card_name:$('manualName').value,final_price:$('manualPrice').value,qty:$('manualQty').value,
    condition:$('manualCondition').value,card_set:$('manualSet').value,card_num:$('manualNumber').value,
    printing:$('manualFinish').value,language:$('manualLanguage').value};
   const result=await api('/api/manual-items',{requestId:manualRequestId,receiptId:r?.id,revision:r?.revision,mode:state.mode,item});
   $('manualItemDialog').close();adoptReceipt(result.receipt);state.stage='review';
   $('cardSearch').value='';$('reviewFilter').value='all';state.page=Math.max(1,Math.ceil(result.receipt.cards.length/state.pageSize));renderReceipt();
   await refreshReceipts();window.scrollTo(0,0);toast('Item added. Check it, then continue to labels or Square.');
  }catch(err){$('manualItemMessage').textContent=err.name==='TypeError'?'Connection interrupted. Click Add item again to retry safely.':err.message}
 });
 $('manualItemForm').inert=false;$('closeManualItem').disabled=false;
}
function closeCard(){$('cardDrawer').hidden=$('drawerBackdrop').hidden=true;state.editing=null}
async function splitOneCopy(){
 if(state.busy)return;$('cardForm').inert=true;
 try{await busyWork(async()=>{
  editCardFromForm();const card=state.receipt.cards.find(c=>c.row_id===state.editing);
  if(card.qty<2)throw Error('At least two copies are needed to separate one.');
  card.qty--;const clone=structuredClone(card);clone.row_id=crypto.randomUUID();clone.split_from=card.row_id;clone.qty=1;clone.reviewed=false;
  state.receipt.cards.splice(state.receipt.cards.indexOf(card)+1,0,clone);markDirty();await saveReceipt();renderReceipt();openCard(clone.row_id);
  toast('One copy separated. Check its condition, finish and price.');
 })}finally{$('cardForm').inert=false}
}
function editCardFromForm(){const c=state.receipt.cards.find(c=>c.row_id===state.editing);if(!c)return;const previous={...c};Object.assign(c,{card_name:$('editName').value.trim(),card_set:$('editSet').value.trim(),card_num:$('editNumber').value.trim(),condition:$('editCondition').value.trim(),language:$('editLanguage').value.trim(),printing:$('editFinish').value.trim(),qty:Number($('editQty').value),final_price:Number($('editPrice').value),storage_location:$('editLocation').value.trim(),sku_override:$('editSku').value.trim(),reviewed:$('editReviewed').checked});if(!c.card_name||!c.condition||!c.printing||!Number.isInteger(c.qty)||c.qty<1||!Number.isFinite(c.final_price)||c.final_price<0){Object.assign(c,previous);throw Error('Enter a name, condition, finish, whole-number quantity and valid price.')}markDirty()}
async function loadActivity(){try{const data=await api('/api/activity');const pending=data.tasks.filter(t=>t.status==='pending');$('taskDot').hidden=!pending.length;$('taskList').innerHTML=data.tasks.length?data.tasks.map(t=>`<article class="task-card ${t.status==='pulled'?'pulled':''}"><div><strong>${t.qty} × ${escapeHTML(t.card_name)}</strong><p>${escapeHTML(t.storage_location)} · ${escapeHTML(t.channel)} · Order ${escapeHTML(t.order_reference)}</p><small>${escapeHTML(t.notes||'')}</small></div>${t.status==='pending'?`<button class="button secondary small" data-complete="${t.id}">Confirm pulled</button>`:'<span class="pill">PULLED</span>'}</article>`).join(''):'<div class="card-table-panel list-empty">No pull tasks yet. Record an online order when a card needs to leave the shop floor.</div>';$('eventList').innerHTML=data.events.length?data.events.map(e=>`<article class="event-row"><span class="event-icon">${icon(e.kind==='labels'?'printer':e.kind==='received'?'check-circle':'activity')}</span><div><a href="#" data-open="${escapeHTML(e.receipt_id)}">${escapeHTML(e.receipt_id)}</a><p>${escapeHTML(e.detail)}</p></div><time>${new Date(e.at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}</time></article>`).join(''):'<div class="list-empty">Your receipt history will appear here.</div>'}catch(e){toast(e.message,true)}}
async function openAdmin(){try{await adminClosing;const auth=await api('/api/auth');state.admin=auth.authenticated;state.configured=auth.configured;$('adminMessage').textContent='';$('adminAuth').hidden=state.admin;$('adminSettings').hidden=!state.admin;$('adminTitle').textContent=state.admin?'Settings that keep work moving.':auth.configured?'Unlock your workspace settings.':'Set up your admin password.';$('authDescription').textContent=auth.configured?'Your saved Square connection stays available to operators. Only admins can change it.':'Choose a password of at least 12 characters. You’ll use it to manage credentials and workflow defaults.';$('confirmPasswordLabel').hidden=auth.configured;$('confirmPassword').required=!auth.configured;$('adminPassword').autocomplete=auth.configured?'current-password':'new-password';$('authSubmit').innerHTML=(auth.configured?'Unlock Admin':'Create admin password')+icon('unlock');$('adminPassword').value=$('confirmPassword').value='';$('adminDialog').showModal();if(state.admin)await loadAdmin();else $('adminPassword').focus()}catch(e){toast(e.message,true)}}
async function loadAdmin(){$('adminTitle').textContent='Settings that keep work moving.';const settings=await api('/api/admin/settings');$('squareToken').value='';$('squareToken').placeholder=settings.tokenSaved?'Saved token · leave blank to keep':'Enter a Square token';$('tokenStatus').textContent=settings.tokenSaved?'Token saved securely. Enter a replacement only when needed.':'No token saved yet.';$('squareEnvironment').value=settings.squareEnvironment;$('squareLocation').replaceChildren(new Option(settings.squareLocationId?'Saved receiving store · '+settings.squareLocationId:'Test connection to load stores',settings.squareLocationId||''));$('watchFolder').value=settings.watchFolder;$('watchMode').value=settings.watchMode;$('defaultCash').value=settings.cashPercent;$('defaultCredit').value=settings.creditPercent;$('defaultMinimum').value=settings.minPrice;$('defaultDpi').value=settings.printerDpi;$('defaultLayout').value=settings.labelLayout;$('squareConnectionMessage').textContent='';$('adminAuth').hidden=true;$('adminSettings').hidden=false;if(adminFocusPickup){adminFocusPickup=false;$('pickupSettings').scrollIntoView({block:'start'});$('watchFolder').focus({preventScroll:true})}}
async function loadPublicSettings(){state.settings=await api('/api/settings');$('minPrice').value=state.settings.minPrice??.7;const ready=state.settings.squareConfigured;$('squareBadge').classList.toggle('ready',!!ready);$('squareBadge').innerHTML=`<span class="status-dot"></span>${ready?'Square saved · '+escapeHTML(state.settings.squareEnvironment):'Square setup needed'}`}
function bind(){document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>navigate(b.dataset.mode));$('newReceipt').onclick=startNewBatch;$('compareManualPrices').onclick=()=>openPriceResearch('manual');$('compareCardPrices').onclick=()=>openPriceResearch('edit');$('closePriceResearch').onclick=$('returnToItem').onclick=()=>$('priceResearchDialog').close();$('priceResearchGame').onchange=renderPriceResearch;$('addManualItem').onclick=openManualItem;$('manualItemForm').onsubmit=saveManualItem;$('closeManualItem').onclick=$('cancelManualItem').onclick=()=>{if(!state.busy)$('manualItemDialog').close()};$('manualItemDialog').addEventListener('cancel',e=>{if(state.busy)e.preventDefault()});$('emptyImport').onclick=()=>$('csvFile').click();$('setupPickup').onclick=setupPickup;$('openNextPickup').onclick=openNextPickup;$('useDownloads').onclick=async()=>{try{const data=await api('/api/admin/downloads-folder',{});$('watchFolder').value=data.folder;$('adminMessage').textContent='Downloads selected. Save settings to start pickup; then export a new CSV from Roca.'}catch(e){$('adminMessage').textContent=e.message}};$('deleteReceipt').onclick=deleteCurrentReceipt;$('reviewContinue').onclick=continueReview;$('backToReview').onclick=$('doneReview').onclick=showCards;$('nextBatch').onclick=startNewBatch;$('openHistory').onclick=openHistory;$('closeHistory').onclick=()=>$('historyDialog').close();$('historySearch').oninput=renderHistory;$('historyList').onclick=e=>{const b=e.target.closest('[data-open]');if(b)openReceipt(b.dataset.open)};$('labelLayout').onchange=()=>state.labelLayout=$('labelLayout').value;$('printerDpi').onchange=()=>state.printerDpi=Number($('printerDpi').value);$('csvFile').onchange=()=>importFile($('csvFile').files[0]);$('receiptPicker').onchange=e=>{if(e.target.value)openReceipt(e.target.value).catch(e=>toast(e.message,true));else startNewBatch()};$('receiptTitle').oninput=()=>{state.receipt.title=$('receiptTitle').value;markDirty()};$('receiptNotes').oninput=()=>{state.receipt.notes=$('receiptNotes').value;markDirty()};$('saveReceipt').onclick=()=>saveReceipt().then(()=>toast('Batch saved.')).catch(e=>toast(e.message,true));$('moveReceipt').onchange=async e=>{const mode=e.target.value;e.target.value='';if(mode)await busyWork(()=>moveReceipt(mode))};$('cardSearch').oninput=$('reviewFilter').onchange=()=>{state.page=1;renderTable()};$('previousPage').onclick=()=>{state.page--;renderTable()};$('nextPage').onclick=()=>{state.page++;renderTable()};$('cardRows').onclick=e=>{if(state.busy)return;const b=e.target.closest('[data-edit]');if(b)openCard(b.dataset.edit)};$('cardRows').onchange=e=>{if(state.busy)return renderTable();const checked=e.target.dataset.review;if(checked){state.receipt.cards.find(c=>c.row_id===checked).reviewed=e.target.checked;markDirty();renderStats();renderTable();renderActions();return}const id=e.target.dataset.include;if(id){state.receipt.cards.find(c=>c.row_id===id).include=e.target.checked;markDirty();renderStats();renderActions()}};$('includePage').onchange=()=>{const cards=filteredCards().slice((state.page-1)*state.pageSize,state.page*state.pageSize);cards.forEach(c=>c.include=$('includePage').checked);markDirty();renderStats();renderTable();renderActions()};$('applyThreshold').onclick=()=>{const threshold=Number($('minPrice').value);if(!Number.isFinite(threshold)||threshold<0)return toast('Enter a valid threshold.',true);state.receipt.cards.forEach(c=>c.include=c.final_price>=threshold);markDirty();renderStats();renderTable();renderActions()};$('markReviewed').onclick=async()=>{if(await confirmAction('Have you checked every included card?','Confirm you have checked the identity, condition, finish and final price of every included card. This does not receive inventory.','I checked these cards')){included().forEach(c=>c.reviewed=true);markDirty();renderStats();renderTable();renderActions()}};$('primaryAction').onclick=primaryAction;$('prepareOnly').onclick=prepareWithoutStock;$('labelsOnly').onclick=labelsWithoutStock;$('cashOffer').onclick=e=>{if(e.target.tagName!=='INPUT'){state.offer='cash';renderActions();$('cashOffer').classList.add('selected');$('creditOffer').classList.remove('selected')}};$('creditOffer').onclick=e=>{if(e.target.tagName!=='INPUT'){state.offer='credit';renderActions();$('creditOffer').classList.add('selected');$('cashOffer').classList.remove('selected')}};for(const [id,key]of [['cashPercent','cash_percent'],['creditPercent','credit_percent']])$(id).oninput=()=>{state.receipt[key]=Number($(id).value);markDirty();renderStats()};$('customerReference').oninput=()=>{state.receipt.customer_reference=$('customerReference').value;markDirty()};document.querySelectorAll('[data-intent]').forEach(b=>b.onclick=()=>{state.receipt.inventory_intent=b.dataset.intent;markDirty();renderReceipt()});for(const [id,key]of [['onlineChannel','channel'],['storageStrategy','storage_strategy'],['storageLocation','storage_location']])$(id).onchange=()=>{state.receipt[key]=$(id).value;markDirty()};$('closeDrawer').onclick=$('drawerBackdrop').onclick=closeCard;$('cardForm').onsubmit=async e=>{e.preventDefault();$('cardForm').inert=true;try{editCardFromForm();await saveReceipt();closeCard();renderReceipt();toast('Card saved.')}catch(err){$('drawerMessage').textContent=err.message}finally{$('cardForm').inert=false}};for(const id of ['editName','editSet','editNumber','editCondition','editLanguage','editFinish'])$(id).oninput=()=>{$('editReviewed').checked=false;if($('editSku').value)$('editSku').value='';$('priceReminder').textContent='Details changed. Confirm the price for this exact variant before marking reviewed.'};$('editPrice').oninput=()=>$('editReviewed').checked=false;$('splitCopy').onclick=splitOneCopy;$('openAdmin').onclick=openAdmin;$('closeAdmin').onclick=()=>{adminFocusPickup=false;$('adminDialog').close()};$('adminDialog').addEventListener('close',()=>{adminClosing=api('/api/auth/logout',{}).catch(()=>{});state.admin=false;$('squareToken').value='';$('adminPassword').value='';});$('authForm').onsubmit=async e=>{e.preventDefault();try{if(!state.configured&&$('adminPassword').value!==$('confirmPassword').value)throw Error('The passwords do not match.');await api('/api/auth/'+(state.configured?'login':'setup'),{password:$('adminPassword').value});$('adminPassword').value=$('confirmPassword').value='';state.admin=state.configured=true;notice('');$('adminMessage').textContent='';await loadAdmin()}catch(e){$('adminMessage').textContent=e.message;$('adminMessage').classList.add('error')}};$('lockAdmin').onclick=async()=>{await api('/api/auth/logout',{});state.admin=false;$('adminDialog').close();toast('Admin locked.')};$('squareEnvironment').onchange=$('squareToken').oninput=()=>{$('squareLocation').replaceChildren(new Option('Test connection to load stores',''));$('squareConnectionMessage').textContent='Connection changed. Test it before choosing a store.'};$('testSquare').onclick=async()=>{const button=$('testSquare');button.disabled=true;try{const result=await api('/api/admin/locations',{squareToken:$('squareToken').value,squareEnvironment:$('squareEnvironment').value});const previous=$('squareLocation').value;const active=result.locations.filter(l=>l.status==='ACTIVE');$('squareLocation').replaceChildren(new Option('Choose receiving store',''));active.forEach(l=>$('squareLocation').add(new Option(`${l.name} (${l.id})`,l.id)));if(active.some(l=>l.id===previous))$('squareLocation').value=previous;else if(active.length===1)$('squareLocation').value=active[0].id;$('squareConnectionMessage').textContent=active.length?`Connected to ${result.environment}. ${active.length} active store(s). Verify your store, then save settings.`:'Connected, but no active locations were returned. Activate the receiving location in Square.'}catch(e){$('squareConnectionMessage').textContent=e.message}finally{button.disabled=false}};$('adminForm').onsubmit=async e=>{e.preventDefault();try{const data={squareToken:$('squareToken').value,squareEnvironment:$('squareEnvironment').value,squareLocationId:$('squareLocation').value,watchFolder:$('watchFolder').value,watchMode:$('watchMode').value,cashPercent:Number($('defaultCash').value),creditPercent:Number($('defaultCredit').value),minPrice:Number($('defaultMinimum').value),printerDpi:Number($('defaultDpi').value),labelLayout:$('defaultLayout').value};await api('/api/admin/settings',data);$('squareToken').value='';await loadPublicSettings();await loadAdmin();$('adminMessage').textContent='Settings saved. Operators can use the connection without entering the token.';$('adminMessage').classList.remove('error');await refreshReceipts();toast('Admin settings saved.')}catch(e){$('adminMessage').textContent=e.message;$('adminMessage').classList.add('error')}};$('changePasswordForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/admin/password',{password:$('newAdminPassword').value});$('newAdminPassword').value='';$('adminDialog').close();state.admin=false;toast('Password changed. All admin sessions are locked.')}catch(e){$('adminMessage').textContent=e.message}};$('eventList').onclick=e=>{const b=e.target.closest('[data-open]');if(b){e.preventDefault();openReceipt(b.dataset.open)}};$('taskList').onclick=async e=>{const b=e.target.closest('[data-complete]');if(b&&await confirmAction('Confirm the cards were pulled?','Confirm the requested copies have been removed from the shop floor and placed with the order. This does not change inventory.','Confirm pulled')){try{await api('/api/tasks/'+b.dataset.complete+'/complete',{});await loadActivity();toast('Pull confirmed.')}catch(e){toast(e.message,true)}}};$('newTask').onclick=()=>$('taskDialog').showModal();$('closeTask').onclick=()=>$('taskDialog').close();$('taskForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/tasks',{channel:$('taskChannel').value,order_reference:$('taskOrder').value,card_name:$('taskCard').value,qty:Number($('taskQty').value),storage_location:$('taskLocation').value,notes:$('taskNotes').value});$('taskDialog').close();$('taskForm').reset();await loadActivity();toast('Order added to the pull queue.')}catch(e){$('taskMessage').textContent=e.message}};$('helpButton').onclick=()=>confirmAction('A few things to know.','Set up automatic pickup once in Admin. Press Export in Roca and save to that folder; your cards appear for review. Check your cards, then Continue. At the finish, choose Add to Square or labels only. Next batch starts fresh; Batch history reopens saved work. Printer settings are saved in Admin. Closing Chrome leaves Roca running; use Stop Roca to shut down.','Got it');let dragDepth=0;document.addEventListener('dragenter',e=>{if(e.dataTransfer.types.includes('Files')){e.preventDefault();dragDepth++;$('dropOverlay').hidden=false}});document.addEventListener('dragleave',()=>{dragDepth--;if(dragDepth<=0)$('dropOverlay').hidden=true});document.addEventListener('dragover',e=>e.preventDefault());document.addEventListener('drop',e=>{e.preventDefault();dragDepth=0;$('dropOverlay').hidden=true;if(e.dataTransfer.files.length)importFile(e.dataTransfer.files[0])});window.addEventListener('beforeunload',e=>{if(state.dirty){e.preventDefault();e.returnValue=''}});document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('cardDrawer').hidden&&!document.querySelector('dialog[open]'))closeCard();if((e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();saveReceipt().then(()=>toast('Batch saved.')).catch(e=>toast(e.message,true))}})}
async function init(){icons();bind();try{const version=await api('/api/version');if(version.version!=='2.3.1')throw Error('An older station is running. Close it and launch the complete Roca Station folder.');$('version').textContent=version.version;await loadPublicSettings();await refreshReceipts();setMode('labels');const auth=await api('/api/auth');state.configured=auth.configured;if(!auth.configured)notice('Welcome to Roca Station. Set an admin password and connect Square in Admin settings when you’re ready.');await pollStation();pollTimer=setInterval(pollStation,3000)}catch(e){notice(e.message,true)}}
init();
