// popup.js -- controls the extension popup UI
// On open: pings the active tab's content script to see if we recognize
// the ATS. Displays status accordingly. On button click, sends AUTOFILL
// message with the user's profile.

const PROFILE_KEYS = [
  ["firstName",         "First name"],
  ["lastName",          "Last name"],
  ["fullName",          "Full name"],
  ["email",             "Email"],
  ["phone",             "Phone"],
  ["linkedin",          "LinkedIn URL"],
  ["github",            "GitHub URL"],
  ["portfolio",         "Portfolio URL"],
  ["currentCompany",    "Current company"],
  ["city",              "City"],
  ["state",             "State"],
  ["country",           "Country"],
  ["zip",               "ZIP"],
  ["address",           "Address"],
  ["yearsExperience",   "Years exp"],
  ["workAuthYes",       "Auth to work (Yes/No)"],
  ["sponsorRequiredYes","Need sponsorship (Yes/No)"],
];

// ATS name -> friendly label. host substring matching.
const ATS_LABELS = [
  ["greenhouse.io",       "Greenhouse"],
  ["lever.co",            "Lever"],
  ["ashbyhq.com",         "Ashby"],
  ["myworkdayjobs.com",   "Workday"],
  ["icims.com",           "iCIMS"],
  ["bamboohr.com",        "BambooHR"],
  ["smartrecruiters.com", "SmartRecruiters"],
  ["workable.com",        "Workable"],
];

const el = (id) => document.getElementById(id);

async function currentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function checkPage() {
  const tab = await currentTab();
  if (!tab) return;
  try {
    const resp = await chrome.tabs.sendMessage(tab.id, { type: "PING" });
    const label = ATS_LABELS.find(([host]) => resp.host?.includes(host));
    if (label) {
      el("status").className = "status ok";
      el("status").textContent = `✅ Detected: ${label[1]} form`;
      el("fill-btn").disabled = false;
    } else {
      el("status").className = "status warn";
      el("status").textContent = `⚠️ Unknown host: ${resp.host || "?"} — fill may miss fields`;
      el("fill-btn").disabled = false;  // still let them try
    }
  } catch (e) {
    el("status").className = "status err";
    el("status").textContent = "❌ Not a supported ATS page. Open a job application first.";
    el("fill-btn").disabled = true;
  }
}

async function fill() {
  const { profile } = await chrome.storage.local.get("profile");
  if (!profile || !profile.email) {
    el("status").className = "status err";
    el("status").textContent = "❌ Profile is empty — click ⚙️ Edit profile first.";
    return;
  }
  const tab = await currentTab();
  const resp = await chrome.tabs.sendMessage(tab.id, { type: "AUTOFILL", profile });
  el("result").style.display = "block";
  el("result").innerHTML = `Filled <b>${resp.filled}</b> field(s) · Skipped <b>${resp.skipped}</b> already-filled`;
}

async function showSettings() {
  const { profile } = await chrome.storage.local.get("profile");
  const container = el("profile-fields");
  container.innerHTML = "";
  for (const [key, label] of PROFILE_KEYS) {
    const div = document.createElement("div");
    div.className = "field";
    const lbl = document.createElement("label");
    lbl.textContent = label;
    const input = document.createElement("input");
    input.type = "text";
    input.dataset.key = key;
    input.value = (profile && profile[key]) || "";
    div.appendChild(lbl);
    div.appendChild(input);
    container.appendChild(div);
  }
  el("settings-panel").style.display = "block";
  el("settings-panel").open = true;
}

async function saveProfile() {
  const profile = {};
  document.querySelectorAll("#profile-fields input").forEach(inp => {
    profile[inp.dataset.key] = inp.value.trim();
  });
  await chrome.storage.local.set({ profile });
  el("status").className = "status ok";
  el("status").textContent = "✅ Profile saved. Reopen popup to refresh.";
}

// Wire everything up
el("fill-btn").addEventListener("click", fill);
el("settings-btn").addEventListener("click", showSettings);
el("save-btn").addEventListener("click", saveProfile);
checkPage();
