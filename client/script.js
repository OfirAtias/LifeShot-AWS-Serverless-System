// ===============================
// API CONFIGURATION
// ===============================
const API_BASE_URL = "https://zat8d5ozy1.execute-api.us-east-1.amazonaws.com";

// ===============================
// GLOBAL STATE
// ===============================
let allEvents = [];
let alertPollTimer = null;

// ===============================
// HELPER: FIX BROKEN IMAGES
// ===============================
// הפונקציה הזו מוודאת שלא נשבור את הקישור של אמזון
function getSafeUrl(url) {
  if (!url) return "";
  // אם הקישור כבר מכיל סימן שאלה (קישור חתום), נוסיף & במקום ?
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}cb=${Date.now()}`; // cb = cache buster
}

function normalizeStatus(s) {
  return String(s || "").toUpperCase();
}
function parseDateSafe(s) {
  const d = new Date(s);
  return isNaN(d.getTime()) ? new Date(0) : d;
}

// ===============================
// AUTH & NAV
// ===============================
function handleLogin() {
  const user = document.getElementById("username").value.toLowerCase();
  if (user.includes("admin")) {
    showScreen("manager-dashboard");
    fetchEvents();
  } else if (user.includes("guard")) {
    showScreen("lifeguard-dashboard");
    checkLiveAlerts();
    if (alertPollTimer) clearInterval(alertPollTimer);
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
  ].forEach((s) => {
    document.getElementById(s).classList.add("hidden");
  });
  document.getElementById(id).classList.remove("hidden");
}

function logout() {
  location.reload();
}

// ===============================
// LIFEGUARD LOGIC (THE FIX)
// ===============================
async function checkLiveAlerts() {
  const dashboard = document.getElementById("lifeguard-dashboard");
  if (!dashboard || dashboard.classList.contains("hidden")) return;

  try {
    const res = await fetch(`${API_BASE_URL}/events`, { cache: "no-store" });
    const data = await res.json();

    // מוצא את אירוע הטביעה הפתוח האחרון שיש לו תמונה
    const activeAlert = data
      .filter((e) => normalizeStatus(e.status) === "OPEN" && e.warningImageUrl)
      .sort(
        (a, b) => parseDateSafe(b.created_at) - parseDateSafe(a.created_at)
      )[0];

    const overlay = document.getElementById("emergency-overlay");
    const imgBefore = document.getElementById("img-before");
    const imgAfter = document.getElementById("img-after");
    const timeEl = document.getElementById("display-time");

    if (activeAlert) {
      // Sound
      new Audio("https://www.soundjay.com/buttons/beep-01a.mp3")
        .play()
        .catch(() => {});
      overlay.classList.remove("hidden");

      // Time
      const now = new Date();
      if (timeEl)
        timeEl.innerText = `${String(now.getHours()).padStart(2, "0")}:${String(
          now.getMinutes()
        ).padStart(2, "0")}`;

      // ✅ שימוש בפונקציה המתוקנת - זה יציג את התמונות!
      if (imgBefore && activeAlert.prevImageUrl) {
        imgBefore.src = getSafeUrl(activeAlert.prevImageUrl);
      }
      if (imgAfter && activeAlert.warningImageUrl) {
        imgAfter.src = getSafeUrl(activeAlert.warningImageUrl);
      }

      // Close Button
      const closeBtn = document.getElementById("close-btn-id");
      if (closeBtn) closeBtn.onclick = () => dismissAlert(activeAlert.eventId);
    } else {
      overlay.classList.add("hidden");
    }
  } catch (e) {
    console.error("Monitoring Error:", e);
  }
}

async function dismissAlert(eventId) {
  try {
    await fetch(`${API_BASE_URL}/events`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eventId }),
    });
    document.getElementById("emergency-overlay").classList.add("hidden");
    checkLiveAlerts();
  } catch (err) {
    console.error(err);
  }
}

// ===============================
// MANAGER LOGIC (GALLERY CARDS)
// ===============================
async function fetchEvents() {
  try {
    const res = await fetch(`${API_BASE_URL}/events`, { cache: "no-store" });
    allEvents = await res.json();
    document.getElementById("stat-total").innerText = allEvents.length;
    renderGallery(allEvents); // מפעיל את הגלריה
  } catch (e) {
    console.error(e);
  }
}

function renderGallery(data) {
  const container = document.getElementById("events-gallery-container");
  if (!container) return;
  container.innerHTML = "";

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
        : createdDate
            .toISOString()
            .replace("T", " ")
            .replace(/\.\d{3}Z$/, "");

    const beforeUrl = evt.prevImageUrl;
    const afterUrl = evt.warningImageUrl;

    // יצירת כרטיס (גלריה)
    const card = document.createElement("div");
    card.className = "event-card-item";

    card.innerHTML = `
      <div class="card-top-row">
         <div>
            <span class="card-id-text">#${
              evt.eventId ? evt.eventId.slice(-4) : "??"
            }</span>
            <div class="card-time-text">${dateStr} UTC</div>
         </div>
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
// LIGHTBOX LOGIC
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
  let zoom = 1;

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

  document.getElementById("zoom-in")?.addEventListener("click", () => {
    zoom += 0.5;
    img.style.transform = `scale(${zoom})`;
  });
  document.getElementById("zoom-out")?.addEventListener("click", () => {
    if (zoom > 0.5) zoom -= 0.5;
    img.style.transform = `scale(${zoom})`;
  });
  document.getElementById("zoom-reset")?.addEventListener("click", () => {
    zoom = 1;
    img.style.transform = `scale(${zoom})`;
  });
});
