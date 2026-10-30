'use strict';
// Read-only release status and manager cleanup preview. No merchant writes.
const previewButton=document.createElement('button');
previewButton.className='button secondary';previewButton.textContent='Connections & cleanup';
previewButton.id='previewConnections';
document.querySelector('.heading-actions').append(previewButton);
const previewDialog=document.createElement('dialog');previewDialog.className='modal';previewDialog.id='previewStatus';
previewDialog.innerHTML='<h2>Connections & cleanup</h2><p><strong>Private pilot</strong></p><h3>Shared store workspace</h3><p>Invited staff can sign in to Shared credit. Hosted account history is shared within the selected store. Square checkout and automatic trade issuance remain disabled until acceptance testing is complete.</p><button class="button secondary" id="openSharedWorkspace">Open Shared credit</button><h3>Square connection</h3><p>Store owners manage OAuth authorization in the hosted Store connections portal. The existing receiving credential remains available to this installation while OAuth receiving is integrated.</p><h3>Old SKU cleanup</h3><p>Requires 30 days of verified zero stock across every location, no pending orders and explicit store opt-in. This preview does not delete Square data.</p><button id="loadCleanupPreview" class="button secondary">View manager cleanup preview</button><p id="cleanupPreviewResult" role="status"></p><form method="dialog"><button class="button primary">Close</button></form>';
document.body.append(previewDialog);
previewButton.onclick=()=>previewDialog.showModal();
document.getElementById('loadCleanupPreview').onclick=async()=>{
 const result=document.getElementById('cleanupPreviewResult');
 try{const data=await api('/api/admin/cleanup');result.textContent=data.items.length+' observed SKUs; '+data.items.filter(x=>x.candidate).length+' eligible candidates. Deletion is disabled.'}
 catch(error){result.textContent=error.message}
};

document.getElementById('openSharedWorkspace').onclick=()=>{previewDialog.close();navigate('cloud')};
