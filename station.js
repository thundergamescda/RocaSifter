'use strict';
const $=id=>document.getElementById(id);
const paths={layers:'M12 3 2 8l10 5 10-5-10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5',exchange:'M3 7h16l-4-4m4 14H3l4 4M19 7l-4 4M3 17l4-4',tag:'M3 3h7l11 11-7 7L3 10V3Zm4 4h.01',globe:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18Z',activity:'M3 12h4l3-8 4 16 3-8h4',lock:'M6 10h12v11H6V10Zm3 0V6a3 3 0 0 1 6 0v4m-3 5v2',unlock:'M6 10h12v11H6V10Zm3 0V6a3 3 0 0 1 6 0m-3 9v2',help:'M12 17h.01M9 9a3 3 0 0 1 6 0c0 2-3 2-3 4m9-1a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',plus:'M12 5v14M5 12h14',scan:'M3 8V3h5m8 0h5v5m0 8v5h-5M8 21H3v-5m4-8h1v8H7m4-8h2v8h-2m5-8h1v8h-1',upload:'M12 15V3m-4 4 4-4 4 4M4 14v6h16v-6',shield:'M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6l-9-4Zm-4 9 3 3 5-5',qr:'M3 3h6v6H3V3Zm12 0h6v6h-6V3ZM3 15h6v6H3v-6Zm12 0h2v2h4v4h-6v-6Zm6-3h-3M12 3v3m0 6h3M3 12h5m4 5v4',check:'m5 12 4 4L19 6','check-circle':'m8 12 3 3 5-6m5 3a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',save:'M4 3h13l4 4v14H4V3Zm4 0v6h8V3m-8 18v-7h9v7',dollar:'M12 2v20m5-16H9a4 4 0 0 0 0 8h6a4 4 0 0 1 0 8H6',search:'m16 16 5 5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z','chevron-left':'m14 5-7 7 7 7','chevron-right':'m10 5 7 7-7 7','arrow-right':'M4 12h16m-6-6 6 6-6 6',printer:'M6 9V3h12v6M6 17H3V9h18v8h-3m-12-3h12v7H6v-7m11-2h.01',x:'m6 6 12 12M6 18 18 6',plug:'M8 3v5m8-5v5M6 8h12v4a6 6 0 0 1-12 0V8Zm6 10v4',edit:'m16 3 5 5L8 21H3v-5L16 3Zm-2 2 5 5'};
function icon(name){return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name]||paths.layers}"/></svg>`}
function icons(root=document){root.querySelectorAll('i[data-icon]').forEach(el=>{el.innerHTML=icon(el.dataset.icon)})}
function escapeHTML(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
const usd=n=>Number(n||0).toLocaleString('en-US',{style:'currency',currency:'USD'});
const modes={labels:{title:'Process cards',eyebrow:'FROM SCAN TO SHELF',description:'Check your cards. Create labels. Choose whether to add stock.',empty:'Let your exports come to you.',copy:'Choose a pickup folder for recognized CSV exports.<br>Or choose a file to review its format and columns.',foot:'Your work is saved automatically.'},trade:{title:'Customer trade-in',eyebrow:'BUY WITH CONFIDENCE',description:'Check the cards, agree on an offer, then create labels.',empty:'Start with the customer’s cards.',copy:'Import a supported CSV export and check the cards first.<br>You’ll agree on the buy before adding stock or creating labels.',foot:'Payment and store credit are handled separately.'},bulk:{title:'Find value in bulk',eyebrow:'MORE TOOLS',description:'Filter a collection, then continue with the cards worth processing.',empty:'Find the cards worth keeping.',copy:'Import a supported CSV export, then use the value filter.<br>Continue with the selected cards when you’re ready.',foot:'Finding value does not add inventory.'},online:{title:'Online preparation',eyebrow:'MORE TOOLS',description:'Check cards, assign storage, and prepare your listing files.',empty:'Get a batch ready for online listing.',copy:'Import your cards and check their details.<br>Storage and inventory choices come at the finish.',foot:'Creates files for manual listing. Nothing is published automatically.'}};
let state={mode:'labels',stage:'review',labelLayout:'side',printerDpi:203,receipt:null,receipts:[],settings:{},dirty:false,busy:false,page:1,pageSize:40,offer:'cash',editing:null,admin:false,configured:false,pickupQueue:[],watcher:{state:'off',folder:''}};
let storeSetupGuide=null;
function hostedProfile(){return state.settings.profile?.profileMode==='hosted'}
function legacyProfile(){return state.settings.profile?.profileMode==='legacy_compatibility'}
window.clearStoreSetupGuide=message=>{cancelSavedReview();storeSetupGuide?.clear(message||'Store sign-in changed. Refresh setup to continue.')};
window.refreshStoreSetupGuide=()=>hostedProfile()?storeSetupGuide?.refresh():Promise.resolve(false);
function configureStoreSetup(){
 $('storeSetupSection').hidden=!hostedProfile();
 if(!hostedProfile()){storeSetupGuide?.destroy();storeSetupGuide=null;return}
 if(storeSetupGuide)return;
 storeSetupGuide=StoreSetup.create($('storeSetupGuide'),{
  loadSnapshot:async({signal})=>{
   const revision=cloudRevision,sessionId=cloudSession.sessionId||'';
   const response=await fetch('/api/cloud/setup-status',{signal,headers:{'X-SS-Local-Session':sessionId},cache:'no-store'});
   const result=await response.json();
   if(revision!==cloudRevision||sessionId!==(cloudSession.sessionId||'')||!hostedProfile())throw Error('Setup context changed');
   if(!response.ok||result.profile?.profileMode!=='hosted'||result.sessionId!==null&&result.sessionId!==sessionId)throw Error('Setup status unavailable');
   return result;
  },
  onAction:async action=>{
   const url=StoreSetup.actionUrl(action);if(url){window.open(url,'_blank','noopener,noreferrer');return}
   if(action==='refresh')return window.refreshCloudSetupSession?.();
   if(action==='sign_in'){await navigate('cloud');return}
   if(action==='review_receiving'){
    if(!state.receipt){toast('Open a checked batch, then choose Hosted receiving to review its location and saved plan.');return}
    await saveReceipt();await window.openHostedReceiving?.();
   }
  }
 });
}
let saveTimer,toastTimer,pollTimer;let adminClosing=Promise.resolve();let polling=false,adminFocusPickup=false;let manualRequestId=null;let priceResearchOrigin='manual',priceResearchRequest=0;let pricingResults={},priceResearchResult=null,priceResearchKey=null,priceResearchTimer;let researchPage=1,researchPane='cards',autoConsidering=new Set();let creditAccount=null,creditTrade=null,creditCustomerRequest=null,creditEntryRequest=null,creditSearchTimer;
async function api(path,body,signal,extraHeaders={}){const r=await fetch(path,{signal,method:body===undefined?'GET':'POST',headers:body===undefined?extraHeaders:{'Content-Type':'application/json',...extraHeaders},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});let data;try{data=await r.json()}catch{throw Error('The station is not responding. Open Stock & Sleeve from your desktop icon to return.')}if(!r.ok){if(data.adminRequired){state.admin=false}throw Error((data.errors||['The request could not complete.']).join('\n'))}return data}
function toast(message,error=false){clearTimeout(toastTimer);$('toast').textContent=message;$('toast').className='toast'+(error?' error':'');$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,error?8000:4500)}
function notice(message,error=false){$('notice').textContent=message;$('notice').className='notice'+(error?' error':'');$('notice').hidden=!message}
async function confirmAction(title,text,label='Confirm'){const d=$('confirmDialog');$('confirmTitle').textContent=title;$('confirmText').textContent=text;$('confirmOkay').textContent=label;d.showModal();return new Promise(resolve=>{let settled=false;const finish=value=>{if(settled)return;settled=true;d.close();resolve(value)};$('confirmOkay').onclick=()=>finish(true);$('confirmCancel').onclick=()=>finish(false);d.oncancel=e=>{e.preventDefault();finish(false)}})}
function included(){return(state.receipt?.cards||[]).filter(c=>c.include)}
function frozen(){return !!(state.receipt?.purchase||state.receipt?.inventory_status==='received'||['needs_attention','committing'].includes(state.receipt?.inventory_status)||window.sharedTradeLocked?.(state.receipt))}
function priceLocked(){return state.receipt?.inventory_status==='received'||locked()}
function locked(){return ['needs_attention','committing'].includes(state.receipt?.inventory_status)||!!window.sharedTradeLocked?.(state.receipt)||!!window.hostedReceiveLocked?.(state.receipt)}
let localEditRevision=0,priceApprovalOperation=null;
const receiptEditFields=['mode','title','notes','customer_reference','pricing_game','cash_percent','credit_percent','inventory_intent','channel','storage_strategy','storage_location'];
const cardEditFields=['card_name','card_num','card_set','condition','printing','language','qty','final_price','include','reviewed','storage_location','sku_override','game','game_label','grader','grade','grade_qualifier','cert_number','serial_number','year','sport','league','manufacturer','team','player','price_confirmed'];
const savedPhysicalFields=['game','game_label','card_name','card_set','card_num','condition','printing','language','item_type','grader','grade','grade_qualifier','cert_number','serial_number','year','sport','league','manufacturer','team','player'];
const reviewValueFields=[...savedPhysicalFields,'qty','final_price','price_confirmed','price_missing','sku_override','include'];
const reviewFieldLabels={game:'Game',game_label:'Game name',card_name:'Card name',card_set:'Set / expansion',card_num:'Collector number',condition:'Condition',printing:'Finish / treatment',language:'Language',item_type:'Item type',grader:'Grading company',grade:'Grade',grade_qualifier:'Grade qualifier',cert_number:'Certificate',serial_number:'Serial number',year:'Year',sport:'Sport',league:'League',manufacturer:'Manufacturer',team:'Team',player:'Player',qty:'Quantity',final_price:'Retail price',price_confirmed:'Price confirmed',price_missing:'Price needed',sku_override:'Existing SKU override',include:'Included',reviewed:'Checked',storage_location:'Storage',mode:'Workflow',title:'Batch title',notes:'Notes',customer_reference:'Customer / reference',pricing_game:'Batch game',cash_percent:'Cash percentage',credit_percent:'Credit percentage',inventory_intent:'Inventory choice',channel:'Channel',storage_strategy:'Storage choice'};
let receiptEpoch=0,receiptBaseline=null,receiptBaselineOwner=null,saveOperation=null,receiptSaveIssue=null,savedReviewOperation=null,reviewUncertainty=null;
function receiptOwner(){return {id:state.receipt?.id,epoch:receiptEpoch,session:drawerSession()}}
function receiptOwnerCurrent(owner){return !!owner&&state.receipt?.id===owner.id&&receiptEpoch===owner.epoch&&drawerSession()===owner.session&&!window.stationStopping}
function issueHere(issue=receiptSaveIssue){return !!issue&&receiptOwnerCurrent(issue.owner)}
function sameEditableValue(a,b){return JSON.stringify(a)===JSON.stringify(b)}
function cancelSavedReview(){const operation=savedReviewOperation;if(!operation)return;operation.invalidated=true;operation.cancel?.();for(const controller of operation.controllers||[])controller.abort();if(!operation.postAttempted&&savedReviewOperation===operation)savedReviewOperation=null}
function trackPriceApprovalEdits(){for(const type of ['input','change'])document.addEventListener(type,event=>{if(event.target.closest?.('#cardForm,#manualItemForm')){++localEditRevision;cancelSavedReview()}})}
function receiptSavePayload(receipt){
 const payload={revision:receipt.revision};
 for(const key of receiptEditFields)if(Object.hasOwn(receipt,key))payload[key]=structuredClone(receipt[key]);
 payload.cards=receipt.cards.map(card=>{const row={row_id:card.row_id};if(card.split_from)row.split_from=card.split_from;for(const key of cardEditFields){if(key==='reviewed'){if(card.reviewed===false)row.reviewed=false}else if(Object.hasOwn(card,key))row[key]=structuredClone(card[key])}return row});
 return payload;
}
function holdReceiptDraft(owner,draft,message){return {owner,draft:structuredClone(draft),message,observed:JSON.stringify(receiptSavePayload(state.receipt)),laterDraft:null,generation:0}}
function retainHeldChanges(issue=receiptSaveIssue){if(!issueHere(issue))return false;const later=receiptSavePayload(state.receipt),signature=JSON.stringify(later);if(signature===issue.observed)return false;issue.laterDraft=later;issue.observed=signature;++issue.generation;return true}
function receiptMutationAllowed(){if(!state.receipt||window.stationStopping)return false;if(savedReviewOperation?.busyHeld){toast('Wait for saved review status before editing.');return false}if(!issueHere())return true;retainHeldChanges();state.dirty=false;renderReceipt();toast('Pending edits are held. Keep them or reload the saved batch before editing.',true);return false}
async function boundedReviewApi(operation,path,body){
 const controller=new AbortController();operation.controllers??=new Set();operation.controllers.add(controller);let timer,abort;
 const stopped=new Promise((_,reject)=>{abort=()=>reject(Error('The saved review request was interrupted or timed out. Check saved status before continuing.'));controller.signal.addEventListener('abort',abort,{once:true});timer=setTimeout(()=>controller.abort(),15000)});
 const request=(async()=>{const response=await fetch(path,{signal:controller.signal,method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});let data;try{data=await response.json()}catch{throw Error('The saved review reply could not be read. Check saved status before continuing.')}if(!data||typeof data!=='object'||Array.isArray(data))throw Error('The saved review reply could not be confirmed. Check saved status before continuing.');if(!response.ok){if(data.adminRequired&&receiptOwnerCurrent(operation.owner))state.admin=false;const messages=Array.isArray(data.errors)?data.errors.filter(message=>typeof message==='string'):[];const error=Error(messages.join('\n')||'The saved review request could not complete.');error.reviewRejected=response.status===400;throw error}return data})();
 try{return await Promise.race([request,stopped])}
 finally{clearTimeout(timer);controller.signal.removeEventListener('abort',abort);operation.controllers.delete(controller)}
}
function checkedReceipt(receipt,id,revision,{newRevision=false}={}){
 if(!receipt||typeof receipt!=='object'||receipt.id!==id||!Number.isInteger(receipt.revision)||receipt.revision<revision||newRevision&&receipt.revision===revision||!Array.isArray(receipt.cards)||!receipt.cards.length)throw Error('The saved batch reply could not be confirmed. Reload the saved batch before continuing.');
 const ids=new Set();for(const card of receipt.cards){if(!card||typeof card!=='object'||typeof card.row_id!=='string'||!card.row_id||ids.has(card.row_id))throw Error('The saved card list could not be confirmed. Reload the batch.');ids.add(card.row_id)}return receipt;
}
function acknowledgeReceipt(receipt){state.receipt=receipt;receiptBaseline=structuredClone(receipt);receiptBaselineOwner=receiptOwner();state.dirty=false}
function clearLocalOutputs(){if(!state.receipt)return;for(const key of ['pdf','zpl','listing','ticket'])if(state.receipt.files)delete state.receipt.files[key];state.receipt.prepared=false;state.stage='review';renderFiles();renderStage()}
function revokePendingReview(){
 if(!state.receipt)return;const known=receiptBaseline&&receiptOwnerCurrent(receiptBaselineOwner)&&receiptBaseline.id===state.receipt.id&&receiptBaseline.revision===state.receipt.revision;
 const prior=new Map((known?receiptBaseline.cards:[]).map(card=>[card.row_id,card])),batchChanged=known&&!sameEditableValue(state.receipt.pricing_game,receiptBaseline.pricing_game);let revoked=false;
 for(const card of state.receipt.cards){const base=prior.get(card.row_id);if(batchChanged||!base||reviewValueFields.some(key=>!sameEditableValue(card[key],base[key]))){card.reviewed=false;revoked=true}else if(base.reviewed&&card.reviewed===false)revoked=true}
 if(revoked)clearLocalOutputs();
}
function saveStateText(){return issueHere()?'Saved state needs attention':issueHere(reviewUncertainty)?'Review outcome needs checking':state.dirty?'Unsaved changes':saving?'Saving…':'All changes saved'}
function markDirty(){if(!state.receipt)return;++localEditRevision;cancelSavedReview();clearTimeout(saveTimer);if(issueHere()){retainHeldChanges();state.dirty=false;renderReceipt();return}revokePendingReview();state.dirty=true;$('saveState').textContent=saveStateText();saveTimer=setTimeout(()=>saveReceipt().catch(e=>toast(e.message,true)),1200)}
function laterEditablePatch(snapshot,current){
 const patch={receipt:{},rows:[],unsafe:false};
 for(const key of receiptEditFields)if(!sameEditableValue(snapshot[key],current[key])){if(!Object.hasOwn(current,key))patch.unsafe=true;else patch.receipt[key]=structuredClone(current[key])}
 const oldIds=snapshot.cards.map(card=>card.row_id),newIds=current.cards.map(card=>card.row_id);
 if(!sameEditableValue(oldIds,newIds))patch.unsafe=true;
 const prior=new Map(snapshot.cards.map(card=>[card.row_id,card]));
 for(const card of current.cards){const before=prior.get(card.row_id);if(!before){patch.unsafe=true;continue}const changes={};for(const key of cardEditFields){if(key==='reviewed'&&card.reviewed!==false)continue;if(!sameEditableValue(before[key],card[key])){if(!Object.hasOwn(card,key))patch.unsafe=true;else changes[key]=structuredClone(card[key])}}if(Object.keys(changes).length)patch.rows.push({row_id:card.row_id,changes})}
 return patch;
}
function applyLaterPatch(server,patch){
 const receipt=structuredClone(server),rows=new Map(receipt.cards.map(card=>[card.row_id,card]));let changed=false;
 if(patch.unsafe||patch.rows.some(row=>!rows.has(row.row_id)))return null;
 for(const [key,value]of Object.entries(patch.receipt))if(!sameEditableValue(receipt[key],value)){receipt[key]=structuredClone(value);changed=true}
 for(const row of patch.rows)for(const [key,value]of Object.entries(row.changes)){const card=rows.get(row.row_id);if(key==='reviewed'&&value!==false)continue;if(!sameEditableValue(card[key],value)){card[key]=structuredClone(value);changed=true}}
 return {receipt,changed};
}
function synchronizeCleanDrawer(){const context=drawerDraft;if(context&&context.epoch===receiptEpoch&&context.receiptId===state.receipt?.id&&context.session===drawerSession()&&!context.busy&&!context.uncertain&&!drawerDirty()&&!state.dirty){const card=state.receipt.cards.find(row=>row.row_id===context.rowId);if(card)acknowledgeDrawerCard(card)}}
let saving=null;
async function saveReceipt(){
 clearTimeout(saveTimer);const owner=receiptOwner();
 if(savedReviewOperation?.busyHeld)throw Error('Wait for saved review status before saving other changes.');
 if(issueHere())throw Error('Your pending changes are held. Use Review saved cards to reload or keep those changes before continuing.');
 if(saving){await saving;if(!receiptOwnerCurrent(owner))return null;if(state.dirty)return saveReceipt();return state.receipt?{id:state.receipt.id,revision:state.receipt.revision}:null}
 if(!state.dirty||!state.receipt)return state.receipt?{id:state.receipt.id,revision:state.receipt.revision}:null;
 const operation={owner,receipt:state.receipt,snapshot:structuredClone(state.receipt),adopted:false};state.dirty=false;$('saveState').textContent='Saving…';
 const current=()=>saveOperation===operation&&receiptOwnerCurrent(owner)&&state.receipt===operation.receipt;
 const promise=(async()=>{try{
  const result=await api('/api/receipts/'+owner.id,receiptSavePayload(operation.snapshot));if(!current())return null;
  const server=checkedReceipt(result.receipt,owner.id,operation.snapshot.revision,{newRevision:true});
  const patch=laterEditablePatch(operation.snapshot,operation.receipt),heldDraft=receiptSavePayload(operation.receipt),reconciled=applyLaterPatch(server,patch);
  acknowledgeReceipt(server);operation.adopted=true;
  if(!reconciled){receiptSaveIssue=holdReceiptDraft(owner,heldDraft,'The batch changed while saving. Pending edits are kept until you choose how to reload.');renderReceipt();throw Error(receiptSaveIssue.message)}
  state.receipt=reconciled.receipt;state.dirty=reconciled.changed;if(state.dirty)revokePendingReview();synchronizeCleanDrawer();renderReceipt();
  return {id:owner.id,revision:server.revision};
 }catch(error){
  if(receiptOwnerCurrent(owner)&&(state.receipt===operation.receipt||operation.adopted)){if(!issueHere())receiptSaveIssue=holdReceiptDraft(owner,receiptSavePayload(operation.receipt),'The save outcome needs checking. Your pending edits are kept here.');state.dirty=false;renderReceipt()}
  throw error;
 }finally{if(saveOperation===operation){saveOperation=null;saving=null;if(receiptOwnerCurrent(owner)){if(issueHere())renderReceipt();else $('saveState').textContent=saveStateText()}}}})();
 saveOperation=operation;saving=promise;return await promise;
}
function savedReviewValue(value){if(value===undefined||value===null||value==='')return 'Not supplied';if(typeof value==='boolean')return value?'Yes':'No';return String(value)}
function appendSavedFact(container,label,value){const term=document.createElement('dt'),detail=document.createElement('dd');term.textContent=label;detail.textContent=savedReviewValue(value);container.append(term,detail)}
function savedSelectionSignature(receipt,rowIds){const rows=new Map(receipt.cards.map(card=>[card.row_id,card]));return JSON.stringify(rowIds.map(id=>rows.get(id)||null))}
function paintSavedReview(receipt,rowIds,page=0){
 const target=$('savedCardReviewFacts');target.replaceChildren();
 $('savedCardReviewSummary').textContent=`${rowIds.length} saved card row${rowIds.length===1?'':'s'} in “${receipt.title||receipt.id}”. Check these saved details and prices. No stock or payment changes are made.`;
 const pages=Math.max(1,Math.ceil(rowIds.length/state.pageSize)),currentPage=Math.max(0,Math.min(page,pages-1)),start=currentPage*state.pageSize,rows=new Map(receipt.cards.map(card=>[card.row_id,card]));
 $('savedReviewPrevious').disabled=currentPage===0;$('savedReviewNext').disabled=currentPage===pages-1;$('savedReviewPage').textContent=`Saved rows ${start+1}–${Math.min(start+state.pageSize,rowIds.length)} of ${rowIds.length}`;
 $('savedCardReviewConfirm').textContent=rowIds.length===1?'I checked this saved card':`I checked all ${rowIds.length} saved cards`;
 for(const id of rowIds.slice(start,start+state.pageSize)){const card=rows.get(id),section=document.createElement('section'),heading=document.createElement('h3'),facts=document.createElement('dl');
  section.className='saved-review-card';heading.textContent=card.card_name||'Saved card';facts.className='saved-review-facts';
  for(const field of savedPhysicalFields)if(Object.hasOwn(card,field))appendSavedFact(facts,reviewFieldLabels[field],card[field]);
  if(Object.hasOwn(receipt,'pricing_game'))appendSavedFact(facts,'Batch game',receipt.pricing_game);
  appendSavedFact(facts,'Quantity',card.qty);appendSavedFact(facts,'Retail price',usd(card.final_price));
  if(card.intake_value!==undefined)appendSavedFact(facts,'Intake value',usd(card.intake_value));
  appendSavedFact(facts,'Price confirmed',card.price_confirmed);if(Object.hasOwn(card,'price_missing'))appendSavedFact(facts,'Price needed',card.price_missing);appendSavedFact(facts,'Included',card.include);
  section.append(heading,facts);
  if(card.pricing_notice){const notice=document.createElement('p');notice.className='saved-review-notice';const reasons=Array.isArray(card.pricing_notice.reasons)?card.pricing_notice.reasons.filter(reason=>typeof reason==='string'):[];notice.textContent=(reasons.join(' ')||'A pricing notice needs your review.')+' Confirming keeps the saved price.';section.append(notice)}
  target.append(section);
 }
}
function ownedReviewConfirmation(operation,receipt,rowIds){
 const dialog=$('savedCardReviewDialog'),invoker=operation.invoker;let page=0;paintSavedReview(receipt,rowIds,page);operation.phase='confirming';dialog.showModal();$('savedCardReviewCancel').focus();
 return new Promise(resolve=>{let settled=false;const finish=confirmed=>{if(settled)return;settled=true;const accepted=confirmed&&operation.current();operation.phase=accepted?'confirmed':'cancelled';operation.cancel=null;$('savedCardReviewConfirm').removeEventListener('click',confirm);$('savedCardReviewCancel').removeEventListener('click',cancel);$('closeSavedCardReview').removeEventListener('click',cancel);$('savedReviewPrevious').removeEventListener('click',previous);$('savedReviewNext').removeEventListener('click',next);dialog.removeEventListener('cancel',escape);dialog.removeEventListener('close',closed);if(dialog.open)dialog.close();const fallback=[...document.querySelectorAll('[data-review]')].find(input=>input.dataset.review===rowIds[0])||$('markReviewed');const focus=invoker?.isConnected&&!invoker.disabled?invoker:fallback;if(receiptOwnerCurrent(operation.owner)&&focus&&!focus.disabled)focus.focus({preventScroll:true});resolve(accepted)};
  const confirm=()=>finish(true),cancel=()=>finish(false),escape=event=>{event.preventDefault();finish(false)},closed=()=>finish(false);operation.cancel=cancel;
  const previous=()=>{if(operation.current()){page=Math.max(0,page-1);paintSavedReview(receipt,rowIds,page)}else finish(false)},next=()=>{if(operation.current()){page=Math.min(Math.ceil(rowIds.length/state.pageSize)-1,page+1);paintSavedReview(receipt,rowIds,page)}else finish(false)};
  $('savedCardReviewConfirm').addEventListener('click',confirm);$('savedCardReviewCancel').addEventListener('click',cancel);$('closeSavedCardReview').addEventListener('click',cancel);$('savedReviewPrevious').addEventListener('click',previous);$('savedReviewNext').addEventListener('click',next);dialog.addEventListener('cancel',escape);dialog.addEventListener('close',closed);
 });
}
async function resolveReceiptSaveIssue(){
 const issue=receiptSaveIssue;if(!issueHere(issue)||issue.resolveAttempt)return false;if(drawerDraft){toast('Close or discard the open card draft before reloading this batch.',true);return false}
 retainHeldChanges(issue);state.dirty=false;renderReceipt();retainHeldChanges(issue);
 const dialog=$('pendingReceiptDialog'),target=$('pendingReceiptFacts'),receipt=state.receipt,editRevision=localEditRevision,invoker=document.activeElement;
 const generation=issue.generation,signature=JSON.stringify(receiptSavePayload(receipt)),attempt={owner:issue.owner,controllers:new Set()};issue.resolveAttempt=attempt;target.replaceChildren();
 const groups=[{title:'Original held edits',draft:issue.draft},...(issue.laterDraft?[{title:'Later local changes',draft:issue.laterDraft}]:[])],totalRows=groups.reduce((count,group)=>count+group.draft.cards.length,0),pages=Math.max(1,Math.ceil(totalRows/state.pageSize));let page=0;
 const current=()=>receiptSaveIssue===issue&&issue.resolveAttempt===attempt&&receiptOwnerCurrent(issue.owner)&&state.receipt===receipt&&localEditRevision===editRevision&&issue.generation===generation&&JSON.stringify(receiptSavePayload(state.receipt))===signature&&!state.dirty&&!drawerDraft&&!saving&&!state.busy;
 const changed=()=>{if(issueHere(issue)){retainHeldChanges(issue);state.dirty=false;renderReceipt();toast('Pending edits changed. Open the held changes again before choosing to discard them.',true)}};
 const showDraft=(draft,title,start,end)=>{const heading=document.createElement('h3'),facts=document.createElement('dl');heading.textContent=title+' · '+draft.cards.length+' card rows';facts.className='saved-review-facts';target.append(heading);for(const key of receiptEditFields)if(Object.hasOwn(draft,key))appendSavedFact(facts,reviewFieldLabels[key]||key,draft[key]);target.append(facts);for(const row of draft.cards.slice(start,end)){const section=document.createElement('section'),name=document.createElement('h4'),values=document.createElement('dl');name.textContent=row.card_name||'Pending card';values.className='saved-review-facts';for(const key of cardEditFields)if(Object.hasOwn(row,key))appendSavedFact(values,reviewFieldLabels[key]||key,row[key]);section.append(name,values);target.append(section)}};
 const paint=()=>{target.replaceChildren();const start=page*state.pageSize,end=start+state.pageSize;let offset=0;for(const group of groups){const first=Math.max(0,Math.min(group.draft.cards.length,start-offset)),last=Math.max(first,Math.min(group.draft.cards.length,end-offset));showDraft(group.draft,group.title,first,last);offset+=group.draft.cards.length}$('pendingReceiptPrevious').disabled=page===0;$('pendingReceiptNext').disabled=page===pages-1;$('pendingReceiptPage').textContent=`Held card rows ${totalRows?start+1:0}–${Math.min(end,totalRows)} of ${totalRows}. Discard applies to all held changes.`};
 $('pendingReceiptMessage').textContent=issue.message+' The original held edits are kept below. Any later local changes are shown separately. Reloading discards these edits only after you confirm.';
 paint();
 try{
  dialog.showModal();$('keepPendingReceipt').focus();
  const reload=await new Promise(resolve=>{let settled=false;const finish=value=>{if(settled)return;settled=true;$('reloadPendingReceipt').onclick=$('keepPendingReceipt').onclick=$('pendingReceiptPrevious').onclick=$('pendingReceiptNext').onclick=null;dialog.oncancel=dialog.onclose=null;if(dialog.open)dialog.close();if(receiptOwnerCurrent(issue.owner)&&invoker?.isConnected&&!invoker.disabled)invoker.focus({preventScroll:true});resolve(value)};$('reloadPendingReceipt').onclick=()=>finish(true);$('keepPendingReceipt').onclick=()=>finish(false);$('pendingReceiptPrevious').onclick=()=>{if(current()){page=Math.max(0,page-1);paint()}else{finish(false);changed()}};$('pendingReceiptNext').onclick=()=>{if(current()){page=Math.min(pages-1,page+1);paint()}else{finish(false);changed()}};dialog.oncancel=event=>{event.preventDefault();finish(false)};dialog.onclose=()=>finish(false)});
  if(!reload)return false;if(!current()){changed();return false}
  const result=await boundedReviewApi(attempt,'/api/receipts/'+issue.owner.id);if(!current()){changed();return false}
  const server=checkedReceipt(result.receipt,issue.owner.id,receipt.revision);acknowledgeReceipt(server);receiptSaveIssue=null;if(issueHere(reviewUncertainty))reviewUncertainty=null;renderReceipt();toast('Saved batch reloaded. Check the displayed cards before reviewing.');return true;
 }catch(error){if(current())toast(error.message,true);else changed();return false}
 finally{if(issue.resolveAttempt===attempt)issue.resolveAttempt=null}
}
async function requestSavedCardReview(rowIds,{drawer=null,invoker=document.activeElement}={}){
 if(!state.receipt||state.busy||savedReviewOperation||window.stationStopping)return;
 if(issueHere()){await resolveReceiptSaveIssue();return}
 if(drawerDraft?.busy){toast('Wait for the card save to finish, then choose Review saved card again.');return}
 if(drawerDraft?.uncertain){$('drawerMessage').textContent='The earlier save outcome needs checking. Close this draft and check the saved batch before reviewing.';return}
 if(drawerDraft&&drawerDraft!==drawer){toast('Finish or close the open card before reviewing saved rows.');return}
 if(drawer&&drawerDirty()){const owner=receiptOwner();if(await saveDrawerDraft({notify:false})&&receiptOwnerCurrent(owner)&&drawerDraft===drawer)toast('Edits saved. Check the saved card, then choose Review saved card again.');return}
 if(state.dirty||saving){const owner=receiptOwner();try{await saveReceipt();if(receiptOwnerCurrent(owner))toast('Changes saved. Check the saved cards, then choose review again.')}catch(error){if(receiptOwnerCurrent(owner))toast(error.message,true)}return}
 if(priceLocked()||!Array.isArray(rowIds)||!rowIds.length||new Set(rowIds).size!==rowIds.length||rowIds.some(id=>typeof id!=='string'||!state.receipt.cards.some(card=>card.row_id===id)))return;
 rowIds=[...rowIds];
 const operation={owner:receiptOwner(),receipt:state.receipt,revision:state.receipt.revision,editRevision:localEditRevision,locks:drawerLocks(),rowSignature:savedSelectionSignature(state.receipt,rowIds),drawer,invoker,phase:'loading',invalidated:false,busyHeld:false,postAttempted:false,controllers:new Set()};
 const otherDialog=()=>[...document.querySelectorAll('dialog[open]')].some(dialog=>dialog.id!=='savedCardReviewDialog'&&!(drawer&&dialog.id==='cardDrawer'));
 const ownsSavedState=()=>savedReviewOperation===operation&&receiptOwnerCurrent(operation.owner)&&state.receipt===operation.receipt&&state.receipt.revision===operation.revision&&savedSelectionSignature(state.receipt,rowIds)===operation.rowSignature&&localEditRevision===operation.editRevision&&drawerLocks()===operation.locks&&!state.dirty&&!saving&&!issueHere()&&!priceLocked()&&(!state.busy||operation.busyHeld)&&drawerDraft===drawer&&(!drawer||drawerCurrent(drawer,operation.editRevision)&&!drawerDirty()&&!drawer.busy&&!drawer.uncertain)&&!otherDialog();
 operation.current=()=>!operation.invalidated&&ownsSavedState()&&(operation.phase==='confirming'?$('savedCardReviewDialog').open:!$('savedCardReviewDialog').open);
 operation.reconcileCurrent=()=>ownsSavedState()&&!$('savedCardReviewDialog').open;
 savedReviewOperation=operation;
 if(!operation.current()){savedReviewOperation=null;return}
 const adoptCurrent=server=>{acknowledgeReceipt(server);operation.receipt=state.receipt;operation.revision=server.revision;operation.rowSignature=savedSelectionSignature(server,rowIds);if(drawer){const card=server.cards.find(row=>row.row_id===drawer.rowId);if(!card)throw Error('This saved card is no longer in the batch. Close and reopen the batch.');acknowledgeDrawerCard(card)}renderReceipt()};
 try{
  const uncertain=issueHere(reviewUncertainty)?reviewUncertainty:null,reply=await boundedReviewApi(operation,'/api/receipts/'+operation.owner.id);if(!operation.current())return;
  const server=checkedReceipt(reply.receipt,operation.owner.id,operation.revision),advanced=server.revision!==operation.revision;
  if(rowIds.some(id=>!server.cards.some(card=>card.row_id===id)))throw Error('The saved selection changed. Reopen the batch and select the cards again.');
  adoptCurrent(server);
  if(uncertain){if(uncertain.rejected||server.revision>uncertain.revision){reviewUncertainty=null;toast('Saved review status reloaded. Check it before making another review choice.')}else toast('Review outcome is still unconfirmed. Saved status was reloaded without resending review.',true);return}
  if(advanced||!operation.current()){toast('The saved batch changed. Check the saved cards, then choose review again.');return}
  const selected=new Map(server.cards.map(card=>[card.row_id,card]));
  if(rowIds.some(id=>{const card=selected.get(id);return savedPhysicalFields.some(key=>Object.hasOwn(card,key)&&card[key]!==null&&typeof card[key]!=='string')||!Number.isInteger(card.qty)||card.qty<1||typeof card.final_price!=='number'||!Number.isFinite(card.final_price)||card.final_price<0}))throw Error('Saved card details need correction. Save the corrected card before reviewing.');
  if(rowIds.some(id=>selected.get(id).price_missing||selected.get(id).final_price===0&&selected.get(id).price_confirmed!==true))throw Error('Enter and save a selling price, or explicitly confirm zero, before reviewing.');
  if(!await ownedReviewConfirmation(operation,server,rowIds)||!operation.current())return;
  operation.phase='posting';operation.postAttempted=true;operation.busyHeld=true;state.busy=true;reviewUncertainty={owner:operation.owner,rowIds:[...rowIds],revision:operation.revision,rejected:false};
  if(drawer){operation.drawerInert=$('cardForm').inert;operation.drawerCloseDisabled=$('closeDrawer').disabled;$('cardForm').inert=true;$('closeDrawer').disabled=true}
  renderActions();
  try{
   const result=await boundedReviewApi(operation,'/api/receipts/'+operation.owner.id+'/review-cards',{revision:operation.revision,rowIds:[...rowIds]});if(!operation.current())return;
   const confirmed=checkedReceipt(result.receipt,operation.owner.id,operation.revision,{newRevision:true});if(rowIds.some(id=>!confirmed.cards.some(card=>card.row_id===id&&card.reviewed===true)))throw Error('The review reply could not be confirmed.');
   adoptCurrent(confirmed);reviewUncertainty=null;toast(rowIds.length===1?'Saved card checked.':'Saved cards checked.');
  }catch(error){
   if(!operation.reconcileCurrent())return;
   const uncertain=reviewUncertainty;if(error.reviewRejected===true&&issueHere(uncertain))uncertain.rejected=true;
   operation.phase='reconciling';
   try{const checked=await boundedReviewApi(operation,'/api/receipts/'+operation.owner.id);if(!operation.reconcileCurrent())return;const current=checkedReceipt(checked.receipt,operation.owner.id,operation.revision),known=uncertain?.rejected||current.revision>uncertain?.revision;adoptCurrent(current);if(known)reviewUncertainty=null;toast(error.message+(known?' Saved status reloaded; no review was retried.':' The outcome remains unconfirmed; no review was retried.'),true)}
   catch(reconcileError){if(operation.reconcileCurrent())toast('Review outcome is unconfirmed. Choose review again to reload saved status; it will not resend confirmation.',true)}
  }
 }catch(error){if(operation.current())toast(error.message,true)}
 finally{
  operation.cancel?.();if(savedReviewOperation===operation){savedReviewOperation=null;if(operation.busyHeld)state.busy=false;if(drawerDraft===drawer&&drawer){$('cardForm').inert=operation.drawerInert??false;$('closeDrawer').disabled=operation.drawerCloseDisabled??false}if(receiptOwnerCurrent(operation.owner)){$('saveState').textContent=saveStateText();renderActions()}}
 }
}
async function quitStation(){
 if(state.busy||window.stationStopping||!await closeCard())return;
 await busyWork(async()=>{
  await saveReceipt();
  if(!await confirmAction('Quit Stock & Sleeve?','Your saved work stays on this computer. Folder pickup stops, and any inventory or shared-credit operation already running will finish first.','Save & quit'))return;
  await saveReceipt();
  const runtime=await api('/api/runtime');
  const response=await fetch('/api/quit',{method:'POST',headers:{'Content-Type':'application/json','X-SS-User-Action':'quit'},body:JSON.stringify({instance:runtime.instance})});
  const result=await response.json();if(!response.ok)throw Error((result.errors||['The app could not quit.']).join('\n'));
  window.stationStopping=true;clearInterval(pollTimer);clearTimeout(saveTimer);clearTimeout(priceResearchTimer);window.dispatchEvent(new Event('station-stopping'));
  const dialog=document.createElement('dialog');dialog.id='quitStatusDialog';dialog.className='modal confirm-modal';
  const title=document.createElement('h2');title.textContent='Finishing your work…';
  const detail=document.createElement('p');detail.textContent='Wait while accepted operations finish. Your saved batches and settings are kept.';
  dialog.append(title,detail);dialog.addEventListener('cancel',e=>e.preventDefault());document.body.append(dialog);dialog.showModal();
  for(let check=0;check<60;check++){
   await new Promise(resolve=>setTimeout(resolve,500));
   try{await fetch('/api/runtime',{cache:'no-store',signal:AbortSignal.timeout(1500)})}
   catch(error){if(error.name==='AbortError'||error.name==='TimeoutError')continue;title.textContent='Stock & Sleeve has stopped.';detail.textContent='You can close this window. Open Stock & Sleeve from your desktop icon when you’re ready to return.';return}
  }
  title.textContent='Still finishing current work.';detail.textContent='The app will stop when the accepted operation finishes. You can close this window; wait before reopening or updating the app.';
 });
}
async function busyWork(work){if(state.busy)return;state.busy=true;renderActions();document.body.style.cursor='progress';try{await work()}catch(e){toast(e.message,true)}finally{state.busy=false;document.body.style.cursor='';renderActions()}}
function setMode(mode){
 if(mode==='credit'&&!state.settings.creditTracking){toast('Use shared credit for this connected store.');mode='cloud'}
 state.mode=mode;document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));
 $('moreToolsNav').open=['bulk','online','activity'].includes(mode);
 const m=modes[mode];$('activityView').hidden=!!m;$('workflowContent').hidden=!m;
 $('pageTitle').textContent=m?.title||'Orders & activity';$('breadcrumb').textContent=m?.title||'Orders & activity';
 $('pageEyebrow').textContent=m?.eyebrow||'MORE TOOLS';$('pageDescription').textContent=m?.description||'Pull online orders and look up batch activity.';
 $('newReceipt').hidden=!m;$('addManualItem').hidden=!m;
 if(m){$('emptyTitle').textContent=m.empty;$('emptyDescription').innerHTML=m.copy;$('workflowFootnote').textContent=hostedProfile()&&mode==='trade'?'Shared credit requires a saved hosted receipt and explicit owner or manager confirmation. Stock is reviewed separately.':m.foot}
 else if(mode!=='credit'&&mode!=='cloud')loadActivity();$('creditView').hidden=mode!=='credit';if(mode==='credit'){$('activityView').hidden=true;$('pageTitle').textContent=$('breadcrumb').textContent='Local credit';$('pageEyebrow').textContent='THIS COMPUTER';$('pageDescription').textContent='Local history from this installation. Shared balances are in Shared credit.';loadCreditCustomers().catch(e=>toast(e.message,true))}
 const cloudView=$('cloudView');if(cloudView)cloudView.hidden=mode!=='cloud';if(mode==='cloud'){$('activityView').hidden=true;$('pageTitle').textContent=$('breadcrumb').textContent='Shared credit';$('pageEyebrow').textContent='YOUR STORE TEAM';$('pageDescription').textContent='One customer balance across your signed-in store computers.';window.enterCloudWorkspace?.()}renderReceipt();renderPickup();
}
async function navigate(mode){if(state.busy||!await closeCard())return;try{await saveReceipt();clearReceipt(mode)}catch(e){toast(e.message,true)}}
function clearReceipt(mode=state.mode){
 if(issueHere())throw Error('Keep or reload the held edits before replacing this batch.');
 requireResolvedDrawer();cancelSavedReview();++receiptEpoch;receiptBaseline=null;receiptBaselineOwner=null;clearTimeout(saveTimer);state.receipt=null;pricingResults={};researchPage=1;researchPane='cards';autoConsidering.clear();state.dirty=false;state.page=1;state.offer='cash';state.stage='review';state.editing=null;
 state.labelLayout=state.settings.labelLayout||'side';state.printerDpi=state.settings.printerDpi||203;
 $('receiptPicker').value='';$('cardSearch').value='';$('reviewFilter').value='all';$('csvFile').value='';$('minPrice').value=state.settings.minPrice??.7;
 for(const id of ['batchOptions','reviewOptions','printOptions'])$(id).open=false;
 $('historyDialog').close();$('outputFiles').replaceChildren();setMode(mode);$('saveState').textContent='Ready for a new batch';window.scrollTo(0,0);
}
async function startNewBatch(){if(state.busy||!await closeCard())return;try{await saveReceipt();clearReceipt();if(!await openNextPickup())toast('Ready for the next export. Previous batches stay saved.')}catch(e){toast(e.message,true)}}


function adoptReceipt(r){
 if(issueHere())throw Error('Keep or reload the held edits before replacing this batch.');
 requireResolvedDrawer();cancelSavedReview();++receiptEpoch;acknowledgeReceipt(r);pricingResults={};researchPage=1;researchPane='cards';autoConsidering.clear();state.page=1;state.offer=r.purchase?.type||'cash';
 state.stage=locked()?'finish':(r.files?.pdf||r.prepared)?'done':'review';
 state.labelLayout=state.settings.labelLayout||'side';state.printerDpi=state.settings.printerDpi||203;
 $('cardSearch').value='';$('reviewFilter').value='all';$('batchOptions').open=false;$('printOptions').open=false;$('reviewOptions').open=r.mode==='bulk';
 $('historyDialog').close();setMode(r.mode);$('receiptPicker').value=r.id;renderReceipt();
}
async function restoreIfDeleted(r){if(!r.deleted_at)return r;if(!await confirmAction('Restore this deleted batch?','Restore its saved cards and original inventory status. Any stock already received stays received, so reopening or reprinting will not add it again.','Restore batch'))return null;return(await api('/api/receipts/'+r.id+'/restore',{})).receipt}
async function deleteCurrentReceipt(){if(!state.receipt||state.busy||!await closeCard())return;await busyWork(async()=>{await saveReceipt();const r=state.receipt;if(!await confirmAction('Delete this batch?',`Remove “${r.title}” from batch history? Square inventory will not change. Receiving history is kept to prevent duplicate uploads. Reimport the same CSV or open its Activity entry to restore it.`,'Delete batch'))return;await api('/api/receipts/'+r.id+'/delete',{revision:r.revision});clearReceipt();await refreshReceipts();toast('Batch removed from history. Square stock was not changed.')})}
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
 $('historyList').innerHTML=rows.length?rows.map(r=>`<button class="history-item" data-open="${escapeHTML(r.id)}"><span><strong>${escapeHTML(r.title)}</strong><small>${escapeHTML(modes[r.mode]?.title||r.mode)} · ${Number(r.units||0).toLocaleString()} copies · ${escapeHTML(r.id)}</small></span><span class="history-statuses"><span class="pill">${receiptStatus(r)}</span>${receiptFinanceStatus(r)&&receiptFinanceStatus(r)!==receiptStatus(r)?'<span class="pill finance-pill">'+receiptFinanceStatus(r)+'</span>':''}</span>${icon('chevron-right')}</button>`).join(''):'<p class="list-empty">No batches found.</p>';
}
async function openHistory(){if(state.busy||!await closeCard())return;try{await saveReceipt();await refreshReceipts();$('historySearch').value='';renderHistory();$('historyDialog').showModal();$('historySearch').focus()}catch(e){toast(e.message,true)}}
async function acknowledgePickup(r){return r.pickup_pending?(await api('/api/receipts/'+r.id+'/pickup-opened',{})).receipt:r}
async function openReceipt(id,{canContinue}={}){
 if(canContinue!==undefined&&typeof canContinue!=='function')throw Error('The saved-record context is invalid.');
 const allowed=receipt=>canContinue===undefined||canContinue(receipt)===true;
 if(!allowed()||state.busy||!await closeCard()||!allowed())return false;
 let adopted=false;
 await busyWork(async()=>{
  if(!allowed())return;
  await saveReceipt();if(!allowed())return;
  let r=(await api('/api/receipts/'+id)).receipt;
  if(canContinue!==undefined&&(!r||r.id!==id))throw Error('The saved record did not match this request.');
  if(!allowed(r))return;
  r=await restoreIfDeleted(r);if(!allowed(r))return;
  if(!r){$('receiptPicker').value=state.receipt?.id||'';return}
  r=await acknowledgePickup(r);if(!allowed(r))return;
  adoptReceipt(r);adopted=true;await refreshReceipts();
 });
 return adopted;
}
function renderPickup(){
 const watch=state.watcher||{},ready=!!watch.folder,pending=state.pickupQueue.filter(r=>r.id!==state.receipt?.id);
 $('setupPickup').hidden=ready;$('pickupReady').hidden=!ready;$('pickupFolderHint').hidden=!ready;
 $('pickupReadyText').textContent=watch.state==='error'?'Export folder needs attention':'Automatic pickup is on';
 $('pickupReady').classList.toggle('attention',watch.state==='error');
 $('pickupFolderHint').textContent=watch.state==='error'?watch.message:'Export folder: '+watch.folder;
 $('emptyImport').className=ready?'button secondary':'button quiet';
 if(ready&&modes[state.mode]){
  $('emptyTitle').textContent='Ready for your next export.';
  $('emptyDescription').textContent='Save a recognized CSV export to the folder below. Choose a file instead if column mapping is needed.';
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
 if(window.stationStopping||polling||state.busy||saving)return;polling=true;
 try{
  await refreshReceipts();if(state.mode==='credit'&&creditAccount&&!document.querySelector('dialog[open]'))await loadCreditAccount(creditAccount.customer.id);
  if(!state.receipt&&!state.dirty&&modes[state.mode]&&!document.querySelector('dialog[open]')&&$('cardDrawer').hidden)await openNextPickup();
  if(state.receipt)await refreshPricing();if(state.mode==='activity')await loadActivity();
 }catch{}finally{polling=false}
}
async function setupPickup(){adminFocusPickup=true;await openAdmin()}

async function importFile(file){
 if(!await closeCard())return; if(window.reviewCsvImport)return window.reviewCsvImport(file);
 if(!file||state.busy)return;if(!file.name.toLowerCase().endsWith('.csv'))return toast('Choose a supported CSV file.',true);if(file.size>20_000_000)return toast('The CSV exceeds the 20 MB limit.',true);
 await busyWork(async()=>{try{
  await saveReceipt();const csv=await file.text();const payload={csv,fileName:file.name,mode:modes[state.mode]?state.mode:'labels'};let data=await api('/api/import',payload);
  if(data.receipt.deleted_at){const restored=await restoreIfDeleted(data.receipt);if(!restored)return;data.receipt=restored}
  else if(data.duplicate){adoptReceipt(data.receipt);const newDelivery=await confirmAction('This batch is already saved.','The saved batch has been opened. Only start another batch if these are a separate physical delivery of additional cards.','These are additional copies');if(newDelivery)data=await api('/api/import',{...payload,newDelivery:true})}
  data.receipt=await acknowledgePickup(data.receipt);adoptReceipt(data.receipt);await refreshReceipts();toast(data.duplicate?'Opened the saved batch. No stock changed.':'Cards imported. Check them before continuing.');
 }finally{$('csvFile').value=''}});
}
function receiptFinanceStatus(r){return r.shared_trade?.state==='confirmed'&&r.purchase?.scope==='hosted'?(r.purchase.type==='cash'?'CASH AGREEMENT RECORDED':r.purchase.type==='credit'?'SHARED CREDIT ISSUED':'HOSTED AGREEMENT'):''}
function receiptStatus(r){return r.inventory_status==='received'?'IN SQUARE':(r.hosted_receive&&!['confirmed','cancelled_before_write','retired_before_creation'].includes(r.hosted_receive.state))?'HOSTED RECEIVING PENDING':r.inventory_status==='needs_attention'?'NEEDS ATTENTION':r.inventory_status==='committing'?'ADDING TO SQUARE':receiptFinanceStatus(r)?receiptFinanceStatus(r):(r.shared_trade&&!['confirmed','cancelled'].includes(r.shared_trade.state))?'SHARED TRADE PENDING':r.prepared?'FILES READY':r.files?.pdf?'LABELS READY':r.purchase?'BUY RECORDED':r.all_reviewed||(r.cards?.some(c=>c.include)&&r.cards.filter(c=>c.include).every(c=>c.reviewed&&!c.price_missing))?'CHECKED':'IN PROGRESS'}
function renderReceipt(){
 renderOrdinaryLabelOptions();
 window.reconcileTradeRecoveryContext?.();
 renderIntakeSummary();
 $('addManualItem').disabled=state.busy||frozen();
 const r=state.receipt;document.body.classList.toggle('has-batch',!!r);$('emptyState').hidden=!!r;$('receiptWorkspace').hidden=!r;
 if(!r){$('reviewStep').classList.remove('active');$('finishStep').classList.remove('active');$('importStep').classList.add('active');$('finishStepText').textContent='Finish';return;}
 $('receiptReference').textContent=`${r.source_name} · ${r.id}`;$('receiptTitle').value=r.title;$('receiptStatus').textContent=receiptStatus(r);$('receiptFinanceStatus').textContent=receiptFinanceStatus(r);$('receiptFinanceStatus').hidden=!receiptFinanceStatus(r)||receiptFinanceStatus(r)===receiptStatus(r);
 $('receiptError').hidden=!r.error;$('receiptError').textContent=r.error||'';$('receiptNotes').value=r.notes||'';$('saveState').textContent=saveStateText();
 const buying=state.mode==='trade'&&!r.purchase&&r.hosted_receive?.state!=='confirmed';
 if(hostedProfile()&&buying)state.offer=null;
 $('tradePanel').querySelector('.offer-grid').hidden=hostedProfile();$('tradePanel').querySelector('.offer-options').hidden=hostedProfile();$('cashOffer').hidden=false;$('cashPercent').closest('label').hidden=false;
 $('tradePanel').querySelector('.panel-intro h3').textContent=hostedProfile()?'Review the hosted offers.':'Agree on the buy.';
 $('tradePanel').querySelector('.panel-intro p').textContent=hostedProfile()?'Choose a shared customer, then select a saved cash agreement or shared-credit offer in the hosted review. Cash payment is handled separately.':'Choose cash or store credit, then record the agreed offer. Payment is handled separately.';
 $('creditOffer').querySelector('span').textContent=hostedProfile()?'SHARED CREDIT ESTIMATE':'STORE CREDIT';
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
 $('markReviewed').disabled=priceLocked()||state.busy;$('includePage').disabled=frozen()||issueHere();$('applyThreshold').disabled=frozen()||issueHere();
 $('batchPricingGame').value=r.pricing_game||'';$('batchPricingGame').disabled=frozen()||issueHere();$('researchBatch').disabled=state.busy||issueHere();renderPricingSummary();$('splitCopy').hidden=frozen();renderStats();renderTable();renderResearchTable();showResearchPane(researchPane);renderFiles();renderStage();renderActions();
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
 const savedPair=/^ss-receipt-label-([a-f0-9-]{36})\.pdf$/.exec(files.pdf||''),savedJob=savedPair?r.label_jobs?.[savedPair[1]]:null;
 if(!online&&savedJob?.state==='published')$('doneSummary').textContent=`Labels for copies ${savedJob.terms.copyOffset+1}–${savedJob.terms.copyOffset+savedJob.terms.copyCount} of ${savedJob.terms.totalCopies.toLocaleString()} reviewed copies. No inventory changed.`;
 const frozenStock=r.hosted_receive?.state==='confirmed'?r.hosted_receive.label_profile?.stock:null;
 const stockLabel={'2x1':'2″ × 1″','3x1':'3″ × 1″','4x2':'4″ × 2″'}[frozenStock||'2x1'];
 $('donePrintHint').textContent=online?'Download the review sheet and publish the listings in your seller account.':stockLabel?'Print at Actual size / 100% on your '+stockLabel+' labels.':'Print at Actual size / 100% using the label stock in the confirmed receiving plan.';
 const file=online?files.listing:files.pdf;$('printLabels').hidden=!file;$('printLabels').href=file?'/files/'+encodeURIComponent(file):'#';
 bindManagedLabelLink($('printLabels'),file,state.receipt.id);
 $('printLabels').innerHTML=icon(online?'globe':'printer')+(online?'Download listing files':'Open labels to print');
 if(savedJob?.state==='published')$('printLabels').textContent='Download label PDF';
}
function renderStats(){$('receiptStatus').textContent=receiptStatus(state.receipt);const cards=included(),copies=cards.reduce((n,c)=>n+c.qty,0),retail=cards.reduce((n,c)=>n+c.qty*c.final_price,0),reviewed=cards.filter(c=>c.reviewed).length;$('statCopies').textContent=copies.toLocaleString();$('statRows').textContent=`${cards.length.toLocaleString()} card rows`;$('statRetail').textContent=usd(retail);$('statReviewed').textContent=`${reviewed} / ${cards.length}`;$('reviewProgress').style.width=(cards.length?reviewed/cards.length*100:0)+'%';$('reviewHint').textContent=reviewed===cards.length&&cards.length?'Ready to continue':'Check condition, finish and price';const intake=cards.reduce((n,c)=>n+c.qty*(c.intake_value??c.final_price),0);$('cashAmount').textContent=usd(intake*(state.receipt.cash_percent||0)/100);$('creditAmount').textContent=usd(intake*(state.receipt.credit_percent||0)/100)}

function filteredCards(){if(!state.receipt)return[];const q=$('cardSearch').value.trim().toLowerCase(),filter=$('reviewFilter').value;return state.receipt.cards.filter(c=>(!q||[c.card_name,c.card_set,c.card_num,c.condition,c.printing,c.sku].join(' ').toLowerCase().includes(q))&&(filter!=='pending'||!c.reviewed)&&(filter!=='included'||c.include)&&(filter!=='excluded'||!c.include))}
function renderTable(){if(!state.receipt)return;const all=filteredCards(),pages=Math.max(1,Math.ceil(all.length/state.pageSize));state.page=Math.min(state.page,pages);const start=(state.page-1)*state.pageSize,cards=all.slice(start,start+state.pageSize);$('tableCount').textContent=state.receipt.cards.length.toLocaleString();$('cardRows').innerHTML=cards.map(c=>`<tr class="${c.include?'':'excluded'}"><td class="check-cell"><input type="checkbox" data-include="${c.row_id}" ${c.include?'checked':''} ${frozen()||state.busy||issueHere()?'disabled':''} aria-label="Include ${escapeHTML(c.card_name)}"></td><td><div class="card-name" title="${escapeHTML(c.card_name)}">${escapeHTML(c.card_name)}</div><div class="card-meta">${c.game?escapeHTML(c.game_label||intakeGames[c.game]||c.game)+' · ':''}${escapeHTML(c.card_set||'Set not supplied')} · #${escapeHTML(c.card_num||'—')}${c.storage_location?' · '+escapeHTML(c.storage_location):''}</div></td><td><span class="condition-tag">${escapeHTML(c.condition)}</span><div class="finish-text">${escapeHTML(c.printing)}</div></td><td class="price-cell">${c.price_missing?'<strong class="credit-debit">Price required</strong>':usd(c.final_price)}${c.intake_value!==undefined?`<small class="intake-value">Intake ${usd(c.intake_value)}</small>`:""}${priceCellResearch(c)}</td><td>${c.qty}</td><td><label class="row-checked"><input type="checkbox" data-review="${c.row_id}" ${c.reviewed?'checked':''} ${priceLocked()||state.busy||issueHere()||c.price_missing?'disabled':''} aria-label="Checked ${escapeHTML(c.card_name)}"><span>${c.reviewed?'Checked':'Check'}</span></label></td><td><button class="table-edit" data-edit="${c.row_id}" aria-label="Edit ${escapeHTML(c.card_name)}">${icon('edit')}<span>Edit</span></button></td></tr>`).join('');$('noMatches').hidden=!!cards.length;$('pageSummary').textContent=all.length?`Showing ${start+1}–${Math.min(start+state.pageSize,all.length)} of ${all.length.toLocaleString()} rows`:'No matching rows';$('pageNumber').textContent=`${state.page} / ${pages}`;$('previousPage').disabled=state.page<=1;$('nextPage').disabled=state.page>=pages;$('includePage').checked=cards.length>0&&cards.every(c=>c.include)}
function renderActions(){
 window.renderSharedTrade?.();
 const r=state.receipt;$('newReceipt').disabled=state.busy;$('addManualItem').disabled=state.busy||issueHere()||frozen()||!!r?.intake_claim;$('openNextPickup').disabled=state.busy;if(!r)return;
 const editingBlocked=state.busy||issueHere(),reviewed=!state.dirty&&!saving&&!issueHere()&&!issueHere(reviewUncertainty)&&included().length>0&&included().every(c=>c.reviewed&&!c.price_missing),buying=state.mode==='trade'&&!r.purchase&&r.hosted_receive?.state!=='confirmed',received=r.inventory_status==='received';
 $('markReviewed').disabled=state.busy||priceLocked();$('reviewSavedCard').disabled=state.busy||priceLocked();$('includePage').disabled=editingBlocked||frozen();$('applyThreshold').disabled=editingBlocked||frozen();$('batchPricingGame').disabled=editingBlocked||frozen();$('researchBatch').disabled=editingBlocked;
 document.querySelectorAll('[data-intent]').forEach(b=>b.disabled=editingBlocked||received||locked());
 for(const id of ['deleteReceipt','moveReceipt','receiptTitle','receiptNotes'])$(id).disabled=editingBlocked||locked();$('saveReceipt').disabled=state.busy||locked();
 $('backToReview').disabled=$('doneReview').disabled=$('nextBatch').disabled=state.busy;
 $('cashPercent').disabled=$('creditPercent').disabled=$('cashOffer').disabled=$('creditOffer').disabled=editingBlocked||!!r.purchase||locked();
 $('customerReference').disabled=editingBlocked||locked();$('minPrice').disabled=locked()||issueHere();
 for(const id of ['onlineChannel','storageStrategy','storageLocation'])$(id).disabled=editingBlocked||locked();
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
 if(buying&&(r.intake_claim||r.shared_trade?.claim_binding_version)){
  main.disabled=true;main.textContent='Use shared trade acceptance';$('actionHint').textContent='This physical intake must use its hosted trade. An owner or manager needs recent enrolled MFA before new acceptance.';
 }else if(!received&&r.inventory_intent==='new'&&(r.intake_claim||r.shared_trade?.claim_binding_version||r.purchase?.physical_intake_id||r.purchase?.scope==='hosted')){
  main.disabled=true;main.textContent='Hosted receiving is not available';$('actionHint').textContent='This batch uses central hosted intake. Its stock cannot be sent through a local Square connection. Keep its saved reference until hosted receiving is available.';
 }
 if(state.busy)main.textContent='Working…';
 window.renderHostedReceiving?.();
 if(hostedProfile()&&!received){
  if($('sharedTradeHint')&&!r.shared_trade?.message)$('sharedTradeHint').textContent='Select a shared customer, review the saved hosted offer, then have an owner or manager confirm.';
  if(buying){main.textContent=r.shared_trade?'Open saved shared trade':'Review hosted cash & credit offers';main.disabled=state.busy||(!r.shared_trade&&!reviewed);$('actionTitle').textContent='Confirm the shared trade in your store.';$('actionHint').textContent='Choose a shared customer and review the saved hosted offer. No local purchase or wallet entry is recorded.';}
  else if(r.inventory_intent==='new'||r.hosted_receive||r.intake_claim||r.purchase?.scope==='hosted'){
   main.textContent=r.hosted_receive?'Open saved hosted receiving':'Review hosted receiving';main.disabled=state.busy||(!r.hosted_receive&&!reviewed);$('actionTitle').textContent='Review the hosted receiving plan.';$('actionHint').textContent='Review the store account, physical intake and exact location before any stock change. A local Square token is never used.';
  }
 }
 window.renderTradeRecovery?.();
}
function renderFiles(){const files=window.hostedReceiveLocked?.(state.receipt)?{}:state.receipt?.files||{},labels={pdf:'Open label PDF',zpl:'Download Zebra ZPL',listing:'Download listing review CSV',ticket:'Open storage ticket',buy:state.receipt?.purchase?.scope==='hosted'?(state.receipt.purchase.type==='cash'?'Open cash agreement PDF':'Open credit receipt PDF'):'Open accepted buy receipt'};const items=Object.entries(files).filter(([key])=>labels[key]);$('outputFiles').hidden=!items.length;$('outputFiles').replaceChildren();for(const [key,file] of items){const link=document.createElement('a');link.href='/files/'+encodeURIComponent(file);link.target='_blank';link.rel='noopener';link.textContent=labels[key];bindManagedLabelLink(link,file,state.receipt.id);$('outputFiles').append(link)}}

function labelStaffSession(){return typeof cloudSession==='object'?cloudSession.sessionId||'':''}
function bindManagedLabelLink(link,file,receiptId){
 link.onclick=null;if(typeof file!=='string'||!file.startsWith('ss-receipt-label-')){link.target='_blank';return}
 link.href='#';link.removeAttribute('target');
 link.onclick=event=>{event.preventDefault();downloadManagedLabel(file,receiptId).catch(error=>toast(error.message,true))};
}
async function downloadManagedLabel(file,receiptId){
 const match=/^ss-receipt-label-([a-f0-9-]{36})\.(pdf|zpl)$/.exec(file);if(!match)throw Error('Reopen the saved label pair before downloading.');
 const owner=receiptOwner(),session=labelStaffSession(),revision=state.receipt?.revision,editRevision=localEditRevision;
 const current=()=>receiptOwnerCurrent(owner)&&labelStaffSession()===session&&state.receipt?.revision===revision&&localEditRevision===editRevision;
 if(owner.id!==receiptId||!current())throw Error('The workspace changed. Reopen its original saved pair.');
 const response=await fetch(`/api/receipt-label-publications/${encodeURIComponent(receiptId)}/${match[1]}/${match[2]}`,{headers:{'X-SS-Local-Session':session},cache:'no-store'});
 if(!response.ok){const result=await response.json();throw Error((result.errors||['This saved pair could not be verified.']).join('\n'))}
 const blob=await response.blob();if(!current())throw Error('The workspace changed. Reopen its original saved pair.');
 if(!blob.size||blob.size>32*1024*1024)throw Error('This label file is incomplete or too large.');
 const url=URL.createObjectURL(blob),anchor=document.createElement('a');anchor.href=url;anchor.download=file;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
let ordinaryLabelRequest=null;
function renderOrdinaryLabelOptions(){
 if(!$('printControls'))return;
 if(!$('labelCopyStart')){
  const controls=document.createElement('div');controls.innerHTML='<label>First copy (rows ordered by saved row ID)<input id="labelCopyStart" type="number" min="1" step="1" value="1"></label><label>Copies in this pair<input id="labelCopyCount" type="number" min="1" max="1000" step="1" value="1"></label><label><input id="labelSeparateReprint" type="checkbox">Create a separate reprint job</label><p class="hint">A reprint changes no stock. Keep the same job when a response is uncertain. Large batches need explicit smaller ranges.</p><label>Saved unfinished job<select id="labelRecoveryJob"></select></label><button type="button" class="button secondary small" id="recoverOrdinaryLabels">Recover this same pair</button>';$('printControls').append(controls);
  $('recoverOrdinaryLabels').onclick=()=>busyWork(async()=>{const id=$('labelRecoveryJob').value,job=state.receipt?.label_jobs?.[id];if(!job)throw Error('Choose a saved unfinished label job.');await createLabels({jobId:id,layout:job.terms.profile.layout,dpi:job.terms.profile.dpi,copyOffset:job.terms.copyOffset,copyCount:job.terms.copyCount})});
  const again=document.createElement('button');again.type='button';again.className='button quiet';again.textContent='Choose another label copy range';again.onclick=()=>{state.stage='finish';renderReceipt();$('printOptions').open=true;$('labelCopyStart').focus()};$('doneView').append(again);
 }
 const receipt=state.receipt,total=receipt?receipt.cards.filter(c=>c.include!==false).reduce((n,c)=>n+c.qty,0):0;
 if($('labelCopyStart').dataset.receipt!==receipt?.id){$('labelCopyStart').dataset.receipt=receipt?.id||'';$('labelCopyStart').value=1;$('labelCopyCount').value=Math.max(1,total);$('labelSeparateReprint').checked=false;ordinaryLabelRequest=null}
 $('labelCopyStart').max=Math.max(1,total);
 const select=$('labelRecoveryJob'),selected=select.value;select.replaceChildren();
 for(const [id,job] of Object.entries(receipt?.label_jobs||{})){if(job.state==='published')continue;const option=document.createElement('option');option.value=id;option.textContent=`${job.state}: copies ${job.terms.copyOffset+1}–${job.terms.copyOffset+job.terms.copyCount}`;select.append(option)}
 if([...select.options].some(o=>o.value===selected))select.value=selected;
 $('recoverOrdinaryLabels').disabled=state.busy||!select.options.length;
}
async function moveReceipt(mode){if(!state.receipt||mode===state.receipt.mode||!receiptMutationAllowed())return;await saveReceipt();state.receipt.mode=mode;markDirty();await saveReceipt();state.stage='review';$('batchOptions').open=false;$('reviewOptions').open=mode==='bulk';setMode(mode);await refreshReceipts();toast('Same batch, next workflow. No stock changed.')}
async function labelsWithoutStock(){if(!receiptMutationAllowed())return;await busyWork(async()=>{if(state.receipt.inventory_status!=='received'){state.receipt.inventory_intent='existing';markDirty();await saveReceipt()}await createLabels()})}
async function prepareWithoutStock(){if(!receiptMutationAllowed())return;await busyWork(async()=>{if(state.receipt.inventory_status!=='received'){state.receipt.inventory_intent='existing';markDirty();await saveReceipt()}await action('prepare');state.stage='done';renderReceipt()})}
async function action(name,data={},labelOwner){const originating=name==='labels'?(labelOwner||receiptOwner()):null;await saveReceipt();if(name==='labels'&&!receiptOwnerCurrent(originating))throw Error('The workspace changed. Reopen the original saved label job.');if(issueHere()||issueHere(reviewUncertainty)||state.dirty)throw Error('Reload and check the saved review status before continuing.');const owner=state.receipt.id,session=labelStaffSession();const result=await api(`/api/receipts/${owner}/${name}`,data,undefined,name==='labels'?{'X-SS-Local-Session':session}:{});if(name==='labels'&&(!receiptOwnerCurrent(originating)||labelStaffSession()!==session))throw Error('The workspace changed. The saved label job can be recovered from its original batch.');acknowledgeReceipt(result.receipt);renderReceipt();await refreshReceipts();return result.receipt}
async function createLabels(recovery){
 const data=recovery||{layout:state.labelLayout,dpi:Number(state.printerDpi),copyOffset:Number($('labelCopyStart').value)-1,copyCount:Number($('labelCopyCount').value)};
 if(!recovery&&(!$('labelCopyStart').checkValidity()||!$('labelCopyCount').checkValidity()))throw Error('Choose a valid first copy and at most 1,000 copies.');
 if(!recovery&&$('labelSeparateReprint').checked){const key=JSON.stringify([state.receipt.id,data]);if(ordinaryLabelRequest?.key!==key)ordinaryLabelRequest={key,jobId:crypto.randomUUID()};data.jobId=ordinaryLabelRequest.jobId}
 const owner=receiptOwner(),session=labelStaffSession();
 const current=()=>receiptOwnerCurrent(owner)&&labelStaffSession()===session;
 try{const receipt=await action('labels',data,owner);if(!current())throw Error('The workspace changed. Reopen the original saved label job.');ordinaryLabelRequest=null;$('labelSeparateReprint').checked=false;state.stage='done';renderReceipt();window.scrollTo(0,0);toast('Label pair saved. Open it to print.');return receipt}
 catch(error){throw Error((error?.message||'The label request could not be confirmed.')+'\nCheck your sign-in and original batch, then retry the same pair or use saved-job recovery.')}
}
async function primaryAction(){
 if(!state.receipt||state.stage!=='finish')return;
 if(state.receipt.original_trade_recovery?.inventoryVerification==='not_checked'){await window.checkRecoveredTradeInventory?.();return}
 if(state.receipt.original_trade_recovery?.inventoryVerification==='existing_operation'&&!state.receipt.hosted_receive){await window.openRecoveredTradeReceiving?.();return}
 if(state.receipt.original_trade_recovery&&state.receipt.retail_review_required)return toast('Review current retail prices and explicitly confirm retail review first.',true);
 if(state.receipt.original_trade_recovery?.inventoryVerification==='no_operation'&&state.receipt.inventory_intent!=='new')return toast('Explicitly choose Add new copies before reviewing a new hosted stock plan.',true);
 if(hostedProfile()&&state.receipt.inventory_status!=='received'){
  if(state.mode==='trade'&&!state.receipt.purchase&&state.receipt.hosted_receive?.state!=='confirmed'){await saveReceipt();await window.openSharedTrade?.();return}
  if(state.receipt.inventory_intent==='new'||state.receipt.hosted_receive||state.receipt.intake_claim||state.receipt.purchase?.scope==='hosted'){await saveReceipt();await window.openHostedReceiving?.();return}
 }
 if(state.receipt.inventory_status!=='received'&&(state.receipt.hosted_receive||state.receipt.intake_claim||state.receipt.purchase?.scope==='hosted')&&(state.mode!=='trade'||state.receipt.purchase)){await window.openHostedReceiving?.();return;}
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
  if(state.mode==='trade'&&!r.purchase&&r.hosted_receive?.state!=='confirmed'){
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
async function openCard(id){
 const receiptId=state.receipt?.id;if(!receiptId||state.busy)return false;
 if(issueHere()){await resolveReceiptSaveIssue();return false}
 if(drawerDraft?.receiptId===receiptId&&drawerDraft.rowId===id&&$('cardDrawer').open)return true;
 const invoker=document.activeElement;if(!await closeCard()||state.receipt?.id!==receiptId)return false;
 return showCardEditor(id,invoker);
}
function populateCardEditor(c){
 $('cardForm').querySelectorAll('input,select,textarea').forEach(input=>input.disabled=false);
 const map={editName:'card_name',editSet:'card_set',editNumber:'card_num',editCondition:'condition',editLanguage:'language',editFinish:'printing',editQty:'qty',editPrice:'final_price',editLocation:'storage_location',editSku:'sku_override',...intakeEditMap};
 for(const [id,key]of Object.entries(map)){$(id).value=c[key]??'';$(id).disabled=(id==='editPrice'?priceLocked():frozen()&&id!=='editLocation')}
 intakeOpenCard(c);$('editReviewed').checked=c.price_missing?false:c.reviewed;intakePriceState();$('editReviewed').disabled=true;$('editGameLabel').disabled=frozen();
 if(locked())$('cardForm').querySelectorAll('input,select,textarea').forEach(input=>input.disabled=true);
 $('cardForm').querySelector('button[type=submit]').disabled=locked();$('compareCardPrices').disabled=false;
 $('splitCopy').disabled=frozen()||c.qty<=1;$('splitCopy').hidden=frozen()||c.qty<=1;
 $('reviewSavedCard').disabled=priceLocked()||state.busy;$('drawerReviewStatus').textContent=c.reviewed?'Saved card checked. Edits may require another review.':'Save any edits, then review the saved card.';
 $('drawerMessage').textContent=[locked()?'A saved operation has locked this batch. Card details are read-only until it is resolved.':frozen()?'Identity is locked. Before receiving, the retail label price can still be updated; the agreed buy stays fixed.':'',c.pricing_notice?c.pricing_notice.reasons.join(' ')+' Review the saved card to keep the saved price.':''].filter(Boolean).join(' ');
}
function priceCellResearch(c){const r=pricingResults[c.row_id];if(!r)return '';const profile=state.mode==='trade'&&!state.receipt.purchase?'intake':'retail',value=r[profile+'_price'];return `<button type="button" class="price-suggestion" data-price-research="${c.row_id}">${value!==null&&value!==undefined?'Review '+usd(value):['queued','running'].includes(r.status)?'Researching…':'View research'}</button>`}
function renderPricingSummary(){if(!$('pricingSummary'))return;const rows=Object.values(pricingResults),pending=rows.filter(r=>['queued','running'].includes(r.status)).length,suggestions=rows.filter(r=>r.retail_price!==undefined&&r.retail_price!==null).length;$('pricingCount').textContent=rows.length?`${rows.length-pending} / ${rows.length}`:'';$('pricingSummary').textContent=!state.settings.pricingEnabled?'Automatic research is off in Admin.':!state.receipt?.pricing_game?'Choose the game to research this batch.':pending?`Researching ${pending.toLocaleString()} rows. You can keep checking cards while research runs.`:suggestions?`${suggestions.toLocaleString()} rows have a price suggestion. Review each before applying.`:state.settings.pricingCompleteOnly?'Full-source pricing is not available in this preview. Open a card to see coverage.':'Review source coverage and cards needing an exact match. Open Price research to review source coverage.'}
async function refreshPricing(){
 const id=state.receipt?.id;if(!id)return;
 const data=await api('/api/receipts/'+id+'/pricing');if(state.receipt?.id!==id)return;
 pricingResults=data.cards;renderPricingSummary();renderTable();renderResearchTable();
 if(state.dirty||state.busy||saving||savedReviewOperation||issueHere()||issueHere(reviewUncertainty)||priceLocked()||!$('cardDrawer').hidden||document.querySelector('dialog[open]'))return;
 const rows=Object.entries(data.cards).filter(([row,r])=>(r.auto_eligible||['conflict','reference'].includes(r.status))&&!autoConsidering.has(id+row+r.checked_at)).slice(0,500);
 if(!rows.length)return;
 rows.forEach(([row,r])=>autoConsidering.add(id+row+r.checked_at));
 // Keep forms and batch actions stable during this short revision-checked write.
 state.busy=true;document.body.inert=true;
 try{
  const changed=await api('/api/receipts/'+id+'/apply-safe-prices',{revision:state.receipt.revision,rowIds:rows.map(([row])=>row)},AbortSignal.timeout(15000));
  if(state.receipt?.id===id&&!state.dirty){acknowledgeReceipt(changed.receipt);renderReceipt();if(changed.applied)toast(`${changed.applied} small price correction(s) applied.`);if(changed.alerts)toast(`${changed.alerts} pricing discrepancy/discrepancies need review.`,true)}
 }catch(e){if(state.receipt?.id===id)toast(e.name==='TimeoutError'?'Price update timed out. Reopen this batch before editing.':e.message,true)}
 finally{document.body.inert=false;state.busy=false;renderActions()}
}
let priceResearchManualLinks=null;
function paintManualResearchLinks(){
 if(!priceResearchManualLinks)return;
 const section=document.createElement('article');section.className='source-evidence';
 const title=document.createElement('strong');title.textContent='Check marketplace searches';section.append(title);
 const note=document.createElement('p');note.textContent='Search links are research aids. Confirm the game, variant, condition and grade yourself; opening a search does not verify a price.';section.append(note);
 const destinations={ebay_sold:['eBay sold','www.ebay.com'],ebay_listings:['eBay listings','www.ebay.com'],pricecharting:['PriceCharting','www.pricecharting.com'],tcgplayer:['TCGplayer','www.tcgplayer.com']};
 for(const [key,[label,host]] of Object.entries(destinations)){
  try{const url=new URL(priceResearchManualLinks.links[key]);if(url.protocol!=='https:'||url.hostname!==host||url.username||url.password)continue;
   const link=document.createElement('a');link.className='button secondary small';link.textContent='Search '+label;link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';section.append(link,document.createTextNode(' '));
  }catch{}
 }
 if(priceResearchManualLinks.gradeNeedsReview){const warning=document.createElement('p');warning.textContent='The supplied grade needs review. Confirm the grader, grade and tier before comparing.';section.append(warning)}
 $('priceResearchLinks').append(section);
}
async function openPriceResearch(origin){
 priceResearchOrigin=origin;const p=origin==='manual'?'manual':'edit';
 if(!$(p+'Name').value.trim())return toast('Enter the item name before researching.',true);
 try{if(origin==='edit'&&!locked()){const draft=drawerDraft,editRevision=localEditRevision;if(!await saveDrawerDraft({researchOnly:true,notify:false})||!drawerCurrent(draft,editRevision))return}
 const sourceCard=origin==='edit'?state.receipt.cards.find(card=>card.row_id===state.editing):null;intakeSetGame($('priceResearchGame'),origin==='manual'?intakeSelectedGame('manualGame'):(sourceCard?.game||state.receipt?.pricing_game||state.settings.pricingGame||'pokemon'),sourceCard?.game_label||$('manualGameLabel').value);
 $('priceResearchGame').disabled=origin==='edit'||!!state.receipt?.pricing_game;$('priceResearchProfile').value=origin==='edit'&&state.mode==='trade'&&!state.receipt.purchase?'intake':'retail';
 $('priceResearchProfile').querySelector('[value="intake"]').disabled=origin==='manual'||!!state.receipt?.purchase;
 $('priceResearchDialog').showModal();await renderPriceResearch();
 }catch(e){toast(e.message,true)}
}
async function renderPriceResearch(){
 const request=++priceResearchRequest,p=priceResearchOrigin==='manual'?'manual':'edit';clearTimeout(priceResearchTimer);priceResearchResult=null;priceResearchManualLinks=null;
 $('approveSuggestedPrice').disabled=true;$('priceProposal').hidden=true;$('priceResearchLinks').replaceChildren();$('priceResearchHint').textContent='';
 const saved=priceResearchOrigin==='edit'&&locked()?state.receipt.cards.find(card=>card.row_id===state.editing):null;
 const card=saved?Object.fromEntries(['card_name','card_set','card_num','condition','printing','language','game_label',...Object.keys(intakeMetadata)].map(key=>[key,saved[key]??''])):{game:$('priceResearchGame').value,card_name:$(p+'Name').value,card_set:$(p+'Set').value,card_num:$(p+'Number').value,condition:$(p+'Condition').value,printing:$(p+'Finish').value,language:$(p+'Language').value,...intakeExtraFields(p)};
 if(saved){card.game=saved.game||state.receipt.pricing_game||state.settings.pricingGame;$('priceResearchHint').textContent='Read-only research for the saved card. Resolve the pending operation before approving price changes.'}
 $('priceResearchName').textContent=card.card_name;$('priceResearchDetails').textContent=['card_set','card_num','condition','printing','language'].map(key=>card[key]).filter(Boolean).join(' · ');
 $('priceResearchMessage').textContent='Researching this exact card…';
 try{
 const [research,links]=await Promise.allSettled([api('/api/pricing/research',card),api('/api/pricing/links',card)]);
 if(request!==priceResearchRequest||!$('priceResearchDialog').open)return;
 if(links.status==='fulfilled')priceResearchManualLinks=links.value;
 if(research.status==='rejected'){paintManualResearchLinks();throw research.reason}
 priceResearchKey=research.value.key;await pollPriceResearch(request);
 }catch(e){if(request===priceResearchRequest)$('priceResearchMessage').textContent=e.message}
}
async function pollPriceResearch(request){
 try{const data=await api('/api/pricing/jobs/'+priceResearchKey);if(request!==priceResearchRequest||!$('priceResearchDialog').open)return;priceResearchResult=data;paintPriceResearch(data);if(['queued','running'].includes(data.status))priceResearchTimer=setTimeout(()=>pollPriceResearch(request),1200)}catch(e){if(request===priceResearchRequest)$('priceResearchMessage').textContent=e.message}
}
function paintPriceResearch(data){
 if(!data)return;const profile=$('priceResearchProfile').value,p=priceResearchOrigin==='manual'?'manual':'edit';
 const card=state.receipt?.cards.find(c=>c.row_id===state.editing),labelPrice=priceResearchOrigin==='edit'&&locked()?card.final_price:Number($(p+'Price').value),current=profile==='intake'?(card?.intake_value??labelPrice):labelPrice,suggestion=data[profile+'_price'];
 $('priceResearchMessage').textContent=[data.message,...(data.flags||[])].filter(Boolean).join(' ');$('priceProposal').hidden=suggestion===null||suggestion===undefined;
 $('currentPriceLabel').textContent=profile==='intake'?'Current intake value':'Current label price';$('priceCurrent').textContent=usd(current);$('priceSuggested').textContent=usd(suggestion);
 $('priceResearchHint').textContent=profile==='intake'?`${state.settings.intakeValuePercent??90}% value setting. Your cash/store-credit buying percentages apply after this; the label price stays separate.`:`${state.settings.retailValuePercent??100}% retail setting. Approval changes the label price. An accepted purchase amount stays fixed.`;
 if(priceResearchOrigin==='edit'&&locked())$('priceResearchHint').textContent='Read-only research for the saved card. Resolve the pending operation before approving price changes.';
 $('priceResearchLinks').innerHTML=Object.entries(data.sources||{}).map(([key,s])=>`<article class="source-evidence"><div><strong>${escapeHTML(s.label||key)}</strong><span class="evidence-status">${escapeHTML({reference:'Market reference',not_connected:'Not connected',not_applicable:'Excluded',unavailable:'Unavailable',needs_identity:'Check match'}[s.status]||s.status)}</span></div>${(s.market||s.low||s.median)?`<p class="evidence-price">${usd(s.market||s.low||s.median)} <small>${s.market?'market reference':s.low?'lowest listing reference':'verified source median'}${s.low_listing?' · low asking '+usd(s.low_listing):''}</small></p>`:''}${s.product_name?`<p>${escapeHTML(s.product_name)} · ${escapeHTML(s.set_name)} · ${escapeHTML(s.finish)}</p>`:''}<p>${escapeHTML(s.message||((s.count!==undefined)?`${s.count} qualifying record(s).`:''))}</p>${s.downloaded_at?`<small>Downloaded ${escapeHTML(new Date(s.downloaded_at).toLocaleString())}${s.source_updated_at?' · snapshot '+escapeHTML(s.source_updated_at):' · source update time not supplied'}</small>`:''}</article>`).join('');
 paintManualResearchLinks();
 $('approveSuggestedPrice').disabled=suggestion===null||suggestion===undefined||(priceResearchOrigin==='edit'&&priceLocked())||!!priceApprovalOperation?.current();$('approveSuggestedPrice').textContent='Approve '+(profile==='intake'?'intake value':'label price');
}
async function approveSuggestedPrice(){
 if(priceApprovalOperation?.current()||!$('priceResearchDialog').open||window.stationStopping)return;
 const origin=priceResearchOrigin,data=priceResearchResult,profile=$('priceResearchProfile').value;
 if(!data||data[profile+'_price']===null||data[profile+'_price']===undefined||origin==='edit'&&priceLocked())return;
 const receiptId=state.receipt?.id,rowId=state.editing,request=priceResearchRequest,editRevision=localEditRevision,entryId=manualRequestId;
 const session=()=>JSON.stringify([cloudRevision,cloudSession.sessionId,cloudSession.signedIn,cloudSession.store,cloudSession.role,state.admin,state.settings.profile]);
 const sessionContext=session();let revision=state.receipt?.revision;
 // IDs survive our own save; only its acknowledged revision may advance below.
 const operation={current:(afterSave=false)=>priceApprovalOperation===operation&&!window.stationStopping&&$('priceResearchDialog').open&&
  priceResearchRequest===request&&priceResearchOrigin===origin&&priceResearchResult===data&&priceResearchKey===data.key&&$('priceResearchProfile').value===profile&&
  localEditRevision===editRevision&&session()===sessionContext&&state.receipt?.id===receiptId&&(afterSave||state.receipt?.revision===revision)&&
  (origin==='manual'?manualRequestId===entryId&&$('manualItemDialog').open:state.editing===rowId&&!!state.receipt?.cards.find(card=>card.row_id===rowId)&&!priceLocked())};
 priceApprovalOperation=operation;if(!operation.current()){priceApprovalOperation=null;return}
 $('approveSuggestedPrice').disabled=true;
 try{
  const latest=await api('/api/pricing/jobs/'+data.key);if(!operation.current())return;
  if(latest.checked_at!==data.checked_at||latest[profile+'_price']!==data[profile+'_price']||latest[profile+'_price']===null)throw Error('This suggestion changed or expired. Close and reopen research before approving.');
  if(origin==='manual'){
   $('manualPrice').value=Number(data.retail_price).toFixed(2);++localEditRevision;
   $('priceResearchDialog').close();toast('Suggested label price applied. Save the item when ready.');return;
  }
  const saved=await saveReceipt();if(!operation.current(true)||saved?.id!==receiptId||saved.revision!==state.receipt.revision)return;
  revision=saved.revision;const card=state.receipt.cards.find(card=>card.row_id===rowId);
  // A may have saved successfully after its dialog closed; never replace B.
  if(!operation.current()||state.dirty)return;
  const response=await api('/api/receipts/'+receiptId+'/approve-price',{revision,rowId,key:data.key,checkedAt:data.checked_at,profile,suggestedPrice:data[profile+'_price'],currentPrice:profile==='intake'?(card.intake_value??card.final_price):card.final_price});
  if(!operation.current()||state.dirty)return;
  const updated=response.receipt?.cards?.find(card=>card.row_id===rowId);
  if(response.receipt?.id!==receiptId||!updated)throw Error('Reopen this item to review the saved price.');
   acknowledgeReceipt(response.receipt);$('editPrice').value=updated.final_price;$('editReviewed').checked=false;acknowledgeDrawerCard(updated);
  $('priceResearchDialog').close();renderReceipt();toast('Price approved. Check the card details before continuing.');
 }catch(error){if(operation.current()){paintPriceResearch(data);$('priceResearchMessage').textContent=error.message}}
 finally{
  const current=operation.current();if(priceApprovalOperation===operation){priceApprovalOperation=null;if(current)$('approveSuggestedPrice').disabled=false}
 }
}
async function openManualItem(){
 if(state.busy||frozen()||!await closeCard())return;
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
// Card fields remain a separate draft until a matching local save is acknowledged.
let drawerDraft=null;
function drawerFields(){return [...$('cardForm').querySelectorAll('input,select,textarea')].filter(field=>field.id&&field.id!=='editReviewed')}
function drawerSnapshot(){return JSON.stringify(drawerFields().map(field=>[field.id,field.value,field.type==='checkbox'?field.checked:null]))}
function drawerDirty(){return !!drawerDraft&&drawerSnapshot()!==drawerDraft.baseline}
function drawerSession(){return JSON.stringify([cloudRevision,cloudSession.sessionId,cloudSession.signedIn,cloudSession.store,cloudSession.role,state.admin,state.settings.profile])}
function drawerLocks(){return JSON.stringify([locked(),frozen(),priceLocked(),state.receipt?.inventory_status,state.receipt?.purchase,state.receipt?.shared_trade,state.receipt?.hosted_receive,state.receipt?.original_trade_recovery])}
function drawerCurrent(context,editRevision){return !!context&&drawerDraft===context&&context.epoch===receiptEpoch&&$('cardDrawer').open&&state.receipt?.id===context.receiptId&&state.editing===context.rowId&&drawerSession()===context.session&&drawerLocks()===context.locks&&(editRevision===undefined||localEditRevision===editRevision)}
function drawerFocus(){
 const fields=drawerFields(),preferred=fields.find(field=>!field.disabled&&!field.closest('[hidden]')&&!field.closest('details:not([open])')&&field.type!=='checkbox');
 (preferred||$('compareCardPrices')||$('closeDrawer')).focus({preventScroll:true});
}
function acknowledgeDrawerCard(card,{keepBlankPrice=false}={}){
 const context=drawerDraft;if(!context||context.epoch!==receiptEpoch||context.session!==drawerSession()||context.receiptId!==state.receipt?.id||context.rowId!==card.row_id)return;
 const blank=keepBlankPrice&&$('editPrice').value==='';populateCardEditor(card);
 context.card=structuredClone(card);context.baseline=drawerSnapshot();context.locks=drawerLocks();context.uncertain=false;
 if(blank)$('editPrice').value='';
}
function showCardEditor(id,invoker=document.activeElement){
 const card=state.receipt?.cards.find(card=>card.row_id===id);if(!card)return false;
 state.editing=id;populateCardEditor(card);$('cardForm').inert=false;$('closeDrawer').disabled=false;const dialog=$('cardDrawer');dialog.hidden=false;
 drawerDraft={epoch:receiptEpoch,receiptId:state.receipt.id,rowId:id,card:structuredClone(card),baseline:drawerSnapshot(),session:drawerSession(),locks:drawerLocks(),invoker,busy:false,uncertain:false,closePromise:null};
 if(!dialog.open)dialog.showModal();drawerFocus();return true;
}
function finishCardClose({focus=true}={}){
 const context=drawerDraft;cancelSavedReview();drawerDraft=null;++localEditRevision;
 if(priceResearchOrigin==='edit'&&$('priceResearchDialog').open)$('priceResearchDialog').close();
 if($('cardDrawer').open)$('cardDrawer').close();$('cardDrawer').hidden=true;if(state.receipt?.id===context?.receiptId&&state.editing===context?.rowId)state.editing=null;
 if(!focus||document.querySelector('dialog[open]'))return;
 const row=[...document.querySelectorAll('[data-edit]')].find(node=>node.dataset.edit===context?.rowId);
 const target=context?.invoker?.isConnected&&!context.invoker.disabled?context.invoker:row||$('openHistory');
 target?.focus({preventScroll:true});
}
function requireResolvedDrawer(){if(drawerDraft&&(drawerDirty()||drawerDraft.busy||drawerDraft.uncertain))throw Error('Finish or discard the open card edit before replacing this batch.');if(drawerDraft)finishCardClose({focus:false})}
function chooseDrawerClose(){
 const dialog=$('drawerCloseDialog');dialog.showModal();
 return new Promise(resolve=>{let settled=false;const finish=choice=>{if(settled)return;settled=true;dialog.close();resolve(choice)};
  $('drawerSaveChanges').onclick=()=>finish('save');$('drawerDiscard').onclick=()=>finish('discard');$('drawerKeepEditing').onclick=()=>finish('keep');
  dialog.oncancel=event=>{event.preventDefault();finish('keep')};$('drawerKeepEditing').focus();
 });
}
async function reconcileDrawerBeforeDiscard(context){
 if(!drawerCurrent(context)||context.busy||context.reconcileAttempt||state.dirty)return false;
 const receipt=state.receipt,revision=receipt.revision,editRevision=localEditRevision;
 const card=JSON.stringify(receipt.cards.find(card=>card.row_id===context.rowId)),draftCard=JSON.stringify(context.card),baseline=context.baseline,raw=drawerSnapshot();
 const session=context.session,locks=context.locks,attempt={kind:'discard-reconciliation',inert:$('cardForm').inert,closeDisabled:$('closeDrawer').disabled};
 const current=()=>context.reconcileAttempt===attempt&&context.busy&&drawerCurrent(context,editRevision)&&state.receipt===receipt&&state.receipt.revision===revision&&!state.dirty&&
  JSON.stringify(state.receipt.cards.find(card=>card.row_id===context.rowId))===card&&JSON.stringify(context.card)===draftCard&&context.baseline===baseline&&drawerSnapshot()===raw&&context.session===session&&context.locks===locks;
 context.reconcileAttempt=attempt;context.busy=true;$('cardForm').inert=true;$('closeDrawer').disabled=true;
 try{
  if(!current())return false;const result=await api('/api/receipts/'+context.receiptId);if(!current())return false;
  if(result.receipt?.id!==context.receiptId||!result.receipt.cards?.some(card=>card.row_id===context.rowId)||!Number.isInteger(result.receipt.revision)||result.receipt.revision<revision)throw Error('The saved card could not be confirmed. Keep this draft and reopen the batch when the connection returns.');
   acknowledgeReceipt(result.receipt);if(issueHere(reviewUncertainty))reviewUncertainty=null;renderReceipt();
  // Adoption and terminal close share this owned continuation; no outer-await gap.
  if(drawerDraft!==context||context.reconcileAttempt!==attempt||state.receipt!==result.receipt)return false;
  finishCardClose();return true;
 }catch(error){if(current())$('drawerMessage').textContent=error.message;return false}
 finally{if(context.reconcileAttempt===attempt){context.reconcileAttempt=null;context.busy=false;if(drawerDraft===context){$('cardForm').inert=attempt.inert;$('closeDrawer').disabled=attempt.closeDisabled}}}
}
async function closeCard(){
 const context=drawerDraft;if(!context)return true;if(context.busy)return false;if(context.closePromise)return context.closePromise;
 if(!drawerDirty()&&!context.uncertain){finishCardClose();return true}
 context.closePromise=(async()=>{
  const choice=await chooseDrawerClose();if(drawerDraft!==context)return false;
  if(choice==='keep')return false;
  if(choice==='save')return saveDrawerDraft({close:true});
  try{
   if(context.uncertain&&(!drawerCurrent(context)||state.dirty)){finishCardClose();toast('Local draft discarded. The earlier save outcome remains unconfirmed. Reopen its saved batch in the original store before retrying.',true);return true}
   if(context.uncertain)return reconcileDrawerBeforeDiscard(context);if(drawerDraft!==context)return false;finishCardClose();return true;
  }
  catch(error){if(drawerDraft===context)$('drawerMessage').textContent=error.message;return false}
 })();try{return await context.closePromise}finally{if(drawerDraft===context)context.closePromise=null}
}
async function saveDrawerDraft({close=false,researchOnly=false,split=false,notify=true}={}){
 const context=drawerDraft;if(!context||context.busy)return false;
 if(state.busy||issueHere()){$('drawerMessage').textContent='Resolve the pending save or review before saving this card draft.';return false}
 if(!drawerCurrent(context)||locked()){$('drawerMessage').textContent='This card or store context changed. Keep the draft and reopen the saved batch before editing.';return false}
 if(!researchOnly&&!$('cardForm').reportValidity())return false;
 let proposed;try{proposed=editCardFromForm({researchOnly,commit:false});if(split&&(frozen()||proposed.qty<2))throw Error('At least two editable copies are needed to separate one.')}
 catch(error){$('drawerMessage').textContent=error.message;return false}
 const editRevision=localEditRevision;context.busy=true;$('cardForm').inert=true;$('closeDrawer').disabled=true;
 let sent=false;
 try{
  const saved=await saveReceipt();if(!drawerCurrent(context,editRevision)||saved?.id!==context.receiptId||saved.revision!==state.receipt.revision)return false;
  const current=state.receipt.cards.find(card=>card.row_id===context.rowId);
  if(JSON.stringify(current)!==JSON.stringify(context.card))throw Error('This saved card changed. Keep your draft and reopen the batch before saving.');
  if(locked()||split&&frozen())throw Error('A saved operation now locks this batch. Keep the draft until it is resolved.');
  const receipt=structuredClone(state.receipt),index=receipt.cards.findIndex(card=>card.row_id===context.rowId);let nextId=context.rowId;
  receipt.cards[index]=proposed;
  if(split){proposed.qty--;const clone=structuredClone(proposed);clone.row_id=crypto.randomUUID();clone.split_from=context.rowId;clone.qty=1;clone.reviewed=false;receipt.cards.splice(index+1,0,clone);nextId=clone.row_id}
  const revision=state.receipt.revision;sent=true;context.uncertain=true;
   const result=await api('/api/receipts/'+context.receiptId,receiptSavePayload(receipt));
  if(!drawerCurrent(context,editRevision)||state.receipt.revision!==revision||state.dirty)return false;
  const card=result.receipt?.cards?.find(card=>card.row_id===nextId);
  if(result.receipt?.id!==context.receiptId||!card||!Number.isInteger(result.receipt.revision)||result.receipt.revision<=revision)throw Error('The save reply could not be confirmed. Keep the draft and check the saved card.');
   acknowledgeReceipt(checkedReceipt(result.receipt,context.receiptId,revision,{newRevision:true}));context.uncertain=false;
  if(split){finishCardClose({focus:false});showCardEditor(nextId,context.invoker)}
  else acknowledgeDrawerCard(card,{keepBlankPrice:researchOnly});
  renderReceipt();if(close)finishCardClose();if(notify)toast(split?'One copy separated. Check its condition, finish and price.':'Card saved.');return true;
 }catch(error){if(drawerDraft===context)$('drawerMessage').textContent=error.message+(sent?' The save outcome is unconfirmed. Keep this draft, retry carefully, or discard after checking the saved batch.':'');return false}
 finally{if(drawerDraft===context){context.busy=false;$('cardForm').inert=false;$('closeDrawer').disabled=false}else if(!drawerDraft){$('cardForm').inert=false;$('closeDrawer').disabled=false}}
}
function handleStationShortcut(event){
 if(!(event.ctrlKey||event.metaKey)||event.key.toLowerCase()!=='s')return;event.preventDefault();
 const nested=[...document.querySelectorAll('dialog[open]')].some(dialog=>dialog.id!=='cardDrawer');
 if(drawerDraft){if(!nested)void saveDrawerDraft();return}
 if(!nested)saveReceipt().then(()=>toast('Batch saved.')).catch(error=>toast(error.message,true));
}
function bindCardEditor(){
 const dialog=$('cardDrawer');let outside=false;
 const beyond=event=>{const rect=dialog.getBoundingClientRect();return event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom};
 dialog.addEventListener('cancel',event=>{event.preventDefault();void closeCard()});
 dialog.addEventListener('pointerdown',event=>{outside=event.target===dialog&&beyond(event)});
 dialog.addEventListener('pointerup',event=>{if(outside&&event.target===dialog&&beyond(event))void closeCard();outside=false});
 $('closeDrawer').onclick=()=>closeCard();$('cardForm').onsubmit=event=>{event.preventDefault();void saveDrawerDraft({close:true})};
 $('reviewSavedCard').onclick=()=>{const context=drawerDraft;if(context)void requestSavedCardReview([context.rowId],{drawer:context,invoker:$('reviewSavedCard')})};
 for(const type of ['input','change'])$('cardForm').addEventListener(type,()=>{const pending=drawerDirty();$('editReviewed').disabled=true;$('editReviewed').checked=!pending&&!state.dirty&&!saving&&!!state.receipt?.cards.find(card=>card.row_id===state.editing)?.reviewed;$('drawerReviewStatus').textContent=pending?'Edits are not reviewed. Save them before reviewing the saved card.':'Review applies only to the saved card.'});
}
async function splitOneCopy(){
 if(state.receipt?.original_trade_recovery)return toast('Original accepted quantities are fixed. Retail prices and storage can be reviewed separately.',true);
 if(state.busy||frozen()||locked())return;await saveDrawerDraft({split:true});
}
function editCardFromForm({researchOnly=false,commit=true}={}){
 if(commit&&!receiptMutationAllowed())throw Error('Resolve the held changes before editing this card.');
 if(locked())throw Error('This saved operation has locked the batch. Card details are read-only until it is resolved.');
 const card=state.receipt.cards.find(card=>card.row_id===state.editing);if(!card)throw Error('Reopen this saved card before editing.');
 const blankPrice=!$('editPrice').value.trim(),price=researchOnly&&blankPrice?card.final_price:Number($('editPrice').value);
 let next;
 if(state.receipt.original_trade_recovery||frozen()){
  if(priceLocked())next={...card,storage_location:$('editLocation').value.trim()};
   else next={...card,final_price:price,storage_location:$('editLocation').value.trim(),price_confirmed:researchOnly&&blankPrice?card.price_confirmed:$('editPriceConfirmed').checked||price>0};
  }else next={...card,card_name:$('editName').value.trim(),card_set:$('editSet').value.trim(),card_num:$('editNumber').value.trim(),condition:$('editCondition').value.trim(),language:$('editLanguage').value.trim(),printing:$('editFinish').value.trim(),qty:Number($('editQty').value),final_price:price,storage_location:$('editLocation').value.trim(),sku_override:$('editSku').value.trim(),...intakeExtraFields('edit')};
  if(price===card.final_price&&$('editPriceConfirmed').checked===(card.price_confirmed===true)){if(Object.hasOwn(card,'price_confirmed'))next.price_confirmed=card.price_confirmed;else delete next.price_confirmed}
  if(researchOnly&&blankPrice||reviewValueFields.some(key=>!sameEditableValue(card[key],next[key])))next.reviewed=false;
 if(researchOnly&&blankPrice)next.price_confirmed=card.price_confirmed;
 if((blankPrice&&!researchOnly&&!priceLocked())||!next.card_name||!next.condition||!next.printing||!Number.isInteger(next.qty)||next.qty<1||!Number.isFinite(next.final_price)||next.final_price<0)throw Error('Enter a name, condition, finish, whole-number quantity and valid price.');
 if(commit){Object.assign(card,next);markDirty()}return next;
}
async function loadActivity(){try{const data=await api('/api/activity');const pending=data.tasks.filter(t=>t.status==='pending');$('taskDot').hidden=!pending.length;$('taskList').innerHTML=data.tasks.length?data.tasks.map(t=>`<article class="task-card ${t.status==='pulled'?'pulled':''}"><div><strong>${t.qty} × ${escapeHTML(t.card_name)}</strong><p>${escapeHTML(t.storage_location)} · ${escapeHTML(t.channel)} · Order ${escapeHTML(t.order_reference)}</p><small>${escapeHTML(t.notes||'')}</small></div>${t.status==='pending'?`<button class="button secondary small" data-complete="${t.id}">Confirm pulled</button>`:'<span class="pill">PULLED</span>'}</article>`).join(''):'<div class="card-table-panel list-empty">No pull tasks yet. Record an online order when a card needs to leave the shop floor.</div>';$('eventList').innerHTML=data.events.length?data.events.map(e=>`<article class="event-row"><span class="event-icon">${icon(e.kind==='labels'?'printer':e.kind==='received'?'check-circle':'activity')}</span><div><a href="#" data-open="${escapeHTML(e.receipt_id)}">${escapeHTML(e.receipt_id)}</a><p>${escapeHTML(e.detail)}</p></div><time>${new Date(e.at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}</time></article>`).join(''):'<div class="list-empty">Your receipt history will appear here.</div>'}catch(e){toast(e.message,true)}}
function verifiedProfile(settings){
 const p=settings?.profile;
 if(!p||p.profileVersion!==1||!['hosted','legacy_compatibility'].includes(p.profileMode)||typeof p.storeBound!=='boolean'||p.localCreditEnabled!==(p.profileMode==='legacy_compatibility')||p.legacySquareEntryEnabled!==(p.profileMode==='legacy_compatibility')||p.requiredReceivingRoute!==(p.profileMode==='hosted'?'hosted':'legacy_compatible'))throw Error('This profile could not be verified. Reopen the complete app before changing settings.');
 return p;
}
async function openAdmin(){
 try{
  if(!hostedProfile()&&!legacyProfile())throw Error('Profile settings are unavailable. Reopen the complete app before changing settings.');
  await adminClosing;const auth=await api('/api/auth');state.admin=auth.authenticated;state.configured=auth.configured;$('adminMessage').textContent='';$('adminAuth').hidden=state.admin;$('adminSettings').hidden=!state.admin;
  $('adminTitle').textContent=state.admin?'Settings that keep work moving.':auth.configured?'Unlock this computer’s settings.':'Protect this computer’s settings.';
  $('authDescription').textContent=hostedProfile()?'This password protects local printer, folder and workflow settings. Your store account and Square OAuth are managed through Store setup.':auth.configured?'Your saved Square connection stays available to operators. Only admins can change it.':'Choose a password of at least 12 characters. You’ll use it to manage credentials and workflow defaults.';
  $('confirmPasswordLabel').hidden=auth.configured;$('confirmPassword').required=!auth.configured;$('adminPassword').autocomplete=auth.configured?'current-password':'new-password';$('authSubmit').innerHTML=(auth.configured?'Unlock settings':'Create device password')+icon('unlock');$('adminPassword').value=$('confirmPassword').value='';$('adminDialog').showModal();if(state.admin)await loadAdmin();else $('adminPassword').focus();
 }catch(e){toast(e.message,true)}
}
async function loadAdmin(){
 const settings=await api('/api/admin/settings'),p=verifiedProfile(settings),legacy=p.profileMode==='legacy_compatibility';
 if(p.profileMode!==state.settings.profile?.profileMode)throw Error('The profile changed. Reopen the app before changing settings.');
 $('adminTitle').textContent=legacy?'Settings that keep work moving.':'This computer’s workflow settings.';
 $('legacySquareSettings').hidden=!legacy;$('hostedSquareSettings').hidden=legacy;
 for(const id of ['squareToken','squareEnvironment','squareLocation','testSquare'])$(id).disabled=!legacy;
 $('squareToken').value='';
 if(legacy){$('squareToken').placeholder=settings.tokenSaved?'Saved token · leave blank to keep':'Enter a Square token';$('tokenStatus').textContent=settings.tokenSaved?'Token saved securely. Enter a replacement only when needed.':'No token saved yet.';$('squareEnvironment').value=settings.squareEnvironment;$('squareLocation').replaceChildren(new Option(settings.squareLocationId?'Saved receiving store · '+settings.squareLocationId:'Test connection to load stores',settings.squareLocationId||''));}
 else{$('squareToken').placeholder='';$('tokenStatus').textContent='';$('squareLocation').replaceChildren();}
 $('adminSettings').querySelector('.admin-status span').textContent=legacy?'Admin unlocked · credentials stay on this computer':'Device settings unlocked · Square connects through your store account';
 $('adminSaveHint').textContent=legacy?'Token fields are never returned by the app.':'These settings do not change store authorization or enable receiving.';
 $('watchFolder').value=settings.watchFolder;$('watchMode').value=settings.watchMode;$('defaultCash').value=settings.cashPercent;$('defaultCredit').value=settings.creditPercent;$('defaultMinimum').value=settings.minPrice;$('defaultDpi').value=settings.printerDpi;$('defaultLayout').value=settings.labelLayout;$('pricingAutoCorrect').checked=settings.pricingAutoCorrect;$('pricingAlertPercent').value=settings.pricingAlertPercent;$('pricingAlertDollars').value=settings.pricingAlertDollars;$('pricingEnabled').checked=settings.pricingEnabled;$('pricingCompleteOnly').checked=settings.pricingCompleteOnly;$('defaultPricingGame').value=settings.pricingGame;$('intakeValuePercent').value=settings.intakeValuePercent;$('retailValuePercent').value=settings.retailValuePercent;$('squareConnectionMessage').textContent='';$('adminAuth').hidden=true;$('adminSettings').hidden=false;
 if(adminFocusPickup){adminFocusPickup=false;$('pickupSettings').scrollIntoView({block:'start'});$('watchFolder').focus({preventScroll:true})}
}
async function loadPublicSettings(){
 const settings=await api('/api/settings');verifiedProfile(settings);state.settings=settings;
 state.settings.creditTracking=legacyProfile()&&settings.creditTracking===true;
 $('creditNav').hidden=!state.settings.creditTracking;$('editionLabel').textContent=hostedProfile()?'CONNECTED STORE':state.settings.creditTracking?'CREDIT TRACKER':'STANDARD';$('minPrice').value=state.settings.minPrice??.7;
 const ready=legacyProfile()&&state.settings.squareConfigured;$('squareBadge').classList.toggle('ready',!!ready);$('squareBadge').innerHTML=`<span class="status-dot"></span>${hostedProfile()?'Store account · hosted Square':ready?'Square saved · '+escapeHTML(state.settings.squareEnvironment):'Square setup needed'}`;
 $('legacySquareSettings').hidden=!legacyProfile();$('hostedSquareSettings').hidden=!hostedProfile();
 for(const id of ['squareToken','squareEnvironment','squareLocation','testSquare'])$(id).disabled=!legacyProfile();
 configureStoreSetup();
}
async function testLegacySquare(){
 if(!legacyProfile())return toast('Use Store setup to review Square OAuth. No local token is used by this profile.',true);
 const button=$('testSquare');button.disabled=true;
 try{const result=await api('/api/admin/locations',{squareToken:$('squareToken').value,squareEnvironment:$('squareEnvironment').value});const previous=$('squareLocation').value;const active=result.locations.filter(l=>l.status==='ACTIVE');$('squareLocation').replaceChildren(new Option('Choose receiving store',''));active.forEach(l=>$('squareLocation').add(new Option(`${l.name} (${l.id})`,l.id)));if(active.some(l=>l.id===previous))$('squareLocation').value=previous;else if(active.length===1)$('squareLocation').value=active[0].id;$('squareConnectionMessage').textContent=active.length?`Connected to ${result.environment}. ${active.length} active store(s). Verify your store, then save settings.`:'Connected, but no active locations were returned. Activate the receiving location in Square.'}
 catch(e){$('squareConnectionMessage').textContent=e.message}finally{button.disabled=false}
}
async function saveAdminSettings(e){
 e.preventDefault();
 try{
  const data={pricingAutoCorrect:$('pricingAutoCorrect').checked,pricingAlertPercent:Number($('pricingAlertPercent').value),pricingAlertDollars:Number($('pricingAlertDollars').value),pricingEnabled:$('pricingEnabled').checked,pricingCompleteOnly:$('pricingCompleteOnly').checked,pricingGame:$('defaultPricingGame').value,intakeValuePercent:Number($('intakeValuePercent').value),retailValuePercent:Number($('retailValuePercent').value),watchFolder:$('watchFolder').value,watchMode:$('watchMode').value,cashPercent:Number($('defaultCash').value),creditPercent:Number($('defaultCredit').value),minPrice:Number($('defaultMinimum').value),printerDpi:Number($('defaultDpi').value),labelLayout:$('defaultLayout').value};
  if(legacyProfile())Object.assign(data,{squareToken:$('squareToken').value,squareEnvironment:$('squareEnvironment').value,squareLocationId:$('squareLocation').value});
  else if(!hostedProfile())throw Error('The profile is not verified. Reopen the app before saving settings.');
  await api('/api/admin/settings',data);$('squareToken').value='';await loadPublicSettings();await loadAdmin();$('adminMessage').textContent=hostedProfile()?'Computer settings saved. Store authorization stays in Store setup.':'Settings saved. Operators can use the connection without entering the token.';$('adminMessage').classList.remove('error');await refreshReceipts();toast('Settings saved.');
 }catch(e){$('adminMessage').textContent=e.message;$('adminMessage').classList.add('error')}
}
function showWorkflowHelp(){return confirmAction('A few things to know.',hostedProfile()?'Connect your invited store account in Store setup. The owner reviews receiving-specific Square OAuth; no local Square token or local wallet is used. Import and check cards, then explicitly review shared trade or hosted receiving. Labels/files only do not add stock. Printer and folder settings stay in this computer’s settings. Closing the window leaves the app running; use Quit app to stop it.':'Set up automatic pickup in Admin for recognized CSV formats. Save new CSV exports to that folder, or choose a file to review its format and map columns. Check your cards, then Continue. At the finish, choose Add to Square or labels only. Next batch starts fresh; Batch history reopens saved work. Printer settings are saved in Admin. Closing the window leaves Stock & Sleeve running. Use Quit app to stop it.','Got it')}

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
 if(p.scope==='hosted'){const cash=p.type==='cash';$('acceptedBuy').innerHTML='<div><span class="section-kicker">'+(cash?'CASH AGREEMENT RECORDED':'SHARED CREDIT ISSUED')+'</span><strong class="trade-approved-amount">'+usd(p.amount)+'</strong><p>'+(cash?'Payment execution is not recorded by Stock & Sleeve. No shared credit was added.':'Shared credit issued once. No local wallet credit was created.')+'</p></div><div class="trade-facts"><span><small>Store</small>'+escapeHTML(p.store)+'</span><span><small>Inventory</small>'+(r.inventory_status==='received'?'Confirmed in Square':'Separate receiving review required')+'</span><span><small>'+(cash?'Payment record':'Credit policy')+'</small>'+(cash?'Agreement only':'Tax policy unreviewed · Square checkout disabled')+'</span></div>';return}
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
 $('cardsTab').onclick=()=>showResearchPane('cards');$('researchTab').onclick=()=>showResearchPane('research');$('researchFilter').onchange=()=>{researchPage=1;renderResearchTable()};$('researchPrevious').onclick=()=>{researchPage--;renderResearchTable()};$('researchNext').onclick=()=>{researchPage++;renderResearchTable()};$('researchRows').onclick=async e=>{const b=e.target.closest('[data-research-edit]');if(b&&await openCard(b.dataset.researchEdit))await openPriceResearch('edit')};
 $('creditSearch').oninput=()=>{clearTimeout(creditSearchTimer);creditSearchTimer=setTimeout(()=>loadCreditCustomers().catch(e=>toast(e.message,true)),250)};$('creditCustomerList').onclick=e=>{const b=e.target.closest('[data-credit-customer]');if(b)loadCreditAccount(b.dataset.creditCustomer).catch(err=>toast(err.message,true))};
 $('creditAddCustomer').onclick=()=>{creditCustomerRequest=crypto.randomUUID();$('creditCustomerForm').reset();$('creditCustomerMessage').textContent='';$('creditCustomerDialog').showModal()};$('creditCloseCustomer').onclick=()=>$('creditCustomerDialog').close();$('creditCustomerForm').onsubmit=async e=>{e.preventDefault();const form=$('creditCustomerForm');form.inert=true;try{const data=await api('/api/credit/customer',{requestId:creditCustomerRequest,name:$('creditCustomerName').value,contact:$('creditCustomerContact').value,square_reference:$('creditSquareReference').value});$('creditCustomerDialog').close();await loadCreditCustomers();await loadCreditAccount(data.customer.id)}catch(err){$('creditCustomerMessage').textContent=err.message}finally{form.inert=false}};
 $('creditCloseEntry').onclick=()=>{$('creditEntryDialog').close();$('creditAdminPassword').value=''};$('creditEntryKind').onchange=renderCreditEntry;$('creditEntryForm').onsubmit=async e=>{e.preventDefault();const form=$('creditEntryForm');form.inert=true;try{const data=await api('/api/credit/transaction',{requestId:creditEntryRequest,customerId:creditAccount.customer.id,version:creditAccount.customer.version,kind:$('creditEntryKind').value,amount:$('creditEntryAmount').value,receiptId:creditTrade?.id,reference:$('creditEntryReference').value,note:$('creditEntryNote').value,adminPassword:$('creditAdminPassword').value});const trade=$('creditEntryKind').value==='trade';creditAccount=data;if(trade)creditTrade=null;$('creditEntryDialog').close();$('creditAdminPassword').value='';renderCreditAccount();await loadCreditCustomers();toast('Credit tracker updated. Square was not changed.')}catch(err){$('creditEntryMessage').textContent=err.message;if(creditAccount)await loadCreditAccount(creditAccount.customer.id)}finally{form.inert=false}};
}

function bind(){trackPriceApprovalEdits();$('openStoreSetupFromAdmin').onclick=()=>{$('adminDialog').close();$('storeSetupDetails').open=true;$('storeSetupSection').scrollIntoView({block:'start'});window.refreshStoreSetupGuide?.()};$('quitApp').onclick=quitStation;bindWorkflowExtensions();document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>navigate(b.dataset.mode));$('newReceipt').onclick=startNewBatch;$('compareManualPrices').onclick=()=>openPriceResearch('manual');$('compareCardPrices').onclick=()=>openPriceResearch('edit');$('closePriceResearch').onclick=$('returnToItem').onclick=()=>$('priceResearchDialog').close();$('priceResearchGame').onchange=()=>{if(priceResearchOrigin==='manual')$('manualGame').value=$('priceResearchGame').value;renderPriceResearch()};$('priceResearchProfile').onchange=()=>paintPriceResearch(priceResearchResult);$('approveSuggestedPrice').onclick=approveSuggestedPrice;$('priceResearchDialog').addEventListener('close',()=>{++priceResearchRequest;clearTimeout(priceResearchTimer)});$('batchPricingGame').onchange=async()=>{if(!receiptMutationAllowed())return;await busyWork(async()=>{state.receipt.pricing_game=$('batchPricingGame').value;markDirty();await saveReceipt();pricingResults={};await refreshPricing();toast('Research started for this game. Open Price research to review source coverage.')})};$('researchBatch').onclick=async()=>{await busyWork(async()=>{await saveReceipt();await api('/api/receipts/'+state.receipt.id+'/research',{revision:state.receipt.revision});await refreshPricing();toast('Research queued. Open Price research to review source coverage.')})};$('addManualItem').onclick=openManualItem;$('manualItemForm').onsubmit=saveManualItem;$('closeManualItem').onclick=$('cancelManualItem').onclick=()=>{if(!state.busy)$('manualItemDialog').close()};$('manualItemDialog').addEventListener('cancel',e=>{if(state.busy)e.preventDefault()});$('emptyImport').onclick=()=>$('csvFile').click();$('setupPickup').onclick=setupPickup;$('openNextPickup').onclick=openNextPickup;$('useDownloads').onclick=async()=>{try{const data=await api('/api/admin/downloads-folder',{});$('watchFolder').value=data.folder;$('adminMessage').textContent='Downloads selected. Save settings to start pickup, then save a new recognized CSV export to that folder.'}catch(e){$('adminMessage').textContent=e.message}};$('deleteReceipt').onclick=deleteCurrentReceipt;$('reviewContinue').onclick=continueReview;$('backToReview').onclick=$('doneReview').onclick=showCards;$('nextBatch').onclick=startNewBatch;$('openHistory').onclick=openHistory;$('closeHistory').onclick=()=>$('historyDialog').close();$('historySearch').oninput=renderHistory;$('historyList').onclick=e=>{const b=e.target.closest('[data-open]');if(b)openReceipt(b.dataset.open)};$('labelLayout').onchange=()=>state.labelLayout=$('labelLayout').value;$('printerDpi').onchange=()=>state.printerDpi=Number($('printerDpi').value);$('csvFile').onchange=()=>importFile($('csvFile').files[0]);$('receiptPicker').onchange=e=>{if(e.target.value)openReceipt(e.target.value).catch(e=>toast(e.message,true));else startNewBatch()};$('receiptTitle').oninput=()=>{if(!receiptMutationAllowed())return;state.receipt.title=$('receiptTitle').value;markDirty()};$('receiptNotes').oninput=()=>{if(!receiptMutationAllowed())return;state.receipt.notes=$('receiptNotes').value;markDirty()};$('saveReceipt').onclick=()=>{if(issueHere())return void resolveReceiptSaveIssue();saveReceipt().then(()=>toast('Batch saved.')).catch(e=>toast(e.message,true))};$('moveReceipt').onchange=async e=>{const mode=e.target.value;e.target.value='';if(mode)await busyWork(()=>moveReceipt(mode))};$('cardSearch').oninput=$('reviewFilter').onchange=()=>{state.page=1;renderTable()};$('previousPage').onclick=()=>{state.page--;renderTable()};$('nextPage').onclick=()=>{state.page++;renderTable()};$('cardRows').onclick=async e=>{if(state.busy)return;const research=e.target.closest('[data-price-research]');if(research){if(await openCard(research.dataset.priceResearch))await openPriceResearch('edit');return}const b=e.target.closest('[data-edit]');if(b)openCard(b.dataset.edit)};$('cardRows').onchange=e=>{if(state.busy)return renderTable();if(!receiptMutationAllowed())return renderTable();const checked=e.target.dataset.review;if(checked){const card=state.receipt.cards.find(c=>c.row_id===checked);if(!card)return;if(e.target.checked){const invoker=e.target;renderTable();void requestSavedCardReview([checked],{invoker})}else{card.reviewed=false;markDirty();renderStats();renderTable();renderActions()}return}const id=e.target.dataset.include;if(id){state.receipt.cards.find(c=>c.row_id===id).include=e.target.checked;markDirty();renderStats();renderActions()}};$('includePage').onchange=()=>{if(!receiptMutationAllowed())return;const cards=filteredCards().slice((state.page-1)*state.pageSize,state.page*state.pageSize);cards.forEach(c=>c.include=$('includePage').checked);markDirty();renderStats();renderTable();renderActions()};$('applyThreshold').onclick=()=>{if(!receiptMutationAllowed())return;const threshold=Number($('minPrice').value);if(!Number.isFinite(threshold)||threshold<0)return toast('Enter a valid threshold.',true);state.receipt.cards.forEach(c=>c.include=c.final_price>=threshold);markDirty();renderStats();renderTable();renderActions()};$('markReviewed').onclick=()=>void requestSavedCardReview(included().map(card=>card.row_id),{invoker:$('markReviewed')});$('primaryAction').onclick=primaryAction;$('prepareOnly').onclick=prepareWithoutStock;$('labelsOnly').onclick=labelsWithoutStock;$('cashOffer').onclick=e=>{if(hostedProfile())return;if(e.target.tagName!=='INPUT'){state.offer='cash';renderActions();$('cashOffer').classList.add('selected');$('creditOffer').classList.remove('selected')}};$('creditOffer').onclick=e=>{if(hostedProfile())return;if(e.target.tagName!=='INPUT'){state.offer='credit';renderActions();$('creditOffer').classList.add('selected');$('cashOffer').classList.remove('selected')}};for(const [id,key]of [['cashPercent','cash_percent'],['creditPercent','credit_percent']])$(id).oninput=()=>{if(!receiptMutationAllowed())return;state.receipt[key]=Number($(id).value);markDirty();renderStats()};$('customerReference').oninput=()=>{if(!receiptMutationAllowed())return;state.receipt.customer_reference=$('customerReference').value;markDirty()};document.querySelectorAll('[data-intent]').forEach(b=>b.onclick=()=>{if(!receiptMutationAllowed())return;state.receipt.inventory_intent=b.dataset.intent;markDirty();renderReceipt()});for(const [id,key]of [['onlineChannel','channel'],['storageStrategy','storage_strategy'],['storageLocation','storage_location']])$(id).onchange=()=>{if(!receiptMutationAllowed())return;state.receipt[key]=$(id).value;markDirty()};bindCardEditor();for(const id of ['editName','editSet','editNumber','editCondition','editLanguage','editFinish'])$(id).oninput=()=>{$('editReviewed').checked=false;$('priceReminder').textContent='Details changed. Save and review the saved card before continuing.'};$('editPrice').oninput=()=>$('editReviewed').checked=false;$('splitCopy').onclick=splitOneCopy;$('openAdmin').onclick=openAdmin;$('closeAdmin').onclick=()=>{adminFocusPickup=false;$('adminDialog').close()};$('adminDialog').addEventListener('close',()=>{adminClosing=api('/api/auth/logout',{}).catch(()=>{});state.admin=false;$('squareToken').value='';$('adminPassword').value='';});$('authForm').onsubmit=async e=>{e.preventDefault();try{if(!state.configured&&$('adminPassword').value!==$('confirmPassword').value)throw Error('The passwords do not match.');await api('/api/auth/'+(state.configured?'login':'setup'),{password:$('adminPassword').value});$('adminPassword').value=$('confirmPassword').value='';state.admin=state.configured=true;notice('');$('adminMessage').textContent='';await loadAdmin()}catch(e){$('adminMessage').textContent=e.message;$('adminMessage').classList.add('error')}};$('lockAdmin').onclick=async()=>{await api('/api/auth/logout',{});state.admin=false;$('adminDialog').close();toast('Admin locked.')};$('squareEnvironment').onchange=$('squareToken').oninput=()=>{$('squareLocation').replaceChildren(new Option('Test connection to load stores',''));$('squareConnectionMessage').textContent='Connection changed. Test it before choosing a store.'};$('testSquare').onclick=testLegacySquare;$('adminForm').onsubmit=saveAdminSettings;$('changePasswordForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/admin/password',{password:$('newAdminPassword').value});$('newAdminPassword').value='';$('adminDialog').close();state.admin=false;toast('Password changed. All admin sessions are locked.')}catch(e){$('adminMessage').textContent=e.message}};$('eventList').onclick=e=>{const b=e.target.closest('[data-open]');if(b){e.preventDefault();openReceipt(b.dataset.open)}};$('taskList').onclick=async e=>{const b=e.target.closest('[data-complete]');if(b&&await confirmAction('Confirm the cards were pulled?','Confirm the requested copies have been removed from the shop floor and placed with the order. This does not change inventory.','Confirm pulled')){try{await api('/api/tasks/'+b.dataset.complete+'/complete',{});await loadActivity();toast('Pull confirmed.')}catch(e){toast(e.message,true)}}};$('newTask').onclick=()=>$('taskDialog').showModal();$('closeTask').onclick=()=>$('taskDialog').close();$('taskForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/tasks',{channel:$('taskChannel').value,order_reference:$('taskOrder').value,card_name:$('taskCard').value,qty:Number($('taskQty').value),storage_location:$('taskLocation').value,notes:$('taskNotes').value});$('taskDialog').close();$('taskForm').reset();await loadActivity();toast('Order added to the pull queue.')}catch(e){$('taskMessage').textContent=e.message}};$('helpButton').onclick=showWorkflowHelp;let dragDepth=0;document.addEventListener('dragenter',e=>{if(e.dataTransfer.types.includes('Files')){e.preventDefault();dragDepth++;$('dropOverlay').hidden=false}});document.addEventListener('dragleave',()=>{dragDepth--;if(dragDepth<=0)$('dropOverlay').hidden=true});document.addEventListener('dragover',e=>e.preventDefault());document.addEventListener('drop',e=>{e.preventDefault();dragDepth=0;$('dropOverlay').hidden=true;if(e.dataTransfer.files.length)importFile(e.dataTransfer.files[0])});window.addEventListener('beforeunload',e=>{if(state.dirty||saving||issueHere()||issueHere(reviewUncertainty)||savedReviewOperation?.phase==='posting'||drawerDirty()||drawerDraft?.busy||drawerDraft?.uncertain){e.preventDefault();e.returnValue=''}});document.addEventListener('keydown',handleStationShortcut)}
async function init(){icons();bind();try{const version=await api('/api/version');if(version.version!=='2.6.0')throw Error('A different version is running. Stop it before opening Stock & Sleeve 2.6.0.');$('version').textContent=version.version;await loadPublicSettings();await refreshReceipts();setMode('labels');const auth=await api('/api/auth');state.configured=auth.configured;if(hostedProfile()){notice('Connect your invited store account in Store setup. Square uses hosted OAuth; intake and labels can be prepared offline.');await window.refreshCloudSetupSession?.();}else if(!auth.configured)notice('Welcome to Stock & Sleeve. Invited staff can open Shared credit. Set up local receiving in Admin; this is a private pilot.');await pollStation();pollTimer=setInterval(pollStation,3000)}catch(e){notice(e.message,true)}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init,{once:true});else init();
