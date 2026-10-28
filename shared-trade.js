'use strict';
// Shared issuance has its own explicitly selected customer and durable receipt.
const sharedTradeStyle=document.createElement('link');sharedTradeStyle.rel='stylesheet';sharedTradeStyle.href='/shared-trade.css';document.head.append(sharedTradeStyle);
const sharedTradePanel=document.createElement('section');sharedTradePanel.id='sharedTradePanel';sharedTradePanel.className='shared-trade-panel';sharedTradePanel.hidden=true;
sharedTradePanel.innerHTML='<div><span class="section-kicker">SHARED CUSTOMER CREDIT</span><h3 id="sharedTradeTitle">Issue credit from a hosted trade.</h3><p id="sharedTradeHint"></p></div><button class="button secondary" id="openSharedTrade">Choose shared customer</button>';
$('acceptedBuy').after(sharedTradePanel);
const sharedTradeDialog=document.createElement('dialog');sharedTradeDialog.id='sharedTradeDialog';sharedTradeDialog.className='modal shared-trade-modal';
sharedTradeDialog.innerHTML=`<div class="modal-heading"><div><span class="section-kicker">SHARED TRADE RECEIPT</span><h2>Review before issuing credit.</h2></div><button class="icon-button" id="closeSharedTrade" aria-label="Close shared trade">${icon('x')}</button></div><p id="sharedTradeMessage" role="status"></p><div id="sharedTradeCustomerPicker" hidden><p>Choose the customer in your signed-in shared store. The local customer reference does not select a shared account.</p><label>Find shared customer<input id="sharedTradeSearch" maxlength="200" placeholder="Name, contact or Square customer ID"></label><button class="button secondary small" id="searchSharedTradeCustomer">Search</button><div id="sharedTradeCustomers"></div><section id="sharedTradeSelected" hidden><strong id="sharedTradeSelectedName"></strong><p id="sharedTradeSelectedDetails"></p><button class="button primary" id="saveSharedTradeDraft">Save hosted draft for this customer</button></section></div><section id="sharedTradeOffer" hidden></section><section id="sharedTradeConsent" hidden><label><input id="sharedTradeAgreed" type="checkbox">The customer agrees to the saved hosted credit offer.</label><label><input id="sharedTradeReceived" type="checkbox">The store has physically received the included cards shown above.</label><p>Acceptance issues shared credit once. Square checkout remains disabled; this does not approve tax treatment or add Square stock.</p></section><div class="shared-trade-actions"><button class="button secondary" id="sharedTradeSignIn" hidden>Open shared store sign-in</button><button class="button secondary" id="refreshSharedTrade" hidden>Check saved trade</button><button class="button secondary" id="cancelSharedTradeDraft" hidden>Cancel hosted draft</button><button class="button primary" id="acceptSharedTrade" hidden>Confirm & issue shared credit</button></div>`;
document.body.append(sharedTradeDialog);
let sharedTradeContext=null,sharedTradeSequence=0;
window.sharedTradePendingMarkup=row=>row.action?.startsWith('trade_')?'<article class="cloud-pending-action"><p>Saved shared trade · '+escapeHTML(row.receiptId)+'</p><button class="button secondary small" data-open-shared-trade="'+escapeHTML(row.receiptId)+'">Open local trade & recover</button></article>':'';
window.sharedTradeLocked=receipt=>!!(receipt?.shared_trade&&!['confirmed','cancelled'].includes(receipt.shared_trade.state));
window.renderSharedTrade=()=>{
 const receipt=state.receipt,intent=receipt?.shared_trade;
 sharedTradePanel.hidden=!receipt||state.mode!=='trade'||(!!receipt.purchase&&receipt.purchase.scope!=='hosted');
 if(sharedTradePanel.hidden)return;
 const pending=window.sharedTradeLocked(receipt),confirmed=intent?.state==='confirmed';
 $('sharedTradeTitle').textContent=confirmed?'Shared trade credit confirmed.':pending?'This batch has a saved shared trade.':'Issue credit from a hosted trade.';
 $('sharedTradeHint').textContent=intent?.message||'Select a shared customer, review the hosted store offer, then have an owner or manager confirm. Local wallet credit is separate.';
 $('openSharedTrade').textContent=confirmed?'View hosted receipt':pending?'Open / recover shared trade':'Choose shared customer';
 $('openSharedTrade').disabled=state.busy||(!intent&&(!receipt.cards.some(card=>card.include)||receipt.cards.some(card=>card.include&&(!card.reviewed||card.price_missing))));
};
function sharedTradeClose(){if(sharedTradeContext?.busy)return;sharedTradeContext=null;++sharedTradeSequence;sharedTradeDialog.close()}
$('closeSharedTrade').onclick=sharedTradeClose;sharedTradeDialog.addEventListener('cancel',event=>{event.preventDefault();sharedTradeClose()});
function sharedTradeConsent(){const context=sharedTradeContext;$('acceptSharedTrade').disabled=!context||context.busy||!['owner','manager'].includes(context.role)||!$('sharedTradeAgreed').checked||!$('sharedTradeReceived').checked}
$('sharedTradeAgreed').onchange=$('sharedTradeReceived').onchange=sharedTradeConsent;
function sharedTradeAdopt(data){
 if(data.receipt&&state.receipt?.id===data.receipt.id){state.receipt=data.receipt;state.dirty=false;renderReceipt()}
 const context=sharedTradeContext;if(!context)return;context.data=data;const trade=data.trade;
 $('sharedTradeCustomerPicker').hidden=true;$('sharedTradeOffer').hidden=!trade;
 $('refreshSharedTrade').hidden=false;$('cancelSharedTradeDraft').hidden=!window.sharedTradeLocked(data.receipt)||trade?.state?.startsWith('accepted_');
 $('sharedTradeConsent').hidden=trade?.state!=='draft';$('acceptSharedTrade').hidden=trade?.state!=='draft';
 $('sharedTradeAgreed').checked=$('sharedTradeReceived').checked=false;
 if(trade){
  const accepted=trade.state==='accepted_credit',customer=data.customer||trade.accepted_receipt?.customer||{};
  $('sharedTradeOffer').innerHTML='<div class="shared-trade-facts"><div><span>Hosted customer</span><strong>'+escapeHTML(customer.display_name||data.receipt.shared_trade.customer_id)+'</strong><small>'+escapeHTML(data.receipt.shared_trade.store)+'</small></div><div><span>'+ (accepted?'Shared credit issued':'Saved hosted credit offer')+'</span><strong>'+usd((accepted?trade.accepted_amount_cents:trade.credit_offer_cents)/100)+'</strong><small>Intake total '+usd(trade.intake_total_cents/100)+'</small></div></div><p class="hint">Hosted receipt '+escapeHTML(trade.id)+' · '+escapeHTML(trade.state)+' · version '+Number(trade.version)+'</p><div class="shared-trade-lines"><table><thead><tr><th>Included item / identity</th><th>Copies</th><th>Intake each</th></tr></thead><tbody>'+trade.lines.filter(line=>line.included).map(line=>'<tr><td><strong>'+escapeHTML(line.identity.name)+'</strong><small>'+escapeHTML([line.identity.game_label||intakeGames[line.identity.game]||line.identity.game,line.identity.set,line.identity.number,line.identity.condition,line.identity.printing,line.identity.language,line.identity.grading_company,line.identity.grade,line.identity.certification,line.identity.year,line.identity.manufacturer,line.identity.player,line.identity.team,line.identity.serial_number].filter(Boolean).join(' · '))+'</small></td><td>'+Number(line.quantity)+'</td><td>'+usd(line.unit_intake_cents/100)+'</td></tr>').join('')+'</tbody></table></div>'+(accepted?'<p class="shared-trade-confirmed">Confirmed in shared credit. No local wallet entry was created. Square checkout is disabled and the credit tax policy remains unreviewed.</p><p class="hint shared-trade-hash">Receipt hash '+escapeHTML(trade.receipt_hash)+'</p>':'<p class="hint">This is the saved hosted store offer. Its rates can differ from the local estimate. Review the customer, cards and amount before agreeing.</p>');
 }
 $('sharedTradeMessage').textContent=data.receipt.shared_trade?.message||'Review the saved hosted receipt.';
 if(trade?.state==='draft'&&!['owner','manager'].includes(context.role))$('sharedTradeMessage').textContent='Draft saved. An owner or manager must sign in to this store to confirm the agreed offer.';
 sharedTradeConsent();
}
async function sharedTradeRun(work){
 const context=sharedTradeContext;if(!context||context.busy)return;context.busy=true;sharedTradeDialog.querySelectorAll('button,input').forEach(node=>node.disabled=true);
 try{await work(context)}catch(error){
  if(sharedTradeContext!==context)return;
  $('sharedTradeMessage').textContent=error.message+' The saved local batch remains available. Use Check saved trade to recover a pending action.';
  try{const data=await api('/api/receipts/'+context.receiptId);if(state.receipt?.id===context.receiptId){state.receipt=data.receipt;state.dirty=false;renderReceipt()}if(data.receipt.shared_trade){$('refreshSharedTrade').hidden=false;$('cancelSharedTradeDraft').hidden=!window.sharedTradeLocked(data.receipt)}}catch{}
 }finally{if(sharedTradeContext===context){context.busy=false;sharedTradeDialog.querySelectorAll('button,input').forEach(node=>node.disabled=false);sharedTradeConsent();await refreshReceipts().catch(()=>{})}}
}
async function sharedTradeSearch(){
 const context=sharedTradeContext;if(!context||context.busy)return;const sequence=++sharedTradeSequence;
 try{const data=await cloudApi('search',{query:$('sharedTradeSearch').value,sessionId:context.sessionId});if(sharedTradeContext!==context||sequence!==sharedTradeSequence)return;
  $('sharedTradeCustomers').innerHTML=data.customers.length?data.customers.map(customer=>'<button class="credit-customer" data-shared-customer="'+escapeHTML(customer.id)+'"><span><strong>'+escapeHTML(customer.display_name)+'</strong><small>'+escapeHTML(customer.contact||customer.square_customer_id||'No contact supplied')+'</small></span><b>'+usd(customer.available_cents/100)+'</b></button>').join(''):'<p>No matching shared customers. Add a customer in Shared credit first.</p>';
 }catch(error){if(sharedTradeContext===context)$('sharedTradeMessage').textContent=error.message}
}
window.openSharedTrade=async()=>{
 if(!state.receipt||state.busy)return;
 try{if(!window.sharedTradeLocked(state.receipt))await saveReceipt()}catch(error){return toast(error.message,true)}
 sharedTradeContext={receiptId:state.receipt.id,busy:false,sessionId:'',role:'',data:null,selected:null};++sharedTradeSequence;
 $('sharedTradeMessage').textContent='Checking the signed-in shared store…';
 for(const id of ['sharedTradeCustomerPicker','sharedTradeOffer','sharedTradeConsent','sharedTradeSignIn','refreshSharedTrade','cancelSharedTradeDraft','acceptSharedTrade','sharedTradeSelected'])$(id).hidden=true;
 $('sharedTradeCustomers').replaceChildren();$('sharedTradeSearch').value='';sharedTradeDialog.showModal();
 await sharedTradeRun(async context=>{
  const session=await cloudApi('session');if(sharedTradeContext!==context)return;
  cloudSession=session;cloudHealthy=session.signedIn;cloudRenderSession();
  if(!session.signedIn){$('sharedTradeMessage').textContent='Sign in to your shared store first, then reopen this saved local trade.';$('sharedTradeSignIn').hidden=false;return}
  context.sessionId=session.sessionId;context.role=session.role;
  if(state.receipt.shared_trade&&!['cancelled'].includes(state.receipt.shared_trade.state)){sharedTradeAdopt(await cloudApi('trade-status',{receiptId:context.receiptId,sessionId:context.sessionId}));return}
  $('sharedTradeCustomerPicker').hidden=false;$('sharedTradeMessage').textContent='Shared store: '+session.store+'. Select the exact customer to create a hosted draft.';
 });
 if(sharedTradeContext&&!$('sharedTradeCustomerPicker').hidden)await sharedTradeSearch();
};
$('openSharedTrade').onclick=window.openSharedTrade;
$('sharedTradeSignIn').onclick=async()=>{sharedTradeClose();await navigate('cloud')};
$('searchSharedTradeCustomer').onclick=sharedTradeSearch;
$('sharedTradeSearch').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();sharedTradeSearch()}};
$('sharedTradeCustomers').onclick=event=>{const button=event.target.closest('[data-shared-customer]');if(!button)return;sharedTradeRun(async context=>{
 const data=await cloudApi('account',{customerId:button.dataset.sharedCustomer,sessionId:context.sessionId});if(data.customer?.id!==button.dataset.sharedCustomer)throw Error('The customer response changed. Search and select again.');context.selected=data.customer;
 $('sharedTradeSelected').hidden=false;$('sharedTradeSelectedName').textContent=data.customer.display_name;$('sharedTradeSelectedDetails').textContent=(data.customer.contact||data.customer.square_customer_id||'No contact supplied')+' · Current available '+usd(data.customer.available_cents/100);
})};
$('saveSharedTradeDraft').onclick=()=>sharedTradeRun(async context=>{if(!context.selected)throw Error('Select a shared customer first.');sharedTradeAdopt(await cloudApi('trade-prepare',{receiptId:context.receiptId,revision:state.receipt.revision,customerId:context.selected.id,sessionId:context.sessionId}))});
$('refreshSharedTrade').onclick=()=>sharedTradeRun(async context=>sharedTradeAdopt(await cloudApi('trade-status',{receiptId:context.receiptId,sessionId:context.sessionId})));
$('acceptSharedTrade').onclick=()=>sharedTradeRun(async context=>{if(!context.data?.confirmationHash||!$('sharedTradeAgreed').checked||!$('sharedTradeReceived').checked)throw Error('Review the hosted offer and both confirmations.');sharedTradeAdopt(await cloudApi('trade-accept',{receiptId:context.receiptId,confirmationHash:context.data.confirmationHash,customerAgreed:true,itemsReceived:true,sessionId:context.sessionId}))});
$('cancelSharedTradeDraft').onclick=async()=>{if(!await confirmAction('Cancel the hosted trade draft?','The app will check the saved action and explicitly void the server draft. If credit was already accepted, it will recover that receipt instead of cancelling it.','Check & cancel draft'))return;await sharedTradeRun(async context=>sharedTradeAdopt(await cloudApi('trade-cancel',{receiptId:context.receiptId,sessionId:context.sessionId})))};
document.addEventListener('click',async event=>{const button=event.target.closest('[data-open-shared-trade]');if(!button)return;try{await openReceipt(button.dataset.openSharedTrade);await window.openSharedTrade()}catch(error){toast(error.message,true)}});
