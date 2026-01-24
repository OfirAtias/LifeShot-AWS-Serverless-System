// LifeShot Lifeguard Dashboard (browser-side logic).
//
// Cosmetic refactor only:
// - Improves readability via spacing, section headers, and comments.
// - Does not change functionality or behavior.

// =============================================================================
// Config
// =============================================================================
const API_BASE_URL =
  window.API_BASE_URL || window.AUTH_BASE_URL || "";

const AUTH_BASE_URL = window.AUTH_BASE_URL || API_BASE_URL;

if (!API_BASE_URL) {
  console.warn("Missing API base URL. Set window.API_BASE_URL (via config.js).");
}

// =============================================================================
// Token storage (localStorage)
// =============================================================================


// Clear all stored tokens and expiry metadata.
function clearTokens() {
  localStorage.removeItem("ls_access_token");
  localStorage.removeItem("ls_id_token");
  localStorage.removeItem("ls_refresh_token");
  localStorage.removeItem("ls_expires_at");
}


// Read the stored access token.
function getAccessToken() {
  return localStorage.getItem("ls_access_token") || "";
}


// Read the stored ID token.
function getIdToken() {
  return localStorage.getItem("ls_id_token") || "";
}


// Determine whether the saved token expiry has passed.
function isTokenExpired() {
  const exp = Number(localStorage.getItem("ls_expires_at") || "0");
  if (!exp) return false;
  return Date.now() > exp - 15_000; // 15s safety window
}

// Choose the token that will be sent to API Gateway.
function getApiBearerToken() {
  const idt = getIdToken();
  if (idt) return idt;
  return getAccessToken();
}


// Build an Authorization header if token exists and is not expired.
function authHeader() {
  const token = getApiBearerToken();
  if (!token || isTokenExpired()) return {};
  return { Authorization: `Bearer ${token}` };
}

// =============================================================================
// State
// =============================================================================
let activeAlertsList = [];
let currentAlertIndex = 0;
let alertPollTimer = null;
let currentLightboxImages = [];
let currentLightboxIndex = 0;

// =============================================================================
// Helpers
// =============================================================================


// Normalize an event status into an uppercase string.
function normalizeStatus(s) {
  return String(s || "").toUpperCase();
}


// Parse a date string safely (returns epoch date on failure).
function parseDateSafe(s) {
  const d = new Date(s);
  return isNaN(d.getTime()) ? new Date(0) : d;
}


// Add a cache-busting query parameter to a URL.
function getSafeUrl(url) {
  if (!url) return "";
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}cb=${Date.now()}`;
}

// =============================================================================
// Auth (Lambda Auth)
// =============================================================================


// Call /auth/me to validate the current token and retrieve role/groups.
async function authMe() {
  // נבדוק "מי אני" על בסיס token שנשמר
  const idToken = getIdToken();
  if (!idToken || isTokenExpired()) return null;

  const res = await fetch(`${AUTH_BASE_URL}/auth/me`, {
    method: "GET",
    headers: { Authorization: `Bearer ${idToken}` },
    cache: "no-store",
  });

  const data = await res.json().catch(() => ({}));
  if (!res.ok) return null;
  return data; // { ok, role, groups, ... }
}


// Call /auth/logout (best-effort) to end the session.
async function authLogout() {
  await fetch(`${AUTH_BASE_URL}/auth/logout`, {
    method: "POST",
    cache: "no-store",
  }).catch(() => {});
}

// =============================================================================
// API fetch (adds Authorization to API Gateway)
// =============================================================================


// Fetch helper that injects Authorization headers and handles auth errors.
async function apiFetch(path, options = {}) {
  const headers = {
    ...(options.headers || {}),
    ...authHeader(),
  };

  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
    cache: "no-store",
  });

  if (res.status === 401 || res.status === 403) {
    console.warn(
      "Unauthorized from API (check authorizer token type / issuer / audience)",
      res.status
    );
    throw new Error(`Unauthorized (${res.status})`);
  }

  return res;
}


// Logout: stop polling, call auth endpoint, clear tokens, then redirect to login.
async function logout() {
  if (alertPollTimer) clearInterval(alertPollTimer);
  alertPollTimer = null;

  await authLogout();

  clearTokens();

  window.location.href = "../pages/login.html";
}

// =============================================================================
// Lifeguard logic
// =============================================================================


// Poll for OPEN events and display an alert overlay when present.
async function checkLiveAlerts() {
  const dashboard = document.getElementById("lifeguard-dashboard");
  if (!dashboard || dashboard.classList.contains("hidden")) return;

  try {
    const res = await apiFetch(`/events`);
    const data = await res.json();

    activeAlertsList = (Array.isArray(data) ? data : [])
      .filter((e) => normalizeStatus(e.status) === "OPEN" && e.warningImageUrl)
      .sort(
        (a, b) => parseDateSafe(b.created_at) - parseDateSafe(a.created_at)
      );

    const overlay = document.getElementById("emergency-overlay");
    const noAlertsState = document.getElementById("no-alerts-state");

    if (activeAlertsList.length > 0) {
      if (noAlertsState) noAlertsState.classList.add("hidden");
      overlay.classList.remove("hidden");
      overlay.classList.add("alert-card-pulse");

      if (currentAlertIndex >= activeAlertsList.length) currentAlertIndex = 0;

      if (!window.alertSoundPlayed) {
        new Audio("https://www.soundjay.com/buttons/beep-01a.mp3")
          .play()
          .catch(() => {});
        window.alertSoundPlayed = true;
      }

      renderCurrentAlert();
    } else {
      window.alertSoundPlayed = false;
      overlay.classList.add("hidden");
      overlay.classList.remove("alert-card-pulse");
      if (noAlertsState) noAlertsState.classList.remove("hidden");
    }
  } catch (e) {
    console.error("Monitoring Error:", e);
  }
}


// Render the currently selected alert card into the UI.
function renderCurrentAlert() {
  if (activeAlertsList.length === 0) return;
  const alertData = activeAlertsList[currentAlertIndex];

  const counter = document.getElementById("alert-counter");
  if (counter) {
    counter.innerText = `Alert ${currentAlertIndex + 1} of ${
      activeAlertsList.length
    }`;
  }

  const created = parseDateSafe(alertData.created_at);
  const timeEl = document.getElementById("display-time");
  if (timeEl) {
    timeEl.innerText = `${String(created.getHours()).padStart(2, "0")}:${String(
      created.getMinutes()
    ).padStart(2, "0")}`;
  }

  const imgBefore = document.getElementById("img-before");
  const imgAfter = document.getElementById("img-after");

  currentLightboxImages = [];
  const beforeUrl = getSafeUrl(alertData.prevImageUrl);
  const afterUrl = getSafeUrl(alertData.warningImageUrl);

  currentLightboxImages.push(beforeUrl);
  currentLightboxImages.push(afterUrl);

  if (imgBefore) {
    imgBefore.src = beforeUrl;
    imgBefore.onclick = () => openLightboxByIndex(0);
  }
  if (imgAfter) {
    imgAfter.src = afterUrl;
    imgAfter.onclick = () => openLightboxByIndex(1);
  }

  const closeBtn = document.getElementById("close-btn-id");
  if (closeBtn) closeBtn.onclick = () => dismissAlert(alertData.eventId);
}


// Navigate to the next alert in the active list.
function nextAlert() {
  if (activeAlertsList.length === 0) return;
  currentAlertIndex++;
  if (currentAlertIndex >= activeAlertsList.length) currentAlertIndex = 0;
  renderCurrentAlert();
}


// Navigate to the previous alert in the active list.
function prevAlert() {
  if (activeAlertsList.length === 0) return;
  currentAlertIndex--;
  if (currentAlertIndex < 0) currentAlertIndex = activeAlertsList.length - 1;
  renderCurrentAlert();
}


// Close an alert by PATCHing the event status to CLOSED.
async function dismissAlert(eventId) {
  try {
    await apiFetch(`/events`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eventId }),
    });
    checkLiveAlerts();
  } catch (err) {
    console.error(err);
  }
}

// =============================================================================
// Lightbox
// =============================================================================


// Open the lightbox with a specific image URL.
function openLightbox(src) {
  const lb = document.getElementById("image-lightbox");
  const img = document.getElementById("lightbox-image");
  if (lb && img) {
    img.src = src;
    lb.classList.add("active");
  }
}


// Open the lightbox for an image by its index in currentLightboxImages.
function openLightboxByIndex(index) {
  currentLightboxIndex = index;
  updateLightboxView();
}


// Update the lightbox DOM to reflect currentLightboxIndex.
function updateLightboxView() {
  const lb = document.getElementById("image-lightbox");
  const img = document.getElementById("lightbox-image");

  if (lb && img && currentLightboxImages.length > 0) {
    img.src = currentLightboxImages[currentLightboxIndex];
    lb.classList.add("active");

    // הצגת/הסתרת חיצים
    const prevBtn = document.getElementById("lightbox-prev");
    const nextBtn = document.getElementById("lightbox-next");
    const showArrows = currentLightboxImages.length > 1 ? "block" : "none";

    if (prevBtn) prevBtn.style.display = showArrows;
    if (nextBtn) nextBtn.style.display = showArrows;
  }
}


// Advance to the next lightbox image.
function nextLightboxImage() {
  if (currentLightboxImages.length === 0) return;
  currentLightboxIndex =
    (currentLightboxIndex + 1) % currentLightboxImages.length;
  updateLightboxView();
}


// Go back to the previous lightbox image.
function prevLightboxImage() {
  if (currentLightboxImages.length === 0) return;
  currentLightboxIndex =
    (currentLightboxIndex - 1 + currentLightboxImages.length) %
    currentLightboxImages.length;
  updateLightboxView();
}

// =============================================================================
// Bootstrap
// =============================================================================
document.addEventListener("DOMContentLoaded", async () => {
  // Lightbox wiring
  const lb = document.getElementById("image-lightbox");
  const img = document.getElementById("lightbox-image");
  const close = document.getElementById("lightbox-close");

  if (close)
    close.onclick = () => {
      lb.classList.remove("active");
      setTimeout(() => (img.src = ""), 300);
    };

  document.body.addEventListener("click", (e) => {
    if (e.target.tagName === "IMG" && e.target.closest("#emergency-overlay")) {
      openLightbox(e.target.src);
    }
  });

  // Role gate
  try {
  const me = await authMe();
  const r = String(me?.role || "").toLowerCase();
  currentUserRole = r;

  // Allow Lifeguard/Guard AND Admin
  if (!(me?.ok && (r === "guard" || r === "lifeguard" || r === "admin"))) {
    window.location.href = "../pages/login.html";
    return;
  }

  const dashboard = document.getElementById("lifeguard-dashboard");
  if (dashboard) dashboard.classList.remove("hidden");

  // ✅ show "Back to Admin" only if admin came here
  const backBtn = document.getElementById("btn-back-admin");
  if (backBtn && r === "admin") {
    backBtn.style.display = "inline-flex";
  }

  checkLiveAlerts();
  alertPollTimer = setInterval(checkLiveAlerts, 5000);
} catch {
  window.location.href = "../pages/login.html";
}


});
