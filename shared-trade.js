'use strict';
// Shared issuance has its own explicitly selected customer and durable receipt.
const sharedTradeStyle=document.createElement('link');sharedTradeStyle.rel='stylesheet';sharedTradeStyle.href='/shared-trade.css';document.head.append(sharedTradeStyle);
const sharedTradePanel=document.createElement('section');sharedTradePanel.id='sharedTradePanel';sharedTradePanel.className='shared-trade-panel';sharedTradePanel.hidden=true;
sharedTradePanel.innerHTML='<div><span class="section-kicker">HOSTED TRADE AGREEMENT</span><h3 id="sharedTradeTitle">Review the saved cash and credit offers.</h3><p id="sharedTradeHint"></p></div><button class="button secondary" id="openSharedTrade">Choose shared customer</button>';
$('acceptedBuy').after(sharedTradePanel);
const sharedTradeDialog=document.createElement('dialog');sharedTradeDialog.id='sharedTradeDialog';sharedTradeDialog.className='modal shared-trade-modal';
sharedTradeDialog.innerHTML=`<div class="modal-heading"><div><span class="section-kicker">HOSTED TRADE RECEIPT</span><h2>Review the saved offers.</h2></div><button class="icon-button" id="closeSharedTrade" aria-label="Close shared trade">${icon('x')}</button></div><p id="sharedTradeMessage" role="status"></p><div id="sharedTradeCustomerPicker" hidden><p>Choose the customer in your signed-in shared store for either cash or credit. The local reference does not select this account.</p><label>Find shared customer<input id="sharedTradeSearch" maxlength="200" placeholder="Name, contact or Square customer ID"></label><button class="button secondary small" id="searchSharedTradeCustomer">Search</button><div id="sharedTradeCustomers"></div><section id="sharedTradeSelected" hidden><strong id="sharedTradeSelectedName"></strong><p id="sharedTradeSelectedDetails"></p><button class="button primary" id="saveSharedTradeDraft">Save hosted draft for this customer</button></section></div><section id="sharedTradeOffer" hidden></section><section id="sharedTradeConsent" hidden><label><input id="sharedTradeAgreed" type="checkbox">The customer agrees to the saved hosted credit offer.</label><label><input id="sharedTradeReceived" type="checkbox">The store has physically received the included cards shown above.</label><p>Acceptance issues shared credit once. Square checkout remains disabled; this does not approve tax treatment or add Square stock.</p></section><div class="shared-trade-actions"><button class="button secondary" id="sharedTradeSignIn" hidden>Open shared store sign-in</button><button class="button secondary" id="refreshSharedTrade" hidden>Check saved trade</button><button class="button secondary" id="cancelSharedTradeDraft" hidden>Cancel hosted draft</button><button class="button primary" id="acceptSharedTrade" hidden>Confirm & issue shared credit</button></div>`;
document.body.append(sharedTradeDialog);
let sharedTradeContext=null,sharedTradeSequence=0;
const sharedTradeKinds=['cash','credit'],sharedTradeHash=/^[a-f0-9]{64}$/;
const sharedTradeKindName=kind=>kind==='cash'?'Cash agreement':'Shared credit';
const sharedTradeTerminal=trade=>['accepted_cash','accepted_credit'].includes(trade?.state);
sharedTradeDialog.querySelector('h2').id='sharedTradeHeading';
$('sharedTradeHeading').textContent='Review the saved offers.';
const sharedTradeChoices=document.createElement('fieldset');sharedTradeChoices.id='sharedTradeOfferChoices';sharedTradeChoices.className='shared-trade-choices';sharedTradeChoices.hidden=true;
sharedTradeChoices.innerHTML='<legend>Choose the agreed offer</legend><div id="sharedTradeChoiceCards"></div><p id="sharedTradeChoiceHint"></p>';
$('sharedTradeConsent').before(sharedTradeChoices);
$('sharedTradeAgreed').parentElement.lastChild.remove();
const sharedTradeAgreement=document.createElement('span');sharedTradeAgreement.id='sharedTradeAgreementText';$('sharedTradeAgreed').after(sharedTradeAgreement);
$('sharedTradeConsent').querySelector('p').id='sharedTradeEffect';
const sharedTradePending=document.createElement('p');sharedTradePending.id='sharedTradePendingAgreement';sharedTradePending.className='shared-trade-pending';sharedTradePending.hidden=true;$('sharedTradeOffer').before(sharedTradePending);
const sharedTradePrint=document.createElement('button');sharedTradePrint.id='printSharedTrade';sharedTradePrint.className='button secondary shared-trade-print';sharedTradePrint.hidden=true;
const sharedTradePrintLink=document.createElement('a');sharedTradePrintLink.id='sharedTradePrintLink';sharedTradePrintLink.className='button secondary';sharedTradePrintLink.target='_blank';sharedTradePrintLink.rel='noopener noreferrer';sharedTradePrintLink.hidden=true;
$('acceptSharedTrade').before(sharedTradePrint,sharedTradePrintLink);
function sharedTradeCurrent(context){return sharedTradeContext===context&&context.authRevision===cloudRevision&&state.receipt?.id===context.receiptId&&(!context.sessionId||cloudSession.sessionId===context.sessionId)}
window.clearSharedTradeForAuthChange=()=>{
 window.clearTradeRecoveryForAuthChange?.();
 if(!sharedTradeContext)return;sharedTradeContext=null;++sharedTradeSequence;
 for(const id of ['sharedTradeOffer','sharedTradeCustomers','sharedTradeSelectedName','sharedTradeSelectedDetails','sharedTradeChoiceCards'])$(id).replaceChildren();
 for(const id of ['sharedTradeCustomerPicker','sharedTradeOffer','sharedTradeSelected','sharedTradeOfferChoices','sharedTradeConsent','refreshSharedTrade','cancelSharedTradeDraft','acceptSharedTrade','intakeClaimStep','sharedTradePendingAgreement','printSharedTrade','sharedTradePrintLink'])if($(id))$(id).hidden=true;
 sharedTradePrintLink.removeAttribute('href');$('sharedTradePendingAgreement').textContent='';
 sharedTradeDialog.querySelectorAll('input').forEach(node=>{node.value='';node.checked=false});sharedTradeDialog.querySelectorAll('button').forEach(node=>node.disabled=false);
 if($('intakeClaimFacts'))$('intakeClaimFacts').textContent='';
 $('sharedTradeHeading').textContent='Store sign-in changed.';$('sharedTradeMessage').textContent='Reopen this saved batch after signing in to its store. Any saved action keeps its original agreement.';$('sharedTradeSignIn').hidden=false;
};
function sharedTradeResetAcknowledgements(){for(const id of ['sharedTradeAgreed','sharedTradeReceived','sharedTradeClaimReviewed'])if($(id))$(id).checked=false}
function sharedTradeValidateView(data){
 const version=data.offerContractVersion,offers=data.offers;
 if(![1,2].includes(version)||!offers||Object.keys(offers).sort().join(',')!=='cash,credit'||![null,'cash','credit'].includes(data.lockedOfferType))throw Error('This saved trade needs a compatible offer review. Check its original receipt; no offer was selected.');
 const intent=data.receipt?.shared_trade,intentVersion=intent&&Object.hasOwn(intent,'offer_contract_version')?intent.offer_contract_version:1;
 if(intentVersion!==version)throw Error('The saved offer version does not match its original intent. Recover that receipt before continuing.');
 for(const kind of sharedTradeKinds){const offer=offers[kind];if(!offer||Object.keys(offer).sort().join(',')!=='amountCents,available,confirmationHash,offerType'||offer.offerType!==kind||!Number.isSafeInteger(offer.amountCents)||offer.amountCents<0||typeof offer.available!=='boolean'||(offer.available?offer.amountCents<=0||!sharedTradeHash.test(offer.confirmationHash):offer.confirmationHash!==null))throw Error('The saved offer could not be verified. Refresh the hosted receipt before agreeing.');}
 if(version===1&&(data.lockedOfferType!=='credit'||offers.cash.available||offers.credit.available&&offers.credit.confirmationHash!==data.confirmationHash)||version===2&&data.confirmationHash!==null)throw Error('The saved offer protocol changed. Recover the original agreement before continuing.');
 const editable=data.trade?.state==='draft'&&data.receipt?.shared_trade?.state==='draft';
 if(version===2&&editable&&data.lockedOfferType!==null)throw Error('A new hosted draft cannot select an offer automatically. Refresh its saved offers.');
 if(!editable&&sharedTradeKinds.some(kind=>offers[kind].available))throw Error('A saved action is already in progress. Recover its original agreement.');
 if(sharedTradeTerminal(data.trade)&&data.lockedOfferType!==data.trade.state.slice('accepted_'.length))throw Error('The hosted agreement kind could not be verified. Keep the original receipt for recovery.');
}
function sharedTradeEditable(context){return !!context?.data&&context.data.trade?.state==='draft'&&context.data.receipt?.shared_trade?.state==='draft'}
function sharedTradeSelectedOffer(context){const kind=context?.offerType;return sharedTradeKinds.includes(kind)?context.data?.offers?.[kind]:null}
function sharedTradePaintChoice(){
 const context=sharedTradeContext;if(!context?.data)return;const editable=sharedTradeEditable(context),offer=sharedTradeSelectedOffer(context),kind=context.offerType;
 $('sharedTradeConsent').hidden=!editable||!offer?.available;$('acceptSharedTrade').hidden=!editable;
 $('sharedTradeHeading').textContent=context.data.receipt.shared_trade?.state==='cancelled'?'Hosted draft cancelled.':sharedTradeTerminal(context.data.trade)?(context.data.lockedOfferType==='cash'?'Cash agreement recorded.':'Shared credit issued.'):!editable?'Recover the saved agreement.':kind==='cash'?'Record the cash agreement.':kind==='credit'?'Issue shared credit.':'Choose the agreed offer.';
 $('acceptSharedTrade').textContent=offer?.available?(kind==='cash'?'Record cash agreement — ':'Issue shared credit — ')+usd(offer.amountCents/100):'Choose an offer';
 $('sharedTradeAgreementText').textContent=offer?'The customer agrees to the saved '+(kind==='cash'?'cash':'shared-credit')+' offer of '+usd(offer.amountCents/100)+'.':'';
 $('sharedTradeEffect').textContent=kind==='cash'?'This records the agreed cash amount only. Stock & Sleeve does not send money or record that the customer was paid. Complete payment through your store’s approved process.':'Adds credit once to this shared customer account. Square checkout remains disabled; credit tax policy remains unreviewed. Acceptance does not add Square stock.';
 $('sharedTradePendingAgreement').hidden=editable||sharedTradeTerminal(context.data.trade)||context.data.receipt.shared_trade?.state==='cancelled';
 $('sharedTradePendingAgreement').textContent=offer?'Saved '+sharedTradeKindName(kind).toLowerCase()+' · '+usd(offer.amountCents/100)+'. Check the original action before doing anything else.':'Check the original saved agreement. No new offer can be selected while it is unresolved.';
 sharedTradeDialog.querySelectorAll('[name="sharedTradeOfferType"]').forEach(node=>node.checked=node.value===kind);sharedTradeConsent();
}
$('sharedTradeOfferChoices').onchange=event=>{const context=sharedTradeContext,kind=event.target.value;if(!context||!sharedTradeCurrent(context)||context.busy||!sharedTradeEditable(context)||!sharedTradeKinds.includes(kind)||!context.data.offers[kind].available)return;context.offerType=kind;sharedTradeResetAcknowledgements();sharedTradePaintChoice()};
window.sharedTradePendingMarkup=row=>row.action?.startsWith('trade_')?'<article class="cloud-pending-action"><p>Saved shared trade · '+escapeHTML(row.receiptId)+'</p><button class="button secondary small" data-open-shared-trade="'+escapeHTML(row.receiptId)+'">Open local trade & recover</button></article>':'';
window.sharedTradeLocked=receipt=>!!(receipt?.shared_trade&&!['confirmed','cancelled'].includes(receipt.shared_trade.state))||!!window.intakeClaimLocked?.(receipt);
window.renderSharedTrade=()=>{
 const receipt=state.receipt,intent=receipt?.shared_trade;
 sharedTradePanel.hidden=!receipt||(state.mode!=='trade'&&intent?.state!=='confirmed')||(!!receipt.purchase&&receipt.purchase.scope!=='hosted')||!!(receipt.hosted_receive&&!intent&&!['cancelled_before_write','retired_before_creation'].includes(receipt.hosted_receive.state));
 if(sharedTradePanel.hidden)return;
 const pending=window.sharedTradeLocked(receipt),confirmed=intent?.state==='confirmed';
 const cash=receipt.purchase?.scope==='hosted'&&receipt.purchase.type==='cash';
 $('sharedTradeTitle').textContent=confirmed?(cash?'Cash agreement recorded.':'Shared credit issued.'):pending?'This batch has a saved hosted trade.':'Review the saved cash and credit offers.';
 $('sharedTradeHint').textContent=intent?.message||'Select a shared customer, review the hosted store offer, then have an owner or manager confirm. Cash payment is handled separately; shared credit uses this store account.';
 $('openSharedTrade').textContent=confirmed?(cash?'View cash agreement':'View credit receipt'):pending?'Open / recover hosted trade':'Choose shared customer';
 $('openSharedTrade').disabled=state.busy||(!intent&&(!receipt.cards.some(card=>card.include)||receipt.cards.some(card=>card.include&&(!card.reviewed||card.price_missing))));
};
function sharedTradeClose(){if(sharedTradeContext?.busy)return;sharedTradeContext=null;++sharedTradeSequence;sharedTradeDialog.close()}
$('closeSharedTrade').onclick=sharedTradeClose;sharedTradeDialog.addEventListener('cancel',event=>{event.preventDefault();sharedTradeClose()});
function sharedTradeConsent(){const context=sharedTradeContext,offer=sharedTradeSelectedOffer(context),editable=sharedTradeEditable(context);$('acceptSharedTrade').disabled=!context||!sharedTradeCurrent(context)||context.busy||!editable||!offer?.available||!['owner','manager'].includes(context.role)||!$('sharedTradeAgreed').checked||!$('sharedTradeReceived').checked||!!(context.data?.receipt?.shared_trade?.claim_binding_version===1&&!$('sharedTradeClaimReviewed')?.checked);sharedTradeDialog.querySelectorAll('[name="sharedTradeOfferType"]').forEach(node=>node.disabled=!!context?.busy||!editable||!context.data.offers[node.value].available)}
$('sharedTradeAgreed').onchange=$('sharedTradeReceived').onchange=sharedTradeConsent;
function sharedTradeAdopt(data){
 const context=sharedTradeContext;if(!context||!sharedTradeCurrent(context))return;sharedTradeValidateView(data);
 if(data.receipt?.id!==context.receiptId)throw Error('The hosted response belongs to another batch. Reopen this saved trade.');
 context.data=data;context.offerType=data.lockedOfferType;sharedTradeResetAcknowledgements();
 state.receipt=data.receipt;state.dirty=false;renderReceipt();window.renderIntakeClaim?.(data.receipt,data.claim);
 const trade=data.trade,accepted=sharedTradeTerminal(trade),editable=sharedTradeEditable(context),cash=data.lockedOfferType==='cash',customer=data.customer||trade?.accepted_receipt?.customer||{};
 $('sharedTradeCustomerPicker').hidden=true;$('sharedTradeOffer').hidden=!trade;$('refreshSharedTrade').hidden=false;
 $('refreshSharedTrade').textContent=accepted?'Refresh original record':'Check saved trade';
 $('cancelSharedTradeDraft').hidden=!window.sharedTradeLocked(data.receipt)||accepted;$('sharedTradeOfferChoices').hidden=!editable;
 sharedTradePrint.hidden=!accepted;sharedTradePrint.textContent=cash?'Prepare cash agreement PDF':'Prepare credit receipt PDF';
 const buy=data.receipt.files?.buy;sharedTradePrintLink.hidden=!accepted||typeof buy!=='string'||!buy;sharedTradePrintLink.textContent=cash?'Open cash agreement PDF':'Open credit receipt PDF';if(!sharedTradePrintLink.hidden)sharedTradePrintLink.href='/files/'+encodeURIComponent(buy);else sharedTradePrintLink.removeAttribute('href');
 $('sharedTradeChoiceCards').innerHTML=sharedTradeKinds.map(kind=>{const offer=data.offers[kind];return '<label class="shared-trade-choice"><input type="radio" name="sharedTradeOfferType" value="'+kind+'"><span><strong>'+sharedTradeKindName(kind)+'</strong><b>'+usd(offer.amountCents/100)+'</b><small>'+(offer.available?(kind==='cash'?'Agreement only · payment handled separately':'Adds credit to this shared account'):(data.offerContractVersion===1&&kind==='cash'?'Original credit-only draft':offer.amountCents<=0?'A positive saved offer is required':'Saved action is locked'))+'</small></span></label>'}).join('');
 $('sharedTradeChoiceHint').textContent=data.offerContractVersion===1?'This is an original credit-only draft. To choose cash, first confirm cancellation, then explicitly start a new hosted draft.':'Choose one saved offer. Neither is selected automatically. These amounts may differ from the local estimates.';
 if(trade){
  const description=accepted?(cash?'Cash agreement recorded':'Shared credit issued'):editable?'Original intake reference':data.lockedOfferType?sharedTradeKindName(data.lockedOfferType)+' · saved action':'Saved hosted draft';
  const shownCents=accepted?trade.accepted_amount_cents:!editable&&data.lockedOfferType?data.offers[data.lockedOfferType].amountCents:trade.intake_total_cents;
  $('sharedTradeOffer').innerHTML='<div class="shared-trade-facts"><div><span>Hosted customer</span><strong>'+escapeHTML(customer.display_name||data.receipt.shared_trade.customer_id)+'</strong><small>'+escapeHTML(data.receipt.shared_trade.store)+'</small></div><div><span>'+escapeHTML(description)+'</span><strong>'+usd(shownCents/100)+'</strong><small>'+(accepted?'Agreement recorded '+escapeHTML(new Date(trade.accepted_receipt.accepted_at).toLocaleString()):'Included cards are listed below.')+'</small></div></div><div class="shared-trade-lines"><table><thead><tr><th>Included item / identity</th><th>Copies</th><th>Intake each</th></tr></thead><tbody>'+trade.lines.filter(line=>line.included).map(line=>'<tr><td><strong>'+escapeHTML(line.identity.name)+'</strong><small>'+escapeHTML([line.identity.game_label||intakeGames[line.identity.game]||line.identity.game,line.identity.set,line.identity.number,line.identity.condition,line.identity.printing,line.identity.language,line.identity.grading_company,line.identity.grade,line.identity.grade_qualifier,line.identity.certification,line.identity.sport,line.identity.league,line.identity.year,line.identity.manufacturer,line.identity.player,line.identity.team,line.identity.serial_number].filter(Boolean).join(' · '))+'</small></td><td>'+Number(line.quantity)+'</td><td>'+usd(line.unit_intake_cents/100)+'</td></tr>').join('')+'</tbody></table></div>'+(accepted?'<div class="shared-trade-confirmed"><strong>'+description+'.</strong><p>'+(cash?'Payment execution is not recorded by Stock & Sleeve. No shared credit was added.':'No local wallet entry was created. Square checkout is disabled and credit tax policy remains unreviewed.')+'</p><p>Stock receiving is a separate reviewed action.</p></div>':'<p class="hint">Review the saved customer, exact cards and selected offer before agreeing. Intake reference values are separate from later retail prices.</p>')+'<details class="shared-trade-technical"><summary>Technical references</summary><p>Hosted receipt '+escapeHTML(trade.id)+' · version '+Number(trade.version)+'</p>'+(trade.receipt_hash?'<p class="shared-trade-hash">Receipt hash '+escapeHTML(trade.receipt_hash)+'</p>':'')+'</details>';
 }
 $('sharedTradeMessage').textContent=data.receipt.shared_trade?.message||'Review the saved hosted receipt.';
 if(editable&&!['owner','manager'].includes(context.role))$('sharedTradeMessage').textContent='Draft saved. An owner or manager must sign in to this store to confirm the agreed offer.';
 sharedTradePaintChoice();
}
async function sharedTradeRun(work){
 const context=sharedTradeContext;if(!context||context.busy||!sharedTradeCurrent(context))return;context.busy=true;sharedTradeDialog.querySelectorAll('button,input').forEach(node=>node.disabled=true);
 try{await work(context)}catch(error){
  if(!sharedTradeCurrent(context)||error.status===401||error.sessionChanged){window.clearSharedTradeForAuthChange();return}
  $('sharedTradeMessage').textContent=error.message+' The saved batch remains available. Use Check saved trade to recover its original action.';window.intakeClaimError?.(error);window.offerOriginalTradeRecovery?.(error,context);
  try{const data=await api('/api/receipts/'+context.receiptId);if(!sharedTradeCurrent(context))return;state.receipt=data.receipt;state.dirty=false;renderReceipt();if(data.receipt.shared_trade){$('refreshSharedTrade').hidden=false;$('cancelSharedTradeDraft').hidden=!window.sharedTradeLocked(data.receipt);if(context.data){context.data.receipt=data.receipt;context.offerType=data.receipt.shared_trade.selected_offer||(context.data.offerContractVersion===1?'credit':null);sharedTradeResetAcknowledgements();sharedTradePaintChoice();$('sharedTradeOfferChoices').hidden=!sharedTradeEditable(context)}}}catch{}
 }finally{if(sharedTradeCurrent(context)){context.busy=false;sharedTradeDialog.querySelectorAll('button,input').forEach(node=>node.disabled=false);window.renderIntakeClaim?.(state.receipt,context.data?.claim);sharedTradeConsent();if(context.intakeNeedsSearch){context.intakeNeedsSearch=false;await sharedTradeSearch()}await refreshReceipts().catch(()=>{})}}
}
async function sharedTradeSearch(){
 const context=sharedTradeContext;if(!context||context.busy||!sharedTradeCurrent(context))return;const sequence=++sharedTradeSequence;
 try{const data=await cloudApi('search',{query:$('sharedTradeSearch').value,sessionId:context.sessionId});if(!sharedTradeCurrent(context)||sequence!==sharedTradeSequence)return;
  $('sharedTradeCustomers').innerHTML=data.customers.length?data.customers.map(customer=>'<button class="credit-customer" data-shared-customer="'+escapeHTML(customer.id)+'"><span><strong>'+escapeHTML(customer.display_name)+'</strong><small>'+escapeHTML(customer.contact||customer.square_customer_id||'No contact supplied')+'</small></span><b>'+usd(customer.available_cents/100)+'</b></button>').join(''):'<p>No matching shared customers. Add a customer in Shared credit first.</p>';
 }catch(error){if(sharedTradeCurrent(context))$('sharedTradeMessage').textContent=error.message}
}
window.openSharedTrade=async()=>{
 if(!state.receipt||state.busy)return;
 window.clearDiscoveredTradeRecovery?.();
 try{if(!window.sharedTradeLocked(state.receipt))await saveReceipt()}catch(error){return toast(error.message,true)}
 sharedTradeContext={receiptId:state.receipt.id,busy:false,sessionId:'',role:'',data:null,selected:null,offerType:null,authRevision:cloudRevision};++sharedTradeSequence;
 $('sharedTradeMessage').textContent='Checking the signed-in shared store…';
 for(const id of ['sharedTradeCustomerPicker','sharedTradeOffer','sharedTradeOfferChoices','sharedTradeConsent','sharedTradeSignIn','refreshSharedTrade','cancelSharedTradeDraft','acceptSharedTrade','sharedTradeSelected','sharedTradePendingAgreement','printSharedTrade','sharedTradePrintLink'])$(id).hidden=true;
 $('sharedTradeHeading').textContent='Review the saved offers.';$('sharedTradeOffer').replaceChildren();$('sharedTradeChoiceCards').replaceChildren();$('sharedTradeCustomers').replaceChildren();$('sharedTradeSelectedName').textContent=$('sharedTradeSelectedDetails').textContent='';$('sharedTradeSearch').value='';sharedTradeResetAcknowledgements();sharedTradeDialog.showModal();
 await sharedTradeRun(async context=>{
  const session=await cloudApi('session');if(!sharedTradeCurrent(context))return;
  cloudSession=session;cloudHealthy=session.signedIn;cloudRenderSession();
  if(!session.signedIn){$('sharedTradeMessage').textContent='Sign in to your shared store first, then reopen this saved local trade.';$('sharedTradeSignIn').hidden=false;return}
  context.sessionId=session.sessionId;context.role=session.role;context.capabilities=session.capabilities;
  if(state.receipt.shared_trade&&!['cancelled'].includes(state.receipt.shared_trade.state)){sharedTradeAdopt(await cloudApi(sharedTradeReadPath(state.receipt),{receiptId:context.receiptId,sessionId:context.sessionId}));return}
  if(window.openIntakeClaim&&!await window.openIntakeClaim(context))return;
  $('sharedTradeCustomerPicker').hidden=false;$('sharedTradeMessage').textContent='Shared store: '+session.store+'. Select the exact customer to create a hosted draft.';
 });
 if(sharedTradeContext&&!$('sharedTradeCustomerPicker').hidden)await sharedTradeSearch();
};
$('openSharedTrade').onclick=window.openSharedTrade;
$('sharedTradeSignIn').onclick=async()=>{sharedTradeClose();await navigate('cloud')};
$('searchSharedTradeCustomer').onclick=sharedTradeSearch;
$('sharedTradeSearch').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();sharedTradeSearch()}};
$('sharedTradeCustomers').onclick=event=>{const button=event.target.closest('[data-shared-customer]');if(!button)return;sharedTradeRun(async context=>{
 const data=await cloudApi('account',{customerId:button.dataset.sharedCustomer,sessionId:context.sessionId});if(!sharedTradeCurrent(context))return;if(data.customer?.id!==button.dataset.sharedCustomer)throw Error('The customer response changed. Search and select again.');context.selected=data.customer;
 $('sharedTradeSelected').hidden=false;$('sharedTradeSelectedName').textContent=data.customer.display_name;$('sharedTradeSelectedDetails').textContent=(data.customer.contact||data.customer.square_customer_id||'No contact supplied')+' · Current available '+usd(data.customer.available_cents/100);
})};
$('saveSharedTradeDraft').onclick=()=>sharedTradeRun(async context=>{if(!context.selected)throw Error('Select a shared customer first.');sharedTradeAdopt(await cloudApi('trade-prepare',{receiptId:context.receiptId,revision:state.receipt.revision,customerId:context.selected.id,sessionId:context.sessionId}))});
function sharedTradeReadPath(receipt){return receipt.shared_trade?.state==='confirmed'?'trade-record-view':'trade-status'}
$('refreshSharedTrade').onclick=()=>sharedTradeRun(async context=>sharedTradeAdopt(await cloudApi(sharedTradeReadPath(state.receipt),{receiptId:context.receiptId,sessionId:context.sessionId})));
sharedTradePrint.onclick=()=>sharedTradeRun(async context=>{if(context.data?.receipt?.shared_trade?.state!=='confirmed'||!sharedTradeTerminal(context.data.trade))throw Error('Only a confirmed hosted agreement can be printed.');sharedTradeAdopt(await cloudApi('trade-print',{receiptId:context.receiptId,sessionId:context.sessionId}));if(sharedTradeCurrent(context))$('sharedTradeMessage').textContent='PDF ready. Open the saved agreement below to print. No financial action was repeated.'});
$('acceptSharedTrade').onclick=()=>sharedTradeRun(async context=>{const offer=sharedTradeSelectedOffer(context);if(!sharedTradeEditable(context)||!offer?.available||!sharedTradeHash.test(offer.confirmationHash)||!['owner','manager'].includes(context.role)||!$('sharedTradeAgreed').checked||!$('sharedTradeReceived').checked||!$('sharedTradeClaimReviewed')?.checked)throw Error('Choose an available saved offer and review every confirmation.');sharedTradeAdopt(await cloudApi('trade-accept',{receiptId:context.receiptId,offerType:context.offerType,confirmationHash:offer.confirmationHash,customerAgreed:true,itemsReceived:true,claimReviewed:true,sessionId:context.sessionId}))});
$('cancelSharedTradeDraft').onclick=async()=>{if(!await confirmAction('Cancel the hosted trade draft?','The app will check the saved action and explicitly void the server draft. If a cash or credit agreement was already accepted, it will recover that receipt instead of cancelling it.','Check & cancel draft'))return;await sharedTradeRun(async context=>sharedTradeAdopt(await cloudApi('trade-cancel',{receiptId:context.receiptId,sessionId:context.sessionId})))};
document.addEventListener('click',async event=>{const button=event.target.closest('[data-open-shared-trade]');if(!button)return;try{await openReceipt(button.dataset.openSharedTrade);await window.openSharedTrade()}catch(error){toast(error.message,true)}});
