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
function saveTokensFromLoginResponse(data) {
  // data: { accessToken, idToken, refreshToken, expiresIn, ... }
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
// AUTH (Lambda Auth - returns tokens in JSON)
// ===============================
async function authLogin(username, password) {
  const res = await fetch(`${AUTH_BASE_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });

  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data?.message || `Login failed (${res.status})`);
  }
  return data; // { ok, role, accessToken, idToken, ... }
}

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
// LOGIN BUTTON (YOUR FORM)
// ===============================
async function handleLogin() {
  const username = (document.getElementById("username")?.value || "").trim();
  const password = (document.getElementById("password")?.value || "").trim();

  const errEl = document.getElementById("auth-error");
  if (errEl) errEl.classList.add("hidden");

  if (!username || !password) {
    if (errEl) {
      errEl.classList.remove("hidden");
      errEl.innerText = "Please enter username/email and password.";
    } else {
      alert("Please enter username/email and password.");
    }
    return;
  }

  try {
    const me = await authLogin(username, password);

    //  לשמור טוקנים אחרי login
    saveTokensFromLoginResponse(me);

    routeAfterLogin(me.role);
  } catch (e) {
    if (errEl) {
      errEl.classList.remove("hidden");
      errEl.innerText = e.message || "Login failed";
    } else {
      alert(e.message || "Login failed");
    }
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

  const errEl = document.getElementById("auth-error");
  if (errEl) {
    errEl.classList.remove("hidden");
    errEl.innerText =
      "No role found. Add user to Admins/Lifeguards group in Cognito.";
  }
}

// ===============================
// BOOTSTRAP
// ===============================
document.addEventListener("DOMContentLoaded", async () => {
  // Auto-login if tokens exist
  try {
    const me = await authMe();
    if (me?.ok) routeAfterLogin(me.role);
  } catch {
    // stay on login
  }
});
