'use strict';
const $=id=>document.getElementById(id);
const paths={layers:'M12 3 2 8l10 5 10-5-10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5',exchange:'M3 7h16l-4-4m4 14H3l4 4M19 7l-4 4M3 17l4-4',tag:'M3 3h7l11 11-7 7L3 10V3Zm4 4h.01',globe:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18Z',activity:'M3 12h4l3-8 4 16 3-8h4',lock:'M6 10h12v11H6V10Zm3 0V6a3 3 0 0 1 6 0v4m-3 5v2',unlock:'M6 10h12v11H6V10Zm3 0V6a3 3 0 0 1 6 0m-3 9v2',help:'M12 17h.01M9 9a3 3 0 0 1 6 0c0 2-3 2-3 4m9-1a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',plus:'M12 5v14M5 12h14',scan:'M3 8V3h5m8 0h5v5m0 8v5h-5M8 21H3v-5m4-8h1v8H7m4-8h2v8h-2m5-8h1v8h-1',upload:'M12 15V3m-4 4 4-4 4 4M4 14v6h16v-6',shield:'M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6l-9-4Zm-4 9 3 3 5-5',qr:'M3 3h6v6H3V3Zm12 0h6v6h-6V3ZM3 15h6v6H3v-6Zm12 0h2v2h4v4h-6v-6Zm6-3h-3M12 3v3m0 6h3M3 12h5m4 5v4',check:'m5 12 4 4L19 6','check-circle':'m8 12 3 3 5-6m5 3a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',save:'M4 3h13l4 4v14H4V3Zm4 0v6h8V3m-8 18v-7h9v7',dollar:'M12 2v20m5-16H9a4 4 0 0 0 0 8h6a4 4 0 0 1 0 8H6',search:'m16 16 5 5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z','chevron-left':'m14 5-7 7 7 7','chevron-right':'m10 5 7 7-7 7','arrow-right':'M4 12h16m-6-6 6 6-6 6',printer:'M6 9V3h12v6M6 17H3V9h18v8h-3m-12-3h12v7H6v-7m11-2h.01',x:'m6 6 12 12M6 18 18 6',plug:'M8 3v5m8-5v5M6 8h12v4a6 6 0 0 1-12 0V8Zm6 10v4',edit:'m16 3 5 5L8 21H3v-5L16 3Zm-2 2 5 5'};
function icon(name){return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name]||paths.layers}"/></svg>`}
function icons(root=document){root.querySelectorAll('i[data-icon]').forEach(el=>{el.innerHTML=icon(el.dataset.icon)})}
function escapeHTML(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
const usd=n=>Number(n||0).toLocaleString('en-US',{style:'currency',currency:'USD'});
const modes={labels:{title:'Process cards',eyebrow:'FROM SCAN TO SHELF',description:'Check your cards. Create labels. Choose whether to add stock.',empty:'Let your exports come to you.',copy:'Choose your export folder once. Then press Export in Roca,<br>and your cards will arrive here for review.',foot:'Your work is saved automatically.'},trade:{title:'Customer trade-in',eyebrow:'BUY WITH CONFIDENCE',description:'Check the cards, agree on an offer, then create labels.',empty:'Start with the customer’s cards.',copy:'Import their Roca export and check the cards first.<br>You’ll agree on the buy before adding stock or creating labels.',foot:'Payment and store credit are handled separately.'},bulk:{title:'Find value in bulk',eyebrow:'MORE TOOLS',description:'Filter a collection, then continue with the cards worth processing.',empty:'Find the cards worth keeping.',copy:'Import the full Roca export, then use the value filter.<br>Continue with the selected cards when you’re ready.',foot:'Finding value does not add inventory.'},online:{title:'Online preparation',eyebrow:'MORE TOOLS',description:'Check cards, assign storage, and prepare your listing files.',empty:'Get a batch ready for online listing.',copy:'Import your cards and check their details.<br>Storage and inventory choices come at the finish.',foot:'Creates files for manual listing. Nothing is published automatically.'}};
let state={mode:'labels',stage:'review',labelLayout:'side',printerDpi:203,receipt:null,receipts:[],settings:{},dirty:false,busy:false,page:1,pageSize:40,offer:'cash',editing:null,admin:false,configured:false,pickupQueue:[],watcher:{state:'off',folder:''}};
let saveTimer,toastTimer,pollTimer;let adminClosing=Promise.resolve();let polling=false,adminFocusPickup=false;let manualRequestId=null;let priceResearchOrigin='manual',priceResearchRequest=0;let pricingResults={},priceResearchResult=null,priceResearchKey=null,priceResearchTimer;let researchPage=1,researchPane='cards',autoConsidering=new Set();let creditAccount=null,creditTrade=null,creditCustomerRequest=null,creditEntryRequest=null,creditSearchTimer;
async function api(path,body,signal){const r=await fetch(path,{signal,method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});let data;try{data=await r.json()}catch{throw Error('The station is not responding. Use Start Roca to reopen the station.')}if(!r.ok){if(data.adminRequired){state.admin=false}throw Error((data.errors||['The request could not complete.']).join('\n'))}return data}
function toast(message,error=false){clearTimeout(toastTimer);$('toast').textContent=message;$('toast').className='toast'+(error?' error':'');$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,error?8000:4500)}
function notice(message,error=false){$('notice').textContent=message;$('notice').className='notice'+(error?' error':'');$('notice').hidden=!message}
async function confirmAction(title,text,label='Confirm'){const d=$('confirmDialog');$('confirmTitle').textContent=title;$('confirmText').textContent=text;$('confirmOkay').textContent=label;d.showModal();return new Promise(resolve=>{let settled=false;const finish=value=>{if(settled)return;settled=true;d.close();resolve(value)};$('confirmOkay').onclick=()=>finish(true);$('confirmCancel').onclick=()=>finish(false);d.oncancel=e=>{e.preventDefault();finish(false)}})}
function included(){return(state.receipt?.cards||[]).filter(c=>c.include)}
function frozen(){return !!(state.receipt?.purchase||state.receipt?.inventory_status==='received'||['needs_attention','committing'].includes(state.receipt?.inventory_status))}
function priceLocked(){return state.receipt?.inventory_status==='received'||locked()}
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
 else if(mode!=='credit'&&mode!=='cloud')loadActivity();$('creditView').hidden=mode!=='credit';if(mode==='credit'){$('activityView').hidden=true;$('pageTitle').textContent=$('breadcrumb').textContent='Local credit';$('pageEyebrow').textContent='THIS COMPUTER';$('pageDescription').textContent='Local history from this installation. Shared balances are in Shared credit.';loadCreditCustomers().catch(e=>toast(e.message,true))}
 const cloudView=$('cloudView');if(cloudView)cloudView.hidden=mode!=='cloud';if(mode==='cloud'){$('activityView').hidden=true;$('pageTitle').textContent=$('breadcrumb').textContent='Shared credit';$('pageEyebrow').textContent='YOUR STORE TEAM';$('pageDescription').textContent='One customer balance across your signed-in store computers.';window.enterCloudWorkspace?.()}renderReceipt();renderPickup();
}
async function navigate(mode){if(state.busy)return;try{await saveReceipt();clearReceipt(mode)}catch(e){toast(e.message,true)}}
function clearReceipt(mode=state.mode){
 clearTimeout(saveTimer);closeCard();state.receipt=null;pricingResults={};researchPage=1;researchPane='cards';autoConsidering.clear();state.dirty=false;state.page=1;state.offer='cash';state.stage='review';state.editing=null;
 state.labelLayout=state.settings.labelLayout||'side';state.printerDpi=state.settings.printerDpi||203;
 $('receiptPicker').value='';$('cardSearch').value='';$('reviewFilter').value='all';$('csvFile').value='';$('minPrice').value=state.settings.minPrice??.7;
 for(const id of ['batchOptions','reviewOptions','printOptions'])$(id).open=false;
 $('historyDialog').close();$('outputFiles').replaceChildren();setMode(mode);$('saveState').textContent='Ready for a new batch';window.scrollTo(0,0);
}
async function startNewBatch(){if(state.busy)return;try{await saveReceipt();clearReceipt();if(!await openNextPickup())toast('Ready for the next export. Previous batches stay saved.')}catch(e){toast(e.message,true)}}


function adoptReceipt(r){
 state.receipt=r;pricingResults={};researchPage=1;researchPane='cards';autoConsidering.clear();state.dirty=false;state.page=1;state.offer=r.purchase?.type||'cash';
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
  await refreshReceipts();if(state.mode==='credit'&&creditAccount&&!document.querySelector('dialog[open]'))await loadCreditAccount(creditAccount.customer.id);
  if(!state.receipt&&!state.dirty&&modes[state.mode]&&!document.querySelector('dialog[open]')&&$('cardDrawer').hidden)await openNextPickup();
  if(state.receipt)await refreshPricing();if(state.mode==='activity')await loadActivity();
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
function receiptStatus(r){return r.inventory_status==='received'?'IN SQUARE':r.inventory_status==='needs_attention'?'NEEDS ATTENTION':r.inventory_status==='committing'?'ADDING TO SQUARE':r.prepared?'FILES READY':r.files?.pdf?'LABELS READY':r.purchase?'BUY RECORDED':r.cards?.some(c=>c.include)&&r.cards.filter(c=>c.include).every(c=>c.reviewed&&!c.price_missing)?'CHECKED':'IN PROGRESS'}
function renderReceipt(){
 renderIntakeSummary();
 $('addManualItem').disabled=state.busy||frozen();
 const r=state.receipt;document.body.classList.toggle('has-batch',!!r);$('emptyState').hidden=!!r;$('receiptWorkspace').hidden=!r;
 if(!r){$('reviewStep').classList.remove('active');$('finishStep').classList.remove('active');$('importStep').classList.add('active');$('finishStepText').textContent='Finish';return;}
 $('receiptReference').textContent=`${r.source_name} · ${r.id}`;$('receiptTitle').value=r.title;$('receiptStatus').textContent=receiptStatus(r);
 $('receiptError').hidden=!r.error;$('receiptError').textContent=r.error||'';$('receiptNotes').value=r.notes||'';$('saveState').textContent=state.dirty?'Unsaved changes':'All changes saved';
 const buying=state.mode==='trade'&&!r.purchase;
 $('tradePanel').hidden=!buying;renderTradeSummary();
 $('labelsPanel').hidden=state.mode==='bulk'||buying;$('onlinePanel').hidden=state.mode!=='online';
 $('customerReference').value=r.customer_reference||'';$('cashPercent').value=r.cash_percent;$('creditPercent').value=r.credit_percent;
 $('cashOffer').classList.toggle('selected',state.offer==='cash');$('creditOffer').classList.toggle('selected',state.offer==='credit');
 $('onlineChannel').value=r.channel;$('storageStrategy').value=r.storage_strategy;$('storageLocation').value=r.storage_location||'';
 document.querySelectorAll('[data-intent]').forEach(b=>{b.classList.toggle('selected',b.dataset.intent===r.inventory_intent);b.setAttribute('aria-pressed',String(b.dataset.intent===r.inventory_intent))});
 $('inventoryIntent').hidden=r.inventory_status==='received';
 $('labelsPanel').querySelector('h2').textContent=r.inventory_status==='received'?'These copies are already in Square.':'How would you like to finish?';
 $('newChoiceTitle').textContent=state.mode==='online'?'Add to Square & create files':'Add to Square & create labels';
 $('existingChoiceTitle').textContent=state.mode==='online'?'Create listing files only':'Create labels only';
 $('inventoryChoiceHint').textContent=r.inventory_status==='received'?'Reprints and prepared files add no more copies.':r.inventory_intent==='new'?'Matching card details and prices add quantity. A different price gets a new variation and barcode; existing prices stay unchanged.':r.inventory_intent==='existing'?'No copies will be added to Square.':'Choose for this batch. Nothing has been added yet.';
 $('labelLayout').value=state.labelLayout;$('printerDpi').value=state.printerDpi;
 $('markReviewed').disabled=priceLocked();$('includePage').disabled=frozen();$('applyThreshold').disabled=frozen();
 $('batchPricingGame').value=r.pricing_game||'';$('batchPricingGame').disabled=frozen();$('researchBatch').disabled=state.busy;renderPricingSummary();$('splitCopy').hidden=frozen();renderStats();renderTable();renderResearchTable();showResearchPane(researchPane);renderFiles();renderStage();renderActions();
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
  if(!included().length||!included().every(c=>c.reviewed&&!c.price_missing))throw Error('Check every included card before continuing.');
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
function renderStats(){$('receiptStatus').textContent=receiptStatus(state.receipt);const cards=included(),copies=cards.reduce((n,c)=>n+c.qty,0),retail=cards.reduce((n,c)=>n+c.qty*c.final_price,0),reviewed=cards.filter(c=>c.reviewed).length;$('statCopies').textContent=copies.toLocaleString();$('statRows').textContent=`${cards.length.toLocaleString()} card rows`;$('statRetail').textContent=usd(retail);$('statReviewed').textContent=`${reviewed} / ${cards.length}`;$('reviewProgress').style.width=(cards.length?reviewed/cards.length*100:0)+'%';$('reviewHint').textContent=reviewed===cards.length&&cards.length?'Ready to continue':'Check condition, finish and price';const intake=cards.reduce((n,c)=>n+c.qty*(c.intake_value??c.final_price),0);$('cashAmount').textContent=usd(intake*(state.receipt.cash_percent||0)/100);$('creditAmount').textContent=usd(intake*(state.receipt.credit_percent||0)/100)}

function filteredCards(){if(!state.receipt)return[];const q=$('cardSearch').value.trim().toLowerCase(),filter=$('reviewFilter').value;return state.receipt.cards.filter(c=>(!q||[c.card_name,c.card_set,c.card_num,c.condition,c.printing,c.sku].join(' ').toLowerCase().includes(q))&&(filter!=='pending'||!c.reviewed)&&(filter!=='included'||c.include)&&(filter!=='excluded'||!c.include))}
function renderTable(){if(!state.receipt)return;const all=filteredCards(),pages=Math.max(1,Math.ceil(all.length/state.pageSize));state.page=Math.min(state.page,pages);const start=(state.page-1)*state.pageSize,cards=all.slice(start,start+state.pageSize);$('tableCount').textContent=state.receipt.cards.length.toLocaleString();$('cardRows').innerHTML=cards.map(c=>`<tr class="${c.include?'':'excluded'}"><td class="check-cell"><input type="checkbox" data-include="${c.row_id}" ${c.include?'checked':''} ${frozen()?'disabled':''} aria-label="Include ${escapeHTML(c.card_name)}"></td><td><div class="card-name" title="${escapeHTML(c.card_name)}">${escapeHTML(c.card_name)}</div><div class="card-meta">${c.game?escapeHTML(c.game_label||intakeGames[c.game]||c.game)+' · ':''}${escapeHTML(c.card_set||'Set not supplied')} · #${escapeHTML(c.card_num||'—')}${c.storage_location?' · '+escapeHTML(c.storage_location):''}</div></td><td><span class="condition-tag">${escapeHTML(c.condition)}</span><div class="finish-text">${escapeHTML(c.printing)}</div></td><td class="price-cell">${c.price_missing?'<strong class="credit-debit">Price required</strong>':usd(c.final_price)}${c.intake_value!==undefined?`<small class="intake-value">Intake ${usd(c.intake_value)}</small>`:""}${priceCellResearch(c)}</td><td>${c.qty}</td><td><label class="row-checked"><input type="checkbox" data-review="${c.row_id}" ${c.reviewed?'checked':''} ${priceLocked()||c.price_missing?'disabled':''} aria-label="Checked ${escapeHTML(c.card_name)}"><span>${c.reviewed?'Checked':'Check'}</span></label></td><td><button class="table-edit" data-edit="${c.row_id}" aria-label="Edit ${escapeHTML(c.card_name)}">${icon('edit')}<span>Edit</span></button></td></tr>`).join('');$('noMatches').hidden=!!cards.length;$('pageSummary').textContent=all.length?`Showing ${start+1}–${Math.min(start+state.pageSize,all.length)} of ${all.length.toLocaleString()} rows`:'No matching rows';$('pageNumber').textContent=`${state.page} / ${pages}`;$('previousPage').disabled=state.page<=1;$('nextPage').disabled=state.page>=pages;$('includePage').checked=cards.length>0&&cards.every(c=>c.include)}
function renderActions(){
 const r=state.receipt;$('newReceipt').disabled=state.busy;$('addManualItem').disabled=state.busy||frozen();$('openNextPickup').disabled=state.busy;if(!r)return;
 const reviewed=included().length>0&&included().every(c=>c.reviewed&&!c.price_missing),buying=state.mode==='trade'&&!r.purchase,received=r.inventory_status==='received';
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
  if(included().some(c=>c.price_missing))throw Error('Resolve missing selling prices before finishing this batch.');
  if(r.inventory_status==='needs_attention'){
   if(!await confirmAction('Retry the saved Square update?','The original saved requests will be reused. Keep the Square connection used for this batch.','Retry saved update'))return;
   const result=await action('receive');if(result.inventory_status!=='received')return toast(result.error||'Square needs attention.',true);
   if(state.mode==='online'){toast('Square confirmed the copies. Choose Create listing files to continue.');return}
   await createLabels();return;
  }
  if(!included().length||!included().every(c=>c.reviewed&&!c.price_missing))throw Error('Check every included card before finishing.');
  if(state.mode==='trade'&&!r.purchase){
   const amount=state.offer==='cash'?$('cashAmount').textContent:$('creditAmount').textContent;
   if(await confirmAction('Record the agreed purchase?',`Record ${amount} ${state.offer==='cash'?'cash':'store credit'} as the agreed buy. Card details will be locked. Handle payment separately; no stock is added yet.`,'Record purchase')){await action('accept-buy',{offerType:state.offer});toast('Buy recorded. Choose how to finish this batch.')}return;
  }
  if(r.inventory_status!=='received'&&!['new','existing'].includes(r.inventory_intent))throw Error('Choose whether this batch should add stock.');
  if(state.mode==='online'&&!r.storage_location.trim()&&included().some(c=>!c.storage_location.trim())){$('storageLocation').focus();throw Error('Enter a storage location before creating listing files.');}
  if(r.inventory_intent==='new'&&r.inventory_status!=='received'){
   const qty=included().reduce((n,c)=>n+c.qty,0);
   if(!await confirmAction('Add these copies to Square?',`Add ${qty.toLocaleString()} copies from this batch to the saved Square store (${state.settings.squareEnvironment||'production'}). Existing stock prices stay unchanged. ${state.mode==='online'?'Listing files':'Labels'} will be created after Square confirms.`,'Add copies to Square'))return;
   const result=await action('receive');if(result.inventory_status!=='received')return toast(result.error||'Square needs attention.',true);
  }
  if(state.mode==='online'){await action('prepare');state.stage='done';renderReceipt();window.scrollTo(0,0);toast('Listing files are ready. Nothing was published.');return;}
  await createLabels();
 });
}
function openCard(id){const c=state.receipt.cards.find(c=>c.row_id===id);if(!c)return;state.editing=id;const map={editName:'card_name',editSet:'card_set',editNumber:'card_num',editCondition:'condition',editLanguage:'language',editFinish:'printing',editQty:'qty',editPrice:'final_price',editLocation:'storage_location',editSku:'sku_override',...intakeEditMap};for(const [id,key]of Object.entries(map)){$(id).value=c[key]??'';$(id).disabled=(id==='editPrice'?priceLocked():frozen()&&id!=='editLocation')}intakeOpenCard(c);$('editReviewed').checked=c.price_missing?false:c.reviewed;intakePriceState();$('splitCopy').disabled=frozen()||c.qty<=1;$('splitCopy').hidden=frozen()||c.qty<=1;$('drawerMessage').textContent=[frozen()?'Identity is locked. Before receiving, the retail label price can still be updated; the agreed buy stays fixed.':'',c.pricing_notice?c.pricing_notice.reasons.join(' ')+' Review the research, or mark checked to keep your chosen price.':''].filter(Boolean).join(' ');$('cardDrawer').hidden=$('drawerBackdrop').hidden=false;$('editName').focus()}
function priceCellResearch(c){const r=pricingResults[c.row_id];if(!r)return '';const profile=state.mode==='trade'&&!state.receipt.purchase?'intake':'retail',value=r[profile+'_price'];return `<button type="button" class="price-suggestion" data-price-research="${c.row_id}">${value!==null&&value!==undefined?'Review '+usd(value):['queued','running'].includes(r.status)?'Researching…':'View research'}</button>`}
function renderPricingSummary(){if(!$('pricingSummary'))return;const rows=Object.values(pricingResults),pending=rows.filter(r=>['queued','running'].includes(r.status)).length,suggestions=rows.filter(r=>r.retail_price!==undefined&&r.retail_price!==null).length;$('pricingCount').textContent=rows.length?`${rows.length-pending} / ${rows.length}`:'';$('pricingSummary').textContent=!state.settings.pricingEnabled?'Automatic research is off in Admin.':!state.receipt?.pricing_game?'Choose the game to research this batch.':pending?`Researching ${pending.toLocaleString()} rows. You can keep checking cards while research runs.`:suggestions?`${suggestions.toLocaleString()} rows have a price suggestion. Review each before applying.`:state.settings.pricingCompleteOnly?'Full-source pricing is not available in this preview. Open a card to see coverage.':'Review source coverage and cards needing an exact match. Open Price research to review source coverage.'}
async function refreshPricing(){
 const id=state.receipt?.id;if(!id)return;
 const data=await api('/api/receipts/'+id+'/pricing');if(state.receipt?.id!==id)return;
 pricingResults=data.cards;renderPricingSummary();renderTable();renderResearchTable();
 if(state.dirty||state.busy||saving||priceLocked()||!$('cardDrawer').hidden||document.querySelector('dialog[open]'))return;
 const rows=Object.entries(data.cards).filter(([row,r])=>(r.auto_eligible||['conflict','reference'].includes(r.status))&&!autoConsidering.has(id+row+r.checked_at)).slice(0,500);
 if(!rows.length)return;
 rows.forEach(([row,r])=>autoConsidering.add(id+row+r.checked_at));
 // Keep forms and batch actions stable during this short revision-checked write.
 state.busy=true;document.body.inert=true;
 try{
  const changed=await api('/api/receipts/'+id+'/apply-safe-prices',{revision:state.receipt.revision,rowIds:rows.map(([row])=>row)},AbortSignal.timeout(15000));
  if(state.receipt?.id===id&&!state.dirty){state.receipt=changed.receipt;renderReceipt();if(changed.applied)toast(`${changed.applied} small price correction(s) applied.`);if(changed.alerts)toast(`${changed.alerts} pricing discrepancy/discrepancies need review.`,true)}
 }catch(e){if(state.receipt?.id===id)toast(e.name==='TimeoutError'?'Price update timed out. Reopen this batch before editing.':e.message,true)}
 finally{document.body.inert=false;state.busy=false;renderActions()}
}
async function openPriceResearch(origin){
 priceResearchOrigin=origin;const p=origin==='manual'?'manual':'edit';
 if(!$(p+'Name').value.trim())return toast('Enter the item name before researching.',true);
 try{if(origin==='edit'){editCardFromForm();await saveReceipt()}
 const sourceCard=origin==='edit'?state.receipt.cards.find(card=>card.row_id===state.editing):null;intakeSetGame($('priceResearchGame'),origin==='manual'?intakeSelectedGame('manualGame'):(sourceCard?.game||state.receipt?.pricing_game||state.settings.pricingGame||'pokemon'),sourceCard?.game_label||$('manualGameLabel').value);
 $('priceResearchGame').disabled=origin==='edit'||!!state.receipt?.pricing_game;$('priceResearchProfile').value=origin==='edit'&&state.mode==='trade'&&!state.receipt.purchase?'intake':'retail';
 $('priceResearchProfile').querySelector('[value="intake"]').disabled=origin==='manual'||!!state.receipt?.purchase;
 $('priceResearchDialog').showModal();await renderPriceResearch();
 }catch(e){toast(e.message,true)}
}
async function renderPriceResearch(){
 const request=++priceResearchRequest,p=priceResearchOrigin==='manual'?'manual':'edit';clearTimeout(priceResearchTimer);priceResearchResult=null;
 $('approveSuggestedPrice').disabled=true;$('priceProposal').hidden=true;$('priceResearchLinks').replaceChildren();$('priceResearchHint').textContent='';
 $('priceResearchName').textContent=$(p+'Name').value;$('priceResearchDetails').textContent=['Set','Number','Condition','Finish','Language'].map(k=>$(p+k).value).filter(Boolean).join(' · ');
 $('priceResearchMessage').textContent='Researching this exact card…';
 try{const data=await api('/api/pricing/research',{game:$('priceResearchGame').value,card_name:$(p+'Name').value,card_set:$(p+'Set').value,card_num:$(p+'Number').value,condition:$(p+'Condition').value,printing:$(p+'Finish').value,language:$(p+'Language').value,...intakeExtraFields(p)});
 if(request!==priceResearchRequest)return;priceResearchKey=data.key;await pollPriceResearch(request);
 }catch(e){if(request===priceResearchRequest)$('priceResearchMessage').textContent=e.message}
}
async function pollPriceResearch(request){
 try{const data=await api('/api/pricing/jobs/'+priceResearchKey);if(request!==priceResearchRequest||!$('priceResearchDialog').open)return;priceResearchResult=data;paintPriceResearch(data);if(['queued','running'].includes(data.status))priceResearchTimer=setTimeout(()=>pollPriceResearch(request),1200)}catch(e){if(request===priceResearchRequest)$('priceResearchMessage').textContent=e.message}
}
function paintPriceResearch(data){
 if(!data)return;const profile=$('priceResearchProfile').value,p=priceResearchOrigin==='manual'?'manual':'edit';
 const card=state.receipt?.cards.find(c=>c.row_id===state.editing),current=profile==='intake'?(card?.intake_value??Number($(p+'Price').value)):Number($(p+'Price').value),suggestion=data[profile+'_price'];
 $('priceResearchMessage').textContent=[data.message,...(data.flags||[])].filter(Boolean).join(' ');$('priceProposal').hidden=suggestion===null||suggestion===undefined;
 $('currentPriceLabel').textContent=profile==='intake'?'Current intake value':'Current label price';$('priceCurrent').textContent=usd(current);$('priceSuggested').textContent=usd(suggestion);
 $('priceResearchHint').textContent=profile==='intake'?`${state.settings.intakeValuePercent??90}% value setting. Your cash/store-credit buying percentages apply after this; the label price stays separate.`:`${state.settings.retailValuePercent??100}% retail setting. Approval changes the label price. An accepted purchase amount stays fixed.`;
 $('priceResearchLinks').innerHTML=Object.entries(data.sources||{}).map(([key,s])=>`<article class="source-evidence"><div><strong>${escapeHTML(s.label||key)}</strong><span class="evidence-status">${escapeHTML({reference:'Market reference',not_connected:'Not connected',not_applicable:'Excluded',unavailable:'Unavailable',needs_identity:'Check match'}[s.status]||s.status)}</span></div>${(s.market||s.low||s.median)?`<p class="evidence-price">${usd(s.market||s.low||s.median)} <small>${s.market?'market reference':s.low?'lowest listing reference':'verified source median'}${s.low_listing?' · low asking '+usd(s.low_listing):''}</small></p>`:''}${s.product_name?`<p>${escapeHTML(s.product_name)} · ${escapeHTML(s.set_name)} · ${escapeHTML(s.finish)}</p>`:''}<p>${escapeHTML(s.message||((s.count!==undefined)?`${s.count} qualifying record(s).`:''))}</p>${s.downloaded_at?`<small>Downloaded ${escapeHTML(new Date(s.downloaded_at).toLocaleString())}${s.source_updated_at?' · snapshot '+escapeHTML(s.source_updated_at):' · source update time not supplied'}</small>`:''}</article>`).join('');
 $('approveSuggestedPrice').disabled=suggestion===null||suggestion===undefined||(priceResearchOrigin==='edit'&&priceLocked());$('approveSuggestedPrice').textContent='Approve '+(profile==='intake'?'intake value':'label price');
}
async function approveSuggestedPrice(){
 const data=priceResearchResult,profile=$('priceResearchProfile').value;if(!data||data[profile+'_price']===null||data[profile+'_price']===undefined)return;
 $('approveSuggestedPrice').disabled=true;
 try{const latest=await api('/api/pricing/jobs/'+data.key);if(latest.checked_at!==data.checked_at||latest[profile+'_price']!==data[profile+'_price']||latest[profile+'_price']===null)throw Error('This suggestion changed or expired. Close and reopen research before approving.');if(priceResearchOrigin==='manual'){$('manualPrice').value=Number(data.retail_price).toFixed(2);$('priceResearchDialog').close();toast('Suggested label price applied. Save the item when ready.');return}
 await saveReceipt();const r=state.receipt,c=r.cards.find(c=>c.row_id===state.editing);
 const response=await api('/api/receipts/'+r.id+'/approve-price',{revision:r.revision,rowId:c.row_id,key:data.key,checkedAt:data.checked_at,profile,suggestedPrice:data[profile+'_price'],currentPrice:profile==='intake'?(c.intake_value??c.final_price):c.final_price});
 state.receipt=response.receipt;state.dirty=false;const updated=state.receipt.cards.find(c=>c.row_id===state.editing);$('editPrice').value=updated.final_price;$('editReviewed').checked=false;$('priceResearchDialog').close();renderReceipt();toast('Price approved. Check the card details before continuing.');
 }catch(e){$('priceResearchMessage').textContent=e.message;paintPriceResearch(data);$('priceResearchMessage').textContent=e.message}
}
async function openManualItem(){
 if(state.busy||frozen())return;
 try{
  await saveReceipt();$('manualItemForm').reset();manualRequestId=crypto.randomUUID();
  $('manualItemMessage').textContent='';$('manualGame').value=state.receipt?.pricing_game||state.settings.pricingGame||'pokemon';$('manualGame').disabled=!!state.receipt?.pricing_game;
  $('manualItemDescription').textContent=state.receipt?'Add to this batch: '+state.receipt.title:'Create a batch without a scan or CSV. Enter a graded card or any item.';
  if(state.receipt?.requires_purchase||state.mode==='trade')$('manualItemDescription').textContent+=' Trade offers use your normal cash and store-credit percentages.';
  intakeManualOpen();$('manualItemDialog').showModal();$('manualName').focus();
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
    printing:$('manualFinish').value,language:$('manualLanguage').value,...intakeExtraFields('manual')};
   const result=await api('/api/manual-items',{requestId:manualRequestId,receiptId:r?.id,revision:r?.revision,mode:state.mode,game:intakeSelectedGame('manualGame'),item});
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
function editCardFromForm(){const c=state.receipt.cards.find(c=>c.row_id===state.editing);if(!c)return;const previous={...c};Object.assign(c,{card_name:$('editName').value.trim(),card_set:$('editSet').value.trim(),card_num:$('editNumber').value.trim(),condition:$('editCondition').value.trim(),language:$('editLanguage').value.trim(),printing:$('editFinish').value.trim(),qty:Number($('editQty').value),final_price:Number($('editPrice').value),storage_location:$('editLocation').value.trim(),sku_override:$('editSku').value.trim(),...intakeExtraFields('edit'),reviewed:$('editReviewed').checked});if((c.price_missing&&!$('editPrice').value.trim())||!c.card_name||!c.condition||!c.printing||!Number.isInteger(c.qty)||c.qty<1||!Number.isFinite(c.final_price)||c.final_price<0){Object.assign(c,previous);throw Error('Enter a name, condition, finish, whole-number quantity and valid price.')}markDirty()}
async function loadActivity(){try{const data=await api('/api/activity');const pending=data.tasks.filter(t=>t.status==='pending');$('taskDot').hidden=!pending.length;$('taskList').innerHTML=data.tasks.length?data.tasks.map(t=>`<article class="task-card ${t.status==='pulled'?'pulled':''}"><div><strong>${t.qty} × ${escapeHTML(t.card_name)}</strong><p>${escapeHTML(t.storage_location)} · ${escapeHTML(t.channel)} · Order ${escapeHTML(t.order_reference)}</p><small>${escapeHTML(t.notes||'')}</small></div>${t.status==='pending'?`<button class="button secondary small" data-complete="${t.id}">Confirm pulled</button>`:'<span class="pill">PULLED</span>'}</article>`).join(''):'<div class="card-table-panel list-empty">No pull tasks yet. Record an online order when a card needs to leave the shop floor.</div>';$('eventList').innerHTML=data.events.length?data.events.map(e=>`<article class="event-row"><span class="event-icon">${icon(e.kind==='labels'?'printer':e.kind==='received'?'check-circle':'activity')}</span><div><a href="#" data-open="${escapeHTML(e.receipt_id)}">${escapeHTML(e.receipt_id)}</a><p>${escapeHTML(e.detail)}</p></div><time>${new Date(e.at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}</time></article>`).join(''):'<div class="list-empty">Your receipt history will appear here.</div>'}catch(e){toast(e.message,true)}}
async function openAdmin(){try{await adminClosing;const auth=await api('/api/auth');state.admin=auth.authenticated;state.configured=auth.configured;$('adminMessage').textContent='';$('adminAuth').hidden=state.admin;$('adminSettings').hidden=!state.admin;$('adminTitle').textContent=state.admin?'Settings that keep work moving.':auth.configured?'Unlock your workspace settings.':'Set up your admin password.';$('authDescription').textContent=auth.configured?'Your saved Square connection stays available to operators. Only admins can change it.':'Choose a password of at least 12 characters. You’ll use it to manage credentials and workflow defaults.';$('confirmPasswordLabel').hidden=auth.configured;$('confirmPassword').required=!auth.configured;$('adminPassword').autocomplete=auth.configured?'current-password':'new-password';$('authSubmit').innerHTML=(auth.configured?'Unlock Admin':'Create admin password')+icon('unlock');$('adminPassword').value=$('confirmPassword').value='';$('adminDialog').showModal();if(state.admin)await loadAdmin();else $('adminPassword').focus()}catch(e){toast(e.message,true)}}
async function loadAdmin(){$('adminTitle').textContent='Settings that keep work moving.';const settings=await api('/api/admin/settings');$('squareToken').value='';$('squareToken').placeholder=settings.tokenSaved?'Saved token · leave blank to keep':'Enter a Square token';$('tokenStatus').textContent=settings.tokenSaved?'Token saved securely. Enter a replacement only when needed.':'No token saved yet.';$('squareEnvironment').value=settings.squareEnvironment;$('squareLocation').replaceChildren(new Option(settings.squareLocationId?'Saved receiving store · '+settings.squareLocationId:'Test connection to load stores',settings.squareLocationId||''));$('watchFolder').value=settings.watchFolder;$('watchMode').value=settings.watchMode;$('defaultCash').value=settings.cashPercent;$('defaultCredit').value=settings.creditPercent;$('defaultMinimum').value=settings.minPrice;$('defaultDpi').value=settings.printerDpi;$('defaultLayout').value=settings.labelLayout;$('pricingAutoCorrect').checked=settings.pricingAutoCorrect;$('pricingAlertPercent').value=settings.pricingAlertPercent;$('pricingAlertDollars').value=settings.pricingAlertDollars;$('pricingEnabled').checked=settings.pricingEnabled;$('pricingCompleteOnly').checked=settings.pricingCompleteOnly;$('defaultPricingGame').value=settings.pricingGame;$('intakeValuePercent').value=settings.intakeValuePercent;$('retailValuePercent').value=settings.retailValuePercent;$('squareConnectionMessage').textContent='';$('adminAuth').hidden=true;$('adminSettings').hidden=false;if(adminFocusPickup){adminFocusPickup=false;$('pickupSettings').scrollIntoView({block:'start'});$('watchFolder').focus({preventScroll:true})}}
async function loadPublicSettings(){state.settings=await api('/api/settings');$('creditNav').hidden=!state.settings.creditTracking;$('editionLabel').textContent=state.settings.creditTracking?'CREDIT TRACKER':'STANDARD';$('minPrice').value=state.settings.minPrice??.7;const ready=state.settings.squareConfigured;$('squareBadge').classList.toggle('ready',!!ready);$('squareBadge').innerHTML=`<span class="status-dot"></span>${ready?'Square saved · '+escapeHTML(state.settings.squareEnvironment):'Square setup needed'}`}
function showResearchPane(pane){researchPane=pane;$('cardsPane').hidden=pane!=='cards';$('researchPane').hidden=pane!=='research';$('cardsTab').setAttribute('aria-selected',String(pane==='cards'));$('researchTab').setAttribute('aria-selected',String(pane==='research'));if(pane==='research')renderResearchTable()}
function needsPriceReview(c,r){return !!c.pricing_notice||r.status==='conflict'&&!(c.pricing_acknowledged?.key===r.key&&c.pricing_acknowledged?.checked_at===r.checked_at)}
function renderResearchTable(){
 if(!state.receipt)return;const filter=$('researchFilter').value;
 const rows=state.receipt.cards.filter(c=>{const r=pricingResults[c.row_id]||{};return filter==='all'||filter==='attention'&&(needsPriceReview(c,r)||['needs_identity','unavailable','incomplete'].includes(r.status))||filter==='suggested'&&r.retail_price!==null&&r.retail_price!==undefined});
 const pages=Math.max(1,Math.ceil(rows.length/40));researchPage=Math.min(researchPage,pages);const start=(researchPage-1)*40;
 const names={queued:'Queued',running:'Researching',reference:'Partial reference',ready:'Research complete',conflict:'Needs review',unavailable:'Source unavailable',needs_identity:'Check identity',incomplete:'Missing evidence',not_requested:'Not requested',stale:'Research expired'};
 $('researchRows').innerHTML=rows.slice(start,start+40).map(c=>{const r=pricingResults[c.row_id]||{},warning=needsPriceReview(c,r);return `<tr><td><div class="card-name">${escapeHTML(c.card_name)}</div><div class="card-meta">${escapeHTML(c.card_set)} #${escapeHTML(c.card_num)} · ${escapeHTML(c.condition)} · ${escapeHTML(c.printing)}</div></td><td>${r.intake_price!==null&&r.intake_price!==undefined?usd(r.intake_price):'—'}</td><td><strong>${r.retail_price!==null&&r.retail_price!==undefined?usd(r.retail_price):'—'}</strong><small class="intake-value">Current label ${usd(c.final_price)}${c.label_rounding?' · Pokémon $1 rule':''}</small></td><td><span class="pill ${warning?'attention-pill':''}">${warning?'Needs review':names[r.status]||'Waiting'}</span>${c.automatic_price?'<small class="intake-value">Small correction applied</small>':''}</td><td><button class="button secondary small" data-research-edit="${c.row_id}">Review</button></td></tr>`}).join('');
 const alerts=state.receipt.cards.filter(c=>needsPriceReview(c,pricingResults[c.row_id]||{})).length;$('researchAlertCount').textContent=alerts?String(alerts):'';$('researchOverview').textContent=alerts?`${alerts} discrepancy/discrepancies need your review.`:'Partial references require approval. Automatic correction needs complete verified evidence within the store’s threshold.';
 $('researchPageSummary').textContent=rows.length?`${start+1}–${Math.min(start+40,rows.length)} of ${rows.length.toLocaleString()} cards`:'No cards in this view';$('researchPrevious').disabled=researchPage<=1;$('researchNext').disabled=researchPage>=pages;
}
function renderTradeSummary(){
 const r=state.receipt,p=r?.purchase;$('acceptedBuy').hidden=!p;if(!p)return;
 const credit=p.type==='credit',tracked=r.tracked_credit;
 $('acceptedBuy').innerHTML=`<div><span class="section-kicker">APPROVED TRADE · ${credit?'STORE CREDIT':'CASH'}</span><strong class="trade-approved-amount">${usd(p.amount)}</strong><p>Buy recorded: ${usd(p.amount)} ${credit?'store credit':'cash'}.</p></div><div class="trade-facts"><span><small>Customer / reference</small>${escapeHTML(r.customer_reference||'Not supplied')}</span><span><small>Approved</small>${escapeHTML(new Date(p.at).toLocaleString())}</span><span><small>Intake value</small>${usd(p.intake_total??r.cards.filter(c=>c.include).reduce((n,c)=>n+c.qty*(c.intake_value??c.final_price),0))}</span><span><small>Status</small>${credit?(tracked?'Recorded in local credit tracker':'Credit agreement recorded; delivery handled separately'):'Payment handled separately'}</span></div>${credit&&state.settings.creditTracking&&!tracked?'<button class="button secondary" id="trackApprovedCredit">Track this approved credit</button>':''}`;
 const button=$('trackApprovedCredit');if(button)button.onclick=async()=>{creditTrade={id:r.id,amount:p.amount,title:r.title};await navigate('credit');toast('Choose a customer, then record the approved trade credit.')};
}
async function loadCreditCustomers(){if(!state.settings.creditTracking)return;const data=await api('/api/credit/search',{query:$('creditSearch').value});$('creditCustomerList').innerHTML=data.customers.length?data.customers.map(c=>`<button class="credit-customer" data-credit-customer="${escapeHTML(c.id)}"><span><strong>${escapeHTML(c.name)}</strong><small>${escapeHTML(c.contact||c.square_reference||'No contact supplied')}</small></span><b>${usd(c.balance)}</b></button>`).join(''):'<p class="list-empty">No matching customers.</p>'}
async function loadCreditAccount(id){creditAccount=await api('/api/credit/account',{customerId:id});renderCreditAccount()}
function renderCreditAccount(){
 if(!creditAccount)return;const c=creditAccount.customer;
 $('creditAccount').innerHTML=`<div class="credit-balance-header"><div><span class="section-kicker">TRACKED CREDIT</span><h2>${escapeHTML(c.name)}</h2><p>${escapeHTML(c.contact)}</p></div><strong>${usd(c.balance)}</strong></div><div class="credit-account-actions"><button id="exportCreditHistory" class="button secondary">Export history</button><button id="recordCreditUse" class="button primary">Record credit used</button>${creditTrade?`<button id="recordTradeCredit" class="button secondary">Add approved trade · ${usd(creditTrade.amount)}</button>`:''}<button id="recordCreditAdjustment" class="button secondary">Opening balance / adjustment</button></div><div class="table-scroll"><table><thead><tr><th>Date / type</th><th>Change</th><th>Balance</th><th>Reference</th></tr></thead><tbody>${creditAccount.entries.map(e=>`<tr><td>${escapeHTML(new Date(e.at).toLocaleDateString())}<small class="intake-value">${escapeHTML({trade:'Trade credit',redeem:'Credit used',opening:'Opening balance',adjust_add:'Adjustment added',adjust_remove:'Adjustment removed'}[e.kind]||e.kind)}</small></td><td class="${e.amount<0?'credit-debit':'credit-add'}">${e.amount>0?'+':''}${usd(e.amount)}</td><td>${usd(e.balance_after)}</td><td>${escapeHTML(e.reference)}${e.note?`<small class="intake-value">${escapeHTML(e.note)}</small>`:''}</td></tr>`).join('')||'<tr><td colspan="4">No credit history yet.</td></tr>'}</tbody></table></div><p class="credit-history-note">Shows the 100 most recent entries. Saved locally; no automatic Square balance or payment updates.</p>`;
 $('exportCreditHistory').onclick=async()=>{try{const data=await api('/api/credit/export',{customerId:creditAccount.customer.id});const url=URL.createObjectURL(new Blob(['\ufeff'+data.csv],{type:'text/csv;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download='Roca_credit_history.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}catch(e){toast(e.message,true)}};
 $('recordCreditUse').onclick=()=>openCreditEntry('redeem');$('recordCreditAdjustment').onclick=()=>openCreditEntry('opening');if($('recordTradeCredit'))$('recordTradeCredit').onclick=()=>openCreditEntry('trade');
}
function openCreditEntry(kind){if(!creditAccount)return;creditEntryRequest=crypto.randomUUID();$('creditEntryForm').reset();$('creditEntryKind').value=kind;$('creditEntryMessage').textContent='';$('creditEntryCustomer').textContent=creditAccount.customer.name+' · Available '+usd(creditAccount.customer.balance);renderCreditEntry();$('creditEntryDialog').showModal()}
function renderCreditEntry(){const kind=$('creditEntryKind').value,trade=kind==='trade';$('creditEntryTitle').textContent=trade?'Record approved trade credit.':kind==='redeem'?'Record credit used.':'Record a credit adjustment.';$('creditEntryKind').querySelector('[value="trade"]').disabled=!creditTrade;$('creditEntryAmount').disabled=trade;$('creditEntryReference').disabled=trade;if(trade&&creditTrade){$('creditEntryAmount').value=creditTrade.amount;$('creditEntryReference').value=creditTrade.id}$('creditAdminLabel').hidden=kind==='trade'||kind==='redeem';$('creditAdminPassword').required=!$('creditAdminLabel').hidden}
function bindWorkflowExtensions(){
 $('cardsTab').onclick=()=>showResearchPane('cards');$('researchTab').onclick=()=>showResearchPane('research');$('researchFilter').onchange=()=>{researchPage=1;renderResearchTable()};$('researchPrevious').onclick=()=>{researchPage--;renderResearchTable()};$('researchNext').onclick=()=>{researchPage++;renderResearchTable()};$('researchRows').onclick=e=>{const b=e.target.closest('[data-research-edit]');if(b){openCard(b.dataset.researchEdit);openPriceResearch('edit')}};
 $('creditSearch').oninput=()=>{clearTimeout(creditSearchTimer);creditSearchTimer=setTimeout(()=>loadCreditCustomers().catch(e=>toast(e.message,true)),250)};$('creditCustomerList').onclick=e=>{const b=e.target.closest('[data-credit-customer]');if(b)loadCreditAccount(b.dataset.creditCustomer).catch(err=>toast(err.message,true))};
 $('creditAddCustomer').onclick=()=>{creditCustomerRequest=crypto.randomUUID();$('creditCustomerForm').reset();$('creditCustomerMessage').textContent='';$('creditCustomerDialog').showModal()};$('creditCloseCustomer').onclick=()=>$('creditCustomerDialog').close();$('creditCustomerForm').onsubmit=async e=>{e.preventDefault();const form=$('creditCustomerForm');form.inert=true;try{const data=await api('/api/credit/customer',{requestId:creditCustomerRequest,name:$('creditCustomerName').value,contact:$('creditCustomerContact').value,square_reference:$('creditSquareReference').value});$('creditCustomerDialog').close();await loadCreditCustomers();await loadCreditAccount(data.customer.id)}catch(err){$('creditCustomerMessage').textContent=err.message}finally{form.inert=false}};
 $('creditCloseEntry').onclick=()=>{$('creditEntryDialog').close();$('creditAdminPassword').value=''};$('creditEntryKind').onchange=renderCreditEntry;$('creditEntryForm').onsubmit=async e=>{e.preventDefault();const form=$('creditEntryForm');form.inert=true;try{const data=await api('/api/credit/transaction',{requestId:creditEntryRequest,customerId:creditAccount.customer.id,version:creditAccount.customer.version,kind:$('creditEntryKind').value,amount:$('creditEntryAmount').value,receiptId:creditTrade?.id,reference:$('creditEntryReference').value,note:$('creditEntryNote').value,adminPassword:$('creditAdminPassword').value});const trade=$('creditEntryKind').value==='trade';creditAccount=data;if(trade)creditTrade=null;$('creditEntryDialog').close();$('creditAdminPassword').value='';renderCreditAccount();await loadCreditCustomers();toast('Credit tracker updated. Square was not changed.')}catch(err){$('creditEntryMessage').textContent=err.message;if(creditAccount)await loadCreditAccount(creditAccount.customer.id)}finally{form.inert=false}};
}

function bind(){bindWorkflowExtensions();document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>navigate(b.dataset.mode));$('newReceipt').onclick=startNewBatch;$('compareManualPrices').onclick=()=>openPriceResearch('manual');$('compareCardPrices').onclick=()=>openPriceResearch('edit');$('closePriceResearch').onclick=$('returnToItem').onclick=()=>$('priceResearchDialog').close();$('priceResearchGame').onchange=()=>{if(priceResearchOrigin==='manual')$('manualGame').value=$('priceResearchGame').value;renderPriceResearch()};$('priceResearchProfile').onchange=()=>paintPriceResearch(priceResearchResult);$('approveSuggestedPrice').onclick=approveSuggestedPrice;$('priceResearchDialog').addEventListener('close',()=>{++priceResearchRequest;clearTimeout(priceResearchTimer)});$('batchPricingGame').onchange=async()=>{await busyWork(async()=>{state.receipt.pricing_game=$('batchPricingGame').value;markDirty();await saveReceipt();pricingResults={};await refreshPricing();toast('Research started for this game. Open Price research to review source coverage.')})};$('researchBatch').onclick=async()=>{await busyWork(async()=>{await saveReceipt();await api('/api/receipts/'+state.receipt.id+'/research',{revision:state.receipt.revision});await refreshPricing();toast('Research queued. Open Price research to review source coverage.')})};$('addManualItem').onclick=openManualItem;$('manualItemForm').onsubmit=saveManualItem;$('closeManualItem').onclick=$('cancelManualItem').onclick=()=>{if(!state.busy)$('manualItemDialog').close()};$('manualItemDialog').addEventListener('cancel',e=>{if(state.busy)e.preventDefault()});$('emptyImport').onclick=()=>$('csvFile').click();$('setupPickup').onclick=setupPickup;$('openNextPickup').onclick=openNextPickup;$('useDownloads').onclick=async()=>{try{const data=await api('/api/admin/downloads-folder',{});$('watchFolder').value=data.folder;$('adminMessage').textContent='Downloads selected. Save settings to start pickup; then export a new CSV from Roca.'}catch(e){$('adminMessage').textContent=e.message}};$('deleteReceipt').onclick=deleteCurrentReceipt;$('reviewContinue').onclick=continueReview;$('backToReview').onclick=$('doneReview').onclick=showCards;$('nextBatch').onclick=startNewBatch;$('openHistory').onclick=openHistory;$('closeHistory').onclick=()=>$('historyDialog').close();$('historySearch').oninput=renderHistory;$('historyList').onclick=e=>{const b=e.target.closest('[data-open]');if(b)openReceipt(b.dataset.open)};$('labelLayout').onchange=()=>state.labelLayout=$('labelLayout').value;$('printerDpi').onchange=()=>state.printerDpi=Number($('printerDpi').value);$('csvFile').onchange=()=>importFile($('csvFile').files[0]);$('receiptPicker').onchange=e=>{if(e.target.value)openReceipt(e.target.value).catch(e=>toast(e.message,true));else startNewBatch()};$('receiptTitle').oninput=()=>{state.receipt.title=$('receiptTitle').value;markDirty()};$('receiptNotes').oninput=()=>{state.receipt.notes=$('receiptNotes').value;markDirty()};$('saveReceipt').onclick=()=>saveReceipt().then(()=>toast('Batch saved.')).catch(e=>toast(e.message,true));$('moveReceipt').onchange=async e=>{const mode=e.target.value;e.target.value='';if(mode)await busyWork(()=>moveReceipt(mode))};$('cardSearch').oninput=$('reviewFilter').onchange=()=>{state.page=1;renderTable()};$('previousPage').onclick=()=>{state.page--;renderTable()};$('nextPage').onclick=()=>{state.page++;renderTable()};$('cardRows').onclick=e=>{if(state.busy)return;const research=e.target.closest('[data-price-research]');if(research){openCard(research.dataset.priceResearch);openPriceResearch('edit');return}const b=e.target.closest('[data-edit]');if(b)openCard(b.dataset.edit)};$('cardRows').onchange=e=>{if(state.busy)return renderTable();const checked=e.target.dataset.review;if(checked){state.receipt.cards.find(c=>c.row_id===checked).reviewed=e.target.checked;markDirty();renderStats();renderTable();renderActions();return}const id=e.target.dataset.include;if(id){state.receipt.cards.find(c=>c.row_id===id).include=e.target.checked;markDirty();renderStats();renderActions()}};$('includePage').onchange=()=>{const cards=filteredCards().slice((state.page-1)*state.pageSize,state.page*state.pageSize);cards.forEach(c=>c.include=$('includePage').checked);markDirty();renderStats();renderTable();renderActions()};$('applyThreshold').onclick=()=>{const threshold=Number($('minPrice').value);if(!Number.isFinite(threshold)||threshold<0)return toast('Enter a valid threshold.',true);state.receipt.cards.forEach(c=>c.include=c.final_price>=threshold);markDirty();renderStats();renderTable();renderActions()};$('markReviewed').onclick=async()=>{if(included().some(c=>c.price_missing))return toast('Enter a selling price or explicitly confirm zero for every included card before marking checked.',true);if(await confirmAction('Have you checked every included card?','Confirm you have checked the identity, condition, finish, price and any pricing discrepancies for every included card. This does not receive inventory.','I checked these cards')){included().forEach(c=>c.reviewed=true);markDirty();renderStats();renderTable();renderActions()}};$('primaryAction').onclick=primaryAction;$('prepareOnly').onclick=prepareWithoutStock;$('labelsOnly').onclick=labelsWithoutStock;$('cashOffer').onclick=e=>{if(e.target.tagName!=='INPUT'){state.offer='cash';renderActions();$('cashOffer').classList.add('selected');$('creditOffer').classList.remove('selected')}};$('creditOffer').onclick=e=>{if(e.target.tagName!=='INPUT'){state.offer='credit';renderActions();$('creditOffer').classList.add('selected');$('cashOffer').classList.remove('selected')}};for(const [id,key]of [['cashPercent','cash_percent'],['creditPercent','credit_percent']])$(id).oninput=()=>{state.receipt[key]=Number($(id).value);markDirty();renderStats()};$('customerReference').oninput=()=>{state.receipt.customer_reference=$('customerReference').value;markDirty()};document.querySelectorAll('[data-intent]').forEach(b=>b.onclick=()=>{state.receipt.inventory_intent=b.dataset.intent;markDirty();renderReceipt()});for(const [id,key]of [['onlineChannel','channel'],['storageStrategy','storage_strategy'],['storageLocation','storage_location']])$(id).onchange=()=>{state.receipt[key]=$(id).value;markDirty()};$('closeDrawer').onclick=$('drawerBackdrop').onclick=closeCard;$('cardForm').onsubmit=async e=>{e.preventDefault();$('cardForm').inert=true;try{editCardFromForm();await saveReceipt();closeCard();renderReceipt();toast('Card saved.')}catch(err){$('drawerMessage').textContent=err.message}finally{$('cardForm').inert=false}};for(const id of ['editName','editSet','editNumber','editCondition','editLanguage','editFinish'])$(id).oninput=()=>{$('editReviewed').checked=false;if($('editSku').value)$('editSku').value='';$('priceReminder').textContent='Details changed. Confirm the price for this exact variant before marking reviewed.'};$('editPrice').oninput=()=>$('editReviewed').checked=false;$('splitCopy').onclick=splitOneCopy;$('openAdmin').onclick=openAdmin;$('closeAdmin').onclick=()=>{adminFocusPickup=false;$('adminDialog').close()};$('adminDialog').addEventListener('close',()=>{adminClosing=api('/api/auth/logout',{}).catch(()=>{});state.admin=false;$('squareToken').value='';$('adminPassword').value='';});$('authForm').onsubmit=async e=>{e.preventDefault();try{if(!state.configured&&$('adminPassword').value!==$('confirmPassword').value)throw Error('The passwords do not match.');await api('/api/auth/'+(state.configured?'login':'setup'),{password:$('adminPassword').value});$('adminPassword').value=$('confirmPassword').value='';state.admin=state.configured=true;notice('');$('adminMessage').textContent='';await loadAdmin()}catch(e){$('adminMessage').textContent=e.message;$('adminMessage').classList.add('error')}};$('lockAdmin').onclick=async()=>{await api('/api/auth/logout',{});state.admin=false;$('adminDialog').close();toast('Admin locked.')};$('squareEnvironment').onchange=$('squareToken').oninput=()=>{$('squareLocation').replaceChildren(new Option('Test connection to load stores',''));$('squareConnectionMessage').textContent='Connection changed. Test it before choosing a store.'};$('testSquare').onclick=async()=>{const button=$('testSquare');button.disabled=true;try{const result=await api(…2079 tokens truncated…turn []
        try:
            value=json.loads(raw)
            if value.get('version')!=1 or value.get('origin')!=ORIGIN or not isinstance(value.get('operations'),list) or len(value['operations'])>50:raise ValueError()
            rows=value['operations']
            for row in rows:
                if (row.get('kind') not in ('customer','adjust') or not isinstance(row.get('body'),dict)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',row.get('request_key',''))
                    or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,49}',row.get('store',''))
                    or row['body'].get('request_key')!=row['request_key']
                    or row.get('fingerprint')!=self._fingerprint(row['body'])):raise ValueError()
                if row['kind']=='adjust':self.customer_id(row.get('customer_id'))
            return rows
        except (ValueError,TypeError,AttributeError):
            raise CloudError('The saved pending-action journal needs recovery. Contact support before changing shared balances.',status=409,pending=True) from None

    def _save_pending(self,rows):
        if rows:self.pending_vault.set(json.dumps({'version':1,'origin':ORIGIN,'operations':rows},separators=(',',':')))
        elif self.pending_vault.path.exists():self.pending_vault.path.unlink()

    def _finish_pending(self,store,key):
        self._save_pending([row for row in self._pending() if (row['store'],row['request_key'])!=(store,key)])

    def _mutate(self,kind,body,customer_id=None,*,expected_session_id=None):
        # Persist the exact retry key/body before the network call. A crash or
        # lost response must not turn one correction into two separate actions.
        with self.lock:
            saved=self._saved()
            self._expected(saved,expected_session_id)
            if not saved or saved['expires_at']<=self.clock():raise CloudError('Sign in to your store cloud account.',status=401)
            key=body.get('request_key')
            if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',key):raise CloudError('A valid request key is required.')
            rows=self._pending();existing=next((row for row in rows if row['store']==saved['store'] and row['request_key']==key),None)
            fingerprint=self._fingerprint(body)
            if existing and (existing['kind']!=kind or existing.get('customer_id')!=customer_id or existing['fingerprint']!=fingerprint):
                raise CloudError('The pending action has different details. Recover that saved action before creating another.',status=409,pending=True)
            if not existing:
                if any(row['store']==saved['store'] for row in rows):raise CloudError('Resolve the saved pending action before changing shared credit again.',status=409,pending=True)
                if len(rows)>=50:raise CloudError('Resolve the pending actions on this computer before continuing.',status=409,pending=True)
                rows.append({'kind':kind,'customer_id':customer_id,'store':saved['store'],'request_key':key,
                             'body':body,'fingerprint':fingerprint,'created_at':self.clock()})
                self._save_pending(rows)
            path='/api/credit/customers'+('/'+customer_id+'/adjust' if kind=='adjust' else '')
            try:result=self._authenticated('POST',path,data=body)
            except CloudError as error:
                # A new action explicitly rejected by the service did not commit.
                # An older uncertain action stays recoverable even after a role change.
                if not existing and error.status<500 and not error.offline:self._finish_pending(saved['store'],key)
                else:error.pending=True
                raise
            self._finish_pending(saved['store'],key)
            return result

    def pending(self,*,expected_session_id=None):
        with self.lock:
            saved=self._saved();rows=self._pending()
            if saved and saved['expires_at']<=self.clock():self._clear();saved=None
            if saved:self._expected(saved,expected_session_id)
            if not saved:return {'pending':[],'hasOtherPending':bool(rows)}
            visible=[{'requestKey':row['request_key'],'action':row['kind'],'customerId':row.get('customer_id'),
                      'amount_cents':row['body'].get('amount_cents'),'entryKind':row['body'].get('kind'),
                      'reference':row['body'].get('reference'),'createdAt':row['created_at']} for row in rows if row['store']==saved['store']]
            return {'pending':visible,'hasOtherPending':any(row['store']!=saved['store'] for row in rows)}

    def recover(self,key,*,retry=False,expected_session_id=None):
        with self.lock:
            saved=self._saved()
            if saved and saved['expires_at']<=self.clock():self._clear();saved=None
            self._expected(saved,expected_session_id)
            if not saved:raise CloudError('Sign in to the store that owns the pending action.',status=401)
            row=next((row for row in self._pending() if row['store']==saved['store'] and row['request_key']==key),None)
            if row is None:raise CloudError('Pending action not found for this store.',status=404)
            status=self._authenticated('GET','/api/credit/requests/'+quote(key,safe=''))
            if status.get('found') is True:
                self._finish_pending(saved['store'],key)
                return {'resolved':True,'alreadyCompleted':True,'result':status.get('result')}
            if status.get('found') is not False:raise CloudError('The server could not confirm the saved action. Keep it for recovery.',status=502,pending=True)
            if not retry:return {'resolved':False,'alreadyCompleted':False,'message':'The server has no completed record yet. Retry the saved action using its original key.'}
            result=self._mutate(row['kind'],row['body'],row.get('customer_id'))
            return {'resolved':True,'alreadyCompleted':False,'result':result}

    def _call(self,method,path,*,token=None,data=None):
        headers={'Accept':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        try:
            response=self.transport.request(method,ORIGIN+path,headers=headers,json=data,
                                            timeout=(5,20),allow_redirects=False)
        except requests.RequestException:
            raise CloudError('Cloud service is unavailable. Shared credit cannot be changed offline. Retry the same saved action when connected.',status=503,offline=True) from None
        if 300<=response.status_code<400:
            raise CloudError('The cloud service returned an unexpected redirect. Contact support.',status=502)
        if len(response.content)>2_000_000:
            raise CloudError('The cloud response was too large. Contact support.',status=502)
        try:result=response.json()
        except (ValueError,TypeError):
            raise CloudError('The cloud service returned an unreadable response. Retry later.',status=502) from None
        if not isinstance(result,dict):raise CloudError('The cloud service response was invalid.',status=502)
        if not 200<=response.status_code<300:
            message=result.get('error')
            if not isinstance(message,str) or not 1<=len(message)<=500:message='The cloud action could not complete. Retry with the same request key.'
            raise CloudError(message,status=response.status_code,offline=response.status_code>=500)
        return result

    def _authenticated(self,method,path,*,data=None,expected_session_id=None):
        with self.lock:
            saved=self._saved()
            self._expected(saved,expected_session_id)
            if not saved or saved['expires_at']<=self.clock():
                self._clear()
                raise CloudError('Sign in to your store cloud account.',status=401)
            try:return self._call(method,path,token=saved['token'],data=data)
            except CloudError as error:
                if error.status==401:self._clear()
                raise

    def login(self,data):
        slug=str(data.get('storeSlug','')).strip().lower()
        email=data.get('email');password=data.get('password')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,49}',slug):raise CloudError('Enter your store ID.')
        if not isinstance(email,str) or len(email)>254 or not isinstance(password,str) or not 1<=len(password)<=128:
            raise CloudError('Enter your staff email and password.')
        with self.lock:
            old=self._saved()
            if old and old['expires_at']<=self.clock():self._clear();old=None
            if old:raise CloudError('Sign out of the current cloud account before switching staff or stores.',status=409)
            result=self._call('POST','/api/auth/login',data={'store_slug':slug,'email':email.strip(),'password':password})
            token=result.get('access_token');expiry=result.get('expires_in')
            if (not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,256}',token)
                or isinstance(expiry,bool) or not isinstance(expiry,int) or not 0<expiry<=28800
                or result.get('store')!=slug or result.get('role') not in ROLES):
                raise CloudError('The cloud sign-in response could not be verified.',status=502)
            # Passwords are sent once over HTTPS and never saved in local settings.
            session_id=uuid.uuid4().hex
            self.vault.set(json.dumps({'origin':ORIGIN,'token':token,'expires_at':self.clock()+expiry,
                                       'store':slug,'role':result['role'],'session_id':session_id},separators=(',',':')))
            return {'signedIn':True,'store':slug,'role':result['role'],'mode':'pilot','portalUrl':ORIGIN,'sessionId':session_id}

    def status(self):
        with self.lock:
            try:
                saved=self._saved()
                if not saved:return {'signedIn':False,'mode':'pilot','portalUrl':ORIGIN}
                me=self._authenticated('GET','/api/me')
                if me.get('store')!=saved['store'] or me.get('role') not in ROLES:
                    self._clear();raise CloudError('The cloud account changed. Sign in again.',status=401)
                return {'signedIn':True,'store':me['store'],'role':me['role'],'mode':'pilot','portalUrl':ORIGIN,'sessionId':saved['session_id']}
            except CloudError as error:
                if error.status==401:return {'signedIn':False,'mode':'pilot','portalUrl':ORIGIN,'message':str(error)}
                if error.offline and saved:return {'signedIn':False,'savedSession':True,'offline':True,'mode':'pilot','portalUrl':ORIGIN,'sessionId':saved['session_id'],'message':str(error)}
                raise

    def logout(self,*,expected_session_id=None):
        with self.lock:
            try:saved=self._saved()
            except CloudError:saved=None
            if saved:self._expected(saved,expected_session_id)
            self._clear()
            if not saved:return {'signedIn':False,'serverRevoked':False,'message':'Signed out on this computer.'}
            try:
                self._call('POST','/api/auth/logout',token=saved['token'],data={})
                return {'signedIn':False,'serverRevoked':True,'message':'Signed out and the server session was revoked.'}
            except CloudError as error:
                if error.status==401:return {'signedIn':False,'serverRevoked':True,'message':'Signed out; the server session has already ended.'}
                return {'signedIn':False,'serverRevoked':False,'message':'Signed out on this computer. The server could not confirm revocation; that session expires within eight hours.'}

    @staticmethod
    def customer_id(value):
        try:return str(uuid.UUID(str(value)))
        except (ValueError,TypeError,AttributeError):raise CloudError('Select a shared-credit customer.') from None

    def customers(self,query='',*,expected_session_id=None):
        if not isinstance(query,str) or len(query)>200:raise CloudError('Keep customer searches under 200 characters.')
        return self._authenticated('GET','/api/credit/customers?q='+quote(query,safe=''),expected_session_id=expected_session_id)

    def account(self,ident,*,expected_session_id=None):
        return self._authenticated('GET','/api/credit/customers/'+self.customer_id(ident),expected_session_id=expected_session_id)

    def create_customer(self,data,*,expected_session_id=None):
        fields={key:data.get(key) for key in ('request_key','display_name','contact','square_customer_id','permission_confirmed')}
        return self._mutate('customer',fields,expected_session_id=expected_session_id)

    def adjust(self,ident,data,*,expected_session_id=None):
        if data.get('kind') not in ('opening','adjust_add','adjust_remove'):
            raise CloudError('Use an opening balance or a manager correction. Trade issuance needs the accepted-trade workflow.')
        fields={key:data.get(key) for key in ('request_key','amount_cents','kind','reference','reason','version')}
        return self._mutate('adjust',fields,self.customer_id(ident),expected_session_id=expected_session_id)
