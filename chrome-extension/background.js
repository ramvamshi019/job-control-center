// background.js -- service worker (Manifest V3)
// - Seeds default profile on first install.
// - Relays APPLY_SUBMITTED from content.js -> JCC backend /applications/log-external.
//   Cross-origin fetch from a content script hits CORS in Chrome, so the
//   POST lives here (extension service workers get to bypass CORS for
//   host_permissions-declared origins).
// - Shows a native chrome notification with the result so the user knows
//   the apply landed in JCC without opening the dashboard.

const DEFAULT_API = "http://localhost:8000";

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(["profile", "apiUrl"], ({ profile, apiUrl }) => {
    if (!profile) {
      chrome.storage.local.set({
        profile: {
          firstName: "", lastName: "", fullName: "",
          email: "", phone: "", linkedin: "",
          github: "", portfolio: "", currentCompany: "",
          city: "", state: "", country: "United States",
          zip: "", address: "",
          yearsExperience: "2",
          workAuthYes: "Yes",
          sponsorRequiredYes: "Yes",
        }
      });
    }
    if (!apiUrl) {
      chrome.storage.local.set({ apiUrl: DEFAULT_API });
    }
  });
});

// Handle apply-submitted messages from content.js. POSTs to the configured
// JCC backend and pops a chrome notification with the result.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type !== "APPLY_SUBMITTED") return;
  chrome.storage.local.get(["apiUrl"], async ({ apiUrl }) => {
    const base = (apiUrl || DEFAULT_API).replace(/\/+$/, "");
    try {
      const resp = await fetch(base + "/applications/log-external", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: msg.url,
          title: msg.title || null,
          company: msg.company || null,
          source: "chrome-extension",
        }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      const tag = data.matched ? "✅ Matched" : "➕ New";
      const title = `JCC · ${tag}: ${data.title?.slice(0, 60) || "?"}`;
      const body = `${data.company_name || "?"} · marked Applied (job #${data.job_id})`;
      chrome.notifications?.create({
        type: "basic",
        iconUrl: "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMDAgMTAwIj48Y2lyY2xlIGN4PSI1MCIgY3k9IjUwIiByPSI0NSIgZmlsbD0iIzFhN2YzNyIvPjx0ZXh0IHg9IjUwIiB5PSI2NSIgdGV4dC1hbmNob3I9Im1pZGRsZSIgZmlsbD0id2hpdGUiIGZvbnQtc2l6ZT0iNTAiIGZvbnQtd2VpZ2h0PSJib2xkIj7imJ48L3RleHQ+PC9zdmc+",
        title,
        message: body,
      });
      sendResponse({ ok: true, ...data });
    } catch (err) {
      chrome.notifications?.create({
        type: "basic",
        iconUrl: "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMDAgMTAwIj48Y2lyY2xlIGN4PSI1MCIgY3k9IjUwIiByPSI0NSIgZmlsbD0iI2I5MWMxYyIvPjwvc3ZnPg==",
        title: "JCC · log-external failed",
        message: (err.message || String(err)).slice(0, 160) +
                 "  (check API URL in popup / SSH tunnel active?)",
      });
      sendResponse({ ok: false, error: String(err) });
    }
  });
  return true; // keep the message channel open for async response
});
