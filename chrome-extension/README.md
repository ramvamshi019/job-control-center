# JCC Autofill — Chrome extension

One-click autofill for job applications on the ATSes JCC crawls:
Greenhouse, Lever, Ashby, Workday, iCIMS, BambooHR, SmartRecruiters,
Workable.

## Why this vs the bookmarklet

The bookmarklet on Fast Apply works for simple forms but breaks on
OAuth-gated multi-step Workday flows. This proper extension:

- Runs on every ATS domain automatically (no drag-to-bookmark ritual)
- Uses LABEL-TEXT matching, not just input names — much more resilient
  when ATSes randomize their `<input id="q_9834_first">` names
- Handles React/Ember/Vue-controlled inputs correctly (native setter
  + input+change+blur events)
- Skips fields you already filled (no accidental overwrites)
- Fills Yes/No radios for the "authorized to work" / "need sponsorship"
  questions every ATS asks 20 different ways

## Install (2 min)

1. Open Chrome and go to `chrome://extensions/`
2. Toggle **Developer mode** ON (top right)
3. Click **Load unpacked**
4. Select the `chrome-extension/` folder from this repo
5. Pin the JCC icon to your toolbar for one-click access

## First-time setup

1. Click the JCC toolbar icon (opens the popup)
2. Click **⚙️ Edit profile**
3. Fill in your fields (name, email, phone, LinkedIn URL, GitHub URL,
   work-auth answers). All stored locally in Chrome — never sent
   anywhere.
4. Click **💾 Save profile**

## Using it

1. Open a job application page (e.g. `https://boards.greenhouse.io/stripe/jobs/12345`)
2. Click the JCC toolbar icon
3. Popup shows **"✅ Detected: Greenhouse form"** — click **⚡ Fill this application**
4. Fields populate; result shows how many filled + how many were already-filled (skipped)
5. Review — the extension deliberately leaves salary/EEO/demographic fields empty
   because those are strategic or personal, not something to automate
6. Submit yourself

## What DOESN'T get filled (on purpose)

- Salary expectations — strategic, always leave blank
- EEO/demographic questions — personal choice
- Cover letter body — should be per-job (see JCC Applied page for drafts)
- Resume file upload — always requires manual file picker

## Troubleshooting

- **"❌ Not a supported ATS page"** → the extension only injects on the
  hosts listed in `manifest.json`. If the employer uses their own custom
  form (not one of the ATSes above), we can't help.
- **Fill button worked but 0 fields filled** → the ATS may have renamed
  their labels. Open browser DevTools → Console, run
  `document.querySelectorAll('input,textarea,select').forEach(e => console.log(e.name || e.id, e.type))`
  and share the output — we can add new label patterns to `content.js`.
- **Wrong yes/no answer picked on a radio** → check the label wording in
  `FIELD_MAP` inside `content.js` and add a more specific regex.

## Privacy

- Profile is stored in `chrome.storage.local` on YOUR device.
- No network calls to any external server from this extension.
- No telemetry, no analytics.
- Open-source; audit `content.js` yourself in ~2 min.
