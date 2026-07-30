// content.js -- runs on every supported ATS form page
// Handles: field detection + one-click autofill on message from popup.
//
// Design notes:
// - Uses LABEL-TEXT matching (not just input names/ids) because ATS
//   customize field IDs unpredictably across employers. Text like "First
//   name" / "Email" is much more stable than `<input id="q_9834_first">`.
// - React/Ember/Vue inputs need their internal state updated via a
//   native setter + input+change events -- not just `element.value = x`.
//   `setNativeValue()` handles that.
// - Skip fields that ALREADY have a value (avoids overwriting the user's
//   edits when they re-click the button).

(() => {
  // ---- FIELD MAPPING ----------------------------------------------------
  // Each entry: [label-substring-regex, profileKey]. First hit wins.
  // Case-insensitive. `\b` word boundaries protect against partial matches
  // ("email" should NOT match "email preferences", but SHOULD match
  // "email address").
  const FIELD_MAP = [
    [/\b(first name|given name)\b/i,                       "firstName"],
    [/\b(last name|surname|family name)\b/i,               "lastName"],
    [/\bfull name\b/i,                                     "fullName"],
    [/\b(email address|email)\b/i,                         "email"],
    [/\b(phone|mobile|cell)\b/i,                           "phone"],
    [/\b(linkedin|linked ?in url|linked ?in profile)\b/i,  "linkedin"],
    [/\b(github|git ?hub url|git ?hub profile)\b/i,        "github"],
    [/\b(portfolio|website|personal site)\b/i,             "portfolio"],
    [/\b(current company|employer)\b/i,                    "currentCompany"],
    [/\b(city|current city)\b/i,                           "city"],
    [/\b(state|province|region)\b/i,                       "state"],
    [/\b(country)\b/i,                                     "country"],
    [/\b(zip|postal|postcode)\b/i,                         "zip"],
    [/\b(address|street)\b/i,                              "address"],
    // Work-authorization Q&A (each ATS phrases this 20 ways -- we cover the
    // top ~5 patterns). Values map to answerYesTo/answerNoTo below.
    [/\b(authorized to work|legally authorized|work authorization|right to work)\b/i, "workAuthYes"],
    [/\b(require sponsorship|need sponsorship|require.*visa|visa sponsorship)\b/i,    "sponsorRequiredYes"],
    [/\b(over 18|older than 18|18 years or older)\b/i,     "workAuthYes"],
    // Salary expectations -- deliberately blank; STRATEGIC field.
    // Total years of experience
    [/\b(years of experience|total experience|professional experience)\b/i, "yearsExperience"],
  ];

  // ---- INPUT-SETTER HELPERS ---------------------------------------------
  // React/Ember inputs ignore `el.value = x` -- they only re-render if
  // you fire the internal setter + dispatch events. This handles all
  // major frameworks.
  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  function setSelectValue(el, value) {
    // Prefer exact match, then case-insensitive contains
    let match = Array.from(el.options).find(o => o.value === value || o.text === value);
    if (!match) {
      const v = value.toLowerCase();
      match = Array.from(el.options).find(o =>
        o.value.toLowerCase().includes(v) || o.text.toLowerCase().includes(v)
      );
    }
    if (match) {
      el.value = match.value;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    return false;
  }

  // ---- LABEL DISCOVERY --------------------------------------------------
  // For each input, find the most-likely human label. Handles: <label for>,
  // parent <label>, aria-label, aria-labelledby, placeholder, name attr.
  function labelFor(el) {
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label");
    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      const ref = document.getElementById(labelledby);
      if (ref) return ref.innerText;
    }
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return lbl.innerText;
    }
    const parentLabel = el.closest("label");
    if (parentLabel) return parentLabel.innerText;
    // Fall back to preceding label sibling
    let sib = el.previousElementSibling;
    while (sib && !sib.matches("label")) sib = sib.previousElementSibling;
    if (sib) return sib.innerText;
    return el.placeholder || el.name || "";
  }

  // ---- CORE AUTOFILL LOOP ----------------------------------------------
  function findProfileKey(labelText) {
    for (const [rx, key] of FIELD_MAP) {
      if (rx.test(labelText)) return key;
    }
    return null;
  }

  function fillFieldsWith(profile) {
    let filled = 0, skipped = 0;
    const inputs = document.querySelectorAll(
      "input[type=text],input[type=email],input[type=tel],input[type=url]," +
      "input:not([type]),textarea,select"
    );
    for (const el of inputs) {
      if (el.disabled || el.readOnly) continue;
      // Skip fields the user already filled
      if (el.value && el.value.trim() && el.tagName !== "SELECT") {
        skipped++;
        continue;
      }
      const label = labelFor(el);
      if (!label) continue;
      const key = findProfileKey(label);
      if (!key) continue;
      const value = profile[key];
      if (!value) continue;
      try {
        if (el.tagName === "SELECT") {
          if (setSelectValue(el, value)) filled++;
        } else {
          setNativeValue(el, value);
          filled++;
        }
      } catch (e) { /* skip on error */ }
    }

    // Handle Yes/No radio buttons for work-auth questions
    const radioGroups = new Map();
    document.querySelectorAll("input[type=radio]").forEach(r => {
      const name = r.name || r.closest("fieldset,label")?.textContent || "";
      if (!radioGroups.has(name)) radioGroups.set(name, []);
      radioGroups.get(name).push(r);
    });
    for (const [name, radios] of radioGroups) {
      if (!radios.length) continue;
      // Look up label in field map -- if it matches workAuthYes or sponsorRequiredYes,
      // pick the corresponding Yes/No radio.
      const groupLabel = labelFor(radios[0]) + " " + name;
      const key = findProfileKey(groupLabel);
      if (!key) continue;
      const wantYes = (key === "workAuthYes") ||
                      (key === "sponsorRequiredYes" && profile.sponsorRequiredYes === "Yes");
      const targetValue = wantYes ? "yes" : "no";
      const target = radios.find(r => {
        const v = (r.value || "").toLowerCase();
        const lbl = (labelFor(r) || "").toLowerCase();
        return v === targetValue || lbl.startsWith(targetValue) || lbl === targetValue;
      });
      if (target && !target.checked) {
        target.click();
        filled++;
      }
    }

    return { filled, skipped };
  }

  // ---- Q&A MEMORY -------------------------------------------------------
  // Learn from what the user types on unmapped fields, replay it next time
  // the same question appears anywhere. Storage key: `qa_memory` in
  // chrome.storage.local, shape { normalized_label: {value, kind, ts} }.
  //
  // Kinds:
  //   'text'    string value
  //   'select'  option label
  //   'radio'   selected radio's label text
  //   'check'   'yes' | 'no' (checkbox)
  //
  // Question normalization: lowercase, collapse whitespace, strip trailing
  // punctuation, drop the "* required" marker, cap at 140 chars. So
  // "Are you willing to relocate? *" and "Are you  willing to relocate?"
  // collide to the same key.
  function normQuestion(s) {
    return (s || "")
      .replace(/\s+/g, " ")
      .replace(/[*••]+$/g, "")
      .replace(/\s*(required|optional)\s*$/i, "")
      .replace(/[?:!.]+$/, "")
      .trim()
      .toLowerCase()
      .slice(0, 140);
  }

  async function loadQAMemory() {
    return new Promise(r =>
      chrome.storage.local.get("qa_memory", ({ qa_memory }) => r(qa_memory || {}))
    );
  }
  async function saveQAEntry(key, entry) {
    const mem = await loadQAMemory();
    mem[key] = { ...entry, ts: Date.now() };
    return new Promise(r => chrome.storage.local.set({ qa_memory: mem }, r));
  }

  // Capture user answers as they change fields. Only saves fields that
  // aren't in the profile FIELD_MAP — those are already handled by the
  // structured profile so no need to duplicate. Debounced on text inputs.
  const _saveTimers = new WeakMap();
  function attachQACapture() {
    // Text/select fields
    document.querySelectorAll("input,textarea,select").forEach(el => {
      if (el.disabled || el.readOnly || el.type === "hidden") return;
      if (el.type === "file" || el.type === "submit" || el.type === "button") return;
      if (el.__jccQACaptured) return;
      el.__jccQACaptured = true;
      const kind = el.tagName === "SELECT" ? "select"
        : el.type === "radio" ? "radio"
        : el.type === "checkbox" ? "check"
        : "text";
      const handler = () => {
        const label = labelFor(el);
        if (!label) return;
        // Skip if this label matches a profile field — the profile owns it.
        if (findProfileKey(label)) return;
        const key = normQuestion(label);
        if (!key || key.length < 3) return;
        let value;
        if (kind === "radio") {
          if (!el.checked) return;
          value = (labelFor(el) || el.value || "").trim();
        } else if (kind === "check") {
          value = el.checked ? "yes" : "no";
        } else if (kind === "select") {
          const opt = el.options[el.selectedIndex];
          value = opt ? (opt.textContent || opt.value).trim() : "";
        } else {
          value = (el.value || "").trim();
        }
        if (!value) return;
        // Debounce text input so we don't save on every keystroke
        if (kind === "text") {
          clearTimeout(_saveTimers.get(el));
          _saveTimers.set(el, setTimeout(() => saveQAEntry(key, { value, kind }), 900));
        } else {
          saveQAEntry(key, { value, kind });
        }
      };
      el.addEventListener("change", handler);
      if (kind === "text") el.addEventListener("blur", handler);
    });
  }

  // Replay memory into the current page — called at end of fillFieldsWith().
  async function replayQAMemory() {
    const mem = await loadQAMemory();
    let replayed = 0;
    const inputs = document.querySelectorAll(
      "input:not([type=file]):not([type=submit]):not([type=button]):not([type=hidden]),textarea,select"
    );
    // Handle non-radio elements
    for (const el of inputs) {
      if (el.disabled || el.readOnly) continue;
      if (el.type === "radio") continue;
      // Skip if user or profile already filled
      if (el.type === "checkbox") {
        if (el.checked) continue;
      } else if (el.tagName !== "SELECT" && el.value && el.value.trim()) {
        continue;
      }
      const label = labelFor(el);
      if (!label || findProfileKey(label)) continue;
      const entry = mem[normQuestion(label)];
      if (!entry) continue;
      try {
        if (el.tagName === "SELECT") {
          if (setSelectValue(el, entry.value)) replayed++;
        } else if (el.type === "checkbox") {
          if (entry.value === "yes" && !el.checked) { el.click(); replayed++; }
        } else {
          setNativeValue(el, entry.value);
          replayed++;
        }
      } catch (e) { /* skip */ }
    }
    // Radio groups: match saved label text to a radio's label
    const groups = new Map();
    document.querySelectorAll("input[type=radio]").forEach(r => {
      const name = r.name || "";
      if (!name) return;
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(r);
    });
    for (const [name, radios] of groups) {
      if (radios.some(r => r.checked)) continue;
      const groupLabel = labelFor(radios[0]);
      if (!groupLabel || findProfileKey(groupLabel)) continue;
      const entry = mem[normQuestion(groupLabel)];
      if (!entry) continue;
      const want = (entry.value || "").toLowerCase();
      const target = radios.find(r => {
        const l = (labelFor(r) || r.value || "").trim().toLowerCase();
        return l === want || l.startsWith(want) || want.startsWith(l);
      });
      if (target) { target.click(); replayed++; }
    }
    return replayed;
  }

  // Attach capture on load + on any DOM mutation (SPAs mount fields late).
  attachQACapture();
  new MutationObserver(() => attachQACapture())
    .observe(document.body || document.documentElement, { subtree: true, childList: true });

  // ---- MESSAGE HANDLER (from popup) ------------------------------------
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === "AUTOFILL") {
      // Profile fields first, then Q&A memory for the custom questions
      const result = fillFieldsWith(msg.profile);
      replayQAMemory().then(async replayed => {
        // Then AI-suggest for what neither of those covered (bounded — see
        // suggestUnknownFields for the safeguards).
        const aiCount = await suggestUnknownFields(msg.profile).catch(() => 0);
        sendResponse({ ...result, replayed, ai_suggested: aiCount });
      });
      return true;   // keep the message channel open
    }
    if (msg.type === "PING") {
      sendResponse({ ok: true, host: window.location.host });
      return true;
    }
  });

  // ---- AI-SUGGEST FALLBACK ---------------------------------------------
  // For any field that (a) isn't in the profile FIELD_MAP, (b) isn't in
  // Q&A memory, and (c) is still empty — send the question + options + a
  // trimmed profile to backend /apply/suggest_answer for a Claude pick.
  // Marked with a soft yellow outline so Ram can see AI-filled vs
  // profile-filled at a glance.
  //
  // Rate limits: at most 8 AI calls per page load (typical form has 3-5
  // unknowns), never fires for questions we've asked before this page
  // load (dedup within-page). Silent no-op if API URL isn't reachable.
  async function suggestUnknownFields(profile) {
    let apiUrl;
    try {
      apiUrl = await new Promise(r =>
        chrome.storage.local.get("apiUrl", ({ apiUrl }) => r(apiUrl || "")));
    } catch (e) { return 0; }
    if (!apiUrl) return 0;
    const base = apiUrl.replace(/\/+$/, "");
    const mem = await loadQAMemory();

    const asked = new Set();
    const collectRadioGroups = () => {
      const groups = new Map();
      document.querySelectorAll("input[type=radio]").forEach(r => {
        const name = r.name || "";
        if (!name) return;
        if (!groups.has(name)) groups.set(name, []);
        groups.get(name).push(r);
      });
      return groups;
    };

    let n = 0;
    const MAX = 8;
    const jobTitle = pickJobTitle();
    const companyName = pickCompanyName();

    // Text/textarea/select unknowns
    const inputs = document.querySelectorAll(
      "input:not([type=file]):not([type=submit]):not([type=button])" +
      ":not([type=hidden]):not([type=radio]):not([type=checkbox]),textarea,select"
    );
    for (const el of inputs) {
      if (n >= MAX) break;
      if (el.disabled || el.readOnly) continue;
      if (el.tagName !== "SELECT" && el.value && el.value.trim()) continue;
      if (el.tagName === "SELECT" && el.value && el.selectedIndex > 0) continue;
      const label = labelFor(el);
      if (!label) continue;
      if (findProfileKey(label)) continue;
      const key = normQuestion(label);
      if (!key || asked.has(key)) continue;
      if (mem[key]) continue;   // memory already handled
      asked.add(key);
      let options = null;
      if (el.tagName === "SELECT") {
        options = Array.from(el.options)
          .map(o => (o.textContent || o.value || "").trim())
          .filter(Boolean).slice(0, 60);
      }
      const suggestion = await callSuggest(base, {
        question: label.trim().slice(0, 300),
        options, kind: el.tagName === "SELECT" ? "select" : "text",
        job_title: jobTitle, company: companyName, profile,
      });
      if (!suggestion?.answer) continue;
      try {
        if (el.tagName === "SELECT") {
          if (setSelectValue(el, suggestion.answer)) { markAI(el); n++; }
        } else {
          setNativeValue(el, suggestion.answer);
          markAI(el);
          n++;
        }
      } catch (e) { /* skip */ }
    }

    // Radio groups
    for (const [name, radios] of collectRadioGroups()) {
      if (n >= MAX) break;
      if (radios.some(r => r.checked)) continue;
      const groupLabel = labelFor(radios[0]);
      if (!groupLabel) continue;
      if (findProfileKey(groupLabel)) continue;
      const key = normQuestion(groupLabel);
      if (!key || asked.has(key) || mem[key]) continue;
      asked.add(key);
      const options = radios
        .map(r => (labelFor(r) || r.value || "").trim())
        .filter(Boolean);
      const suggestion = await callSuggest(base, {
        question: groupLabel.trim().slice(0, 300),
        options, kind: "radio",
        job_title: jobTitle, company: companyName, profile,
      });
      if (!suggestion?.answer) continue;
      const want = suggestion.answer.trim().toLowerCase();
      const target = radios.find(r => {
        const l = (labelFor(r) || r.value || "").trim().toLowerCase();
        return l === want || l.startsWith(want) || want.startsWith(l);
      });
      if (target) { target.click(); markAI(target); n++; }
    }
    return n;
  }

  async function callSuggest(base, body) {
    try {
      const resp = await fetch(base + "/apply/suggest_answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) return null;
      return await resp.json();
    } catch (e) { return null; }
  }

  function markAI(el) {
    // Soft yellow border so Ram can eyeball what Claude picked before submit
    try {
      el.style.outline = "2px solid #f59e0b";
      el.style.outlineOffset = "1px";
      el.title = (el.title || "") + " · [AI-suggested — review before submit]";
    } catch (e) { /* skip */ }
  }

  // ---- AUTO-LOG-APPLIED ON SUBMIT --------------------------------------
  // Fires ONCE per page load when the user's submit lands. Sends
  // APPLY_SUBMITTED to background.js which POSTs /applications/log-external.
  // Guarded so re-clicking a button (validation error, etc) doesn't spam.
  let applyLogged = false;

  function pickJobTitle() {
    // Try common ATS patterns for the posting title on the current page.
    const sel = [
      'h1.app-title', 'h1[data-qa="posting-title"]', 'h1.posting-headline',
      'div.job-title', 'h1.headline', 'h2.header-large', 'h1',
    ];
    for (const s of sel) {
      const el = document.querySelector(s);
      if (el && el.textContent && el.textContent.trim().length > 3) {
        return el.textContent.trim().slice(0, 200);
      }
    }
    return document.title.trim().slice(0, 200);
  }

  function pickCompanyName() {
    // Meta tags are the most reliable for company name across ATSes.
    const og = document.querySelector('meta[property="og:site_name"]');
    if (og?.content) return og.content.trim().slice(0, 120);
    const app = document.querySelector('meta[name="application-name"]');
    if (app?.content) return app.content.trim().slice(0, 120);
    // Fall back to subdomain — greenhouse.io/{company}, jobs.lever.co/{co}
    const host = window.location.host.toLowerCase();
    const path = window.location.pathname.split("/").filter(Boolean);
    if (host.includes("greenhouse") || host.includes("lever") ||
        host.includes("ashby") || host.includes("smartrecruiters")) {
      return (path[0] || host).slice(0, 120);
    }
    return "";
  }

  function fireApplyLogged() {
    if (applyLogged) return;
    applyLogged = true;
    const payload = {
      type: "APPLY_SUBMITTED",
      url: window.location.href,
      title: pickJobTitle(),
      company: pickCompanyName(),
    };
    try {
      chrome.runtime.sendMessage(payload);
    } catch (e) {
      // Extension context invalidated (background reload) — ignore.
    }
  }

  // Heuristic detection — ATSes have wildly different DOMs so we cast a
  // wide net. Any of these fires the log:
  //   (1) A native <form> submit event (works on most Greenhouse/Lever/Ashby)
  //   (2) A click on a button whose text matches /submit|apply|send application/i
  //   (3) URL changes to include /confirmation|/thank|/success (SPA post-nav)
  document.addEventListener("submit", (e) => {
    // Only count submissions on FORMS that contain an email or resume input
    // — filters out search-bar submits, cookie-banner acks, etc.
    const form = e.target;
    if (form?.tagName !== "FORM") return;
    const hasApplyFields = !!form.querySelector(
      'input[type="email"], input[type="file"], input[name*="resume" i]'
    );
    if (hasApplyFields) fireApplyLogged();
  }, true);

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("button, input[type='submit'], a[role='button']");
    if (!btn) return;
    const label = (btn.textContent || btn.value || "").trim().toLowerCase();
    if (/^(submit application|submit|send application|apply now)$/i.test(label)) {
      // Delay slightly so validation errors have a chance to cancel — a
      // confirmation URL change (below) will catch it if it goes through.
      setTimeout(fireApplyLogged, 1500);
    }
  }, true);

  let lastUrl = window.location.href;
  new MutationObserver(() => {
    if (window.location.href === lastUrl) return;
    lastUrl = window.location.href;
    if (/(confirmation|thank[-_ ]?you|success|submitted|complete)/i
        .test(window.location.pathname + window.location.search)) {
      fireApplyLogged();
    }
  }).observe(document, { subtree: true, childList: true });
})();
