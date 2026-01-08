// ===============================
// CONFIG (set these to match your Cognito + API)
// ===============================
const API_BASE_URL =
  window.API_BASE_URL || "https://zat8d5ozy1.execute-api.us-east-1.amazonaws.com";

// MUST be the exact Cognito domain you created (no typos)
const COGNITO_DOMAIN =
  window.COGNITO_DOMAIN || "https://us-east-12jgjgvvg3.auth.us-east-1.amazoncognito.com";

const COGNITO_CLIENT_ID =
  window.COGNITO_CLIENT_ID || "64l10nbtrs68ojhpe1am42dafn";

// IMPORTANT: For HTTP Cognito allows ONLY localhost (not 127.0.0.1) in many cases
const COGNITO_REDIRECT_URI =
  window.COGNITO_REDIRECT_URI || "http://localhost:5500/client/index.html";

const COGNITO_SCOPES = window.COGNITO_SCOPES || "openid email";

// ===============================
// STATE
// ===============================
let allEvents = [];
let activeAlertsList = [];
let currentAlertIndex = 0;
let alertPollTimer = null;

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
// TOKEN STORAGE
// ===============================
function getQueryParam(name) {
  return new URLSearchParams(window.location.search).get(name);
}

function saveTokens(tokens) {
  localStorage.setItem("ls_access_token", tokens.access_token || "");
  localStorage.setItem("ls_id_token", tokens.id_token || "");
  localStorage.setItem("ls_refresh_token", tokens.refresh_token || "");
  localStorage.setItem("ls_token_type", tokens.token_type || "Bearer");
  if (tokens.expires_in) {
    localStorage.setItem(
      "ls_expires_at",
      String(Date.now() + tokens.expires_in * 1000)
    );
  }
}

function clearTokens() {
  localStorage.removeItem("ls_access_token");
  localStorage.removeItem("ls_id_token");
  localStorage.removeItem("ls_refresh_token");
  localStorage.removeItem("ls_token_type");
  localStorage.removeItem("ls_expires_at");
}

function getAccessToken() {
  return localStorage.getItem("ls_access_token") || "";
}

function isTokenExpired() {
  const exp = Number(localStorage.getItem("ls_expires_at") || "0");
  return !exp || Date.now() > exp - 15_000; // 15s safety window
}

// Minimal JWT payload decode (no verification - API GW verifies)
function decodeJwtPayload(token) {
  try {
    const parts = token.split(".");
    if (parts.length < 2) return null;
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64 + "===".slice((base64.length + 3) % 4);
    const json = atob(padded);
    return JSON.parse(json);
  } catch {
    return null;
  }
}

function getUserGroupsFromToken() {
  const idToken = localStorage.getItem("ls_id_token") || "";
  const payload = decodeJwtPayload(idToken);
  const groups = payload?.["cognito:groups"];
  if (Array.isArray(groups)) return groups.map(String);
  return [];
}

function getPrimaryRole() {
  const groups = getUserGroupsFromToken().map((g) => g.toLowerCase());
  // adjust names if your groups are different
  if (groups.includes("admins") || groups.includes("admin")) return "admin";
  if (
    groups.includes("lifeguards") ||
    groups.includes("guard") ||
    groups.includes("lifeguard")
  )
    return "guard";
  return "";
}

function authHeader() {
  const token = getAccessToken();
  if (!token) return {};
  return { Authorization: `Bearer ${token}` };
}

// Wrapper: fetch with Authorization header + basic 401 handling
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
    console.warn("Auth failed, redirecting to login...");
    logout();
    throw new Error(`Unauthorized (${res.status})`);
  }

  return res;
}

// ===============================
// AUTH (Cognito Hosted UI - CORRECT ENDPOINTS)
// ===============================

// Correct login/signup entry is /oauth2/authorize (NOT /login or /signup)
function buildAuthorizeUrl({ signup = false } = {}) {
  const url = new URL(`${COGNITO_DOMAIN}/oauth2/authorize`);

  url.searchParams.set("client_id", COGNITO_CLIENT_ID);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("scope", COGNITO_SCOPES);
  url.searchParams.set("redirect_uri", COGNITO_REDIRECT_URI);

  // show signup screen
  if (signup) url.searchParams.set("screen_hint", "signup");

  return url.toString();
}

function cognitoLogin() {
  window.location.href = buildAuthorizeUrl({ signup: false });
}

function cognitoSignup() {
  window.location.href = buildAuthorizeUrl({ signup: true });
}

// Exchange Authorization Code -> Tokens
async function exchangeCodeForTokens(code) {
  const tokenUrl = `${COGNITO_DOMAIN}/oauth2/token`;

  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: COGNITO_CLIENT_ID,
    code,
    redirect_uri: COGNITO_REDIRECT_URI,
  });

  const res = await fetch(tokenUrl, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });

  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Token exchange failed: ${res.status} ${txt}`);
  }

  return res.json();
}

function cognitoLogoutRedirect() {
  // Cognito logout endpoint
  const url = new URL(`${COGNITO_DOMAIN}/logout`);
  url.searchParams.set("client_id", COGNITO_CLIENT_ID);
  url.searchParams.set("logout_uri", COGNITO_REDIRECT_URI);
  window.location.href = url.toString();
}

// ===============================
// UI NAV
// ===============================
function showScreen(id) {
  [
    "login-screen",
    "signup-screen",
    "lifeguard-dashboard",
    "manager-dashboard",
    "demo-screen",
  ].forEach((s) => {
    const el = document.getElementById(s);
    if (el) el.classList.add("hidden");
  });

  const target = document.getElementById(id);
  if (target) {
    target.classList.remove("hidden");
    if (id === "demo-screen") renderDemoPage();
  }
}

function logout() {
  if (alertPollTimer) clearInterval(alertPollTimer);
  alertPollTimer = null;

  clearTokens();

  // Logout through Cognito so Hosted UI session is cleared
  if (COGNITO_DOMAIN && COGNITO_CLIENT_ID && COGNITO_REDIRECT_URI) {
    cognitoLogoutRedirect();
    return;
  }

  location.reload();
}

function routeAfterLogin() {
  const role = getPrimaryRole();

  if (role === "admin") {
    showScreen("manager-dashboard");
    fetchEvents();
    return;
  }

  if (role === "guard") {
    showScreen("lifeguard-dashboard");
    checkLiveAlerts();
    alertPollTimer = setInterval(checkLiveAlerts, 3000);
    return;
  }

  // No group -> show error on login screen
  const err = document.getElementById("auth-error");
  if (err) {
    err.classList.remove("hidden");
    err.innerText =
      "Your user has no role (cognito:groups). Add it to Admins / Lifeguards group in Cognito.";
  }
  showScreen("login-screen");
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
      .sort((a, b) => parseDateSafe(b.created_at) - parseDateSafe(a.created_at));

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
    counter.innerText = `Alert ${currentAlertIndex + 1} of ${activeAlertsList.length}`;
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
// MANAGER LOGIC
// ===============================
async function fetchEvents() {
  try {
    const res = await apiFetch(`/events`);
    allEvents = await res.json();
    const stat = document.getElementById("stat-total");
    if (stat) stat.innerText = Array.isArray(allEvents) ? allEvents.length : 0;
    renderGallery(Array.isArray(allEvents) ? allEvents : []);
  } catch (e) {
    console.error(e);
  }
}

function renderGallery(data) {
  const container = document.getElementById("events-gallery-container");
  if (!container) return;
  container.innerHTML = "";

  if (!Array.isArray(data) || data.length === 0) {
    container.innerHTML = `
      <div class="no-events-message">
        <div class="no-events-icon">📂</div>
        <h3 class="no-events-title">No Events Found</h3>
        <p class="no-events-sub">The event log is currently empty.</p>
      </div>
    `;
    return;
  }

  const sorted = [...data].sort(
    (a, b) => parseDateSafe(b.created_at) - parseDateSafe(a.created_at)
  );

  sorted.forEach((evt) => {
    const status = normalizeStatus(evt.status || "UNKNOWN");
    const statusClass = status === "OPEN" ? "status-open" : "status-resolved";

    const createdDate = parseDateSafe(evt.created_at);
    const dateStr =
      createdDate.getTime() === 0
        ? "N/A"
        : createdDate.toISOString().replace("T", " ").substring(0, 19);

    const beforeUrl = evt.prevImageUrl;
    const afterUrl = evt.warningImageUrl;

    const card = document.createElement("div");
    card.className = "event-card-item";

    card.innerHTML = `
      <div class="card-top-row">
         <div class="card-time-text">${dateStr}</div>
         <span class="status-badge ${statusClass}">${status}</span>
      </div>
      <div class="card-images-row">
         <div class="card-img-wrap">
            <span class="card-img-label">Before</span>
            ${
              beforeUrl
                ? `<img src="${getSafeUrl(beforeUrl)}" class="card-img-obj" onclick="openLightbox(this.src)">`
                : `<div class="no-img-box">No Image</div>`
            }
         </div>
         <div class="card-img-wrap">
            <span class="card-img-label">After</span>
            ${
              afterUrl
                ? `<img src="${getSafeUrl(afterUrl)}" class="card-img-obj" style="border: 2px solid #ff4757;" onclick="openLightbox(this.src)">`
                : `<div class="no-img-box">No Image</div>`
            }
         </div>
      </div>
    `;

    container.appendChild(card);
  });
}

function filterTable(type) {
  if (type === "ALL") renderGallery(allEvents);
  else renderGallery(allEvents.filter((e) => normalizeStatus(e.status) === type));
}

// ===============================
// DEMO PAGE LOGIC
// ===============================
function renderDemoPage() {
  renderSingleCamera("demo-container-cam1", "Test1", 8);
  renderSingleCamera("demo-container-cam2", "Test2", 12);
}

function renderSingleCamera(containerId, folderName, imageCount) {
  const container = document.getElementById(containerId);
  if (!container) return;

  container.innerHTML = "";

  const gridDiv = document.createElement("div");
  gridDiv.className = "multi-img-grid";

  for (let i = 1; i <= imageCount; i++) {
    let filename = folderName === "Test1" ? `${i}.png` : `Test2_${i}.png`;
    const imgSrc = `images/${folderName}/${filename}`;

    const img = document.createElement("img");
    img.src = imgSrc;
    img.className = "mini-cam-img";
    img.alt = `${folderName} Event ${i}`;
    img.onclick = function () {
      openLightbox(this.src);
    };
    img.onerror = function () {
      this.style.display = "none";
    };

    gridDiv.appendChild(img);
  }

  container.appendChild(gridDiv);
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

// ===============================
// BOOTSTRAP
// ===============================
async function handleAuthCallbackIfNeeded() {
  const code = getQueryParam("code");
  if (!code) return false;

  try {
    const tokens = await exchangeCodeForTokens(code);
    saveTokens(tokens);

    // clean URL (remove ?code=...)
    const cleanUrl = window.location.origin + window.location.pathname;
    window.history.replaceState({}, document.title, cleanUrl);

    return true;
  } catch (e) {
    console.error(e);
    const err = document.getElementById("auth-error");
    if (err) {
      err.classList.remove("hidden");
      err.innerText =
        "Token exchange failed. Make sure callback URL in Cognito is EXACTLY: " +
        COGNITO_REDIRECT_URI;
    }
    clearTokens();
    return false;
  }
}

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

  // 1) if returned from Cognito with ?code=...
  await handleAuthCallbackIfNeeded();

  // 2) if token exists -> route
  const token = getAccessToken();
  if (token && !isTokenExpired()) {
    routeAfterLogin();
  } else {
    showScreen("login-screen");
  }
});
