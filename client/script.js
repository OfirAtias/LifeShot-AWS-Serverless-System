// ===============================
// API CONFIGURATION
// ===============================
const API_BASE_URL = "https://zat8d5ozy1.execute-api.us-east-1.amazonaws.com";

let allEvents = [];
let activeAlertsList = [];
let currentAlertIndex = 0;
let alertPollTimer = null;

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
// AUTH
// ===============================
function handleLogin() {
  const user = document.getElementById("username").value.toLowerCase();
  if (user.includes("admin")) {
    showScreen("manager-dashboard");
    fetchEvents();
  } else if (user.includes("guard")) {
    showScreen("lifeguard-dashboard");
    checkLiveAlerts();
    alertPollTimer = setInterval(checkLiveAlerts, 3000);
  } else {
    alert("Access Denied.");
  }
}

function showScreen(id) {
  [
    "login-screen",
    "signup-screen",
    "lifeguard-dashboard",
    "manager-dashboard",
    "demo-screen", // <--- החדש
  ].forEach((s) => {
    const el = document.getElementById(s);
    if (el) el.classList.add("hidden");
  });

  const target = document.getElementById(id);
  if (target) target.classList.remove("hidden");
}

// ... שאר הקוד נשאר זהה ...

function logout() {
  location.reload();
}

// ===============================
// LIFEGUARD LOGIC (TOGGLE STATES)
// ===============================
async function checkLiveAlerts() {
  const dashboard = document.getElementById("lifeguard-dashboard");
  if (!dashboard || dashboard.classList.contains("hidden")) return;

  try {
    const res = await fetch(`${API_BASE_URL}/events`, { cache: "no-store" });
    const data = await res.json();

    activeAlertsList = data
      .filter((e) => normalizeStatus(e.status) === "OPEN" && e.warningImageUrl)
      .sort(
        (a, b) => parseDateSafe(b.created_at) - parseDateSafe(a.created_at)
      );

    const overlay = document.getElementById("emergency-overlay");
    const noAlertsState = document.getElementById("no-alerts-state");

    if (activeAlertsList.length > 0) {
      // === ALERT MODE ===
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
      // === WAITING MODE ===
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

  document.getElementById("alert-counter").innerText = `Alert ${
    currentAlertIndex + 1
  } of ${activeAlertsList.length}`;

  const created = parseDateSafe(alertData.created_at);
  document.getElementById("display-time").innerText = `${String(
    created.getHours()
  ).padStart(2, "0")}:${String(created.getMinutes()).padStart(2, "0")}`;

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
    await fetch(`${API_BASE_URL}/events`, {
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
    const res = await fetch(`${API_BASE_URL}/events`, { cache: "no-store" });
    allEvents = await res.json();
    document.getElementById("stat-total").innerText = allEvents.length;
    renderGallery(allEvents);
  } catch (e) {
    console.error(e);
  }
}

function renderGallery(data) {
  const container = document.getElementById("events-gallery-container");
  if (!container) return;
  container.innerHTML = "";

  // ✅ בדיקה: אם אין אירועים, הצג הודעה מעוצבת
  if (data.length === 0) {
    container.innerHTML = `
      <div class="no-events-message">
        <div class="no-events-icon">📂</div>
        <h3 class="no-events-title">No Events Found</h3>
        <p class="no-events-sub">The event log is currently empty.</p>
      </div>
    `;
    return;
  }

  // מכאן זה אותו קוד רגיל שמייצר את הכרטיסים
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
                ? `<img src="${getSafeUrl(
                    beforeUrl
                  )}" class="card-img-obj" onclick="openLightbox(this.src)">`
                : `<div class="no-img-box">No Image</div>`
            }
         </div>
         <div class="card-img-wrap">
            <span class="card-img-label">After</span>
            ${
              afterUrl
                ? `<img src="${getSafeUrl(
                    afterUrl
                  )}" class="card-img-obj" style="border: 2px solid #ff4757;" onclick="openLightbox(this.src)">`
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
  else
    renderGallery(allEvents.filter((e) => normalizeStatus(e.status) === type));
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

document.addEventListener("DOMContentLoaded", () => {
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
});
