'use strict';
// Read-only release status and manager cleanup preview. No merchant writes.
const previewButton=document.createElement('button');
previewButton.className='button secondary';previewButton.textContent='Connections & cleanup';
previewButton.id='previewConnections';
document.querySelector('.heading-actions').append(previewButton);
const previewDialog=document.createElement('dialog');previewDialog.className='modal';previewDialog.id='previewStatus';
previewDialog.innerHTML='<h2>Connections & cleanup</h2><p><strong>Local test preview</strong></p><p>Credit balances are stored on this computer. Automatic Square redemption and shared balances are not connected.</p><h3>Connect Square with OAuth</h3><p>Hosted OAuth processing is under development. A registered HTTPS callback and hosted store sign-in are required before this can be enabled. Existing receiving connections continue to use the saved credential.</p><button class="button secondary" disabled>Connect Square · hosted setup required</button><h3>Old SKU cleanup</h3><p>Requires 30 days of verified zero stock across every location, no pending orders and explicit store opt-in. This preview does not delete Square data.</p><button id="loadCleanupPreview" class="button secondary">View manager cleanup preview</button><p id="cleanupPreviewResult" role="status"></p><form method="dialog"><button class="button primary">Close</button></form>';
document.body.append(previewDialog);
previewButton.onclick=()=>previewDialog.showModal();
document.getElementById('loadCleanupPreview').onclick=async()=>{
 const result=document.getElementById('cleanupPreviewResult');
 try{const data=await api('/api/admin/cleanup');result.textContent=data.items.length+' observed SKUs; '+data.items.filter(x=>x.candidate).length+' eligible candidates. Deletion is disabled.'}
 catch(error){result.textContent=error.message}
};
