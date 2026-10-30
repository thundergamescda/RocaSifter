'use strict';
// Central physical references precede customer selection and new trade acceptance.
const intakeClaimStyle=document.createElement('link');intakeClaimStyle.rel='stylesheet';intakeClaimStyle.href='/intake-claim.css';document.head.append(intakeClaimStyle);
const intakeClaimSection=document.createElement('section');intakeClaimSection.id='intakeClaimStep';intakeClaimSection.className='intake-claim-step';
intakeClaimSection.innerHTML=`<h3>Identify this physical intake.</h3><p>Use the same physical tag/reference on every store computer for these actual cards. A file hash or local batch number does not prove another physical delivery.</p><p id="intakeClaimFacts" role="status"></p><div id="intakeClaimInputs"><label>Physical intake tag / reference<input id="intakePhysicalReference" maxlength="100" placeholder="For example INTAKE-001"></label><label class="claim-attestation"><input id="intakePhysicalVerified" type="checkbox">I verified this reference identifies the same physical cards at this store.</label><button id="createIntakeClaim" class="button secondary">Create / recover central reference</button><label>Existing central intake ID<input id="intakeExistingId" maxlength="36" placeholder="Open the reference from another computer"></label><button id="selectIntakeClaim" class="button secondary">Use existing intake reference</button></div><button id="checkIntakeClaim" class="button secondary" hidden>Recover saved intake reference</button><details id="intakeAdditionalSection" hidden><summary>A genuinely separate physical delivery</summary><p>This does not retry the original cards. A manager must verify a different physical delivery with the same source, provide a different physical tag and explain why. The server requires enrolled MFA verified within 15 minutes.</p><label>Different physical tag<input id="intakeAdditionalTag" maxlength="100"></label><label>Reason for separate delivery<input id="intakeAdditionalReason" minlength="10" maxlength="200"></label><label class="claim-attestation"><input id="intakeAdditionalVerified" type="checkbox">These are genuinely different physical copies, not excluded cards or a retry.</label><button id="createAdditionalIntake" class="button secondary">Create linked delivery reference</button></details><p class="hint">One accepted operation consumes the reference. Only included copies are accepted. Excluded copies remain NOT RECEIVED and cannot be reused in a continuation or called a new delivery.</p>`;
$('sharedTradeCustomerPicker').before(intakeClaimSection);
const intakeMfaHelp=document.createElement('p');intakeMfaHelp.id='intakeMfaHelp';intakeMfaHelp.hidden=true;intakeMfaHelp.textContent='Enroll MFA in the hosted security setup if needed, then sign out and sign in HERE to supply fresh verification. Keep this saved batch and its pending actions; no automatic sign-out is performed.';
const intakeMfaSetup=document.createElement('a');intakeMfaSetup.id='intakeMfaSetup';intakeMfaSetup.className='button secondary';intakeMfaSetup.href='https://stock-and-sleeve-api.onrender.com/';intakeMfaSetup.target='_blank';intakeMfaSetup.rel='noopener noreferrer';intakeMfaSetup.textContent='Open hosted security setup';intakeMfaSetup.hidden=true;
intakeClaimSection.append(intakeMfaHelp,intakeMfaSetup);
const claimReview=document.createElement('label');claimReview.innerHTML='<input id="sharedTradeClaimReviewed" type="checkbox">I reviewed the central intake reference, selected/all copies and excluded-copy limitation.';
$('sharedTradeConsent').prepend(claimReview);
window.intakeClaimLocked=receipt=>!!(receipt?.intake_claim&&receipt.intake_claim.state!=='confirmed');
window.intakeClaimPendingMarkup=row=>row.action==='intake_claim'?'<article class="cloud-pending-action"><p>Saved physical intake · '+escapeHTML(row.receiptId)+'</p><button class="button secondary small" data-open-shared-trade="'+escapeHTML(row.receiptId)+'">Open intake & recover</button></article>':'';
const priorSharedPendingMarkup=window.sharedTradePendingMarkup;
window.sharedTradePendingMarkup=row=>window.intakeClaimPendingMarkup(row)||priorSharedPendingMarkup?.(row)||'';
function intakeClaimCounts(receipt){
 const original=receipt.source_manifest?.rows||[],all=original.reduce((n,row)=>n+row.quantity,0),selected=receipt.cards.filter(row=>row.include).reduce((n,row)=>n+row.qty,0);
 return selected>all?`Selected ${selected} / original ${all} copies. Selected quantity exceeds the original; review it before accepting.`:`Selected ${selected} / original ${all} copies · ${all-selected} excluded copies remain NOT RECEIVED.`;
}
window.renderIntakeClaim=(receipt,claim=null)=>{
 const context=sharedTradeContext;if(!context)return;
 const intent=receipt.intake_claim,legacyFinal=receipt.shared_trade?.state==='confirmed'&&!receipt.shared_trade.claim_binding_version;
 intakeClaimSection.hidden=legacyFinal;if(legacyFinal)return;
 const supported=context.capabilities?.physical_intake_claims===1&&context.capabilities?.trade_claim_required===true;
 const ready=intent?.state==='confirmed',pending=!!intent&&!ready,valid=supported&&!!receipt.source_manifest&&!receipt.source_manifest_error,manager=['owner','manager'].includes(context.role);
 $('intakeClaimFacts').textContent=!supported?'The hosted service must be upgraded for central physical intake before a new trade can be accepted.':!valid?(receipt.source_manifest_error||'This older batch has no original intake manifest. Reimport its verified original CSV or have the owner review a new manual intake. Current edited cards cannot become an original source.'):ready?`Central tag ${claim?.physical_reference||intent.physical_reference} · ${intent.physical_intake_id} · ${claim?.state||intent.server_state}. ${intakeClaimCounts(receipt)}`:pending?`Saved tag ${intent.requested_reference||intent.selected_id} needs confirmation. ${intakeClaimCounts(receipt)}`:intakeClaimCounts(receipt);
 $('intakeClaimInputs').hidden=ready||!valid;
 $('intakePhysicalReference').disabled=pending||!manager;
 $('intakePhysicalVerified').disabled=ready||!manager;
 $('createIntakeClaim').disabled=pending||!valid||!manager;
 $('selectIntakeClaim').disabled=!valid||!manager||!!intent?.additional_delivery_of;
 $('intakeExistingId').disabled=!manager||!!intent?.additional_delivery_of;
 $('checkIntakeClaim').hidden=!pending;
 $('intakeAdditionalSection').hidden=!ready||!!receipt.purchase||!!(receipt.shared_trade&&!['cancelled'].includes(receipt.shared_trade.state));
 $('sharedTradeCustomerPicker').hidden=!ready||!!context.data?.trade;
 context.claimReady=ready;
};
window.openIntakeClaim=async context=>{
 $('intakeMfaHelp').hidden=$('intakeMfaSetup').hidden=true;
 $('intakePhysicalReference').value='';$('intakeExistingId').value='';$('intakePhysicalVerified').checked=false;
 $('intakeAdditionalTag').value=$('intakeAdditionalReason').value='';$('intakeAdditionalVerified').checked=false;$('intakeAdditionalSection').open=false;
 if(context.capabilities?.physical_intake_claims!==1||context.capabilities?.trade_claim_required!==true){
  intakeClaimSection.hidden=false;$('intakeClaimInputs').hidden=true;$('checkIntakeClaim').hidden=true;$('intakeAdditionalSection').hidden=true;
  $('intakeClaimFacts').textContent='The hosted service must be upgraded for central physical intake before a new trade can be accepted. The saved local batch is safe.';context.claimReady=false;return false;
 }
 if(state.receipt.intake_claim){const result=await cloudApi('intake-status',{receiptId:context.receiptId,sessionId:context.sessionId});intakeClaimAdopt(result);return context.claimReady}
 window.renderIntakeClaim(state.receipt);return false;
};
function intakeClaimAdopt(data){
 const context=sharedTradeContext;if(!context||data.receipt?.id!==context.receiptId)return;
 state.receipt=data.receipt;state.dirty=false;renderReceipt();window.renderIntakeClaim(data.receipt,data.claim);
 if(context.claimReady){$('sharedTradeMessage').textContent='Central physical intake confirmed. Select the exact shared customer, then review the hosted offer.';context.intakeNeedsSearch=true}
}
window.intakeClaimError=error=>{
 if(error.physicalIntakeId)$('intakeExistingId').value=error.physicalIntakeId;
 if(error.code==='MFA_STEP_UP_REQUIRED'){$('sharedTradeSignIn').hidden=false;$('sharedTradeSignIn').textContent='Open shared sign-in / MFA';$('intakeMfaHelp').hidden=$('intakeMfaSetup').hidden=false;}
 if(sharedTradeContext&&state.receipt)window.renderIntakeClaim(state.receipt);
};
$('createIntakeClaim').onclick=()=>sharedTradeRun(async context=>intakeClaimAdopt(await cloudApi('intake-prepare',{receiptId:context.receiptId,revision:state.receipt.revision,physicalReference:$('intakePhysicalReference').value,physicalDeliveryVerified:$('intakePhysicalVerified').checked,sessionId:context.sessionId})));
$('selectIntakeClaim').onclick=()=>sharedTradeRun(async context=>intakeClaimAdopt(await cloudApi('intake-select',{receiptId:context.receiptId,revision:state.receipt.revision,physicalIntakeId:$('intakeExistingId').value,physicalDeliveryVerified:$('intakePhysicalVerified').checked,sessionId:context.sessionId})));
$('checkIntakeClaim').onclick=()=>sharedTradeRun(async context=>intakeClaimAdopt(await cloudApi('intake-status',{receiptId:context.receiptId,sessionId:context.sessionId})));
$('createAdditionalIntake').onclick=()=>sharedTradeRun(async context=>intakeClaimAdopt(await cloudApi('intake-additional',{receiptId:context.receiptId,revision:state.receipt.revision,physicalReference:$('intakeAdditionalTag').value,additionalDeliveryReason:$('intakeAdditionalReason').value,physicalDeliveryVerified:$('intakeAdditionalVerified').checked,additionalDeliveryConfirmed:$('intakeAdditionalVerified').checked,sessionId:context.sessionId})));
$('sharedTradeClaimReviewed').onchange=sharedTradeConsent;
