"use strict";
(() => {
  const $ = id => document.getElementById(id), uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
  const hash = /^[a-f0-9]{64}$/, identities = [["name","Card / player"],["game","Game"],["game_label","Game label"],["kind","Item type"],["set","Set"],["number","Number"],["condition","Condition"],["printing","Printing / finish"],["language","Language"],["grading_company","Grading company"],["grade","Grade"],["grade_qualifier","Grade qualifier"],["certification","Certificate"],["year","Year"],["sport","Sport"],["league","League"],["team","Team"],["player","Player"],["serial_number","Serial number"],["manufacturer","Manufacturer"]];
  let generation = 0, controller = new AbortController(), session = null, busy = false, selected = null, preview = null, choiceRevision = 0;
  let bindingCursor = null, listCursor = null, listLimit = 50, bindingQuery = "", bindings = new Map(), templates = new Map(), bindingPages = new Set(), listPages = new Set();
  let pendingApproval = null, revokeTarget = null, pendingRevoke = null;
  const stamp = () => { try { return localStorage.getItem("ss-auth-epoch") || ""; } catch { return ""; } };
  const current = (epoch, authStamp) => epoch === generation && authStamp === stamp();
  const text = (value, limit = 1000) => typeof value === "string" && value.length <= limit;
  function message(id, value, error = false) { $(id).textContent = value; $(id).classList.toggle("error", error); }
  function hasMfa() { return !!(session?.mfa_enabled && session?.mfa_authenticated); }
  function controls() {
    $("templateRefreshSession").disabled = busy;
    $("templateSearchButton").disabled = busy || !!pendingApproval;
    $("templateSearch").disabled = busy || !!pendingApproval;
    $("templatePrefix").disabled = busy || !!pendingApproval;
    $("templatePreviewButton").disabled = busy || !!pendingApproval || !selected;
    $("templateRefreshList").disabled = busy;
    $("templateMoreBindings").disabled = busy || !!pendingApproval;
    $("templateMoreList").disabled = busy;
    for (const button of $("templateBindings").querySelectorAll("button")) button.disabled = busy || !!pendingApproval;
    for (const button of $("templateList").querySelectorAll("button")) button.disabled = busy || !!pendingRevoke;
    for (const id of ["templateIdentityConfirmed","templateTaxConfirmed","templateSiblingsConfirmed","templateApprovalReason"]) $(id).disabled = busy || !!pendingApproval;
    $("templateApproveButton").disabled = busy || !preview || !hasMfa();
    $("templateApproveButton").textContent = pendingApproval ? "Retry the same approval" : "Approve this price family";
    $("templateRevokeReason").disabled = busy || !!pendingRevoke;
    $("templateRevokeButton").disabled = busy || !revokeTarget || !hasMfa();
    $("templateRevokeButton").textContent = pendingRevoke ? "Retry the same revocation" : "Revoke approval";
    $("templateCancelRevoke").disabled = busy || !!pendingRevoke;
    $("templateProofGuidance").textContent = hasMfa() ? "The server checks that your enrolled MFA proof is still within 15 minutes when approving." : "Enroll MFA in the workspace if needed, then sign out and sign in again for a fresh proof. Your saved approvals remain available.";
  }
  function setBusy(value) { busy = value; controls(); }
  function discardPreview() { preview = null; pendingApproval = null; $("templateReview").hidden = true; $("templateReviewFacts").replaceChildren(); $("templateTaxFacts").replaceChildren(); $("templateCategoryFacts").replaceChildren(); $("templateReferences").replaceChildren(); $("templateApproveForm").reset(); }
  function reset(value = "Your sign-in changed. Refresh your owner session to continue.") {
    generation++; controller.abort(); controller = new AbortController(); session = null; busy = false; selected = null; choiceRevision++;
    discardPreview(); revokeTarget = pendingRevoke = null; bindings.clear(); templates.clear(); bindingPages.clear(); listPages.clear(); bindingCursor = listCursor = null; listLimit = 50;
    for (const id of ["templateSearchForm","templatePreviewForm","templateRevokeForm"]) $(id).reset();
    for (const id of ["templateBindings","templateList","templateSelectedFacts"]) $(id).replaceChildren();
    for (const id of ["templateWorkspace","templateSelected","templateRevoke","templateMoreBindings","templateMoreList"]) $(id).hidden = true;
    $("templateStore").textContent = ""; $("templateRevokeName").textContent = "";
    for (const id of ["templateStatus","templateBindingStatus","templateListStatus"]) message(id, "");
    message("templateSession", value); controls();
  }
  async function readJson(response) {
    if (!response.headers.get("Content-Type")?.toLowerCase().includes("application/json") || !response.body) throw Error("The service returned an unsupported response. Refresh before continuing.");
    const reader = response.body.getReader(), decoder = new TextDecoder("utf-8", {fatal:true}); let size = 0, output = "";
    try { while (true) { const part = await reader.read(); if (part.done) break; size += part.value.byteLength; if (size > 2*1024*1024) { await reader.cancel(); const error=Error("This review exceeds the supported response size. No complete review was loaded."); error.responseTooLarge=true; throw error; } output += decoder.decode(part.value, {stream:true}); } output += decoder.decode(); return JSON.parse(output); }
    finally { reader.releaseLock(); }
  }
  async function requestJson(path, options = {}) {
    const parent = controller.signal, local = new AbortController(); let timedOut = false;
    const abort = () => local.abort(); if (parent.aborted) local.abort(); else parent.addEventListener("abort",abort,{once:true});
    const timer = setTimeout(() => { timedOut = true; local.abort(); },65000);
    try { const response = await fetch(path,{credentials:"same-origin",cache:"no-store",redirect:"error",...options,signal:local.signal}); return {response,result:await readJson(response)}; }
    catch (error) { if (timedOut) throw Error("The request timed out. An action may still have completed; recover the same request or review saved approvals."); throw error; }
    finally { clearTimeout(timer); parent.removeEventListener("abort",abort); }
  }
  async function sameOwner(epoch, authStamp) {
    const {response,result:me} = await requestJson("/api/me"); if (!current(epoch,authStamp)) return false;
    if (!response.ok || me.role !== "owner" || !session || me.csrf_token !== session.csrf_token || me.store !== session.store) { reset(); return false; }
    return true;
  }
  async function api(path, body, epoch = generation, authStamp = stamp()) {
    if (body !== undefined && !(await sameOwner(epoch,authStamp))) return null;
    if (!current(epoch,authStamp)) return null;
    const {response,result} = await requestJson(path, {method:body === undefined ? "GET" : "POST",
      headers:body === undefined ? {Accept:"application/json"} : {Accept:"application/json","Content-Type":"application/json","X-SS-CSRF":session?.csrf_token || ""},body:body === undefined ? undefined : JSON.stringify(body)});
    if (!current(epoch,authStamp)) return null;
    if (!response.ok) { if (response.status === 401) reset("Sign in to the workspace, then refresh this owner view."); else if (response.status === 403 && session && !(await sameOwner(epoch,authStamp))) return null; const error=Error(text(result.error) ? result.error : "The review could not be confirmed. Keep its saved request and refresh the approvals list."); error.status=response.status; throw error; }
    if (path !== "/api/me" && session && !(await sameOwner(epoch,authStamp))) return null;
    return result;
  }
  function fact(dl, label, value, code = false) { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = label; const node = code ? document.createElement("code") : dd; node.textContent = String(value); if (code) dd.append(node); dl.append(dt,dd); }
  function identity(dl, value) { for (const [key,label] of identities) if (value[key]) fact(dl,label,key === "kind" ? value[key] === "slab" ? "Graded slab" : "Raw card" : value[key]); }
  function validIdentity(value) { return value && typeof value === "object" && !Array.isArray(value) && text(value.name,200) && value.name && text(value.game,100) && value.game && ["card","slab"].includes(value.kind) && identities.every(([key]) => value[key] === undefined || text(value[key],255)); }
  function validProvider(value) { return value && ["production","sandbox"].includes(value.environment) && text(value.merchant_id,255) && value.merchant_id && text(value.application_id,255) && value.application_id; }
  const sameIdentity = (left,right) => Object.keys(left).sort().join("|") === Object.keys(right).sort().join("|") && Object.keys(left).every(key => left[key] === right[key]);
  function validTax(row) { return row && text(row.id,255) && row.id && text(row.name,255) && row.name && text(row.percentage,100) && /^(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,6})?$/.test(row.percentage) && Number(row.percentage)<=100 && ["ADDITIVE","INCLUSIVE"].includes(row.inclusion_type) && typeof row.enabled === "boolean" && ["TAX_SUBTOTAL_PHASE","TAX_TOTAL_PHASE"].includes(row.calculation_phase); }
  function validFacts(facts) { return facts && uuid.test(facts.binding_id) && Number.isSafeInteger(facts.binding_version) && facts.binding_version > 0 && validIdentity(facts.identity) && hash.test(facts.identity_hash) && text(facts.base_sku,100) && facts.base_sku && facts.parent && text(facts.parent.id,255) && text(facts.parent.name,255) && facts.location && text(facts.location.id,255) && text(facts.location.name,255) && validProvider(facts.provider) && text(facts.variation_name_prefix,80) && facts.variation_name_prefix && Array.isArray(facts.taxes) && facts.taxes.length <= 100 && facts.taxes.every(validTax) && Array.isArray(facts.categories) && facts.categories.length <= 101 && facts.categories.every(row => row && text(row.id,255) && text(row.name,255)) && (facts.taxability === null || typeof facts.taxability === "boolean") && text(facts.policy_reference,1000) && hash.test(facts.parent_policy_hash) && hash.test(facts.catalog_snapshot_hash) && facts.new_parent_creation === false && facts.receiving_enabled === false; }
  function setting(container, title, lines) { container.replaceChildren(); const heading = document.createElement("h3"); heading.textContent = title; container.append(heading); const list = document.createElement("ul"); for (const line of lines) { const li = document.createElement("li"); li.textContent = line; list.append(li); } container.append(list); }
  function renderReview(value) {
    const facts = value.facts, dl = $("templateReviewFacts"); dl.replaceChildren(); identity(dl,facts.identity);
    fact(dl,"Existing parent",facts.parent.name || "Unnamed item"); fact(dl,"Receiving location",facts.location.name || facts.location.id); fact(dl,"Retained source SKU",JSON.stringify(facts.base_sku),true); fact(dl,"New variation prefix",facts.variation_name_prefix);
    fact(dl,"Square environment",facts.provider.environment === "production" ? "Production" : "Sandbox");
    const taxes = ["Item taxability: " + (facts.taxability === true ? "taxable" : facts.taxability === false ? "not taxable" : "not explicitly set; existing configuration is preserved")];
    if (!facts.taxes.length) taxes.push("No tax references are attached to the existing parent.");
    for (const row of facts.taxes) taxes.push(`${row.name}: ${row.percentage == null ? "rate not specified" : row.percentage + "%"}; ${row.inclusion_type === "INCLUSIVE" ? "included in price" : row.inclusion_type === "ADDITIVE" ? "added to price" : "inclusion not specified"}; ${row.enabled ? "enabled" : "disabled"}; ${row.calculation_phase === "TAX_SUBTOTAL_PHASE" ? "subtotal calculation phase" : "total calculation phase"}.`);
    setting($("templateTaxFacts"),"Existing tax configuration",taxes);
    setting($("templateCategoryFacts"),"Existing categories",facts.categories.length ? facts.categories.map(row => row.name) : ["No category references are attached to the existing parent."]);
    const refs = $("templateReferences"); refs.replaceChildren(); for (const [label,item] of [["Parent reference",facts.parent.id],["Policy reference",facts.policy_reference],["Settings hash",facts.parent_policy_hash],["Catalog snapshot",facts.catalog_snapshot_hash],["Application",facts.provider.application_id],["Merchant",facts.provider.merchant_id]]) fact(refs,label,item,true);
    $("templateReview").hidden = false; controls();
  }
  function choose(binding) { if (busy || pendingApproval) return; selected = binding; choiceRevision++; discardPreview(); const dl = $("templateSelectedFacts"); dl.replaceChildren(); identity(dl,binding.identity); fact(dl,"Retained SKU",JSON.stringify(binding.source_sku),true); fact(dl,"Current price",new Intl.NumberFormat("en-US",{style:"currency",currency:"USD"}).format(binding.selling_cents/100)); fact(dl,"Location",binding.location.name || binding.location.id); $("templateSelected").hidden = false; for (const button of $("templateBindings").querySelectorAll("button")) button.setAttribute("aria-pressed",String(button.dataset.id === binding.binding_id)); message("templateStatus", "Source selected. Enter the variation prefix and review its current Square settings."); controls(); $("templatePrefix").focus(); }
  async function loadBindings(append = false) {
    const epoch = generation, authStamp = stamp(), cursor = append ? bindingCursor : null, query = $("templateSearch").value.trim();
    if (!append) { bindingQuery = query; bindingPages.clear(); bindings.clear(); $("templateBindings").replaceChildren(); bindingCursor = null; }
    if (append && query !== bindingQuery) return; setBusy(true); message("templateBindingStatus","Loading reviewed cards…");
    try { const params = new URLSearchParams({q:bindingQuery,limit:"50"}); if (cursor) params.set("cursor",cursor); const result = await api("/api/receiving/catalog-bindings?"+params,undefined,epoch,authStamp); if (!result || !current(epoch,authStamp)) return;
      if (!Array.isArray(result.bindings) || result.bindings.length > 50 || (result.next_cursor !== null && !uuid.test(result.next_cursor)) || (result.next_cursor && (result.next_cursor === cursor || bindingPages.has(result.next_cursor)))) throw Error("The catalog page could not be validated. Refresh this list before continuing.");
      if (new Set(result.bindings.map(row=>row?.binding_id)).size !== result.bindings.length) throw Error("The catalog page repeats a mapping. Refresh before selecting a source.");
      for (const row of result.bindings) { if (!row || !uuid.test(row.binding_id) || !Number.isSafeInteger(row.version) || row.version < 1 || !validIdentity(row.identity) || !hash.test(row.identity_hash) || !text(row.source_sku,100) || !row.source_sku || !Number.isSafeInteger(row.selling_cents) || row.selling_cents < 0 || row.currency !== "USD" || !row.location || !text(row.location.id,255) || !text(row.location.name,255) || !validProvider(row.provider) || bindings.has(row.binding_id)) throw Error("The reviewed catalog list contains inconsistent records. Refresh before selecting a source."); }
      for (const row of result.bindings) { bindings.set(row.binding_id,row); const button = document.createElement("button"), name = document.createElement("strong"), detail = document.createElement("small"); button.type="button"; button.className="template-choice"; button.dataset.id=row.binding_id; button.setAttribute("aria-pressed","false"); name.textContent=row.identity.name; detail.textContent=[row.identity.game_label || row.identity.game,row.identity.set,row.identity.number,row.identity.condition,row.identity.grade_qualifier,row.location.name,"SKU "+JSON.stringify(row.source_sku)].filter(Boolean).join(" · "); button.append(name,detail); button.addEventListener("click",()=>choose(row)); $("templateBindings").append(button); }
      if (cursor) bindingPages.add(cursor); bindingCursor=result.next_cursor; $("templateMoreBindings").hidden=!bindingCursor; message("templateBindingStatus",bindings.size ? `${bindings.size} reviewed mappings loaded${bindingCursor ? "; more available" : ""}.` : "No reviewed mappings found. Review a catalog identity first.");
    } catch (error) { if (current(epoch,authStamp)) message("templateBindingStatus",error.message,true); } finally { if (current(epoch,authStamp)) setBusy(false); }
  }
  function openRevoke(row) { if (busy || pendingRevoke || row.state !== "approved") return; revokeTarget=row; $("templateRevokeForm").reset(); $("templateRevokeName").textContent=`${row.facts.identity.name} · ${row.facts.variation_name_prefix} · ${row.facts.location.name}`; $("templateRevoke").hidden=false; controls(); $("templateRevokeReason").focus(); }
  async function loadTemplates(append = false) {
    const epoch=generation,authStamp=stamp(),cursor=append ? listCursor : null;
    if (!append) { templates.clear(); listPages.clear(); $("templateList").replaceChildren(); listCursor=null; listLimit=50; }
    setBusy(true); message("templateListStatus","Loading saved approvals…");
    try { let result;
      while (true) { const params=new URLSearchParams({limit:String(listLimit)}); if (cursor) params.set("cursor",cursor);
        try { result=await api("/api/receiving/price-templates?"+params,undefined,epoch,authStamp); break; }
        catch(error) { if (!current(epoch,authStamp)) return; if (listLimit>1 && (error.responseTooLarge || error.status===413)) { listLimit=Math.max(1,Math.floor(listLimit/2)); message("templateListStatus","Loading fewer approvals so every review stays complete…"); continue; } throw error; }
      }
      if (!result || !current(epoch,authStamp)) return;
      if (!Array.isArray(result.templates) || result.templates.length>listLimit || (result.next_cursor !== null && !uuid.test(result.next_cursor)) || (result.next_cursor && (result.next_cursor===cursor || listPages.has(result.next_cursor)))) throw Error("The approval page could not be validated. Refresh it before continuing.");
      if (new Set(result.templates.map(row=>row?.template_id)).size !== result.templates.length) throw Error("The approval page repeats a record. Refresh it before continuing.");
      for (const row of result.templates) if (!row || !uuid.test(row.template_id) || row.version!==1 || !["approved","revoked"].includes(row.state) || !validFacts(row.facts) || templates.has(row.template_id)) throw Error("The saved approvals contain inconsistent review facts. Refresh before using them.");
      for (const row of result.templates) { templates.set(row.template_id,row); const article=document.createElement("article"), badge=document.createElement("span"), heading=document.createElement("h3"), info=document.createElement("p"), button=document.createElement("button"); article.className="template-approval"; article.dataset.id=row.template_id; badge.className="pill"; badge.textContent=row.state === "approved" ? "Approved" : "Revoked"; heading.textContent=row.facts.identity.name; info.textContent=[row.facts.variation_name_prefix,row.facts.location.name,"Source SKU "+JSON.stringify(row.facts.base_sku),row.facts.provider.environment].join(" · "); article.append(badge,heading,info); const details=document.createElement("details"), summary=document.createElement("summary"), facts=document.createElement("dl"); summary.textContent="Reviewed identity and reference"; facts.className="template-facts compact"; identity(facts,row.facts.identity); fact(facts,"Template",row.template_id,true); fact(facts,"Version",row.version); fact(facts,"Policy",row.facts.policy_reference,true); details.append(summary,facts); article.append(details); if (row.state === "approved") { button.type="button"; button.className="quiet"; button.textContent="Review revocation"; button.addEventListener("click",()=>openRevoke(row)); article.append(button); } $("templateList").append(article); }
      if (cursor) listPages.add(cursor); listCursor=result.next_cursor; $("templateMoreList").hidden=!listCursor; message("templateListStatus",templates.size ? `${templates.size} approvals loaded${listCursor ? "; more available" : ""}.` : "No saved price-family approvals yet.");
    } catch (error) { if (current(epoch,authStamp)) message("templateListStatus",error.message,true); } finally { if (current(epoch,authStamp)) setBusy(false); }
  }
  async function refresh() {
    reset("Checking your owner session…"); const epoch=generation,authStamp=stamp();
    try { const me=await api("/api/me",undefined,epoch,authStamp); if (!me || !current(epoch,authStamp)) return; if (me.role !== "owner" || !text(me.store,100) || !me.store || !text(me.csrf_token,512) || !me.csrf_token) { reset("Only the store owner can review price-family approvals. Sign in from the workspace."); return; } session=Object.freeze(me); $("templateStore").textContent=me.store; $("templateWorkspace").hidden=false; message("templateSession","Signed in as owner of "+me.store+". Review source and settings before approving."); controls(); await loadTemplates(); if (current(epoch,authStamp)) await loadBindings(); }
    catch (error) { if (current(epoch,authStamp)) { reset("Owner review is unavailable. Sign in to the workspace and refresh."); message("templateStatus",error.message,true); } }
  }
  $("templateSearchForm").addEventListener("submit",event=>{event.preventDefault();if (!busy && session && !pendingApproval) loadBindings();});
  $("templatePrefix").addEventListener("input",()=>{ if (pendingApproval) return; choiceRevision++; discardPreview(); controls(); });
  $("templatePreviewForm").addEventListener("submit",async event=>{
    event.preventDefault(); if (busy || !session || !selected || pendingApproval) return; discardPreview(); const epoch=generation,authStamp=stamp(),revision=choiceRevision,binding=selected,prefix=$("templatePrefix").value.trim(); setBusy(true); message("templateStatus","Reading the current parent and its settings…");
    try { const result=await api("/api/receiving/price-templates/previews",{binding_id:binding.binding_id,variation_name_prefix:prefix},epoch,authStamp); if (!result || !current(epoch,authStamp) || revision !== choiceRevision || binding !== selected) return;
      if (!uuid.test(result.preview_id) || !hash.test(result.preview_hash) || result.approval_required !== true || !Number.isSafeInteger(result.expires_in) || result.expires_in<1 || result.expires_in>900 || !validFacts(result.facts) || result.facts.binding_id!==binding.binding_id || result.facts.binding_version!==binding.version || result.facts.identity_hash!==binding.identity_hash || !sameIdentity(result.facts.identity,binding.identity) || result.facts.variation_name_prefix!==prefix || result.facts.base_sku!==binding.source_sku || result.facts.location.id!==binding.location.id || ["merchant_id","application_id","environment"].some(key=>result.facts.provider[key]!==binding.provider[key])) throw Error("The review differs from the selected source mapping. Refresh and review it again.");
      preview=Object.freeze(result); renderReview(result); message("templateStatus","Current settings loaded. Review every item before confirming this price family.");
    } catch (error) { if (current(epoch,authStamp)) message("templateStatus",error.message,true); } finally { if (current(epoch,authStamp)) setBusy(false); }
  });
  $("templateApproveForm").addEventListener("submit",async event=>{
    event.preventDefault(); if (busy || !preview || !session || !hasMfa()) return; const epoch=generation,authStamp=stamp(),target=preview;
    if (!pendingApproval) pendingApproval={id:target.preview_id,body:{request_key:crypto.randomUUID(),preview_hash:target.preview_hash,preserve_existing_tax_configuration_confirmed:$("templateTaxConfirmed").checked,preserve_existing_siblings_confirmed:$("templateSiblingsConfirmed").checked,source_identity_confirmed:$("templateIdentityConfirmed").checked,approval_reason:$("templateApprovalReason").value.trim(),policy_reference:target.facts.policy_reference}};
    setBusy(true); message("templateStatus","Saving this owner approval in Stock & Sleeve…");
    try { const result=await api(`/api/receiving/price-templates/${encodeURIComponent(pendingApproval.id)}/approve`,pendingApproval.body,epoch,authStamp); if (!result || !current(epoch,authStamp) || preview !== target) return; if (!uuid.test(result.template_id) || result.version!==1 || result.state!=="approved" || result.receiving_enabled!==false || result.new_parent_creation!==false) throw Error("The approval result could not be validated. Retry this exact review or refresh saved approvals."); discardPreview(); message("templateStatus","Price family approved in Stock & Sleeve. Square items and stock were not changed; receiving activation remains separate."); await loadTemplates(); }
    catch (error) { if (current(epoch,authStamp)) message("templateStatus",error.message+" The request is retained for an exact retry; do not invent a replacement approval.",true); } finally { if (current(epoch,authStamp)) setBusy(false); }
  });
  $("templateRevokeForm").addEventListener("submit",async event=>{
    event.preventDefault(); if (busy || !session || !revokeTarget || !hasMfa()) return; const epoch=generation,authStamp=stamp(),target=revokeTarget;
    if (!pendingRevoke) pendingRevoke={id:target.template_id,body:{request_key:crypto.randomUUID(),version:target.version,reason:$("templateRevokeReason").value.trim()}};
    setBusy(true); message("templateStatus","Saving the revocation…");
    try { const result=await api(`/api/receiving/price-templates/${encodeURIComponent(pendingRevoke.id)}/revoke`,pendingRevoke.body,epoch,authStamp); if (!result || !current(epoch,authStamp) || revokeTarget !== target) return; if (result.template_id!==target.template_id || result.version!==target.version || result.state!=="revoked") throw Error("The revocation result could not be validated. Retry this exact request or refresh approvals."); pendingRevoke=revokeTarget=null; $("templateRevoke").hidden=true; $("templateRevokeForm").reset(); message("templateStatus","Approval revoked. Existing Square effects and recovery records were not undone."); await loadTemplates(); }
    catch (error) { if (current(epoch,authStamp)) message("templateStatus",error.message+" The same revocation request is retained for recovery.",true); } finally { if (current(epoch,authStamp)) setBusy(false); }
  });
  $("templateCancelRevoke").addEventListener("click",()=>{ if (busy || pendingRevoke) return; revokeTarget=null; $("templateRevoke").hidden=true; $("templateRevokeForm").reset(); controls(); });
  $("templateRefreshSession").addEventListener("click",refresh);
  $("templateRefreshList").addEventListener("click",()=>{ if (!busy && session) loadTemplates(); });
  $("templateMoreList").addEventListener("click",()=>{ if (!busy && listCursor) loadTemplates(true); });
  $("templateMoreBindings").addEventListener("click",()=>{ if (!busy && !pendingApproval && bindingCursor) loadBindings(true); });
  window.addEventListener("storage",event=>{if(event.key==="ss-auth-epoch")reset();});
  let channel=null; try { channel=new BroadcastChannel("ss-auth-transition");channel.onmessage=()=>reset(); } catch {}
  window.addEventListener("pagehide",()=>{reset("This review view closed.");channel?.close();});
  refresh();
})();
