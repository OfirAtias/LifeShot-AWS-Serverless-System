// ===============================
// API configuration
// ===============================
const API_BASE_URL = "https://zat8d5ozy1.execute-api.us-east-1.amazonaws.com";

// ===============================
// Global state
// ===============================
let allEvents = [];
let alertPollTimer = null;

// ===============================
// Handle user authentication and redirection
// ===============================
function handleLogin() {
  const user = document.getElementById("username").value.toLowerCase();

  if (user.includes("admin")) {
    showScreen("manager-dashboard");
    fetchEvents();
  } else if (user.includes("guard")) {
    showScreen("lifeguard-dashboard");

    // Start monitoring for live alerts
    checkLiveAlerts();
    if (alertPollTimer) clearInterval(alertPollTimer);
    alertPollTimer = setInterval(checkLiveAlerts, 3000);
  } else {
    alert("Access Denied. Please use 'guard' or 'admin' as username.");
  }
}

// ===============================
// Switch between different system screens
// ===============================
function showScreen(id) {
  const screens = [
    "login-screen",
    "signup-screen",
    "lifeguard-dashboard",
    "manager-dashboard",
  ];

  screens.forEach((s) => {
    const el = document.getElementById(s);
    if (el) el.classList.add("hidden");
  });

  const target = document.getElementById(id);
  if (target) target.classList.remove("hidden");
}

// ===============================
// Logout and reset system
// ===============================
function logout() {
  location.reload();
}

// ===============================
// Helpers
// ===============================
function normalizeStatus(s) {
  return String(s || "").toUpperCase();
}

function parseDateSafe(s) {
  const d = new Date(s);
  return isNaN(d.getTime()) ? new Date(0) : d;
}

// Picks latest OPEN event that has warningImageUrl (drowning event)
function getLatestOpenDrowningEvent(events) {
  const openDrowning = events
    .filter((e) => normalizeStatus(e.status) === "OPEN" && !!e.warningImageUrl)
    .sort((a, b) => parseDateSafe(b.created_at) - parseDateSafe(a.created_at));

  return openDrowning[0] || null;
}

// ===============================
// Lifeguard: Monitors for alerts and updates UI
// ===============================
async function checkLiveAlerts() {
  const dashboard = document.getElementById("lifeguard-dashboard");
  if (!dashboard || dashboard.classList.contains("hidden")) return;

  try {
    const res = await fetch(`${API_BASE_URL}/events`, { cache: "no-store" });
    const data = await res.json();

    // Take the latest OPEN event that includes warningImageUrl
    const activeAlert = getLatestOpenDrowningEvent(data);

    const overlay = document.getElementById("emergency-overlay");

    // Carousel elements
    const carousel = document.getElementById("before-after-carousel");
    const imgBefore = document.getElementById("img-before");
    const imgAfter = document.getElementById("img-after");
    const titleEl = document.getElementById("carousel-title");
    const btnPrev = document.getElementById("carousel-prev-btn");
    const btnNext = document.getElementById("carousel-next-btn");

    if (activeAlert) {
      // Play alert sound
      new Audio("https://www.soundjay.com/buttons/beep-01a.mp3")
        .play()
        .catch(() => {});

      if (overlay) overlay.classList.remove("hidden");

      // Current time
      const now = new Date();
      const hours = String(now.getHours()).padStart(2, "0");
      const minutes = String(now.getMinutes()).padStart(2, "0");
      const timeEl = document.getElementById("display-time");
      if (timeEl) timeEl.innerText = `${hours}:${minutes}`;

      // You removed zones/risk logic, so we won't use display-zone/display-score anymore.
      const scoreEl = document.getElementById("display-score");
      if (scoreEl) scoreEl.innerText = `ALERT`;

      // URLs from API (fresh presigned)
      const prevUrl = activeAlert.prevImageUrl || null;
      const warnUrl = activeAlert.warningImageUrl || null;

      // Show carousel
      if (carousel) carousel.style.display = "block";

      // Set images (cache bust)
      if (imgBefore) {
        if (prevUrl) {
          imgBefore.src = `${prevUrl}${prevUrl.includes("?") ? "&" : "?"}t=${Date.now()}`;
          imgBefore.style.display = "block";
        } else {
          imgBefore.style.display = "none";
          imgBefore.src = "";
        }
      }

      if (imgAfter) {
        if (warnUrl) {
          imgAfter.src = `${warnUrl}${warnUrl.includes("?") ? "&" : "?"}t=${Date.now()}`;
          imgAfter.style.display = "block";
        } else {
          imgAfter.style.display = "none";
          imgAfter.src = "";
        }
      }

      // Optional: simple “focus” highlight with prev/next (not changing images, just title label)
      let focus = "BEFORE";
      const setFocus = (which) => {
        focus = which;
        if (titleEl) titleEl.innerText = which;
        if (imgBefore) imgBefore.style.outline = which === "BEFORE" ? "3px solid rgba(255,255,255,0.35)" : "none";
        if (imgAfter) imgAfter.style.outline = which === "AFTER" ? "3px solid rgba(255,255,255,0.35)" : "none";
      };
      setFocus("BEFORE");

      if (btnPrev) btnPrev.onclick = () => setFocus("BEFORE");
      if (btnNext) btnNext.onclick = () => setFocus("AFTER");

      // Dismiss button
      const closeBtn = document.getElementById("close-btn-id");
      if (closeBtn) closeBtn.onclick = () => dismissAlert(activeAlert.eventId);

    } else {
      // No active alert
      if (overlay) overlay.classList.add("hidden");

      if (carousel) carousel.style.display = "none";
      if (imgBefore) {
        imgBefore.style.display = "none";
        imgBefore.src = "";
      }
      if (imgAfter) {
        imgAfter.style.display = "none";
        imgAfter.src = "";
      }
    }
  } catch (e) {
    console.error("Monitoring Error:", e);
  }
}

// ===============================
// Update event status to CLOSED via PATCH
// ===============================
async function dismissAlert(eventId) {
  try {
    const res = await fetch(`${API_BASE_URL}/events`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eventId }),
    });

    if (res.ok) {
      const overlay = document.getElementById("emergency-overlay");
      if (overlay) overlay.classList.add("hidden");
      checkLiveAlerts();
    }
  } catch (err) {
    console.error("Dismissal Error:", err);
  }
}

// ===============================
// Manager: fetch + render table + drowning gallery
// ===============================
async function fetchEvents() {
  try {
    const res = await fetch(`${API_BASE_URL}/events`, { cache: "no-store" });
    allEvents = await res.json();

    // Update stats
    const totalEl = document.getElementById("stat-total");
    if (totalEl) totalEl.innerText = allEvents.length;

    renderTable(allEvents);
    renderDrowningGallery(allEvents);
  } catch (e) {
    console.error("Fetch Events Error:", e);
  }
}

function renderTable(data) {
  const tableBody = document.getElementById("events-table-body");
  if (!tableBody) return;

  tableBody.innerHTML = "";

  // Sort by created_at descending
  const sorted = [...data].sort((a, b) => parseDateSafe(b.created_at) - parseDateSafe(a.created_at));

  sorted.forEach((event, index) => {
    const row = document.createElement("tr");

    const status = normalizeStatus(event.status || "UNKNOWN");
    const statusClass = status === "OPEN" ? "status-open" : "status-resolved";

    // Format date to YYYY-MM-DD HH:mm:ss UTC
    const createdDate = parseDateSafe(event.created_at);
    const created = createdDate.getTime() === 0 
      ? "N/A"
      : createdDate.toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC');

    // Calculate response time
    let responseTime = "—";
    if (event.closedAt && event.created_at) {
      const createdMs = parseDateSafe(event.created_at).getTime();
      const closedMs = parseDateSafe(event.closedAt).getTime();
      if (createdMs > 0 && closedMs > createdMs) {
        const diffSec = Math.floor((closedMs - createdMs) / 1000);
        const mins = Math.floor(diffSec / 60);
        const secs = diffSec % 60;
        responseTime = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
      }
    } else if (status === "OPEN") {
      responseTime = "In progress";
    }

    // Sequential ID (1-based)
    const displayId = index + 1;

    row.innerHTML = `
      <td>
        <span style="color:#64748b; font-weight:600; font-size:13px;" data-event-id="${event.eventId || ''}">
          ${displayId}
        </span>
      </td>

      <td>
        <span class="status-badge ${statusClass}">${status}</span>
      </td>

      <td style="color:#64748b; font-size:12px;">
        ${created}
      </td>

      <td style="font-size:12px; color:#475569; font-weight:500;">
        ${responseTime}
      </td>
    `;

    tableBody.appendChild(row);
  });
}

// Filter table (ALL / OPEN)
function filterTable(filterType) {
  if (filterType === "ALL") {
    renderTable(allEvents);
    renderDrowningGallery(allEvents);
  } else {
    const filtered = allEvents.filter((e) => normalizeStatus(e.status) === filterType);
    renderTable(filtered);
    renderDrowningGallery(filtered);
  }
}

// ===============================
// Manager: Drowning gallery (Before/After cards)
// ===============================
function renderDrowningGallery(events) {
  const gallery = document.getElementById("drowning-gallery");
  if (!gallery) return;

  gallery.innerHTML = "";

  // Only events that have drowning images
  const drowningEvents = [...events]
    .filter((e) => !!e.warningImageUrl) // has AFTER
    .sort((a, b) => parseDateSafe(b.created_at) - parseDateSafe(a.created_at));

  if (drowningEvents.length === 0) {
    gallery.innerHTML = `
      <div style="color:#64748b; font-size:12px;">
        No drowning images found yet (no events with warningImageUrl).
      </div>`;
    return;
  }

  drowningEvents.forEach((evt) => {
    const card = document.createElement("div");
    card.style.background = "white";
    card.style.border = "1px solid rgba(148,163,184,0.35)";
    card.style.borderRadius = "14px";
    card.style.padding = "12px";
    card.style.boxShadow = "0 8px 20px rgba(15,23,42,0.06)";

    const beforeUrl = evt.prevImageUrl || null;
    const afterUrl = evt.warningImageUrl || null;

    const status = normalizeStatus(evt.status);
    const created = evt.created_at || "N/A";

    const beforeImg = beforeUrl
      ? `${beforeUrl}${beforeUrl.includes("?") ? "&" : "?"}t=${Date.now()}`
      : "";

    const afterImg = afterUrl
      ? `${afterUrl}${afterUrl.includes("?") ? "&" : "?"}t=${Date.now()}`
      : "";

    // Format date
    const createdDate = parseDateSafe(evt.created_at);
    const createdFormatted = createdDate.getTime() === 0 
      ? "N/A"
      : createdDate.toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC');

    card.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px; margin-bottom:10px;">
        <div style="min-width:0;">
          <div style="font-family:monospace; font-size:11px; color:#64748b; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
            ${evt.eventId || "N/A"}
          </div>
          <div style="font-size:11px; color:#64748b; margin-top:3px;">
            ${createdFormatted}
          </div>
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <span class="status-badge ${status === "OPEN" ? "status-open" : "status-resolved"}">${status}</span>
        </div>
      </div>

      <div style="display:flex; gap:10px;">
        <div style="flex:1;">
          <div style="font-size:10px; font-weight:600; opacity:0.7; margin-bottom:6px; color:#475569;">BEFORE</div>
          ${
            beforeImg
              ? `<img src="${beforeImg}" style="width:100%; border-radius:10px;" />`
              : `<div style="font-size:11px; color:#94a3b8;">No prevImageUrl</div>`
          }
        </div>

        <div style="flex:1;">
          <div style="font-size:10px; font-weight:600; opacity:0.7; margin-bottom:6px; color:#475569;">AFTER</div>
          ${
            afterImg
              ? `<img src="${afterImg}" style="width:100%; border-radius:10px;" />`
              : `<div style="font-size:11px; color:#94a3b8;">No warningImageUrl</div>`
          }
        </div>
      </div>
    `;

    gallery.appendChild(card);
  });
}

// ===============================
// IMAGE LIGHTBOX (Minimal JS)
// ===============================
(function() {
  // Create lightbox HTML on load
  if (!document.getElementById('image-lightbox')) {
    const lightbox = document.createElement('div');
    lightbox.id = 'image-lightbox';
    lightbox.innerHTML = `
      <div id="lightbox-content">
        <button id="lightbox-close" aria-label="Close">&times;</button>
        <img id="lightbox-image" src="" alt="Lightbox Image" />
        <div id="lightbox-zoom-controls">
          <button class="lightbox-zoom-btn" id="zoom-out" aria-label="Zoom Out">-</button>
          <button class="lightbox-zoom-btn" id="zoom-reset" aria-label="Reset Zoom">⟲</button>
          <button class="lightbox-zoom-btn" id="zoom-in" aria-label="Zoom In">+</button>
        </div>
      </div>
    `;
    document.body.appendChild(lightbox);

    let currentZoom = 1;

    // Open lightbox
    document.addEventListener('click', (e) => {
      if (e.target.tagName === 'IMG' && e.target.src && !e.target.closest('#image-lightbox')) {
        const lb = document.getElementById('image-lightbox');
        const lbImg = document.getElementById('lightbox-image');
        lbImg.src = e.target.src;
        currentZoom = 1;
        lbImg.style.transform = `scale(${currentZoom})`;
        lb.classList.add('active');
      }
    });

    // Close lightbox
    const closeLightbox = () => {
      document.getElementById('image-lightbox').classList.remove('active');
      currentZoom = 1;
    };

    document.getElementById('lightbox-close').addEventListener('click', closeLightbox);
    
    document.getElementById('image-lightbox').addEventListener('click', (e) => {
      if (e.target.id === 'image-lightbox') closeLightbox();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeLightbox();
    });

    // Zoom controls
    const lbImg = document.getElementById('lightbox-image');
    
    document.getElementById('zoom-in').addEventListener('click', () => {
      currentZoom = Math.min(currentZoom + 0.25, 3);
      lbImg.style.transform = `scale(${currentZoom})`;
    });

    document.getElementById('zoom-out').addEventListener('click', () => {
      currentZoom = Math.max(currentZoom - 0.25, 0.5);
      lbImg.style.transform = `scale(${currentZoom})`;
    });

    document.getElementById('zoom-reset').addEventListener('click', () => {
      currentZoom = 1;
      lbImg.style.transform = `scale(${currentZoom})`;
    });

    // Mouse wheel zoom
    lbImg.addEventListener('wheel', (e) => {
      e.preventDefault();
      if (e.deltaY < 0) {
        currentZoom = Math.min(currentZoom + 0.1, 3);
      } else {
        currentZoom = Math.max(currentZoom - 0.1, 0.5);
      }
      lbImg.style.transform = `scale(${currentZoom})`;
    });
  }
})();
