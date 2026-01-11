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
let allEvents = [];
let currentLightboxImages = [];
let currentLightboxIndex = 0;
let myPieChart = null; // משתנה לשמירת הגרף

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

  // דיבאג קצר - אפשר למחוק אחרי שזה עובד
  // console.log("API auth token prefix:", (headers.Authorization || "").slice(0, 35));

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

// ===============================
// UI NAV
// ===============================
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

async function logout() {
  await authLogout();

  clearTokens();

  window.location.href = "../pages/login.html";
}

// ===============================
// MANAGER LOGIC
// ===============================
async function fetchEvents() {
  try {
    const res = await apiFetch(`/events`);
    allEvents = await res.json();

    // ✅ התיקון: מגדירים את המשתנה הזה כאן כדי להשתמש בו גם לגלריה וגם לגרף
    const dataArr = Array.isArray(allEvents) ? allEvents : [];

    const stat = document.getElementById("stat-total");
    if (stat) stat.innerText = dataArr.length;

    // שליחת הנתונים לגלריה
    renderGallery(dataArr);

    // שליחת הנתונים לגרף החדש
    updateManagerChart(dataArr);
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
// DEMO PAGE LOGIC
// ===============================
function renderDemoPage() {
  currentLightboxImages = []; // איפוס רשימת התמונות לניווט
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
    const imgSrc = `../images/${folderName}/${filename}`;

    // שמירת האינדקס לניווט
    const globalIndex = currentLightboxImages.length;
    currentLightboxImages.push(imgSrc);

    const img = document.createElement("img");
    img.src = imgSrc;
    img.className = "mini-cam-img";
    img.alt = `${folderName} Event ${i}`;

    // שליחה לפונקציית פתיחה לפי אינדקס
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
// CHART LOGIC (MANAGER)
// ===============================
function updateManagerChart(events) {
  const ctx = document.getElementById("eventsPieChart");
  if (!ctx) return;

  // חישוב נתונים (היום מול אתמול)
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);

  const isSameDate = (d1, d2) =>
    d1.getFullYear() === d2.getFullYear() &&
    d1.getMonth() === d2.getMonth() &&
    d1.getDate() === d2.getDate();

  let countToday = 0;
  let countYesterday = 0;

  events.forEach((e) => {
    const d = parseDateSafe(e.created_at);
    if (isSameDate(d, today)) countToday++;
    if (isSameDate(d, yesterday)) countYesterday++;
  });

  // בדיקה: אם אין בכלל נתונים, נציג נתוני דמו כדי שהגרף יופיע (לבדיקה)
  // תוכלי למחוק את ה-if הזה כשיש נתונים אמיתיים
  if (countToday === 0 && countYesterday === 0) {
    countToday = 5; // סתם מספרים לבדיקה
    countYesterday = 3;
  }

  // אם כבר יש גרף קיים, נהרוס אותו כדי לא ליצור כפילויות
  if (window.myPieChart instanceof Chart) {
    window.myPieChart.destroy();
  }

  // יצירת הגרף
  window.myPieChart = new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: ["Today", "Yesterday"],
      datasets: [
        {
          data: [countToday, countYesterday],
          backgroundColor: [
            "#22d3ee", // ציאן חזק
            "rgba(255, 255, 255, 0.3)", // לבן שקוף
          ],
          borderColor: "transparent",
          borderWidth: 0,
          hoverOffset: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false, // חשוב מאוד כדי שיתאים לגובה שקבענו ב-CSS
      plugins: {
        legend: {
          position: "bottom",
          labels: {
            color: "white",
            font: { size: 14, family: "'Segoe UI', sans-serif" },
            padding: 20,
          },
        },
      },
    },
  });
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

  // Role gate
  try {
    const me = await authMe();
    const r = String(me?.role || "").toLowerCase();

    if (!(me?.ok && r === "admin")) {
      window.location.href = "../pages/login.html";
      return;
    }

    showScreen("manager-dashboard");
    fetchEvents();
  } catch {
    window.location.href = "../pages/login.html";
  }
});
