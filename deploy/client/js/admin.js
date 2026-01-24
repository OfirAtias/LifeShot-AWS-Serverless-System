// LifeShot Admin Dashboard (browser-side logic).
//
// Cosmetic refactor only:
// - Improves readability via spacing, section headers, and comments.
// - Does not change functionality or behavior.

// =============================================================================
// Config
// =============================================================================
// 1) Try localStorage first (so bucket/urls won’t “run away”)
const API_BASE_URL =
  localStorage.getItem("LS_API_BASE_URL") ||
  window.API_BASE_URL ||
  window.AUTH_BASE_URL ||
  "";

const AUTH_BASE_URL = API_BASE_URL;

const DETECTOR_LAMBDA_URL =
  localStorage.getItem("LS_DETECTOR_LAMBDA_URL") ||
  window.DETECTOR_LAMBDA_URL ||
  "";

// 2) Simple helper to set/update URLs from console once
window.LS_setEndpoints = function (apiBaseUrl, detectorUrl) {
  if (apiBaseUrl) localStorage.setItem("LS_API_BASE_URL", apiBaseUrl.trim());
  if (detectorUrl)
    localStorage.setItem("LS_DETECTOR_LAMBDA_URL", detectorUrl.trim());
  console.log("Saved", {
    LS_API_BASE_URL: localStorage.getItem("LS_API_BASE_URL"),
    LS_DETECTOR_LAMBDA_URL: localStorage.getItem("LS_DETECTOR_LAMBDA_URL"),
  });
  console.log("Reload the page now.");
};

if (!API_BASE_URL) {
  console.warn(
    "Missing API base URL. Run in console: LS_setEndpoints('https://xxxx.execute-api.us-east-1.amazonaws.com','https://xxxx.lambda-url.us-east-1.on.aws/')",
  );
}
if (!DETECTOR_LAMBDA_URL) {
  console.warn(
    "Missing Detector URL. Run in console: LS_setEndpoints('https://xxxx.execute-api.us-east-1.amazonaws.com','https://xxxx.lambda-url.us-east-1.on.aws/')",
  );
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
  if (!exp) return false; // אם אין expires, לא חוסמים
  return Date.now() > exp - 15_000; // 15s safety window
}

// API Gateway JWT Authorizer
// Choose the token that will be sent to API Gateway (prefers access token).
function getApiBearerToken() {
  const at = getAccessToken();
  if (at) return at;
  return getIdToken(); // fallback
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
let allEvents = [];
let currentLightboxImages = [];
let currentLightboxIndex = 0;
let myPieChart = null; // משתנה לשמירת הגרף

// ✅ NEW: Prevent Test1->Test2 chaining / double clicks / parallel runs
let detectorInFlight = false;
let detectorAbort = null;

// Enable/disable the run-test buttons while the detector is in-flight.
function setDetectorButtonsDisabled(disabled) {
  // ✅ Update these IDs to match your actual buttons if needed
  const btn1 = document.getElementById("btn-run-test1");
  const btn2 = document.getElementById("btn-run-test2");
  if (btn1) btn1.disabled = disabled;
  if (btn2) btn2.disabled = disabled;
}

// Allow stopping a running detector request from console
window.LS_stopDetector = function () {
  try {
    if (detectorAbort) detectorAbort.abort();
  } catch {}
  detectorAbort = null;
  detectorInFlight = false;
  setDetectorButtonsDisabled(false);
  setDetectorOverlay(false);
  console.log("Detector aborted by user");
};

// =============================================================================
// Polling (refresh events every 5s)
// =============================================================================
let eventsPollTimer = null;
let eventsFetchInFlight = false;

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

// ✅ NEW: Format responseSeconds (lifeguard response/close time)
function formatResponseSeconds(val) {
  const n = Number(val);
  if (!Number.isFinite(n) || n < 0) return "N/A";
  return `${Math.round(n)}s`;
}

// =============================================================================
// Number counter animation
// =============================================================================

// Animate a numeric counter in the UI up to targetValue.
function animateCounter(element, targetValue, duration = 1500) {
  if (!element) return;

  const startValue = 0;
  const startTime = performance.now();

  function update(currentTime) {
    const elapsed = currentTime - startTime;
    const progress = Math.min(elapsed / duration, 1);

    const ease = 1 - Math.pow(1 - progress, 3);
    const current = Math.floor(ease * targetValue);

    element.innerText = current;

    if (progress < 1) {
      requestAnimationFrame(update);
    } else {
      element.innerText = targetValue;
    }
  }

  requestAnimationFrame(update);
}

// =============================================================================
// Loading overlay (injected UI)
// =============================================================================

// Inject the detector overlay HTML/CSS once.
function ensureDetectorOverlay() {
  if (document.getElementById("detector-overlay")) return;

  const style = document.createElement("style");
  style.id = "detector-overlay-style";
  style.textContent = `
    #detector-overlay {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,.45);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 99999;
      backdrop-filter: blur(6px);
    }
    #detector-overlay.active { display: flex; }
    #detector-overlay .box {
      min-width: 280px;
      max-width: 90vw;
      padding: 18px 18px 16px;
      border-radius: 16px;
      background: rgba(20, 30, 45, .72);
      border: 1px solid rgba(255,255,255,.18);
      box-shadow: 0 10px 30px rgba(0,0,0,.35);
      color: #fff;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      text-align: center;
    }
    #detector-overlay .title {
      font-weight: 700;
      letter-spacing: .2px;
      margin-bottom: 6px;
      font-size: 16px;
    }
    #detector-overlay .msg {
      opacity: .9;
      font-size: 14px;
      margin-bottom: 12px;
    }
    #detector-overlay .row {
      display: flex;
      gap: 10px;
      align-items: center;
      justify-content: center;
    }
    #detector-overlay .spinner {
      width: 18px;
      height: 18px;
      border-radius: 999px;
      border: 2px solid rgba(255,255,255,.28);
      border-top-color: rgba(255,255,255,.95);
      animation: detSpin .9s linear infinite;
    }
    @keyframes detSpin { to { transform: rotate(360deg); } }
    #detector-overlay .small {
      font-size: 12px;
      opacity: .75;
      margin-top: 8px;
    }
  `;
  document.head.appendChild(style);

  const overlay = document.createElement("div");
  overlay.id = "detector-overlay";
  overlay.innerHTML = `
    <div class="box">
      <div class="title" id="detector-overlay-title">Running detector…</div>
      <div class="msg" id="detector-overlay-msg">Please wait</div>
      <div class="row">
        <div class="spinner" id="detector-overlay-spinner"></div>
        <div id="detector-overlay-status">Working…</div>
      </div>
    </div>
  `;
  overlay.addEventListener("click", (e) => {
    e.preventDefault();
  });
  document.body.appendChild(overlay);
}

// Toggle the detector overlay and optionally update its UI text.
function setDetectorOverlay(active, { title, msg, status, spinning } = {}) {
  ensureDetectorOverlay();
  const overlay = document.getElementById("detector-overlay");
  const t = document.getElementById("detector-overlay-title");
  const m = document.getElementById("detector-overlay-msg");
  const s = document.getElementById("detector-overlay-status");
  const sp = document.getElementById("detector-overlay-spinner");

  if (t && title != null) t.textContent = title;
  if (m && msg != null) m.textContent = msg;
  if (s && status != null) s.textContent = status;
  if (sp && spinning != null) sp.style.display = spinning ? "block" : "none";

  if (!overlay) return;
  if (active) overlay.classList.add("active");
  else overlay.classList.remove("active");
}

// Sleep helper for delaying UI transitions.
function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// =============================================================================
// Auth (Lambda Auth)
// =============================================================================

// Call /auth/me to validate the current token and retrieve role/groups.
async function authMe() {
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
// API fetch (adds Authorization for API Gateway)
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
      res.status,
    );
    throw new Error(`Unauthorized (${res.status})`);
  }

  return res;
}

// Attempt to delete an event (requires backend support).
async function deleteEvent(eventId) {
  if (!eventId) return;

  const ok = confirm(`Delete event ${eventId} permanently?`);
  if (!ok) return;

  try {
    const res = await apiFetch(`/events`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eventId }),
    });

    const data = await res.json().catch(() => ({}));
    if (!res.ok)
      throw new Error(data?.error || `DELETE failed (${res.status})`);

    // רענון
    await fetchEvents();
    alert("Event deleted ✅");
  } catch (e) {
    console.error(e);
    alert("Delete failed ❌ (check token/permissions/logs)");
  }
}

// =============================================================================
// UI navigation
// =============================================================================

// Show a given screen and hide the others.
function showScreen(id) {
  ["manager-dashboard", "demo-screen"].forEach((s) => {
    const el = document.getElementById(s);
    if (el) el.classList.add("hidden");
  });

  const target = document.getElementById(id);
  if (target) {
    target.classList.remove("hidden");
    if (id === "demo-screen") renderDemoPage();
  }
}

// Logout: call auth endpoint, clear tokens, then redirect to login.
async function logout() {
  await authLogout();
  clearTokens();
  window.location.href = "../pages/login.html";
}

// =============================================================================
// Run detector (LifeShot-Detector Lambda Function URL)
// =============================================================================

// Trigger the detector for a specific demo test dataset.
async function runDetectorTest(testName) {
  // ✅ HARD STOP: no parallel runs / no chaining
  if (detectorInFlight) {
    console.warn("Detector already running. Ignoring click.");
    return;
  }
  detectorInFlight = true;
  setDetectorButtonsDisabled(true);

  detectorAbort = new AbortController();

  setDetectorOverlay(true, {
    title: "Running detector…",
    msg: `Triggering ${testName} (please wait)`,
    status: "Working…",
    spinning: true,
  });

  try {
    const payload =
      testName === "Test2"
        ? {
            prefix: "LifeShot/DrowningSet/Test2/",
            max_frames: 12,
            single_prefix_only: true,
          }
        : {
            prefix: "LifeShot/DrowningSet/Test1/",
            max_frames: 8,
            single_prefix_only: true,
          };

    console.log("DETECTOR_LAMBDA_URL =", DETECTOR_LAMBDA_URL);
    console.log("payload =", payload);

    const res = await fetch(DETECTOR_LAMBDA_URL, {
      method: "POST",
      headers: {
        "Content-Type": "text/plain;charset=UTF-8",
      },
      body: JSON.stringify(payload),
      signal: detectorAbort.signal,
    });

    const text = await res.text();
    let data = {};
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }

    if (!res.ok) {
      console.error("Detector failed:", res.status, data);

      setDetectorOverlay(true, {
        title: "Detector failed ❌",
        msg: `HTTP ${res.status}`,
        status: "Check console / CloudWatch logs",
        spinning: false,
      });
      await sleep(1400);
      return;
    }

    console.log("Detector result:", data);

    setDetectorOverlay(true, {
      title: "Done ✅",
      msg: `${testName} triggered successfully`,
      status: "Completed",
      spinning: false,
    });
    await sleep(900);
  } catch (err) {
    if (err?.name === "AbortError") {
      console.warn("Detector request aborted");
      // no alert
    } else {
      console.error("Detector error:", err);

      setDetectorOverlay(true, {
        title: "Detector error ❌",
        msg: "Request error",
        status: "Check console",
        spinning: false,
      });
      await sleep(1400);

      alert("Error triggering detector. Check console.");
    }
  } finally {
    setDetectorOverlay(false);
    detectorInFlight = false;
    detectorAbort = null;
    setDetectorButtonsDisabled(false);
  }
}

// =============================================================================
// Manager logic
// =============================================================================

// Fetch event list from API and update UI widgets.
async function fetchEvents() {
  if (eventsFetchInFlight) return; // prevent overlapping calls
  eventsFetchInFlight = true;

  try {
    const res = await apiFetch(`/events`);
    allEvents = await res.json();

    const dataArr = Array.isArray(allEvents) ? allEvents : [];

    const statTotal = document.getElementById("stat-total");
    if (statTotal) animateCounter(statTotal, dataArr.length);

    const statOpen = document.getElementById("stat-open");
    if (statOpen) {
      const openCount = dataArr.filter(
        (e) => normalizeStatus(e.status) === "OPEN",
      ).length;
      animateCounter(statOpen, openCount);
    }

    renderGallery(dataArr);
    updateManagerChart(dataArr);
  } catch (e) {
    console.error(e);
  } finally {
    eventsFetchInFlight = false;
  }
}

function startEventsPolling(intervalMs = 5000) {
  stopEventsPolling(); // avoid duplicates

  // fetch immediately, then every interval
  fetchEvents();
  eventsPollTimer = setInterval(() => {
    // optional: don’t poll when tab is hidden
    if (document.hidden) return;
    fetchEvents();
  }, intervalMs);
}

function stopEventsPolling() {
  if (eventsPollTimer) clearInterval(eventsPollTimer);
  eventsPollTimer = null;
}

// Render the events gallery cards (and prepare lightbox image list).
function renderGallery(data) {
  const container = document.getElementById("events-gallery-container");
  if (!container) return;
  container.innerHTML = "";

  currentLightboxImages = [];

  if (!Array.isArray(data) || data.length === 0) {
    container.innerHTML = `
      <div class="no-events-message">
        <h3 class="no-events-title">No Events Found</h3>
        <p class="no-events-sub">The event log is currently empty.</p>
      </div>
    `;
    return;
  }

  const sorted = [...data].sort(
    (a, b) => parseDateSafe(b.created_at) - parseDateSafe(a.created_at),
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

    let beforeOnClick = "";
    if (beforeUrl) {
      const idx = currentLightboxImages.length;
      currentLightboxImages.push(getSafeUrl(beforeUrl));
      beforeOnClick = `onclick="openLightboxByIndex(${idx})"`;
    }

    let afterOnClick = "";
    if (afterUrl) {
      const idx = currentLightboxImages.length;
      currentLightboxImages.push(getSafeUrl(afterUrl));
      afterOnClick = `onclick="openLightboxByIndex(${idx})"`;
    }

    // ✅ NEW: show responseSeconds only for CLOSED events
    const responseLine =
      status === "CLOSED"
        ? `<div class="card-response-text">Response Time: <b>${formatResponseSeconds(
            evt.responseSeconds,
          )}</b></div>`
        : "";

    const card = document.createElement("div");
    card.className = "event-card-item";

    card.innerHTML = `
      <div class="card-top-row">
         <div class="card-time-text">${dateStr}</div>
         <span class="status-badge ${statusClass}">${status}</span>
      </div>

      ${responseLine}

      <div class="card-images-row">
         <div class="card-img-wrap">
            <span class="card-img-label">Before</span>
            ${
              beforeUrl
                ? `<img src="${getSafeUrl(
                    beforeUrl,
                  )}" class="card-img-obj" ${beforeOnClick}>`
                : `<div class="no-img-box">No Image</div>`
            }
         </div>
         <div class="card-img-wrap">
            <span class="card-img-label">After</span>
            ${
              afterUrl
                ? `<img src="${getSafeUrl(
                    afterUrl,
                  )}" class="card-img-obj" style="border: 2px solid #ff4757;" ${afterOnClick}>`
                : `<div class="no-img-box">No Image</div>`
            }
         </div>
      </div>
    `;

    container.appendChild(card);
  });
}

// Filter gallery by status.
function filterTable(type) {
  if (type === "ALL") renderGallery(allEvents);
  else
    renderGallery(allEvents.filter((e) => normalizeStatus(e.status) === type));
}

// =============================================================================
// Demo page logic
// =============================================================================

// Render the demo screen with two camera grids.
function renderDemoPage() {
  currentLightboxImages = [];
  renderSingleCamera("demo-container-cam1", "Test1", 8);
  renderSingleCamera("demo-container-cam2", "Test2", 12);
}

// Render a single camera image grid and wire up lightbox clicks.
function renderSingleCamera(containerId, folderName, imageCount) {
  const container = document.getElementById(containerId);
  if (!container) return;

  container.innerHTML = "";

  const gridDiv = document.createElement("div");
  gridDiv.className = "multi-img-grid";

  for (let i = 1; i <= imageCount; i++) {
    let filename = folderName === "Test1" ? `${i}.png` : `Test2_${i}.png`;
    const imgSrc = `../images/${folderName}/${filename}`;

    const globalIndex = currentLightboxImages.length;
    currentLightboxImages.push(imgSrc);

    const img = document.createElement("img");
    img.src = imgSrc;
    img.className = "mini-cam-img";
    img.alt = `${folderName} Event ${i}`;

    img.onclick = function () {
      openLightboxByIndex(globalIndex);
    };
    img.onerror = function () {
      this.style.display = "none";
    };

    gridDiv.appendChild(img);
  }
  container.appendChild(gridDiv);
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
// Manager dashboard chart
// =============================================================================

// Update the doughnut chart with recent activity stats.
function updateManagerChart(events) {
  const canvas = document.getElementById("eventsPieChart");
  const titleElement = document.getElementById("chartTitle");
  const noEventsMsg = document.getElementById("noEventsMessage");

  if (!canvas) return;

  if (!events || events.length === 0) {
    if (window.myPieChart instanceof Chart) window.myPieChart.destroy();
    canvas.style.display = "none";
    if (noEventsMsg) noEventsMsg.style.display = "block";
    if (titleElement) titleElement.innerText = "NO DATA";
    return;
  }

  canvas.style.display = "block";
  if (noEventsMsg) noEventsMsg.style.display = "none";

  const now = Date.now();
  const MS_24H = 24 * 60 * 60 * 1000;

  let countLast24h = 0;
  let countPrev24h = 0;
  let latestDate = null;

  const isSameDay = (d1, d2) => {
    return (
      d1.getFullYear() === d2.getFullYear() &&
      d1.getMonth() === d2.getMonth() &&
      d1.getDate() === d2.getDate()
    );
  };

  const formatDate = (d) => {
    try {
      return new Intl.DateTimeFormat("he-IL", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
      }).format(d);
    } catch {
      return d.toISOString().substring(0, 10);
    }
  };

  events.forEach((e) => {
    const d = new Date(e.created_at);
    if (isNaN(d.getTime())) return;

    if (!latestDate || d.getTime() > latestDate.getTime()) latestDate = d;

    const diff = now - d.getTime();
    if (diff >= 0 && diff < MS_24H) countLast24h++;
    else if (diff >= MS_24H && diff < 2 * MS_24H) countPrev24h++;
  });

  const noRecent = countLast24h === 0 && countPrev24h === 0;
  const lastEventString = latestDate ? formatDate(latestDate) : "N/A";

  let chartLabels = [];
  let chartData = [];
  let chartColors = [];

  if (noRecent) {
    let countOnLatestDay = 0;
    if (latestDate) {
      events.forEach((e) => {
        const d = new Date(e.created_at);
        if (isSameDay(d, latestDate)) {
          countOnLatestDay++;
        }
      });
    }

    chartLabels = [lastEventString];
    chartData = [countOnLatestDay];
    chartColors = ["#274272"];

    if (titleElement)
      titleElement.innerText = `LAST ACTIVITY: ${lastEventString}`;
  } else {
    if (titleElement) titleElement.innerText = "ACTIVITY TODAY VS YESTERDAY";

    if (countPrev24h === 0) {
      chartLabels = ["Today"];
      chartData = [countLast24h];
      chartColors = ["#274272"];
    } else if (countLast24h === 0) {
      chartLabels = ["Yesterday"];
      chartData = [countPrev24h];
      chartColors = ["rgba(255, 255, 255, 0.3)"];
    } else {
      chartLabels = ["Today", "Yesterday"];
      chartData = [countLast24h, countPrev24h];
      chartColors = ["#274272", "rgba(255, 255, 255, 0.3)"];
    }
  }

  if (window.myPieChart instanceof Chart) window.myPieChart.destroy();

  window.myPieChart = new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: chartLabels,
      datasets: [
        {
          data: chartData,
          backgroundColor: chartColors,
          borderColor: "transparent",
          borderWidth: 0,
          hoverOffset: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: "bottom",
          labels: {
            color: "white",
            font: { size: 14, family: "'Segoe UI', sans-serif" },
            padding: 20,
          },
        },
        tooltip: { enabled: true },
      },
    },
  });
}

// =============================================================================
// Bootstrap
// =============================================================================
document.addEventListener("DOMContentLoaded", async () => {
  // inject overlay early (so first click is instant)
  ensureDetectorOverlay();

  const lb = document.getElementById("image-lightbox");
  const img = document.getElementById("lightbox-image");
  const close = document.getElementById("lightbox-close");

  if (close)
    close.onclick = () => {
      lb.classList.remove("active");
      setTimeout(() => (img.src = ""), 300);
    };

  try {
    const me = await authMe();
    const r = String(me?.role || "").toLowerCase();

    if (!(me?.ok && r === "admin")) {
      window.location.href = "../pages/login.html";
      return;
    }

    showScreen("manager-dashboard");
    startEventsPolling(20000);
  } catch {
    window.location.href = "../pages/login.html";
  }
});
