// ===============================
// CONFIG
// ===============================
const API_BASE_URL =
  window.API_BASE_URL ||
  "https://2q66aqqv1c.execute-api.us-east-1.amazonaws.com";

// ✅ Auth routes are on the same HTTP API by default
// (still allows overriding from HTML if you set window.AUTH_BASE_URL)
const AUTH_BASE_URL = window.AUTH_BASE_URL || API_BASE_URL;

// ===============================
// TOKEN STORAGE (LOCALSTORAGE)
// ===============================
function saveTokensFromLoginResponse(data) {
  localStorage.setItem("ls_access_token", data?.accessToken || "");
  localStorage.setItem("ls_id_token", data?.idToken || "");
  localStorage.setItem("ls_refresh_token", data?.refreshToken || "");

  if (data?.expiresIn) {
    localStorage.setItem(
      "ls_expires_at",
      String(Date.now() + Number(data.expiresIn) * 1000)
    );
  } else {
    localStorage.removeItem("ls_expires_at");
  }
}

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
  if (!exp) return false;
  return Date.now() > exp - 15_000; // 15s safety
}

// ✅ For API Gateway JWT Authorizer: use Access Token (recommended).
// Fallback to ID token only if missing (debug)
function getApiBearerToken() {
  const at = getAccessToken();
  if (at) return at;
  return getIdToken();
}

function authHeader() {
  const token = getApiBearerToken();
  if (!token || isTokenExpired()) return {};
  return { Authorization: `Bearer ${token}` };
}

// ===============================
// AUTH (Lambda/Auth routes - returns tokens in JSON)
// ===============================
async function authLogin(username, password) {
  const res = await fetch(`${AUTH_BASE_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });

  const data = await res.json().catch(() => ({}));

  // ✅ if NEW_PASSWORD_REQUIRED
  if (res.status === 409 && data?.challenge === "NEW_PASSWORD_REQUIRED") {
    return data; // { challenge, session, username, ... }
  }

  if (!res.ok) {
    throw new Error(data?.message || `Login failed (${res.status})`);
  }

  return data; // { ok, role, accessToken, idToken, ... }
}

async function authCompletePassword(username, session, newPassword) {
  const res = await fetch(`${AUTH_BASE_URL}/auth/complete-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, session, newPassword }),
  });

  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data?.message || `Password change failed (${res.status})`);
  }
  return data; // tokens like login
}

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
  return data;
}

async function authLogout() {
  await fetch(`${AUTH_BASE_URL}/auth/logout`, {
    method: "POST",
    cache: "no-store",
  }).catch(() => {});
  clearTokens();
}

// ===============================
// UI HELPERS (very simple)
// ===============================
function showError(msg) {
  const errEl = document.getElementById("auth-error");
  if (errEl) {
    errEl.classList.remove("hidden");
    errEl.innerText = msg || "Login failed";
  } else {
    alert(msg || "Login failed");
  }
}

function hideError() {
  const errEl = document.getElementById("auth-error");
  if (errEl) errEl.classList.add("hidden");
}

// ===============================
// LOGIN BUTTON (YOUR FORM)
// ===============================
async function handleLogin() {
  const username = (document.getElementById("username")?.value || "").trim();
  const password = (document.getElementById("password")?.value || "").trim();

  hideError();

  if (!username || !password) {
    showError("Please enter username/email and password.");
    return;
  }

  try {
    const resp = await authLogin(username, password);

    // ✅ must change password
    if (resp?.challenge === "NEW_PASSWORD_REQUIRED") {
      const newPassword = prompt(
        "Your account requires a new password. Enter a new password:"
      );
      if (!newPassword) {
        showError("Password change cancelled.");
        return;
      }

      const tokens = await authCompletePassword(
        resp.username || username,
        resp.session,
        newPassword
      );

      saveTokensFromLoginResponse(tokens);
      routeAfterLogin(tokens.role);
      return;
    }

    // ✅ normal
    saveTokensFromLoginResponse(resp);
    routeAfterLogin(resp.role);
  } catch (e) {
    showError(e?.message || "Login failed");
  }
}

function routeAfterLogin(role) {
  const r = String(role || "").toLowerCase();

  if (r === "admin") {
    window.location.href = "admin.html";
    return;
  }

  if (r === "guard" || r === "lifeguard") {
    window.location.href = "lifeguard.html";
    return;
  }

  showError("No role found. Add user to Admins/Lifeguards group in Cognito.");
}

// ===============================
// BOOTSTRAP
// ===============================
document.addEventListener("DOMContentLoaded", async () => {
  // זמנית: לא עושים auto redirect כדי לעצור לופים
});
