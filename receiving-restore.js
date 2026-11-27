'use strict';
// Review and open an original local stock record. Provider actions are separate.
const originalStockStyle=document.createElement('link');originalStockStyle.rel='stylesheet';originalStockStyle.href='/receiving-restore.css';document.head.append(originalStockStyle);
const originalStockEntry=document.createElement('button');originalStockEntry.id='reviewOriginalStockPlan';originalStockEntry.className='button secondary';originalStockEntry.textContent='Review original stock plan';originalStockEntry.hidden=true;$('sharedTradePanel').append(originalStockEntry);
const originalStockDialog=document.createElement('dialog');originalStockDialog.id='originalStockDialog';originalStockDialog.className='modal original-stock-modal';originalStockDialog.setAttribute('aria-labelledby','originalStockHeading');
originalStockDialog.innerHTML=`<div class="modal-heading"><div><span class="section-kicker">ORIGINAL STOCK PLAN</span><h2 id="originalStockHeading">Review the original plan.</h2></div><button class="icon-button" id="closeOriginalStock" aria-label="Close original stock review">${icon('x')}</button></div><p id="originalStockMessage" class="original-stock-message" role="status" aria-live="polite"></p><section id="originalStockHistory" hidden><label>Saved receiving plan<select id="originalStockChoice"></select></label><div id="originalStockHistoryFact" class="original-stock-history-fact"></div><button id="previewOriginalStock" class="button secondary">Review selected plan</button></section><section id="originalStockPreview" hidden><div id="originalStockState"></div><div id="originalStockCounts" class="original-stock-counts"></div><div id="originalStockPlanFacts" class="original-stock-plan-facts"></div><p id="originalStockLabelNote" class="hint"></p><div class="original-stock-table-wrap"><table class="original-stock-table"><thead><tr><th>Original item / frozen SKU</th><th>Copies</th><th>Frozen price</th></tr></thead><tbody id="originalStockRows"></tbody></table></div><div class="original-stock-pager"><span id="originalStockPageCount" aria-live="polite"></span><div><button class="button quiet small" id="originalStockPrevious">Previous</button><button class="button quiet small" id="originalStockNext">Next</button></div></div><details class="original-stock-technical"><summary>Original references</summary><div id="originalStockReferences"></div></details></section><section id="originalStockConsent" class="original-stock-consent" hidden><p>This opens the original plan as a separate stock record on this computer. Your agreement and imported batch stay saved. Stock, payments and credit are unchanged.</p><label><input type="checkbox" id="originalStockReviewed">I reviewed the original quantities, prices, SKUs and label profile.</label><label id="originalStockPhysicalLabel" hidden><input type="checkbox" id="originalStockPhysical">I checked that this import is the same physical delivery as the original plan.</label><label id="originalStockRestoreLabel" hidden><input type="checkbox" id="originalStockRestore">Restore the same stock record that was deleted on this computer.</label></section><div class="original-stock-footer"><button class="button secondary" id="cancelOriginalStock">Close</button><button class="button primary" id="openOriginalStockRecord" disabled>Open original stock record</button></div>`;
document.body.append(originalStockDialog);
const originalStockPane=document.createElement('section');originalStockPane.id='originalStockPane';originalStockPane.className='original-stock-pane';originalStockPane.hidden=true;$('receiptWorkspace').before(originalStockPane);
const stockOrigin='https://stock-and-sleeve-api.onrender.com',stockUUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/,stockHash=/^[a-f0-9]{64}$/,stockLocalId=/^R-[A-Za-z0-9-]{1,100}$/;
const stockStates={ready:'Ready for review',catalog_pending:'Checking the original catalog step',catalog_confirmed:'Catalog confirmed · stock not confirmed',inventory_pending:'Checking original stock confirmation',outcome_unknown:'Awaiting original operation evidence',needs_review:'Owner review needed',received:'Original stock confirmed',cancelled_before_write:'Cancelled before any write'};
let originalStockContext=null,originalStockImportHint=null,originalStockPaneKey='',originalStockPaneIdentity='',originalStockPanePage=0,originalStockRejectedAnchor='';
const stockEsc=value=>escapeHTML(String(value??''));
const stockInt=(value,min=0,max=100000000)=>Number.isSafeInteger(value)&&value>=min&&value<=max;
const stockString=(value,max=500)=>typeof value==='string'&&value.length>0&&value.length<=max;
const stockManager=()=>hostedProfile()&&cloudSession.signedIn&&['owner','manager'].includes(cloudSession.role)&&stockString(cloudSession.sessionId,200);
const stockAuth=()=>JSON.stringify([cloudRevision,cloudSession.sessionId||'',cloudSession.store||'',cloudSession.role||'',!!cloudSession.signedIn]);
const stockAnchor=()=>({receiptId:state.receipt?.id||null,revision:state.receipt?.revision??null});
const stockAnchorKey=()=>JSON.stringify([stockAnchor(),stockAuth()]);
const stockPendingInputs=()=>!!state.dirty||!$('cardDrawer').hidden||['manualItemDialog','importDialog','priceResearchDialog'].some(id=>!!$(id)?.open);
const stockChild=receipt=>!!receipt&&Object.prototype.hasOwnProperty.call(receipt,'original_receiving_recovery');
const stockFinancial=receipt=>!!receipt&&!stockChild(receipt)&&receipt.purchase?.scope==='hosted'&&['cash','credit'].includes(receipt.purchase.type)&&receipt.shared_trade?.state==='confirmed'&&stockUUID.test(receipt.purchase.hosted_trade_id||'')&&stockHash.test(receipt.purchase.receipt_hash||'');
function stockInvalid(message='The original stock plan could not be verified. Close this review and reopen the same saved agreement.'){throw Error(message)}
function stockCurrent(context){const anchor=stockAnchor();return !!context&&context===originalStockContext&&context.auth===stockAuth()&&context.anchor.receiptId===anchor.receiptId&&context.anchor.revision===anchor.revision&&!stockPendingInputs()&&stockManager()}
function stockProfile(value){return !!value&&typeof value==='object'&&!Array.isArray(value)&&['2x1','3x1','4x2'].includes(value.stock)&&['side','standard','fold'].includes(value.layout)&&[203,300].includes(value.dpi)}
function stockProvider(value){return !!value&&typeof value==='object'&&!Array.isArray(value)&&['production','sandbox'].includes(value.environment)&&['merchant_id','application_id','location_id'].every(key=>stockString(value[key]))}
function stockIdentity(value){return !!value&&typeof value==='object'&&!Array.isArray(value)&&stockString(value.name)&&stockString(value.game,100)}
function stockRowsValid(rows,total,offset,kind){
 if(!Array.isArray(rows)||rows.length!==Math.min(100,total-offset))return false;
 const seen=new Set();for(const row of rows){
  if(!row||!stockUUID.test(row.reviewLineId||'')||seen.has(row.reviewLineId)||!stockHash.test(row.sourceLineKey||'')||!stockHash.test(row.identityHash||'')||!stockIdentity(row.identity)||!stockIdentity(row.sourceIdentity)||!stockInt(row.quantity,1,100000)||!stockInt(row.sellingCents)||typeof row.zeroPriceConfirmed!=='boolean'||row.reviewed!==true||row.sellingCents===0&&!row.zeroPriceConfirmed||row.unitIntakeCents!==null&&!stockInt(row.unitIntakeCents)||!stockString(row.sourceSku,100)||!stockString(row.targetSku,100)||!stockString(row.parentId)||row.variationId!==null&&!stockString(row.variationId)||!['reuse_existing','create_price'].includes(row.action)||row.acceptedLineId!==null&&!stockUUID.test(row.acceptedLineId)||kind==='accepted_trade'&&!stockUUID.test(row.acceptedLineId||''))return false;
  seen.add(row.reviewLineId);
 }return true;
}
function stockCountsValid(value){return !!value&&typeof value==='object'&&['physicalOriginalCopies','acceptedCopies','selectedCopies','acceptedNotReceivedCopies','outsideAgreementCopies'].every(key=>stockInt(value[key]))&&value.physicalOriginalCopies>=value.acceptedCopies&&value.acceptedCopies>=value.selectedCopies&&value.acceptedNotReceivedCopies===value.acceptedCopies-value.selectedCopies&&value.outsideAgreementCopies===value.physicalOriginalCopies-value.acceptedCopies}
function stockStateCopy(state){
 if(state==='received')return 'The original stock confirmation is saved. Opening this record adds no more stock.';
 if(state==='cancelled_before_write')return 'The original plan was cancelled before a write. Opening its record does not restart it.';
 if(state==='ready')return 'The original plan is ready for review. Stock has not been confirmed by this record.';
 if(state==='catalog_confirmed')return 'The original catalog step is confirmed. Stock confirmation remains separate.';
 return 'Keep this original operation while its evidence is checked. Opening this record does not resend stock.';
}
function stockDetails(identity){return [identity.game_label||intakeGames[identity.game]||identity.game,identity.set,identity.number,identity.condition,identity.printing,identity.language,identity.grading_company,identity.grade,identity.grade_qualifier,identity.certification,identity.sport,identity.league,identity.year,identity.manufacturer,identity.player,identity.team,identity.serial_number].filter(value=>value!==undefined&&value!==null&&value!=='').map(value=>String(value)).join(' · ')}
function stockRowMarkup(row){return '<tr data-original-row="'+stockEsc(row.reviewLineId)+'"><td><strong>'+stockEsc(row.identity.name)+'</strong><small>'+stockEsc(stockDetails(row.identity))+'</small><code>“'+stockEsc(row.targetSku)+'”</code>'+(row.sourceSku!==row.targetSku?'<small>Original family SKU <code>“'+stockEsc(row.sourceSku)+'”</code></small>':'')+'</td><td>'+row.quantity.toLocaleString()+'</td><td>'+usd(row.sellingCents/100)+'</td></tr>'}
function stockCountMarkup(counts,selected){
 if(counts===null)return '<div class="original-stock-count selected"><strong>'+selected.toLocaleString()+'</strong><span>Selected copies</span></div><p class="original-stock-inventory-note">This is a historical inventory plan. Accepted-agreement totals do not apply.</p>';
 return [['physicalOriginalCopies','Original physical copies'],['acceptedCopies','Accepted copies'],['selectedCopies','Selected for this plan'],['acceptedNotReceivedCopies','Accepted, not selected'],['outsideAgreementCopies','Outside this agreement']].map(([key,label])=>'<div class="original-stock-count '+(key==='selectedCopies'?'selected':'')+'"><strong>'+counts[key].toLocaleString()+'</strong><span>'+label+'</span></div>').join('');
}
function stockClearPreview(){
 const context=originalStockContext;if(context){context.preview=null;context.confirmationHash='';clearTimeout(context.expiryTimer)}
 for(const id of ['originalStockPreview','originalStockConsent'])$(id).hidden=true;
 for(const id of ['originalStockState','originalStockCounts','originalStockPlanFacts','originalStockRows','originalStockReferences'])$(id).replaceChildren();
 $('originalStockLabelNote').textContent=$('originalStockPageCount').textContent='';
 for(const id of ['originalStockReviewed','originalStockPhysical','originalStockRestore'])$(id).checked=false;
 $('originalStockPhysicalLabel').hidden=$('originalStockRestoreLabel').hidden=true;$('openOriginalStockRecord').disabled=true;$('openOriginalStockRecord').textContent='Open original stock record';
}
function stockClearHistory(){stockClearPreview();$('originalStockHistory').hidden=true;$('originalStockChoice').replaceChildren();$('originalStockHistoryFact').replaceChildren();if(originalStockContext){originalStockContext.history=[];originalStockContext.selected=null}}
function stockFocusBack(node){if(node?.isConnected&&!node.disabled&&node.getClientRects().length)node.focus();else $('openHistory')?.focus()}
function stockClose({focus=true}={}){const context=originalStockContext;stockClearHistory();originalStockContext=null;$('originalStockMessage').textContent='';originalStockDialog.close();if(focus)stockFocusBack(context?.returnFocus)}
window.clearOriginalReceivingRestoreForAuthChange=()=>{originalStockImportHint=null;originalStockRejectedAnchor='';originalStockPaneKey='';stockClose({focus:false});stockRender()};
function stockReconcile(){
 if(originalStockContext&&!stockCurrent(originalStockContext)){stockClose({focus:false});originalStockImportHint=null}
 if(originalStockImportHint&&(originalStockImportHint.auth!==stockAuth()||originalStockImportHint.financialReceiptId!==state.receipt?.id||originalStockImportHint.financialRevision!==state.receipt?.revision||state.dirty))originalStockImportHint=null;
}
function stockButtons(){
 const context=originalStockContext,preview=context?.preview,busy=!!context?.busy;
 for(const node of originalStockDialog.querySelectorAll('button,input,select'))if(!['closeOriginalStock','cancelOriginalStock'].includes(node.id))node.disabled=busy;
 $('previewOriginalStock').disabled=busy||!context?.history?.some(row=>row.receivingId===$('originalStockChoice').value)||!stockCurrent(context);
 $('originalStockPrevious').disabled=busy||!preview||context.offset<=0;$('originalStockNext').disabled=busy||!preview||context.offset+100>=preview.totalRows;
 $('openOriginalStockRecord').disabled=busy||!preview||!stockCurrent(context)||Date.now()>=context.expiresAt||!$('originalStockReviewed').checked||preview.samePhysicalDeliveryRequired&&!$('originalStockPhysical').checked||preview.restoreRequired&&!$('originalStockRestore').checked;
}
async function stockRun(work){
 const context=originalStockContext;if(!context||context.busy)return;if(!stockCurrent(context)){stockClose({focus:false});return}
 context.busy=true;stockButtons();try{await work(context)}catch(error){
  if(!stockCurrent(context)){if(originalStockContext===context)stockClose({focus:false});return}
  stockClearPreview();
  if(error.status===401||error.status===403||error.sessionChanged){originalStockRejectedAnchor=stockAnchorKey();stockClearHistory();originalStockPaneKey=''}
  $('originalStockMessage').textContent=error.message;stockRenderPane();
 }finally{if(stockCurrent(context)){context.busy=false;stockButtons()}else if(originalStockContext===context)stockClose({focus:false})}
}
function stockBase(context){const body={financialReceiptId:context.financialReceiptId,financialRevision:context.financialRevision,receivingId:context.selected.receivingId,sessionId:context.sessionId};if(context.importHint)Object.assign(body,{fromImportReceiptId:context.importHint.fromImportReceiptId,fromImportRevision:context.importHint.fromImportRevision});return body}
function stockValidateHistory(data,context){
 if(!data||data.financialReceiptId!==context.financialReceiptId||data.financialRevision!==context.financialRevision||data.lookupComplete!==true||!Array.isArray(data.operations)||data.operations.length>1)stockInvalid('The original stock history could not be confirmed. Reopen this saved agreement.');
 for(const row of data.operations)if(!row||!stockUUID.test(row.receivingId||'')||!stockInt(row.serverRevision)||!stockHash.test(row.planHash||'')||!Object.prototype.hasOwnProperty.call(stockStates,row.state)||!stockProfile(row.labelProfile)||!stockProvider(row.provider)||!stockInt(row.selectedCopies))stockInvalid();
 return data.operations;
}
function stockShowHistory(context){
 $('originalStockHistory').hidden=false;const select=$('originalStockChoice');select.replaceChildren();
 if(!context.history.length){select.add(new Option('No original receiving plan found',''));$('originalStockMessage').textContent='No receiving plan is linked to this agreement yet. Your agreement and shared credit stay unchanged.';$('originalStockHistoryFact').textContent='Close this review to return to the saved agreement.';return}
 const row=context.history[0];select.add(new Option(stockStates[row.state]+' · '+row.selectedCopies.toLocaleString()+' selected copies',row.receivingId));context.selected=row;
 $('originalStockHistoryFact').innerHTML='<strong>'+stockEsc(stockStates[row.state])+'</strong><span>Original location '+stockEsc(row.provider.location_id)+'</span><span>'+stockEsc(row.labelProfile.stock)+' inches · '+stockEsc(row.labelProfile.layout)+' · '+row.labelProfile.dpi+' DPI</span>';
 $('originalStockMessage').textContent='Review the original frozen quantities and prices before opening its stock record.';$('previewOriginalStock').focus();
}
async function stockOpen(options={},trigger=originalStockEntry){
 stockClose({focus:false});originalStockPaneKey='';
 if(!stockManager()||state.busy)return;
 const current=state.receipt,marker=current?.original_receiving_recovery;
 if(!stockFinancial(current)&&!stockChild(current))return;
 const context={anchor:stockAnchor(),auth:stockAuth(),sessionId:cloudSession.sessionId,returnFocus:trigger,busy:false,history:[],preview:null,confirmationHash:'',offset:0};originalStockContext=context;originalStockDialog.showModal();
 if(stockPendingInputs()){$('originalStockMessage').textContent='Close your current editor or finish saving its changes, then reopen this review. Your entries have been kept.';$('openOriginalStockRecord').disabled=true;originalStockContext=null;return}
 await stockRun(async context=>{
  $('originalStockMessage').textContent='Checking the original receiving history…';
  if(stockFinancial(current)){context.financialReceiptId=current.id;context.financialRevision=current.revision}
  else{
   if(!marker||!stockLocalId.test(marker.financialReceiptId||''))stockInvalid();
   const result=await api('/api/receipts/'+encodeURIComponent(marker.financialReceiptId));if(!stockCurrent(context))return;
   const financial=result?.receipt;if(!stockFinancial(financial)||financial.id!==marker.financialReceiptId||!stockInt(financial.revision,1))stockInvalid();context.financialReceiptId=financial.id;context.financialRevision=financial.revision;
  }
  const hint=options.fromImportReceiptId?options:originalStockImportHint;
  if(hint&&hint.financialReceiptId===context.financialReceiptId&&hint.financialRevision===context.financialRevision&&stockLocalId.test(hint.fromImportReceiptId||'')&&stockInt(hint.fromImportRevision,1))context.importHint={fromImportReceiptId:hint.fromImportReceiptId,fromImportRevision:hint.fromImportRevision};
  const result=await cloudApi('receiving-restoration-history',{financialReceiptId:context.financialReceiptId,financialRevision:context.financialRevision,sessionId:context.sessionId});if(!stockCurrent(context))return;
  context.history=stockValidateHistory(result,context);originalStockRejectedAnchor='';stockShowHistory(context);
 });
}
function stockValidatePreview(data,context){
 const p=data?.preview,row=context.selected;
 if(!p||p.version!==1||p.financialReceiptId!==context.financialReceiptId||!stockUUID.test(p.tradeId||'')||!stockHash.test(p.receiptHash||'')||p.receivingId!==row.receivingId||p.serverRevision!==row.serverRevision||p.planHash!==row.planHash||!stockHash.test(p.restorationSourceHash||'')||!stockHash.test(p.proofHash||'')||!['accepted_trade','inventory_only'].includes(p.workflowKind)||!Object.prototype.hasOwnProperty.call(stockStates,p.state)||!stockProvider(p.provider)||!stockProfile(p.labelProfile)||typeof p.restoreRequired!=='boolean'||typeof p.samePhysicalDeliveryRequired!=='boolean'||p.samePhysicalDeliveryRequired!==!!context.importHint||p.existingProjectionId!==null&&!stockLocalId.test(p.existingProjectionId)||p.restoreRequired&&!p.existingProjectionId||!Array.isArray(p.allowedActions)||p.allowedActions.length||typeof p.received!=='boolean'||p.received!==(p.state==='received')||typeof p.labelsVerified!=='boolean'||p.labelError!==null&&typeof p.labelError!=='string'||!stockInt(p.totalRows,1,1000)||p.rowsOffset!==0||p.rowsLimit!==100||!stockRowsValid(p.rows,p.totalRows,0,p.workflowKind)||!stockHash.test(data.confirmationHash||'')||!stockInt(data.expiresIn,1,300))stockInvalid();
 if(p.workflowKind==='accepted_trade'&&!stockCountsValid(p.counts)||p.workflowKind==='inventory_only'&&p.counts!==null)stockInvalid();
 if(p.counts&&p.counts.selectedCopies!==row.selectedCopies||p.labelProfile.stock!==row.labelProfile.stock||p.labelProfile.layout!==row.labelProfile.layout||p.labelProfile.dpi!==row.labelProfile.dpi||['location_id','merchant_id','application_id','environment'].some(key=>p.provider[key]!==row.provider[key]))stockInvalid();
 return p;
}
function stockShowPage(context,rows,offset){
 $('originalStockRows').innerHTML=rows.map(stockRowMarkup).join('');context.offset=offset;$('originalStockPageCount').textContent='Showing '+(offset+1).toLocaleString()+'–'+(offset+rows.length).toLocaleString()+' of '+context.preview.totalRows.toLocaleString()+' original rows';stockButtons();
}
function stockShowPreview(context,data){
 const p=stockValidatePreview(data,context);stockClearPreview();context.preview=p;context.confirmationHash=data.confirmationHash;context.expiresAt=Date.now()+data.expiresIn*1000;
 context.selected.state=p.state;$('originalStockChoice').selectedOptions[0].textContent=stockStates[p.state]+' · '+context.selected.selectedCopies.toLocaleString()+' selected copies';$('originalStockHistoryFact').querySelector('strong').textContent=stockStates[p.state];
 $('originalStockState').innerHTML='<div class="original-stock-state '+(p.received?'confirmed':'')+'"><span class="section-kicker">'+stockEsc(stockStates[p.state])+'</span><h3>'+(p.existingProjectionId?'Original stock record saved':'Original plan, kept intact')+'</h3><p>'+stockEsc(stockStateCopy(p.state))+'</p></div>';
 $('originalStockCounts').innerHTML=stockCountMarkup(p.counts,context.selected.selectedCopies);
 $('originalStockPlanFacts').innerHTML='<div><span>Original Square location</span><strong>'+stockEsc(p.provider.location_id)+'</strong><small>'+stockEsc(p.provider.environment==='sandbox'?'Square test environment':'Live Square store')+'</small></div><div><span>Frozen label profile</span><strong>'+stockEsc(p.labelProfile.stock)+' inches</strong><small>'+stockEsc(p.labelProfile.layout)+' · '+p.labelProfile.dpi+' DPI</small></div>';
 $('originalStockLabelNote').textContent=p.labelError?'Label verification needs review: '+p.labelError:'These are the original plan’s frozen prices and SKUs. Printing and stock actions are separate from this review.';
 $('originalStockReferences').innerHTML=[['Receiving plan',p.receivingId],['Server revision',p.serverRevision],['Financial record',p.financialReceiptId],['Original trade',p.tradeId],['Agreement proof',p.receiptHash],['Plan proof',p.planHash],['Source proof',p.restorationSourceHash],['Review proof',p.proofHash],...(p.existingProjectionId?[['Saved stock record',p.existingProjectionId]]:[])].map(([name,value])=>'<p><strong>'+stockEsc(name)+'</strong><code>'+stockEsc(value)+'</code></p>').join('');
 $('originalStockPreview').hidden=$('originalStockConsent').hidden=false;$('originalStockPhysicalLabel').hidden=!p.samePhysicalDeliveryRequired;$('originalStockRestoreLabel').hidden=!p.restoreRequired;$('openOriginalStockRecord').textContent=p.restoreRequired?'Restore original stock record':'Open original stock record';
 $('originalStockMessage').textContent=p.restoreRequired?'Review and explicitly restore the same deleted stock record.':p.existingProjectionId?'Open the existing local record of this original plan.':'Review the original plan before saving its separate stock record.';stockShowPage(context,p.rows,0);
 context.expiryTimer=setTimeout(()=>{if(stockCurrent(context)){stockClearPreview();$('originalStockMessage').textContent='This review expired. Review the selected plan again.';stockButtons()}},data.expiresIn*1000);
}
async function stockPreview(){await stockRun(async context=>{context.selected=context.history.find(row=>row.receivingId===$('originalStockChoice').value);if(!context.selected)stockInvalid();stockClearPreview();$('originalStockMessage').textContent='Checking the original plan and its frozen rows…';const result=await cloudApi('receiving-restoration-preview',stockBase(context));if(stockCurrent(context))stockShowPreview(context,result)})}
async function stockPage(offset){await stockRun(async context=>{const p=context.preview;if(!p||!stockInt(offset)||offset>=p.totalRows)stockInvalid();const result=await cloudApi('receiving-restoration-rows',{...stockBase(context),confirmationHash:context.confirmationHash,offset,limit:100});if(!stockCurrent(context))return;if(result?.version!==1||result.receivingId!==p.receivingId||result.serverRevision!==p.serverRevision||result.proofHash!==p.proofHash||result.totalRows!==p.totalRows||result.offset!==offset||result.limit!==100||!stockRowsValid(result.rows,p.totalRows,offset,p.workflowKind))stockInvalid();stockShowPage(context,result.rows,offset)})}
function stockMarkerValid(receipt){const m=receipt?.original_receiving_recovery;return !!m&&m.version===1&&m.serviceOrigin===stockOrigin&&stockUUID.test(m.tenantId||'')&&stockLocalId.test(m.financialReceiptId||'')&&stockUUID.test(m.receivingId||'')&&stockInt(m.serverRevision)&&stockHash.test(m.planHash||'')&&stockHash.test(m.restorationSourceHash||'')&&stockHash.test(m.proofHash||'')&&receipt.purchase===null&&!['shared_trade','tracked_credit','original_trade_recovery'].some(key=>Object.prototype.hasOwnProperty.call(receipt,key))&&receipt.requires_purchase===false&&receipt.inventory_intent===null&&receipt.mode==='labels'&&receipt.hosted_receive?.restoration_projection===true&&receipt.hosted_receive.receiving_id===m.receivingId&&receipt.hosted_receive.server_revision===m.serverRevision&&receipt.hosted_receive.plan_hash===m.planHash}
function stockChildMatches(receipt,preview,context){
 if(!stockMarkerValid(receipt)||!stockInt(receipt.revision,1)||!stockString(receipt.title,5000)||!Array.isArray(receipt.cards)||receipt.cards.length!==preview.totalRows||receipt.inventory_status!==(preview.received?'received':'not_received')||receipt.hosted_receive.server_state!==preview.state||!stockProfile(receipt.hosted_receive.label_profile))return false;
 const marker=receipt.original_receiving_recovery;
 for(const key of ['tradeId','receiptHash','receivingId','serverRevision','planHash','restorationSourceHash','proofHash'])if(marker[key]!==preview[key])return false;
 if(marker.financialReceiptId!==context.financialReceiptId)return false;
 for(const key of ['stock','layout','dpi'])if(receipt.hosted_receive.label_profile[key]!==preview.labelProfile[key])return false;
 const cards=new Map();let copies=0;
 for(const card of receipt.cards){
  if(!card||!stockUUID.test(card.row_id||'')||cards.has(card.row_id)||!stockHash.test(card.source_line_key||'')||!stockString(card.card_name)||!stockString(card.game,100)||!stockInt(card.qty,1,100000)||typeof card.final_price!=='number'||!Number.isFinite(card.final_price)||card.final_price<0||card.final_price>1000000||Math.abs(card.final_price*100-Math.round(card.final_price*100))>1e-7||!stockString(card.sku,100)||card.include!==true||card.reviewed!==true||card.price_confirmed!==true||card.price_missing!==false)return false;
  cards.set(card.row_id,card);copies+=card.qty;
 }
 if(copies!==context.selected.selectedCopies)return false;
 for(const row of preview.rows){const card=cards.get(row.reviewLineId);if(!card||card.source_line_key!==row.sourceLineKey||card.card_name!==row.identity.name||card.game!==row.identity.game||card.qty!==row.quantity||Math.round(card.final_price*100)!==row.sellingCents||card.sku!==row.targetSku)return false}
 return true;
}
async function stockAdopt(){await stockRun(async context=>{
 const p=context.preview;if(!p||Date.now()>=context.expiresAt||!$('originalStockReviewed').checked||p.samePhysicalDeliveryRequired&&!$('originalStockPhysical').checked||p.restoreRequired&&!$('originalStockRestore').checked)stockInvalid('Review the original plan and required confirmations again.');
 const body={...stockBase(context),expectedServerRevision:p.serverRevision,expectedPlanHash:p.planHash,expectedRestorationSourceHash:p.restorationSourceHash,confirmationHash:context.confirmationHash,restoreOriginalPlanConfirmed:true};if(p.samePhysicalDeliveryRequired)body.samePhysicalDeliveryConfirmed=true;if(p.restoreRequired)body.restoreExisting=true;
 $('originalStockMessage').textContent='Opening the original stock record…';const result=await cloudApi('receiving-restoration-adopt',body);if(!stockCurrent(context))return;
 const receipt=result?.stockReceipt,r=result?.restoration,o=result?.operation;if(result?.readOnlyFinancial!==true||result.providerActionPerformed!==false||!receipt||!stockLocalId.test(receipt.id||'')||!stockChildMatches(receipt,p,context)||receipt.id===context.financialReceiptId||receipt.id===context.importHint?.fromImportReceiptId||p.existingProjectionId&&receipt.id!==p.existingProjectionId||r?.receivingId!==p.receivingId||r.serverRevision!==p.serverRevision||r.planHash!==p.planHash||r.financialReceiptId!==context.financialReceiptId||typeof r.reused!=='boolean'||r.restored!==p.restoreRequired||o?.receiving_id!==p.receivingId||o.revision!==p.serverRevision||o.plan_hash!==p.planHash||o.state!==p.state)stockInvalid();
 const auth=context.auth;stockClose({focus:false});originalStockImportHint=null;originalStockPanePage=0;originalStockPaneKey='';adoptReceipt(receipt);
 try{await refreshReceipts()}catch(error){if(state.receipt?.id===receipt.id&&stockAuth()===auth)toast('The stock record opened. Refresh History to update its saved list.',true);return}
 if(state.receipt?.id===receipt.id&&stockAuth()===auth){toast('Original stock record opened. Stock, payments and credit are unchanged.');$('reviewSavedOriginalStock')?.focus()}
 })}
function stockRenderPane(){
 const receipt=state.receipt,isChild=stockChild(receipt);document.body.classList.toggle('original-stock-record-active',isChild);
 $('receiptWorkspace').hidden=isChild||!receipt;
 if(window.renderIntakeStockProjection?.(receipt)===true){originalStockPane.hidden=true;originalStockPane.replaceChildren();originalStockPaneKey=originalStockPaneIdentity='';originalStockPanePage=0;return true}
 originalStockPane.hidden=!isChild;
 if(!isChild){originalStockPaneKey=originalStockPaneIdentity='';originalStockPane.replaceChildren();originalStockPanePage=0;return}
 if(originalStockPaneIdentity!==receipt.id){originalStockPaneIdentity=receipt.id;originalStockPanePage=0}
 const key=JSON.stringify([receipt.id,receipt.revision,stockAuth(),originalStockPanePage,originalStockRejectedAnchor]);if(key===originalStockPaneKey)return;originalStockPaneKey=key;
 if(!stockManager()||originalStockRejectedAnchor===stockAnchorKey()){originalStockPane.innerHTML='<span class="section-kicker">ORIGINAL STOCK RECORD</span><h2>Review the original stock record.</h2><p>Sign in as an owner or manager in this store to review its original plan.</p>'+(stockManager()?'<button class="button secondary" id="reviewSavedOriginalStock">Review original plan again</button>':'');if($('reviewSavedOriginalStock'))$('reviewSavedOriginalStock').onclick=()=>stockOpen({},$('reviewSavedOriginalStock'));return}
 const marker=receipt.original_receiving_recovery,stateName=receipt.hosted_receive?.server_state;
 if(!stockMarkerValid(receipt)||!Object.prototype.hasOwnProperty.call(stockStates,stateName)||(stateName==='received')!==(receipt.inventory_status==='received')||!Array.isArray(receipt.cards)||!receipt.cards.length||receipt.cards.length>1000||receipt.cards.some(card=>!stockString(card.card_name)||!stockInt(card.qty,1,100000)||!Number.isFinite(card.final_price)||card.final_price<0||card.final_price>1000000||!stockString(card.sku,100))){originalStockPane.innerHTML='<span class="section-kicker">ORIGINAL STOCK RECORD</span><h2>This saved record needs owner review.</h2><p>Its original stock facts could not be verified. Your agreement and imported batches have been kept.</p>';return}
 originalStockPanePage=Math.min(originalStockPanePage,Math.floor((receipt.cards.length-1)/100));
 const count=receipt.cards.reduce((sum,card)=>sum+card.qty,0),start=Math.min(originalStockPanePage*100,Math.max(0,receipt.cards.length-1)),rows=receipt.cards.slice(start,start+100),chosen=receipt.hosted_receive.label_profile;
 originalStockPane.innerHTML='<div class="original-stock-pane-heading"><div><span class="section-kicker">READ-ONLY ORIGINAL STOCK RECORD</span><h2>Original stock record</h2><p>Last saved state: <strong>'+stockEsc(stockStates[stateName])+'</strong></p></div><button class="button secondary" id="reviewSavedOriginalStock">Review original plan / refresh</button></div><p class="original-stock-pane-note">'+stockEsc(stockStateCopy(stateName))+' This record keeps the original plan separate from your financial agreement.</p><div class="original-stock-plan-facts"><div><span>Selected copies</span><strong>'+count.toLocaleString()+'</strong><small>'+receipt.cards.length.toLocaleString()+' original rows</small></div><div><span>Frozen label profile</span><strong>'+stockEsc(stockProfile(chosen)?chosen.stock+' inches':'Review the original plan')+'</strong><small>'+stockEsc(stockProfile(chosen)?chosen.layout+' · '+chosen.dpi+' DPI':'')+'</small></div></div><div class="original-stock-table-wrap"><table class="original-stock-table"><thead><tr><th>Original item / frozen SKU</th><th>Copies</th><th>Frozen price</th></tr></thead><tbody>'+rows.map(card=>'<tr><td><strong>'+stockEsc(card.card_name)+'</strong><small>'+stockEsc([card.game_label||intakeGames[card.game]||card.game,card.card_set,card.card_num,card.condition,card.printing,card.language,card.grader,card.grade,card.grade_qualifier,card.cert_number,card.sport,card.league,card.year,card.manufacturer,card.player,card.team,card.serial_number].filter(Boolean).join(' · '))+'</small><code>“'+stockEsc(card.sku)+'”</code></td><td>'+card.qty.toLocaleString()+'</td><td>'+usd(card.final_price)+'</td></tr>').join('')+'</tbody></table></div><div class="original-stock-pager"><span>Showing '+(start+1)+'–'+(start+rows.length)+' of '+receipt.cards.length.toLocaleString()+' saved rows</span><div><button class="button quiet small" id="savedStockPrevious" '+(start===0?'disabled':'')+'>Previous</button><button class="button quiet small" id="savedStockNext" '+(start+100>=receipt.cards.length?'disabled':'')+'>Next</button></div></div><details class="original-stock-technical"><summary>Original references</summary><p><strong>Saved stock record</strong><code>'+stockEsc(receipt.id)+'</code></p><p><strong>Receiving plan</strong><code>'+stockEsc(marker.receivingId)+'</code></p><p><strong>Server revision</strong><code>'+marker.serverRevision+'</code></p><p><strong>Financial record</strong><code>'+stockEsc(marker.financialReceiptId)+'</code></p></details>';
 $('reviewSavedOriginalStock').onclick=()=>stockOpen({},$('reviewSavedOriginalStock'));$('savedStockPrevious').onclick=()=>{originalStockPanePage=Math.max(0,originalStockPanePage-1);stockRenderPane()};$('savedStockNext').onclick=()=>{originalStockPanePage++;stockRenderPane()};
}
function stockRender(){stockReconcile();originalStockEntry.hidden=!stockManager()||!stockFinancial(state.receipt);originalStockEntry.disabled=state.busy;if(stockRenderPane()===true){window.clearOriginalStockLabelsForKindChange?.();window.clearOriginalStockActionsForKindChange?.();return}window.renderOriginalStockLabels?.();window.renderOriginalStockActions?.()}
window.noteOriginalStockRestoreImport=data=>{if(!data||!stockLocalId.test(data.financialReceiptId||'')||!stockInt(data.financialRevision,1)||!stockLocalId.test(data.fromImportReceiptId||'')||!stockInt(data.fromImportRevision,1))return;originalStockImportHint={...data,auth:stockAuth()};stockRender()};
originalStockEntry.onclick=()=>stockOpen({},originalStockEntry);$('previewOriginalStock').onclick=stockPreview;$('openOriginalStockRecord').onclick=stockAdopt;
$('originalStockPrevious').onclick=()=>{if(originalStockContext)stockPage(Math.max(0,originalStockContext.offset-100))};$('originalStockNext').onclick=()=>{if(originalStockContext)stockPage(originalStockContext.offset+100)};
$('originalStockChoice').onchange=()=>{stockClearPreview();if(originalStockContext)originalStockContext.selected=originalStockContext.history.find(row=>row.receivingId===$('originalStockChoice').value);stockButtons()};
for(const id of ['originalStockReviewed','originalStockPhysical','originalStockRestore'])$(id).onchange=stockButtons;
$('closeOriginalStock').onclick=$('cancelOriginalStock').onclick=()=>stockClose();originalStockDialog.addEventListener('cancel',event=>{event.preventDefault();stockClose()});
const stockPriorRender=window.renderTradeRecovery;window.renderTradeRecovery=()=>{stockPriorRender?.();stockRender()};
const stockPriorAuthClear=window.clearTradeRecoveryForAuthChange;window.clearTradeRecoveryForAuthChange=()=>{stockPriorAuthClear?.();window.clearOriginalReceivingRestoreForAuthChange()};
for(const type of ['input','change'])document.addEventListener(type,event=>{if(!originalStockDialog.contains(event.target))queueMicrotask(()=>stockReconcile())});
stockRender();
