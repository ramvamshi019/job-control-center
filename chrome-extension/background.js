// background.js -- service worker (Manifest V3)
// Currently just a message router. Kept minimal so we don't accumulate
// state in the worker (SWs can be evicted at any time).

chrome.runtime.onInstalled.addListener(() => {
  // On first install, seed default profile if the user has none yet.
  chrome.storage.local.get("profile", ({ profile }) => {
    if (!profile) {
      chrome.storage.local.set({
        profile: {
          firstName: "",
          lastName: "",
          fullName: "",
          email: "",
          phone: "",
          linkedin: "",
          github: "",
          portfolio: "",
          currentCompany: "",
          city: "",
          state: "",
          country: "United States",
          zip: "",
          address: "",
          yearsExperience: "2",
          workAuthYes: "Yes",           // "Yes, I am authorized to work"
          sponsorRequiredYes: "Yes",    // F-1 OPT typically needs sponsorship later
        }
      });
    }
  });
});
