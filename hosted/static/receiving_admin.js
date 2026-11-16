"use strict";
(() => {
  const $ = id => document.getElementById(id), form = $("receivingAdminForm"), approveForm = $("receivingAdminApproveForm");
  let generation = 0, controller = new AbortController(), session = null, preview = null, busy = false;
  let previewRevision = 0, reviewRequest = 0, review = null, previewDeadline = 0, expiryTimer = null;
  const reviewPages = new Set(), reviewBindings = new Set(), uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/, hash = /^[0-9a-f]{64}$/;
  const gameChoices = new Map(), retirementControls = new Set();
  const fields = [["set", "Set", 200], ["number", "Card number", 100], ["language", "Language", 50], ["condition", "Condition", 100], ["printing", "Printing / finish", 100]];
  const slabFields = [["grading_company", "Grading company", 100], ["grade", "Grade", 30], ["grade_qualifier", "Grade qualifier / label tier", 100], ["certification", "Certification reference", 100], ["year", "Year", 20], ["sport", "Sport", 100], ["league", "League", 100], ["team", "Team", 100], ["player", "Player", 150], ["serial_number", "Serial number", 100], ["manufacturer", "Manufacturer", 100]];
  const physicalFields = {game:"game", game_label:"game_label", card_name:"name", card_set:"set", card_num:"number",
    condition:"condition", printing:"printing", language:"language", item_type:"kind", grader:"grading_company",
    grade:"grade", grade_qualifier:"grade_qualifier", cert_number:"certification", serial_number:"serial_number",
    year:"year", sport:"sport", league:"league", manufacturer:"manufacturer", team:"team", player:"player"};
  for (const [target, rows] of [["receivingAdminIdentityFields", fields], ["receivingAdminSlabFields", slabFields]]) {
    for (const [name, label, limit] of rows) {
      const wrapper = document.createElement("label"), input = document.createElement("input");
      wrapper.append(document.createTextNode(label)); input.name = name; input.maxLength = limit; input.autocomplete = "off";
      wrapper.append(input); $(target).append(wrapper);
    }
  }
  function progress(message, error = false) { const node = $("receivingAdminProgress"); node.textContent = message; node.classList.toggle("error", error); }
  function reviewStatus(message, error = false) { const node = $("receivingAdminMappingStatus"); node.textContent = message; node.classList.toggle("error", error); }
  function currentPreview(epoch, revision, target) { return epoch === generation && revision === previewRevision && preview === target && !!session; }
  function freshMfa() { return session?.mfa_enabled === true && session?.mfa_authenticated === true && session?.mfa_fresh === true; }
  function mfaGuidance() { return session?.mfa_enabled === true ? "Sign in again with MFA before continuing." : "Set up MFA and sign in before continuing."; }
  function approvalReady() { return !!preview && Date.now() < previewDeadline && review?.target === preview && review.conflict_exists === false && freshMfa(); }
  function validGames(value) {
    if (!value || value.catalog_games_version !== 1 || value.supports_additional_games !== true
      || !Array.isArray(value.games) || value.games.length < 1 || value.games.length > 100) return false;
    const seen = new Set();
    for (const row of value.games) {
      if (!row || typeof row.id !== "string" || !/^[a-z0-9][a-z0-9-]{0,99}$/.test(row.id) || seen.has(row.id)
        || typeof row.label !== "string" || row.label.length < 1 || row.label.length > 100) return false;
      seen.add(row.id);
    }
    return true;
  }
  function selectGame() {
    const value = form.elements.game.value;
    form.elements.game_label.value = gameChoices.get(value) || "";
    form.elements.game_label.required = value === "__additional__";
    form.elements.game_label.placeholder = value === "__additional__" ? "Full game name from intake" : "Exact label from intake";
  }
  form.elements.game.addEventListener("change", selectGame);
  function controls() {
    $("receivingAdminApprove").disabled = busy || !approvalReady();
    $("receivingAdminOAuthButton").disabled = busy || !freshMfa();
    $("receivingAdminReviewRefresh").disabled = busy || !preview || Date.now() >= previewDeadline;
    $("receivingAdminReviewMore").disabled = busy || !review || Date.now() >= previewDeadline;
    $("receivingAdminReviewMore").hidden = !review?.next_cursor;
    for (const entry of retirementControls) {
      const reason = entry.pending?.reason || entry.reason.value.trim();
      entry.button.disabled = busy || !currentPreview(entry.epoch, entry.revision, entry.target)
        || Date.now() >= previewDeadline || review?.target !== entry.target || review.conflict_exists !== true
        || !reviewBindings.has(entry.row.binding_id) || !freshMfa() || !entry.confirm.checked || reason.length < 10 || reason.length > 200;
      entry.form.inert = busy; entry.reason.readOnly = !!entry.pending;
      entry.confirm.disabled = busy || !!entry.pending;
      entry.button.textContent = entry.pending ? "Retry same retirement" : "Retire mapping";
    }
  }
  function discardPreview() {
    previewRevision++; reviewRequest++; clearTimeout(expiryTimer); expiryTimer = null;
    preview = review = null; previewDeadline = 0; reviewPages.clear(); reviewBindings.clear(); retirementControls.clear();
    $("receivingAdminReview").hidden = true; $("receivingAdminFacts").replaceChildren(); approveForm.reset();
    $("receivingAdminMappingReview").hidden = true; $("receivingAdminConflicts").replaceChildren(); reviewStatus(""); controls();
  }
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
    session = current; controls();
    return true;
  }
  async function api(path, body, epoch = generation) {
    if (body !== undefined && !(await sameOwner(epoch))) return null;
    const response = await fetch(path, {method: body === undefined ? "GET" : "POST", credentials: "same-origin", signal: controller.signal,
      headers: body === undefined ? {} : {"Content-Type": "application/json", "X-SS-CSRF": session?.csrf_token || ""}, body: body === undefined ? undefined : JSON.stringify(body)});
    const result = await response.json();
    if (epoch !== generation) return null;
    if (!response.ok) {
      if (response.status === 401) reset("Sign in to the workspace, then refresh your owner session.");
      else if (response.status === 403 && session && !(await sameOwner(epoch))) return null;
      const error = new Error(result.error || "This review could not be completed.");
      error.code = typeof result.code === "string" ? result.code : null; error.status = response.status; throw error;
    }
    if (path !== "/api/me" && session && !(await sameOwner(epoch))) return null;
    return result;
  }
  function setBusy(value) { busy = value; form.inert = value; approveForm.inert = value; $("receivingAdminOAuthForm").inert = value; $("receivingAdminRefresh").disabled = value; controls(); }
  function fact(dl, label, value) { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = label; dd.textContent = String(value); dl.append(dt, dd); }
  function validConflict(row) {
    const text = (value, limit) => typeof value === "string" && value.length > 0 && value.length <= limit;
    const identityKeys = new Set(["name", "game", "kind", "game_label", ...fields.map(field => field[0]), ...slabFields.map(field => field[0])]);
    return row && uuid.test(row.binding_id) && Number.isSafeInteger(row.version) && row.version > 0 && hash.test(row.identity_hash)
      && row.identity && typeof row.identity === "object" && !Array.isArray(row.identity) && text(row.identity.name, 200) && text(row.identity.game, 100)
      && ["card", "slab"].includes(row.identity.kind) && Object.entries(row.identity).every(([key, value]) => identityKeys.has(key) && text(value, 255))
      && text(row.retained_sku, 100) && text(row.location_id, 255) && Number.isSafeInteger(row.selling_cents) && row.selling_cents >= 0 && row.selling_cents <= 100000000;
  }
  function renderConflict(row) {
    const card = document.createElement("article"), heading = document.createElement("h4"), dl = document.createElement("dl");
    card.className = "selection"; heading.textContent = row.identity.name; card.append(heading, dl);
    for (const [key, value] of Object.entries(row.identity)) fact(dl, key === "game_label" ? "Game label from intake" : key.replaceAll("_", " "),
      key === "kind" ? value === "slab" ? "Graded slab" : "Raw card" : key === "game" ? gameChoices.get(value) || row.identity.game_label || value : value);
    const location = Array.from(form.elements.location_id.options).find(option => option.value === row.location_id);
    fact(dl, "Location", location?.textContent || row.location_id); fact(dl, "Location reference", row.location_id);
    fact(dl, "Retained SKU", row.retained_sku);
    fact(dl, "Saved USD price", new Intl.NumberFormat("en-US", {style: "currency", currency: "USD"}).format(row.selling_cents / 100));
    fact(dl, "Mapping version", row.version); fact(dl, "Mapping reference", row.binding_id);
    const retireForm = document.createElement("form"), reasonLabel = document.createElement("label"), reason = document.createElement("input");
    retireForm.className = "mapping-retirement"; reason.name = "retirement_reason"; reason.minLength = 10; reason.maxLength = 200;
    reason.required = true; reason.autocomplete = "off"; reasonLabel.append(document.createTextNode("Internal retirement reason"), reason);
    const confirmLabel = document.createElement("label"), confirm = document.createElement("input"), confirmation = document.createElement("span");
    confirmLabel.className = "check"; confirm.type = "checkbox"; confirm.name = "retirement_confirmed"; confirm.required = true;
    confirmation.textContent = "I reviewed this mapping and want to stop using it for new receiving."; confirmLabel.append(confirm, confirmation);
    const help = document.createElement("p"), button = document.createElement("button"), note = document.createElement("p");
    help.className = "help"; help.textContent = "Use a card or catalog reason. Keep customer and payment details out of this note.";
    button.type = "submit"; button.className = "quiet"; button.disabled = true; button.textContent = "Retire mapping";
    note.className = "status"; note.setAttribute("role", "status"); note.setAttribute("aria-live", "polite");
    retireForm.append(reasonLabel, confirmLabel, help, button, note); card.append(retireForm);
    const entry = {row, form:retireForm, reason, confirm, button, note, target:preview, epoch:generation, revision:previewRevision, pending:null};
    retirementControls.add(entry); retireForm.addEventListener("input", controls);
    retireForm.addEventListener("submit", event => { event.preventDefault(); retireMapping(entry); });
    $("receivingAdminConflicts").append(card);
  }
  async function retireMapping(entry) {
    const {epoch, revision, target, row} = entry;
    if (busy || !currentPreview(epoch, revision, target) || Date.now() >= previewDeadline || !entry.form.reportValidity()) return;
    setBusy(true);
    try {
      if (!(await sameOwner(epoch)) || !currentPreview(epoch, revision, target)) return;
      if (!freshMfa()) { entry.note.textContent = mfaGuidance(); return; }
      if (Date.now() >= previewDeadline || review?.target !== target || review.conflict_exists !== true || !reviewBindings.has(row.binding_id)) return;
      const reason = entry.reason.value.trim();
      if (!entry.confirm.checked || reason.length < 10 || reason.length > 200) return;
      if (!entry.pending) entry.pending = {request_key:crypto.randomUUID(), version:row.version, reason};
      const body = entry.pending; entry.note.textContent = "Retiring this mapping in Stock & Sleeve…";
      const result = await api(`/api/receiving/catalog-bindings/${encodeURIComponent(row.binding_id)}/revoke`, body, epoch);
      if (!result || !currentPreview(epoch, revision, target)) return;
      if (result.binding_id !== row.binding_id || result.version !== row.version || result.revoked !== true) throw new Error("The retirement result could not be verified. Retry the same request or refresh existing mappings.");
      progress("Mapping retired. Square items and stock are unchanged. Review any remaining mappings before separately approving this replacement.");
      await loadReview(false, target);
    } catch (error) {
      if (currentPreview(epoch, revision, target)) { entry.note.textContent = error.message + (entry.pending ? " Retry the same request or refresh existing mappings." : " Try again or refresh your owner session."); entry.note.classList.add("error"); }
    } finally { if (currentPreview(epoch, revision, target)) setBusy(false); }
  }
  async function loadReview(append = false, target = preview) {
    if (!target || preview !== target || !session) return;
    const epoch = generation, revision = previewRevision, request = ++reviewRequest, previous = review, cursor = append ? previous?.next_cursor : null;
    if (append && (!previous || !cursor)) return;
    review = null;
    if (!append) { reviewPages.clear(); reviewBindings.clear(); retirementControls.clear(); $("receivingAdminConflicts").replaceChildren(); }
    $("receivingAdminMappingReview").hidden = false; reviewStatus("Checking existing mappings…"); setBusy(true);
    try {
      if (Date.now() >= previewDeadline) throw new Error("This preview expired. Read the current Square target again.");
      const params = new URLSearchParams({limit: "25"}); if (cursor) params.set("cursor", cursor);
      const result = await api(`/api/receiving/catalog-bindings/previews/${encodeURIComponent(target.preview_id)}/review?${params}`, undefined, epoch);
      if (!result || !currentPreview(epoch, revision, target) || request !== reviewRequest) return;
      if (Date.now() >= previewDeadline) throw new Error("This preview expired. Read the current Square target again.");
      if (result.review_version !== 1 || result.preview_id !== target.preview_id || result.preview_hash !== target.preview_hash
        || typeof result.conflict_exists !== "boolean" || !Array.isArray(result.conflicts) || result.conflicts.length > 25
        || (result.next_cursor !== null && !uuid.test(result.next_cursor))
        || (!result.conflict_exists && (result.conflicts.length || result.next_cursor))
        || (append && result.conflict_exists !== previous.conflict_exists)) throw new Error("Existing mappings changed or could not be verified. Refresh existing mappings before continuing.");
      let last = cursor;
      for (const row of result.conflicts) {
        if (!validConflict(row) || reviewBindings.has(row.binding_id) || (last && row.binding_id <= last)) throw new Error("The mapping list could not be verified. Refresh existing mappings before continuing.");
        last = row.binding_id;
      }
      if (result.next_cursor && (result.conflicts.length !== 25 || result.next_cursor !== last || reviewPages.has(result.next_cursor))) throw new Error("The mapping page could not be verified. Refresh existing mappings before continuing.");
      for (const row of result.conflicts) { reviewBindings.add(row.binding_id); renderConflict(row); }
      if (cursor) reviewPages.add(cursor);
      review = {...result, target};
      reviewStatus(result.conflict_exists ? "An earlier or different physical mapping exists for this Square variation. Approval is unavailable. Review the saved mappings below, including their locations and retained SKUs."
        : freshMfa() ? "No conflicting card identity mappings were found for this preview. Confirm the physical identity to request approval. The server checks again when you submit."
        : "No conflicting card identity mappings were found. " + mfaGuidance(), result.conflict_exists);
    } catch (error) {
      if (currentPreview(epoch, revision, target) && request === reviewRequest) { review = null; reviewStatus(error.message + " Approval remains unavailable until a current mapping review succeeds.", true); }
    } finally { if (currentPreview(epoch, revision, target) && request === reviewRequest) setBusy(false); }
  }
  async function refresh() {
    reset("Checking your owner session…"); const epoch = generation;
    try {
      const me = await api("/api/me", undefined, epoch); if (!me || epoch !== generation) return;
      if (me.role !== "owner") { reset("Only the store owner can approve catalog mappings."); return; }
      session = me;
      $("receivingAdminOAuth").hidden = false; controls();
      if (!freshMfa()) $("receivingAdminOAuthStatus").textContent = mfaGuidance();
      const readiness = await api("/api/receiving/readiness", undefined, epoch); if (!readiness || epoch !== generation) return;
      const games = await api("/api/receiving/catalog-games", undefined, epoch); if (!games || epoch !== generation) return;
      if (!validGames(games)) throw new Error("Supported games could not be verified. Refresh the owner workspace before continuing.");
      gameChoices.clear(); const gameSelect = form.elements.game;
      gameSelect.replaceChildren(new Option("Choose a game or sport category", ""));
      for (const row of games.games) { gameChoices.set(row.id, row.label); gameSelect.append(new Option(row.label, row.id)); }
      gameSelect.append(new Option("Another game supported by your scanner", "__additional__"));
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
      controls();
      if (!freshMfa()) progress("You may preview a mapping. " + mfaGuidance());
    } catch (error) { if (epoch === generation) { reset("Owner review is unavailable. Check your session and Square authorization, then refresh."); progress(error.message, true); } }
  }
  form.addEventListener("input", () => { discardPreview(); setBusy(false); });
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (busy || !session) return; discardPreview(); const epoch = generation, revision = previewRevision, requestedAt = Date.now(); setBusy(true);
    const identity = {};
    for (const name of ["name", "game", "kind", "game_label", ...fields.map(row => row[0]), ...slabFields.map(row => row[0])]) {
      const value = form.elements[name].value.trim(); if (value) identity[name] = value;
    }
    try {
      if (identity.game === "__additional__") {
        const resolved = await api(`/api/receiving/catalog-games?${new URLSearchParams({name:identity.game_label || ""})}`, undefined, epoch);
        if (!resolved || epoch !== generation || revision !== previewRevision) return;
        if (!validGames(resolved) || !resolved.resolved || typeof resolved.resolved.game !== "string"
          || typeof resolved.resolved.game_label !== "string" || !resolved.resolved.game_label || resolved.resolved.game_label.length > 100
          || !(gameChoices.has(resolved.resolved.game) || /^other:[a-z0-9][a-z0-9-]{0,93}$/.test(resolved.resolved.game))) {
          throw new Error("The additional game could not be verified. Review its full name from intake.");
        }
        identity.game = resolved.resolved.game; identity.game_label = resolved.resolved.game_label;
      } else if (!gameChoices.has(identity.game)) throw new Error("Choose a supported game or enter another scanner-supported game.");
      const physicalFacts = Object.fromEntries(Object.entries(physicalFields).map(([field, source]) => [field, identity[source] || ""]));
      const result = await api("/api/receiving/catalog-bindings/previews", {identity, physical_ownership_version:2, physical_facts:physicalFacts,
        variation_id: form.elements.variation_id.value.trim(), location_id: form.elements.location_id.value}, epoch);
      if (!result || epoch !== generation || revision !== previewRevision) return;
      if (!uuid.test(result.preview_id) || !hash.test(result.preview_hash) || !Number.isSafeInteger(result.expires_in) || result.expires_in < 1 || result.expires_in > 900) throw new Error("The mapping preview could not be verified. Read the current Square target again.");
      const ownership = result.facts?.physical_ownership;
      if (!ownership || typeof ownership !== "object" || Array.isArray(ownership) || Object.keys(ownership).length !== 3
        || ownership.version !== 2 || typeof ownership.key !== "string" || !hash.test(ownership.key)
        || !ownership.facts || typeof ownership.facts !== "object" || Array.isArray(ownership.facts)
        || Object.keys(ownership.facts).length !== Object.keys(physicalFields).length
        || Object.entries(physicalFacts).some(([field, value]) => ownership.facts[field] !== value.normalize("NFC"))) {
        throw new Error("The full physical card details were not confirmed. Read the current Square target again.");
      }
      preview = result; previewDeadline = requestedAt + result.expires_in * 1000;
      expiryTimer = setTimeout(() => {
        if (!currentPreview(epoch, revision, result)) return;
        reviewRequest++; review = null; setBusy(false);
        reviewStatus("This preview expired. Read the current Square target again before approving.", true);
      }, Math.max(0, previewDeadline - Date.now()));
      const facts = result.facts, rows = [...Object.entries(facts.identity).map(([key, value]) => [key === "game_label" ? "Game label from intake" : key, key === "game" ? gameChoices.get(value) || facts.identity.game_label || value : value]), ["Retained SKU", facts.sku], ["Current USD price", (facts.selling_cents / 100).toFixed(2)], ["Square variation", facts.variation_id], ["Square parent", facts.parent_id], ["Receiving location", facts.location_id]];
      for (const [key, value] of rows) { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = key.replaceAll("_", " "); dd.textContent = String(value); $("receivingAdminFacts").append(dt, dd); }
      $("receivingAdminReview").hidden = false; progress("Current provider facts loaded. Review existing mappings before confirming the identity.");
      await loadReview(false, result);
    } catch (error) { if (epoch === generation && revision === previewRevision) progress(error.message, true); }
    finally { if (epoch === generation && revision === previewRevision) setBusy(false); }
  });
  $("receivingAdminOAuthForm").addEventListener("submit", async event => {
    event.preventDefault(); if (busy || !session || !event.currentTarget.elements.permissions_confirmed.checked) return;
    const epoch = generation; setBusy(true); discardPreview(); $("receivingAdminOAuthStatus").textContent = "Preparing the explicit Square receiving consent request…";
    try {
      if (!(await sameOwner(epoch))) return;
      if (!freshMfa()) { $("receivingAdminOAuthStatus").textContent = mfaGuidance(); return; }
      const result = await api("/api/square/connect", {profile: "receiving", receiving_permissions_confirmed: true}, epoch);
      if (!result || epoch !== generation) return;
      const url = new URL(result.authorization_url);
      if (url.protocol !== "https:" || !["connect.squareup.com", "connect.squareupsandbox.com"].includes(url.hostname) || url.pathname !== "/oauth2/authorize" || url.username || url.password) throw new Error("Square authorization returned an unsupported destination.");
      window.location.assign(url.href);
    } catch (error) { if (epoch === generation) $("receivingAdminOAuthStatus").textContent = error.message; }
    finally { if (epoch === generation) setBusy(false); }
  });
  approveForm.addEventListener("submit", async event => {
    event.preventDefault(); if (busy || !approvalReady()) return; const epoch = generation, revision = previewRevision, target = preview; setBusy(true);
    try {
      if (!(await sameOwner(epoch)) || !currentPreview(epoch, revision, target)) return;
      if (!approvalReady()) { reviewStatus("A current mapping review and fresh MFA proof are required. Refresh existing mappings or sign in again.", true); return; }
      const result = await api(`/api/receiving/catalog-bindings/${encodeURIComponent(target.preview_id)}/approve`, {request_key: crypto.randomUUID(), preview_hash: target.preview_hash, identity_confirmed: approveForm.elements.identity_confirmed.checked, reason: approveForm.elements.reason.value.trim()}, epoch);
      if (!result || !currentPreview(epoch, revision, target)) return;
      discardPreview(); progress("Catalog mapping approved in Stock & Sleeve. Square items, prices and stock were not changed.");
    } catch (error) {
      if (!currentPreview(epoch, revision, target)) return;
      review = null; progress(error.message, true); controls();
      if (error.code === "CATALOG_IDENTITY_CONFLICT") await loadReview(false, target);
      else {
        if (error.code === "MFA_STEP_UP_REQUIRED") session = {...session, mfa_fresh: false};
        reviewStatus("Approval could not be confirmed. Refresh existing mappings before continuing; sign in again if a fresh MFA proof is required.", true);
      }
    }
    finally { if (epoch === generation && (revision === previewRevision || !preview)) setBusy(false); }
  });
  $("receivingAdminReviewRefresh").addEventListener("click", () => { if (!busy) loadReview(); });
  $("receivingAdminReviewMore").addEventListener("click", () => { if (!busy) loadReview(true); });
  $("receivingAdminRefresh").addEventListener("click", refresh);
  window.addEventListener("storage", event => { if (event.key === "ss-auth-epoch") reset(); });
  try { const channel = new BroadcastChannel("ss-auth-transition"); channel.onmessage = () => reset(); } catch (_) { /* Unsupported browser: explicit same-session verification remains. */ }
  window.addEventListener("pageshow", event => { if (event.persisted) reset(); });
  refresh();
})();
