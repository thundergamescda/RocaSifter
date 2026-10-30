'use strict';
// Per-card identity survives mixed-game exports and manual slab entry.
const intakeGames={pokemon:'Pokémon',magic:'Magic: The Gathering',riftbound:'Riftbound',onepiece:'One Piece',unionarena:'Union Arena',gundam:'Gundam',sports:'Sports'};
const intakeMetadata={grader:'Grading company',grade:'Grade',grade_qualifier:'Grade qualifier',cert_number:'Certification number',serial_number:'Serial number',year:'Year',manufacturer:'Manufacturer / brand',team:'Team',player:'Player'};
const intakeEditMap={editGame:'game'};
function intakeFieldId(prefix,key){return prefix+key.split('_').map(word=>word[0].toUpperCase()+word.slice(1)).join('')}
function intakeAddFields(prefix,container){
 const group=document.createElement('details');group.className='wide intake-identity';
 group.innerHTML='<summary>Slab, serial and sports details</summary><p class="hint">Keep certification and serial numbers exactly as printed, including leading zeros. These identify a specific item.</p><div class="form-grid">'+Object.entries(intakeMetadata).map(([key,label])=>`<label>${label}<input id="${intakeFieldId(prefix,key)}" maxlength="500"></label>`).join('')+'</div>';
 container.append(group);
 if(prefix==='edit')for(const key of Object.keys(intakeMetadata))intakeEditMap[intakeFieldId(prefix,key)]=key;
}
function intakeGameSelect(select){
 select.replaceChildren(...Object.entries(intakeGames).map(([value,label])=>new Option(label,value)),new Option('Other supported game…','other'));
 const label=document.createElement('label');label.className='intake-other-game';label.hidden=true;
 label.innerHTML=`Game name<input id="${select.id}Label" maxlength="100" placeholder="Exact game / product line">`;
 select.closest('label').after(label);
 select.addEventListener('change',()=>{label.hidden=select.value!=='other';$(select.id+'Label').required=select.value==='other'});
}
intakeGameSelect($('manualGame'));
for(const id of ['batchPricingGame','defaultPricingGame','priceResearchGame']){
 const select=$(id);if(!select)continue;const value=select.value;
 select.replaceChildren(...(id==='batchPricingGame'?[new Option('Per-card games / mixed batch','')]:[]),...Object.entries(intakeGames).map(([key,label])=>new Option(label,key)));select.value=value;
}
const editGameLabel=document.createElement('label');editGameLabel.innerHTML='Game<select id="editGame"></select>';
$('cardForm').querySelector('.form-grid').prepend(editGameLabel);intakeGameSelect($('editGame'));
intakeAddFields('manual',$('manualItemForm').querySelector('.form-grid'));intakeAddFields('edit',$('cardForm').querySelector('.form-grid'));
const zeroLabel=document.createElement('label');zeroLabel.className='cloud-check';zeroLabel.id='editZeroConfirmation';zeroLabel.hidden=true;
zeroLabel.innerHTML='<input id="editPriceConfirmed" type="checkbox">I confirm a $0 selling price for this item.';
$('editPrice').closest('label').after(zeroLabel);
function intakeSetGame(select,value,label=''){
 if(value.startsWith('other:')&&$(select.id+'Label')){select.value='other';$(select.id+'Label').value=label||value.slice(6).replaceAll('-',' ')}
 else if(value.startsWith('other:')){if(![...select.options].some(option=>option.value===value))select.add(new Option(label||value.slice(6).replaceAll('-',' '),value));select.value=value}
 else if(Object.hasOwn(intakeGames,value))select.value=value;
 else{if(![...select.options].some(option=>option.value===value))select.add(new Option(label||value,value));select.value=value}
 select.dispatchEvent(new Event('change'));
}
function intakeSelectedGame(id){return $(id).value==='other'?$(id+'Label').value.trim():$(id).value}
function intakeExtraFields(prefix){
 const result=Object.fromEntries(Object.keys(intakeMetadata).map(key=>[key,$(intakeFieldId(prefix,key)).value.trim()]));
 result.game=intakeSelectedGame(prefix==='manual'?'manualGame':'editGame');
 if(result.grader&&result.grade)result.condition=[result.grader,result.grade,result.grade_qualifier].filter(Boolean).join(' ');
 if(prefix==='edit')result.price_confirmed=$('editPriceConfirmed').checked||Number($('editPrice').value)>0;
 return result;
}
function intakeManualOpen(){
 intakeSetGame($('manualGame'),state.receipt?.pricing_game||state.settings.pricingGame||'pokemon',state.receipt?.pricing_game_label||'');
 $('manualGame').disabled=false;
}
function intakePriceState(){
 const value=$('editPrice').value.trim(),zero=value!==''&&Number(value)===0;
 $('editZeroConfirmation').hidden=!zero;
 const c=state.receipt?.cards.find(card=>card.row_id===state.editing);
 const missing=!!c?.price_missing&&(!value||(zero&&!$('editPriceConfirmed').checked));
 $('editReviewed').disabled=priceLocked()||missing;if(missing)$('editReviewed').checked=false;
}
function intakeOpenCard(card){
 intakeSetGame($('editGame'),card.game||state.receipt.pricing_game||'pokemon',card.game_label||'');
 if(card.price_missing)$('editPrice').value='';$('editPriceConfirmed').checked=card.price_confirmed===true;
 $('editPriceConfirmed').disabled=priceLocked();intakePriceState();
}
$('editPrice').addEventListener('input',intakePriceState);$('editPriceConfirmed').addEventListener('change',intakePriceState);
for(const id of ['editGame','editGameLabel',...Object.keys(intakeEditMap).filter(id=>id!=='editGame')])$(id).addEventListener('input',()=>{$('editReviewed').checked=false;$('editSku').value='';$('priceReminder').textContent='Identity changed. Verify the exact game, variant, grade and price before marking checked.'});
const intakeSummary=document.createElement('section');intakeSummary.id='intakeSummary';intakeSummary.className='credit-intro';intakeSummary.hidden=true;
$('receiptWorkspace').prepend(intakeSummary);
function renderIntakeSummary(){
 const receipt=state.receipt,summary=receipt?.intake_summary;intakeSummary.hidden=!summary;
 if(!summary)return;
 const issues=summary.issues||[];const missing=receipt.cards.filter(card=>card.include&&card.price_missing).length;
 intakeSummary.innerHTML='<strong>Intake review</strong><p>'+escapeHTML(receipt.mixed_games?'Mixed-game batch. Each card keeps its own game.':'Cards retain their source identity and item details.')+(missing?' '+missing+' included row(s) need a selling price.':'')+'</p>'+(issues.length?'<details><summary>'+issues.length+' intake note(s)</summary><ul>'+issues.map(issue=>'<li>'+escapeHTML((issue.row?'Row '+issue.row+': ':'')+(issue.message||issue.code||'Review source details'))+'</li>').join('')+'</ul></details>':'');
}
