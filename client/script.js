// API configuration
const API_BASE_URL = "https://zat8d5ozy1.execute-api.us-east-1.amazonaws.com";

// Handle user authentication and redirection
function handleLogin() {
  const user = document.getElementById("username").value.toLowerCase();

  if (user.includes("admin")) {
    showScreen("manager-dashboard");
    fetchEvents();
  } else if (user.includes("guard")) {
    showScreen("lifeguard-dashboard");
    // Start monitoring for live alerts
    checkLiveAlerts();
    setInterval(checkLiveAlerts, 3000);
  } else {
    alert("Access Denied. Please use 'guard' or 'admin' as username.");
  }
}

//Monitors for drowning alerts and updates the UI with real-time data
async function checkLiveAlerts() {
  const dashboard = document.getElementById("lifeguard-dashboard");
  if (dashboard.classList.contains("hidden")) return;

  try {
    const res = await fetch(`${API_BASE_URL}/events`);
    const data = await res.json();

    console.log("Raw data from API:", data);

    if (data.length > 0) {
      console.log("Example of first event timestamp:", data[0].timestamp);
    }

    // Find high-risk open alerts
    const activeAlert = data.find(
      (evt) =>
        String(evt.status).toUpperCase() === "OPEN" &&
        parseFloat(evt.riskScore) > 85
    );

    const overlay = document.getElementById("emergency-overlay");

    if (activeAlert) {
      // Play alert sound
      new Audio("https://www.soundjay.com/buttons/beep-01a.mp3")
        .play()
        .catch(() => {});

      overlay.classList.remove("hidden");

      // Update Zone and Risk Score
      document.getElementById("display-zone").innerText = `Zone ${
        activeAlert.zone || "4"
      } — Depth 2m`;
      document.getElementById(
        "display-score"
      ).innerText = `${activeAlert.riskScore}/100`;

      // Generate and update current time
      const now = new Date();
      const hours = String(now.getHours()).padStart(2, "0");
      const minutes = String(now.getMinutes()).padStart(2, "0");
      document.getElementById("display-time").innerText = `${hours}:${minutes}`;

      // Assign dismissal logic
      document.getElementById("close-btn-id").onclick = () =>
        dismissAlert(activeAlert.eventId);
    } else {
      overlay.classList.add("hidden");
    }
  } catch (e) {
    console.error("Monitoring Error:", e);
  }
}

// Update event status to RESOLVED via PATCH request
async function dismissAlert(eventId) {
  try {
    const res = await fetch(`${API_BASE_URL}/events`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eventId: eventId }),
    });

    if (res.ok) {
      document.getElementById("emergency-overlay").classList.add("hidden");
      checkLiveAlerts();
    }
  } catch (err) {
    console.error("Dismissal Error:", err);
  }
}

// Fetch historical data for Manager view
async function fetchEvents() {
  try {
    const res = await fetch(`${API_BASE_URL}/events`);
    const data = await res.json();
    document.getElementById("stat-total").innerText = data.length;
  } catch (e) {
    console.error("Data Fetch Error:", e);
  }
}

// Switch between different system screens
function showScreen(id) {
  const screens = [
    "login-screen",
    "signup-screen",
    "lifeguard-dashboard",
    "manager-dashboard",
  ];
  screens.forEach((s) => {
    document.getElementById(s).classList.add("hidden");
  });
  document.getElementById(id).classList.remove("hidden");
}

//Logout and reset system
function logout() {
  location.reload();
}

let allEvents = []; // Global variable to store fetched data

async function fetchEvents() {
  try {
    const res = await fetch(`${API_BASE_URL}/events`);
    allEvents = await res.json();

    // Update stats
    document.getElementById("stat-total").innerText = allEvents.length;

    // Initial table render
    renderTable(allEvents);
  } catch (e) {
    console.error("Fetch Events Error:", e);
  }
}

function renderTable(data) {
  const tableBody = document.getElementById("events-table-body");
  tableBody.innerHTML = "";

  // Sort by created_at descending
  data.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

  data.forEach((event) => {
    const row = document.createElement("tr");
    const status = String(event.status || "UNKNOWN").toUpperCase();
    const statusClass = status === "OPEN" ? "status-open" : "status-resolved";

    // Formatting the date part only from created_at string
    const datePart = event.created_at ? event.created_at.split(" ")[0] : "N/A";

    // Clean up drowning type for display (e.g., PASSIVE_DROWNING -> Passive)
    const rawType = event.drowning_type || "N/A";
    const cleanType = rawType.replace("_DROWNING", "").toLowerCase();
    const displayType = cleanType.charAt(0).toUpperCase() + cleanType.slice(1);

    row.innerHTML = `
      <td><span style="color:#94a3b8; font-family: monospace; font-size:11px;">#${String(
        event.eventId
      ).substring(4, 10)}</span></td>
      <td style="font-weight:600;">Zone ${event.zone || "4"}</td>
      <td><strong style="color: ${
        event.riskScore > 90 ? "#dc2626" : "#1e293b"
      }">${event.riskScore}%</strong></td>
      <td style="font-size:12px;">${displayType}</td>
      <td style="font-size:11px; color:#64748b;">${
        event.camera_id || "CAM-01"
      }</td>
      <td><span class="status-badge ${statusClass}">${status}</span></td>
      <td style="color:#64748b; font-size:11px;">${datePart}</td>
    `;
    tableBody.appendChild(row);
  });
}

function filterTable(filterType) {
  if (filterType === "ALL") {
    renderTable(allEvents);
  } else {
    const filtered = allEvents.filter(
      (e) => String(e.status).toUpperCase() === filterType
    );
    renderTable(filtered);
  }
}
