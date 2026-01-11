// ===============================
// CONFIG
// ===============================
const API_BASE_URL =
  window.API_BASE_URL ||
  "https://zat8d5ozy1.execute-api.us-east-1.amazonaws.com";

const AUTH_BASE_URL =
  window.AUTH_BASE_URL || "https://YOUR_AUTH_FUNCTION_URL.on.aws";

// ===============================
// TOKEN STORAGE (LOCALSTORAGE)
// ===============================
function clearTokens() {
  localStorage.removeItem("ls_access_token");
  localStorage.removeItem("ls_id_token");
  localStorage.removeItem("ls_refresh_token");
  localStorage.removeItem("ls_expires_at");
}

function getAccessToken() {
  return localStorage.getItem("ls_access_token") || "";
}

function getIdToken() {
  return localStorage.getItem("ls_id_token") || "";
}

function isTokenExpired() {
  const exp = Number(localStorage.getItem("ls_expires_at") || "0");
  if (!exp) return false; // אם אין expires, לא חוסמים
  return Date.now() > exp - 15_000; // 15s safety window
}

// ✅ IMPORTANT: API Gateway JWT/Cognito authorizer בדרך כלל מצפה ל-ID token
function getApiBearerToken() {
  const idt = getIdToken();
  if (idt) return idt;
  return getAccessToken();
}

function authHeader() {
  const token = getApiBearerToken();
  if (!token || isTokenExpired()) return {};
  return { Authorization: `Bearer ${token}` };
}

// ===============================
// STATE
// ===============================
let activeAlertsList = [];
let currentAlertIndex = 0;
let alertPollTimer = null;
let currentLightboxImages = [];
let currentLightboxIndex = 0;

// ===============================
// HELPERS
// ===============================
function normalizeStatus(s) {
  return String(s || "").toUpperCase();
}

function parseDateSafe(s) {
  const d = new Date(s);
  return isNaN(d.getTime()) ? new Date(0) : d;
}

function getSafeUrl(url) {
  if (!url) return "";
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}cb=${Date.now()}`;
}

// ===============================
// AUTH (Lambda Auth)
// ===============================
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

async function authLogout() {
  await fetch(`${AUTH_BASE_URL}/auth/logout`, {
    method: "POST",
    cache: "no-store",
  }).catch(() => {});
}

// ===============================
// API FETCH (adds Authorization to API Gateway)
// ===============================
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

  // ✅ בזמן בדיקה לא עושים logout אוטומטי - כדי שתראה מה חוזר ב-Network
  if (res.status === 401 || res.status === 403) {
    console.warn(
      "Unauthorized from API (check authorizer token type / issuer / audience)",
      res.status
    );
    throw new Error(`Unauthorized (${res.status})`);
  }

  return res;
}

async function logout() {
  if (alertPollTimer) clearInterval(alertPollTimer);
  alertPollTimer = null;

  await authLogout();

  clearTokens();

  window.location.href = "../pages/login.html";
}

// ===============================
// LIFEGUARD LOGIC
// ===============================
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

  if (imgBefore) imgBefore.src = getSafeUrl(alertData.prevImageUrl);
  if (imgAfter) imgAfter.src = getSafeUrl(alertData.warningImageUrl);

  const closeBtn = document.getElementById("close-btn-id");
  if (closeBtn) closeBtn.onclick = () => dismissAlert(alertData.eventId);
}

function nextAlert() {
  if (activeAlertsList.length === 0) return;
  currentAlertIndex++;
  if (currentAlertIndex >= activeAlertsList.length) currentAlertIndex = 0;
  renderCurrentAlert();
}

function prevAlert() {
  if (activeAlertsList.length === 0) return;
  currentAlertIndex--;
  if (currentAlertIndex < 0) currentAlertIndex = activeAlertsList.length - 1;
  renderCurrentAlert();
}

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

// ===============================
// LIGHTBOX
// ===============================
function openLightbox(src) {
  const lb = document.getElementById("image-lightbox");
  const img = document.getElementById("lightbox-image");
  if (lb && img) {
    img.src = src;
    lb.classList.add("active");
  }
}

function openLightboxByIndex(index) {
  currentLightboxIndex = index;
  updateLightboxView();
}

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

function nextLightboxImage() {
  if (currentLightboxImages.length === 0) return;
  currentLightboxIndex =
    (currentLightboxIndex + 1) % currentLightboxImages.length;
  updateLightboxView();
}

function prevLightboxImage() {
  if (currentLightboxImages.length === 0) return;
  currentLightboxIndex =
    (currentLightboxIndex - 1 + currentLightboxImages.length) %
    currentLightboxImages.length;
  updateLightboxView();
}

// ===============================
// BOOTSTRAP
// ===============================
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

    if (!(me?.ok && (r === "guard" || r === "lifeguard"))) {
      window.location.href = "../pages/login.html";
      return;
    }

    const dashboard = document.getElementById("lifeguard-dashboard");
    if (dashboard) dashboard.classList.remove("hidden");

    checkLiveAlerts();
    alertPollTimer = setInterval(checkLiveAlerts, 3000);
  } catch {
    window.location.href = "../pages/login.html";
  }
});
