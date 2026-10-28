'use strict';
// A file becomes a draft only after its mapped preview is explicitly accepted.
const importStyle=document.createElement('link');importStyle.rel='stylesheet';importStyle.href='/import.css';document.head.append(importStyle);
const importDialog=document.createElement('dialog');importDialog.id='importDialog';importDialog.className='modal import-modal';
importDialog.innerHTML=`<div class="modal-heading"><div><span class="section-kicker">CSV / SCANNER INTAKE</span><h2>Review your export.</h2></div><button id="importClose" class="icon-button" type="button" aria-label="Cancel import">${icon('x')}</button></div>
<p id="importFileName" class="muted"></p><p class="import-assurance">This preview does not create a batch or change inventory. Check the columns and quantities before importing an unchecked draft.</p>
<p id="importMessage" role="status" class="inline-message"></p>
<section id="importExisting" class="import-overview" hidden><strong id="importExistingText"></strong><button id="importOpenExisting" type="button" class="button secondary small">Open saved batch</button><p>Only import again for a separate physical delivery of additional copies. You will confirm that choice before another draft is created.</p></section>
<details id="importMapping"><summary>File format and column mapping</summary><p>Use Auto-detect for standard exports. For another scanner, choose the columns that hold each field, then update the preview.</p>
<div class="import-columns-grid"><label>File format<select id="importAdapter"><option value="auto">Auto-detect</option><option value="roca">Roca-style CSV</option><option value="tcgplayer_app">TCGplayer app CSV</option><option value="tcgplayer_inventory">TCGplayer inventory CSV</option><option value="mapped">Mapped columns</option></select></label>
<label>Game for rows without a game<select id="importDefaultGame"><option value="">Use the saved batch default</option>${Object.entries(intakeGames).map(([key,label])=>`<option value="${key}">${escapeHTML(label)}</option>`).join('')}<option value="other">Another game…</option></select></label>
<label id="importOtherGameLabel" hidden>Game name<input id="importOtherGame" maxlength="100" placeholder="Exact game / product line"></label></div>
<p id="importDefaultHint" class="hint"></p><div id="importColumns" class="import-columns-grid"></div>
<details id="importAdvanced"><summary>Slab, sports and source identifiers</summary><div id="importExtraColumns" class="import-columns-grid"></div></details>
<p class="hint">Only map an existing 12-digit Square SKU to Square SKU. TCGplayer and scanner IDs stay separate. Copying a market-reference column as the selling price still creates unchecked rows that need your review.</p>
<button id="importUpdatePreview" type="button" class="button secondary">Update preview</button></details>
<section id="importOverview" class="import-overview" hidden></section>
<section id="importSnapshot" class="import-snapshot" hidden><strong>These are stock snapshot quantities.</strong><p>Total Quantity and Set Stock describe an inventory snapshot, not necessarily new copies. Prefer Add to Quantity for a delivery. This app will not replace Square stock counts.</p><label><input id="importSnapshotAck" type="checkbox">I confirm these quantities represent the physical copies I intend to bring into this draft.</label></section>
<section id="importIssues" class="import-issues" hidden></section>
<section id="importNormalized" hidden><h3>Card preview <small>First 15 valid rows</small></h3><div class="import-table-wrap"><table><thead><tr><th>Source row</th><th>Card / game</th><th>Condition / finish</th><th>Copies</th><th>Selling price</th></tr></thead><tbody id="importRows"></tbody></table></div></section>
<details id="importSource"><summary>See source columns and example values</summary><p class="hint">First five source rows, with long cells shortened for preview.</p><div class="import-table-wrap"><table><thead id="importSourceHead"></thead><tbody id="importSourceRows"></tbody></table></div></details>
<div class="import-actions"><span id="importActionHint">Review the file before importing.</span><div><button id="importCancel" class="button secondary" type="button">Cancel</button><button id="importCommit" class="button primary" type="button" disabled>Import unchecked draft</button></div></div>`;
document.body.append(importDialog);
const importPrimaryFields=new Set(['card_name','game','card_set','card_num','condition','printing','language','quantity','price','market_price','square_sku']);
const importFieldLabels={card_name:'Card name',card_num:'Collector number',card_set:'Set / collection',game:'Game / product line',quantity:'Copies to import',price:'Selling price',market_price:'Market reference',square_sku:'Existing Square SKU'};
const importFormatLabels={roca:'Roca-style CSV',tcgplayer_app:'TCGplayer app CSV',tcgplayer_inventory:'TCGplayer inventory CSV',canonical:'Standard / mapped columns'};
let importContext=null;
function importOptions(){
 const mapping={};importDialog.querySelectorAll('[data-import-field]').forEach(select=>{if(select.value)mapping[select.dataset.importField]=select.value});
 const options={adapter:$('importAdapter').value,column_mapping:mapping};
 const selected=$('importDefaultGame').value;
 if(selected)options.default_game=selected==='other'?$('importOtherGame').value.trim():selected;
 if(selected==='other'&&!options.default_game)throw Error('Enter the exact game name, or use the file’s game column.');
 if(mapping.quantity)options.quantity_column=mapping.quantity;
 if(mapping.price)options.price_column=mapping.price;
 return options;
}
function importButtons(){
 const context=importContext,preview=context?.preview;
 const ready=!!(context&&!context.loading&&!context.committing&&!context.dirty&&preview?.can_import&&(!preview.requires_quantity_confirmation||$('importSnapshotAck').checked));
 $('importCommit').disabled=!ready;$('importUpdatePreview').disabled=!context||context.loading||context.committing;
 for(const id of ['importClose','importCancel'])$(id).disabled=!!context?.committing;
 $('importActionHint').textContent=context?.committing?'Saving an unchecked draft…':context?.loading?'Checking the file…':context?.dirty?'Update the preview to apply these choices.':preview?.error_count?'Correct the mapping or source errors before importing.':preview?.requires_quantity_confirmation&&!$('importSnapshotAck').checked?'Confirm the snapshot quantities before importing.':ready?'Importing creates a draft for card review. No inventory changes.':'Review the file before importing.';
}
function importChanged(){
 if(!importContext||importContext.committing)return;
 ++importContext.revision;importContext.loading=false;importContext.dirty=true;$('importSnapshotAck').checked=false;
 $('importOtherGameLabel').hidden=$('importDefaultGame').value!=='other';
 $('importMessage').textContent='Your choices changed. Update the preview before importing.';importButtons();
}
function importBuildMappings(columns,fields){
 const key=JSON.stringify(columns);if(importContext.columnsKey===key)return;importContext.columnsKey=key;
 $('importColumns').replaceChildren();$('importExtraColumns').replaceChildren();
 for(const field of fields){
  const label=document.createElement('label');label.append(document.createTextNode(importFieldLabels[field.key]||field.label));
  const select=document.createElement('select');select.id='importMap'+field.key.split('_').map(word=>word[0].toUpperCase()+word.slice(1)).join('');select.dataset.importField=field.key;
  select.add(new Option('Auto-detect / not supplied',''));for(const column of columns)select.add(new Option(column,column));
  select.addEventListener('change',importChanged);label.append(select);$(importPrimaryFields.has(field.key)?'importColumns':'importExtraColumns').append(label);
 }
}
function importRender(data){
 const preview=data.preview;importContext.preview=preview;importContext.existing=data.existing;importContext.dirty=false;
 $('importExisting').hidden=!data.existing;if(data.existing)$('importExistingText').textContent='This export is already saved: '+data.existing.title+(data.existing.deleted?' (deleted batch)':'');
 importBuildMappings(preview.columns||[],data.mapping_fields||[]);
 $('importDefaultHint').textContent='Rows with a game column keep their own game. Otherwise the saved batch default is '+(intakeGames[preview.default_batch_game]||preview.default_batch_game||'not selected')+'. Choose a game above if this file needs another one.';
 $('importOverview').hidden=false;$('importOverview').innerHTML='<strong>'+Number(preview.row_count||0).toLocaleString()+' valid row(s)</strong><span>Format: '+escapeHTML(importFormatLabels[preview.adapter]||'not detected')+'</span><span>Quantity: '+escapeHTML(preview.quantity_column||'one copy when absent')+' · '+(preview.quantity_semantics==='snapshot'?'Stock snapshot':'Copies to bring in')+'</span>'+(preview.unpriced_count?'<span class="import-warning">'+Number(preview.unpriced_count).toLocaleString()+' row(s) still need a selling price after import.</span>':'');
 $('importSnapshot').hidden=!preview.requires_quantity_confirmation;$('importSnapshotAck').checked=false;
 const issues=preview.issues||[];$('importIssues').hidden=!issues.length;
 $('importIssues').innerHTML='<h3>'+Number(preview.error_count||0)+' error(s) · '+Number(preview.warning_count||0)+' review note(s)</h3><ul>'+issues.map(issue=>'<li class="'+(issue.severity==='error'?'import-error':'')+'">'+escapeHTML((issue.row?'Row '+issue.row+': ':'')+issue.message)+'</li>').join('')+'</ul>'+(preview.issue_count>issues.length?'<p>Showing the first '+issues.length+' of '+Number(preview.issue_count).toLocaleString()+' issues. Correct the source export or mapping and update the preview.</p>':'');
 $('importNormalized').hidden=!(preview.rows||[]).length;
 $('importRows').innerHTML=(preview.rows||[]).map(card=>'<tr><td>'+Number(card.source_row)+'</td><td><strong>'+escapeHTML(card.card_name)+'</strong><small>'+escapeHTML(card.game_label||card.game||'Use batch default')+' · '+escapeHTML(card.card_set||'Set not supplied')+' #'+escapeHTML(card.card_num||'—')+'</small></td><td>'+escapeHTML(card.condition)+'<small>'+escapeHTML(card.printing)+' · '+escapeHTML(card.language)+'</small></td><td>'+Number(card.qty)+'</td><td>'+(card.final_price===null?'<strong class="import-warning">Price required</strong>':usd(card.final_price))+'</td></tr>').join('');
 $('importSourceHead').innerHTML='<tr>'+(preview.columns||[]).map(column=>'<th>'+escapeHTML(column)+'</th>').join('')+'</tr>';
 $('importSourceRows').innerHTML=(preview.sample_rows||[]).map(row=>'<tr>'+row.map(value=>'<td>'+escapeHTML(value)+'</td>').join('')+'</tr>').join('');
 if(preview.error_count)$('importMapping').open=true;
 $('importMessage').textContent=preview.can_import?'Preview updated. Check the copies, identity and prices below.':'Choose the intended columns, or correct the source file and import it again.';
}
async function importPreview(){
 const context=importContext;if(!context||context.committing)return;
 const revision=++context.revision;context.loading=true;context.dirty=true;importButtons();$('importMessage').textContent='Checking the export…';
 try{const options=importOptions();const data=await api('/api/import/preview',{csv:context.csv,intakeOptions:options});if(importContext!==context||context.revision!==revision)return;context.options=options;importRender(data)}
 catch(error){if(importContext===context&&context.revision===revision){context.preview=null;$('importMessage').textContent=error.message}}
 finally{if(importContext===context&&context.revision===revision){context.loading=false;importButtons()}}
}
function importClose(){if(importContext?.committing)return;importContext=null;importDialog.close();$('csvFile').value=''}
$('importClose').onclick=$('importCancel').onclick=importClose;
$('importOpenExisting').onclick=async()=>{const context=importContext;if(!context?.existing||context.committing)return;const id=context.existing.id;importClose();try{await openReceipt(id)}catch(error){toast(error.message,true)}};
importDialog.addEventListener('cancel',event=>{event.preventDefault();importClose()});
$('importUpdatePreview').onclick=importPreview;$('importSnapshotAck').onchange=importButtons;
$('importAdapter').onchange=$('importDefaultGame').onchange=$('importOtherGame').oninput=importChanged;
$('csvFile').accept='.csv,.tsv,text/csv,text/tab-separated-values';
$('importCommit').onclick=async()=>{
 const context=importContext;if(!context||$('importCommit').disabled)return;context.committing=true;importButtons();
 importDialog.querySelectorAll('select,input').forEach(input=>input.disabled=true);
 try{
  await busyWork(async()=>{
   try{
    await saveReceipt();const payload={csv:context.csv,fileName:context.name,mode:context.mode,intakeOptions:{...context.options,confirm_snapshot:context.preview.requires_quantity_confirmation&&$('importSnapshotAck').checked}};
    let data=await api('/api/import',payload);
    importContext=null;importDialog.close();
    if(data.receipt.deleted_at){const restored=await restoreIfDeleted(data.receipt);if(!restored)return;data.receipt=restored}
    else if(data.duplicate){adoptReceipt(data.receipt);const additional=await confirmAction('This batch is already saved.','The saved batch has been opened. Only start another batch if these are a separate physical delivery of additional cards.','These are additional copies');if(additional)data=await api('/api/import',{...payload,newDelivery:true})}
    data.receipt=await acknowledgePickup(data.receipt);adoptReceipt(data.receipt);await refreshReceipts();toast(data.duplicate?'Opened the saved batch. No stock changed.':'Unchecked draft imported. Check the cards before continuing.');
   }catch(error){if(importContext===context)$('importMessage').textContent=error.message;throw error}
  });
 }catch(error){if(importContext===context)$('importMessage').textContent=error.message;else toast(error.message,true)}
 finally{context.committing=false;importDialog.querySelectorAll('select,input').forEach(input=>input.disabled=false);importButtons();$('csvFile').value=''}
};
window.reviewCsvImport=async file=>{
 if(!file||state.busy)return;
 if(importContext){$('importMessage').textContent='Finish or cancel this preview before choosing another export.';return}
 if(!/\.(csv|tsv)$/i.test(file.name))return toast('Choose a CSV or TSV scanner export.',true);
 if(file.size>20000000)return toast('The export exceeds 20 MB. Split the batch before importing.',true);
 try{
  await saveReceipt();const csv=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());
  importContext={csv,name:file.name,mode:modes[state.mode]?state.mode:'labels',revision:0,preview:null,loading:false,committing:false,dirty:true,columnsKey:null};
  $('importFileName').textContent=file.name;$('importAdapter').value='auto';$('importDefaultGame').value='';$('importOtherGame').value='';$('importOtherGameLabel').hidden=true;
  $('importMapping').open=false;$('importAdvanced').open=false;$('importSource').open=false;
  for(const id of ['importOverview','importSnapshot','importIssues','importNormalized','importExisting'])$(id).hidden=true;
  $('importColumns').replaceChildren();$('importExtraColumns').replaceChildren();$('importMessage').textContent='';importDialog.showModal();await importPreview();
 }catch(error){toast(error instanceof TypeError?'The export is not valid UTF-8 text. Save it as UTF-8 CSV and try again.':error.message,true)}
 finally{$('csvFile').value=''}
};
