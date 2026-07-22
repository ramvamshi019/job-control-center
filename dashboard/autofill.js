/* ---------------------------------------------------------------------------
 * Fast-Apply autofill bookmarklet (source).
 *
 * The dashboard's "⚡ Fast Apply" page substitutes __PROFILE_JSON__ with the
 * live /resume/profile payload and hands the result over as a bookmarklet, so
 * personal details are injected at render time and never committed to this repo.
 *
 * Scope, deliberately: it FILLS and then STOPS.
 *   - never clicks Submit/Apply — you review every application yourself
 *   - never touches file inputs (browsers forbid setting them from script, and
 *     the résumé upload is the one step worth eyeballing anyway)
 *   - never answers EEO/demographic questions (race, gender, veteran,
 *     disability) — those are yours to answer, not a script's
 *   - never handles passwords or creates accounts, which rules out Workday and
 *     most iCIMS flows by design
 *
 * Targets the form-based ATSs: SmartRecruiters, Greenhouse, Lever, Ashby.
 * Field matching is heuristic (label -> aria-label -> placeholder -> name/id)
 * because every ATS names things differently; unmatched fields are simply left
 * alone and reported, rather than guessed at.
 * ------------------------------------------------------------------------- */
(function () {
  var P = __PROFILE_JSON__;

  // Split the stored full name once: most forms want first/last separately.
  var parts = (P.name || "").trim().split(/\s+/);
  var first = parts[0] || "";
  var last = parts.length > 1 ? parts[parts.length - 1] : "";

  // value -> the patterns that should receive it, most specific first. Order
  // matters: "first name" must be tested before the generic "name".
  var RULES = [
    [first, [/\bfirst[\s_-]*name\b/, /\bgiven[\s_-]*name\b/, /\bfname\b/]],
    [last, [/\blast[\s_-]*name\b/, /\bfamily[\s_-]*name\b/, /\bsurname\b/, /\blname\b/]],
    [P.name, [/\bfull[\s_-]*name\b/, /\byour name\b/, /^name$/]],
    [P.email, [/\be-?mail\b/]],
    [P.phone, [/\bphone\b/, /\bmobile\b/, /\btelephone\b/, /\bcell\b/]],
    [P.linkedin, [/\blinked-?in\b/]],
    [P.location, [/\blocation\b/, /\bcity\b/, /\baddress\b/, /where are you based/]],
    [P.website, [/\bwebsite\b/, /\bportfolio\b/, /\bpersonal site\b/]],
    [P.github, [/\bgit-?hub\b/]]
  ];

  // Questions that must stay untouched — see the header note.
  var SKIP = /\b(gender|race|ethnic|veteran|disab|sexual|pronoun|salary|compensation|password|ssn|social security)\b/;

  /* Every scrap of text that might name this field, lowercased. */
  function labelFor(el) {
    var bits = [el.name, el.id, el.placeholder, el.getAttribute("aria-label"),
                el.getAttribute("autocomplete")];
    if (el.id) {
      var l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) bits.push(l.innerText);
    }
    var wrap = el.closest("label, .field, .application-field, [class*=field]");
    if (wrap) bits.push((wrap.innerText || "").slice(0, 120));
    return bits.filter(Boolean).join(" ").toLowerCase();
  }

  /* Set through the native setter so React/Vue-based ATSs register the change. */
  function setValue(el, val) {
    var proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, val);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.style.outline = "2px solid #16a34a";
  }

  var filled = [], skipped = [], untouched = [];
  var els = document.querySelectorAll("input, textarea");

  for (var i = 0; i < els.length; i++) {
    var el = els[i];
    var t = (el.type || "").toLowerCase();
    if (t === "file" || t === "hidden" || t === "submit" || t === "button" ||
        t === "checkbox" || t === "radio" || t === "password") continue;
    if (el.disabled || el.readOnly || !el.offsetParent) continue;
    if (el.value && el.value.trim()) continue;   // never clobber your own edits

    var lab = labelFor(el);
    if (!lab) continue;
    if (SKIP.test(lab)) { skipped.push(lab.slice(0, 40)); continue; }

    var hit = false;
    for (var r = 0; r < RULES.length && !hit; r++) {
      var val = RULES[r][0], pats = RULES[r][1];
      if (!val) continue;
      for (var p = 0; p < pats.length; p++) {
        if (pats[p].test(lab)) { setValue(el, val); filled.push(lab.slice(0, 40)); hit = true; break; }
      }
    }
    if (!hit) untouched.push(lab.slice(0, 40));
  }

  var msg = "✅ Filled " + filled.length + " field(s).\n" +
            "📎 Upload your résumé manually — file inputs can't be scripted.\n" +
            (skipped.length ? "⏭️  Left " + skipped.length + " demographic/sensitive field(s) for you.\n" : "") +
            (untouched.length ? "❓ " + untouched.length + " field(s) unrecognised — check before submitting:\n   • "
              + untouched.slice(0, 6).join("\n   • ") + "\n" : "") +
            "\n⚠️  Nothing was submitted. Review everything, then click Submit yourself.";
  alert(msg);
})();
