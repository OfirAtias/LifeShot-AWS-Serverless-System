import {
  CognitoIdentityProviderClient,
  InitiateAuthCommand,
} from "@aws-sdk/client-cognito-identity-provider";

// ===============================
// ENV
// ===============================
const REGION = process.env.COGNITO_REGION || "us-east-1";
const CLIENT_ID = process.env.COGNITO_CLIENT_ID;
const USER_POOL_ID = process.env.COGNITO_USER_POOL_ID;

const COOKIE_DOMAIN = process.env.COOKIE_DOMAIN || ""; // empty in dev
const COOKIE_SECURE =
  String(process.env.COOKIE_SECURE || "false").toLowerCase() === "true";
const ALLOWED_ORIGIN = process.env.ALLOWED_ORIGIN || "http://localhost:5500";
const COOKIE_NAME_PREFIX = process.env.COOKIE_NAME_PREFIX || "ls";

// sanity
if (!CLIENT_ID) console.warn("Missing env COGNITO_CLIENT_ID");
if (!USER_POOL_ID) console.warn("Missing env COGNITO_USER_POOL_ID");

const cognito = new CognitoIdentityProviderClient({ region: REGION });

// ===============================
// HELPERS
// ===============================
function corsHeaders(origin) {
  const reqOrigin = origin || "";
  const allowOrigin =
    reqOrigin && reqOrigin === ALLOWED_ORIGIN ? reqOrigin : ALLOWED_ORIGIN;

  return {
    "Access-Control-Allow-Origin": allowOrigin,
    "Access-Control-Allow-Credentials": "true",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  };
}

function json(statusCode, bodyObj, origin, extraHeaders = {}, cookies = []) {
  return {
    statusCode,
    headers: {
      "Content-Type": "application/json",
      ...corsHeaders(origin),
      ...extraHeaders,
    },
    // Function URL supports multiValueHeaders for Set-Cookie
    multiValueHeaders: cookies.length ? { "Set-Cookie": cookies } : undefined,
    body: JSON.stringify(bodyObj),
  };
}

function parseJsonBody(event) {
  try {
    if (!event.body) return {};
    return typeof event.body === "string" ? JSON.parse(event.body) : event.body;
  } catch {
    return {};
  }
}

function parseCookies(event) {
  const out = {};

  // Function URL often supplies event.cookies array
  if (Array.isArray(event.cookies)) {
    event.cookies.forEach((c) => {
      const idx = c.indexOf("=");
      if (idx > -1)
        out[c.slice(0, idx).trim()] = decodeURIComponent(c.slice(idx + 1));
    });
    return out;
  }

  const raw = event.headers?.cookie || event.headers?.Cookie || "";
  raw.split(";").forEach((p) => {
    const idx = p.indexOf("=");
    if (idx > -1)
      out[p.slice(0, idx).trim()] = decodeURIComponent(p.slice(idx + 1));
  });
  return out;
}

function cookieBaseOptions() {
  const parts = ["Path=/", "HttpOnly", "SameSite=Lax"];
  if (COOKIE_SECURE) parts.push("Secure");
  if (COOKIE_DOMAIN) parts.push(`Domain=${COOKIE_DOMAIN}`);
  return parts.join("; ");
}

function setCookie(name, value, maxAgeSeconds) {
  const base = cookieBaseOptions();
  const maxAge = Number(maxAgeSeconds || 0);
  const maxAgePart = maxAge ? `Max-Age=${maxAge}` : "Max-Age=0";
  return `${name}=${encodeURIComponent(value || "")}; ${base}; ${maxAgePart}`;
}

function clearCookie(name) {
  const base = cookieBaseOptions();
  return `${name}=; ${base}; Max-Age=0`;
}

function decodeJwtPayload(token) {
  try {
    const parts = String(token || "").split(".");
    if (parts.length < 2) return null;

    const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64 + "===".slice((b64.length + 3) % 4);
    const jsonStr = Buffer.from(padded, "base64").toString("utf8");
    return JSON.parse(jsonStr);
  } catch {
    return null;
  }
}

function getRoleFromGroups(groups) {
  const g = (groups || []).map((x) => String(x).toLowerCase());
  if (g.includes("admins") || g.includes("admin")) return "admin";
  if (g.includes("lifeguards") || g.includes("lifeguard") || g.includes("guard"))
    return "guard";
  return "unknown";
}

// ===============================
// ROUTES
// ===============================
async function routeLogin(event, origin) {
  const body = parseJsonBody(event);

  const username = String(body.username || "").trim();
  const password = String(body.password || "").trim();

  if (!username || !password) {
    return json(400, { message: "username and password are required" }, origin);
  }

  const cmd = new InitiateAuthCommand({
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: CLIENT_ID,
    AuthParameters: {
      USERNAME: username,
      PASSWORD: password,
    },
  });

  try {
    const resp = await cognito.send(cmd);
    const auth = resp.AuthenticationResult || {};

    const accessToken = auth.AccessToken || "";
    const idToken = auth.IdToken || "";
    const refreshToken = auth.RefreshToken || "";
    const expiresIn = Number(auth.ExpiresIn || 3600);

    if (!accessToken || !idToken) {
      return json(401, { message: "Login failed (no tokens returned)" }, origin);
    }

    const payload = decodeJwtPayload(idToken) || {};
    const groups = Array.isArray(payload["cognito:groups"])
      ? payload["cognito:groups"]
      : [];
    const role = getRoleFromGroups(groups);

    const cookies = [
      setCookie(`${COOKIE_NAME_PREFIX}_at`, accessToken, expiresIn),
      setCookie(`${COOKIE_NAME_PREFIX}_id`, idToken, expiresIn),
    ];

    if (refreshToken) {
      cookies.push(
        setCookie(`${COOKIE_NAME_PREFIX}_rt`, refreshToken, 60 * 60 * 24 * 30)
      ); // 30d
    }

    return json(
      200,
      {
        ok: true,
        role,
        username: payload["cognito:username"] || username,
        groups,
        expiresIn,
      },
      origin,
      {},
      cookies
    );
  } catch (err) {
    const detail = err?.name || "AuthError";
    return json(401, { message: "Invalid credentials", detail }, origin);
  }
}

async function routeMe(event, origin) {
  const cookies = parseCookies(event);
  const idToken = cookies[`${COOKIE_NAME_PREFIX}_id`] || "";
  if (!idToken) return json(401, { ok: false, message: "Not logged in" }, origin);

  const payload = decodeJwtPayload(idToken);
  if (!payload) return json(401, { ok: false, message: "Invalid token" }, origin);

  const groups = Array.isArray(payload["cognito:groups"])
    ? payload["cognito:groups"]
    : [];
  const role = getRoleFromGroups(groups);

  return json(
    200,
    {
      ok: true,
      role,
      username: payload["cognito:username"] || payload["username"] || "",
      email: payload["email"] || "",
      groups,
    },
    origin
  );
}

async function routeLogout(event, origin) {
  const cookies = [
    clearCookie(`${COOKIE_NAME_PREFIX}_at`),
    clearCookie(`${COOKIE_NAME_PREFIX}_id`),
    clearCookie(`${COOKIE_NAME_PREFIX}_rt`),
  ];
  return json(200, { ok: true }, origin, {}, cookies);
}

// ===============================
// MAIN HANDLER
// ===============================
export const handler = async (event) => {
  const method = event.requestContext?.http?.method || event.httpMethod || "";
  const path =
    event.requestContext?.http?.path || event.rawPath || event.path || "";

  const origin = event.headers?.origin || event.headers?.Origin || "";

  // CORS preflight
  if (method === "OPTIONS") {
    return {
      statusCode: 204,
      headers: corsHeaders(origin),
      body: "",
    };
  }

  if (method === "POST" && path.endsWith("/auth/login"))
    return routeLogin(event, origin);
  if (method === "GET" && path.endsWith("/auth/me"))
    return routeMe(event, origin);
  if (method === "POST" && path.endsWith("/auth/logout"))
    return routeLogout(event, origin);

  return json(404, { message: "Not found", path, method }, origin);
};
