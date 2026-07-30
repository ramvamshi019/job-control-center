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

  // ---- MESSAGE HANDLER (from popup) ------------------------------------
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === "AUTOFILL") {
      const result = fillFieldsWith(msg.profile);
      sendResponse(result);
      return true;
    }
    if (msg.type === "PING") {
      sendResponse({ ok: true, host: window.location.host });
      return true;
    }
  });
})();
