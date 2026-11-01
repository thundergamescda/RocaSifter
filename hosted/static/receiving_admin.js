"use strict";
(() => {
  const $ = id => document.getElementById(id), form = $("receivingAdminForm"), approveForm = $("receivingAdminApproveForm");
  let generation = 0, controller = new AbortController(), session = null, preview = null, busy = false;
  const fields = [["set", "Set", 200], ["number", "Card number", 100], ["language", "Language", 50], ["condition", "Condition", 100], ["printing", "Printing / finish", 100]];
  const slabFields = [["grading_company", "Grading company", 100], ["grade", "Grade", 30], ["grade_qualifier", "Grade qualifier / label tier", 100], ["certification", "Certification reference", 100], ["year", "Year", 20], ["sport", "Sport", 100], ["league", "League", 100], ["team", "Team", 100], ["player", "Player", 150], ["serial_number", "Serial number", 100], ["manufacturer", "Manufacturer", 100]];
  for (const [target, rows] of [["receivingAdminIdentityFields", fields], ["receivingAdminSlabFields", slabFields]]) {
    for (const [name, label, limit] of rows) {
      const wrapper = document.createElement("label"), input = document.createElement("input");
      wrapper.append(document.createTextNode(label)); input.name = name; input.maxLength = limit; input.autocomplete = "off";
      wrapper.append(input); $(target).append(wrapper);
    }
  }
  function progress(message, error = false) { const node = $("receivingAdminProgress"); node.textContent = message; node.classList.toggle("error", error); }
  function discardPreview() { preview = null; $("receivingAdminReview").hidden = true; $("receivingAdminFacts").replaceChildren(); approveForm.reset(); }
  function reset(message = "Session changed. Refresh your owner session to continue.") {
    generation++; controller.abort(); controller = new AbortController(); session = null; busy = false;
    discardPreview(); form.reset(); $("receivingAdminWorkspace").hidden = true; $("receivingAdminOAuth").hidden = true;
    $("receivingAdminOAuthForm").reset(); $("receivingAdminOAuthStatus").textContent = ""; $("receivingAdminSession").textContent = message;
    $("receivingAdminRefresh").disabled = false;
    form.inert = false; approveForm.inert = false; $("receivingAdminOAuthForm").inert = false; progress("");
  }
  async function sameOwner(epoch) {
    const response = await fetch("/api/me", {credentials: "same-origin", signal: controller.signal});
    const current = await response.json();
    if (epoch !== generation) return false;
    if (!response.ok || current.role !== "owner" || !session || current.csrf_token !== session.csrf_token) { reset(); return false; }
    return true;
  }
  async function api(path, body, epoch = generation) {
    if (body !== undefined && !(await sameOwner(epoch))) return null;
    const response = await fetch(path, {method: body === undefined ? "GET" : "POST", credentials: "same-origin", signal: controller.signal,
      headers: body === undefined ? {} : {"Content-Type": "application/json", "X-SS-CSRF": session?.csrf_token || ""}, body: body === undefined ? undefined : JSON.stringify(body)});
    const result = await response.json();
    if (epoch !== generation) return null;
    if (!response.ok) { if (response.status === 401) reset("Sign in to the workspace, then refresh your owner session."); throw new Error(result.error || "This review could not be completed."); }
    if (path !== "/api/me" && session && !(await sameOwner(epoch))) return null;
    return result;
  }
  function setBusy(value) { busy = value; form.inert = value; approveForm.inert = value; $("receivingAdminOAuthForm").inert = value; $("receivingAdminRefresh").disabled = value; }
  async function refresh() {
    reset("Checking your owner session…"); const epoch = generation;
    try {
      const me = await api("/api/me", undefined, epoch); if (!me || epoch !== generation) return;
      if (me.role !== "owner") { reset("Only the store owner can approve catalog mappings."); return; }
      session = me;
      $("receivingAdminOAuth").hidden = false; $("receivingAdminOAuthButton").disabled = !(me.mfa_enabled && me.mfa_authenticated);
      if (!me.mfa_enabled || !me.mfa_authenticated) $("receivingAdminOAuthStatus").textContent = "Enroll MFA and sign in with a fresh proof before requesting additional permissions.";
      const readiness = await api("/api/receiving/readiness", undefined, epoch); if (!readiness || epoch !== generation) return;
      let locations;
      try { locations = await api("/api/receiving/locations", undefined, epoch); }
      catch (error) {
        if (epoch === generation) { $("receivingAdminSession").textContent = "Owner session ready. Square location access is unavailable."; $("receivingAdminOAuthStatus").textContent = error.message; }
        return;
      }
      if (!locations || epoch !== generation) return;
      const select = form.elements.location_id; select.replaceChildren(new Option("Choose a Square location", ""));
      for (const row of locations.locations) select.append(new Option(row.name || row.id, row.id));
      $("receivingAdminStore").textContent = me.store;
      $("receivingAdminSession").textContent = "Owner session ready. Review one current catalog target.";
      $("receivingAdminReadiness").textContent = readiness.write_enabled ? "This store has hosted receiving admission enabled. This page still changes only the mapping registry." : "Hosted receiving writes remain disabled for this server or store. Mapping review does not enable them.";
      $("receivingAdminWorkspace").hidden = false;
      $("receivingAdminApprove").disabled = !(me.mfa_enabled && me.mfa_authenticated);
      if (!me.mfa_enabled || !me.mfa_authenticated) progress("You may preview a mapping. Enroll MFA and sign in with a fresh proof before approving it.");
    } catch (error) { if (epoch === generation) { reset("Owner review is unavailable. Check your session and Square authorization, then refresh."); progress(error.message, true); } }
  }
  form.addEventListener("input", discardPreview);
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (busy || !session) return; discardPreview(); const epoch = generation; setBusy(true);
    const identity = {};
    for (const name of ["name", "game", "kind", "game_label", ...fields.map(row => row[0]), ...slabFields.map(row => row[0])]) {
      const value = form.elements[name].value.trim(); if (value) identity[name] = value;
    }
    try {
      const result = await api("/api/receiving/catalog-bindings/previews", {identity, variation_id: form.elements.variation_id.value.trim(), location_id: form.elements.location_id.value}, epoch);
      if (!result || epoch !== generation) return; preview = result;
      const facts = result.facts, rows = [...Object.entries(facts.identity), ["Retained SKU", facts.sku], ["Current USD price", (facts.selling_cents / 100).toFixed(2)], ["Square variation", facts.variation_id], ["Square parent", facts.parent_id], ["Receiving location", facts.location_id]];
      for (const [key, value] of rows) { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = key.replaceAll("_", " "); dd.textContent = String(value); $("receivingAdminFacts").append(dt, dd); }
      $("receivingAdminReview").hidden = false; progress("Current provider facts loaded. Confirm the identity before saving the mapping.");
    } catch (error) { if (epoch === generation) progress(error.message, true); }
    finally { if (epoch === generation) setBusy(false); }
  });
  $("receivingAdminOAuthForm").addEventListener("submit", async event => {
    event.preventDefault(); if (busy || !session || !event.currentTarget.elements.permissions_confirmed.checked) return;
    const epoch = generation; setBusy(true); discardPreview(); $("receivingAdminOAuthStatus").textContent = "Preparing the explicit Square receiving consent request…";
    try {
      const result = await api("/api/square/connect", {profile: "receiving", receiving_permissions_confirmed: true}, epoch);
      if (!result || epoch !== generation) return;
      const url = new URL(result.authorization_url);
      if (url.protocol !== "https:" || !["connect.squareup.com", "connect.squareupsandbox.com"].includes(url.hostname) || url.pathname !== "/oauth2/authorize" || url.username || url.password) throw new Error("Square authorization returned an unsupported destination.");
      window.location.assign(url.href);
    } catch (error) { if (epoch === generation) $("receivingAdminOAuthStatus").textContent = error.message; }
    finally { if (epoch === generation) setBusy(false); }
  });
  approveForm.addEventListener("submit", async event => {
    event.preventDefault(); if (busy || !preview || !session) return; const epoch = generation, target = preview; setBusy(true);
    try {
      const result = await api(`/api/receiving/catalog-bindings/${encodeURIComponent(target.preview_id)}/approve`, {request_key: crypto.randomUUID(), preview_hash: target.preview_hash, identity_confirmed: approveForm.elements.identity_confirmed.checked, reason: approveForm.elements.reason.value.trim()}, epoch);
      if (!result || epoch !== generation || preview !== target) return;
      discardPreview(); progress("Catalog mapping approved in Stock & Sleeve. Square items, prices and stock were not changed.");
    } catch (error) { if (epoch === generation) progress(error.message, true); }
    finally { if (epoch === generation) setBusy(false); }
  });
  $("receivingAdminRefresh").addEventListener("click", refresh);
  window.addEventListener("storage", event => { if (event.key === "ss-auth-epoch") reset(); });
  try { const channel = new BroadcastChannel("ss-auth-transition"); channel.onmessage = () => reset(); } catch (_) { /* Unsupported browser: explicit same-session verification remains. */ }
  window.addEventListener("pageshow", event => { if (event.persisted) reset(); });
  refresh();
})();
