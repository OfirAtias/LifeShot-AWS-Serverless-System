import {
  CognitoIdentityProviderClient,
  InitiateAuthCommand,
} from "@aws-sdk/client-cognito-identity-provider";

// ===============================
// ENV
// ===============================
const REGION = process.env.COGNITO_REGION || "us-east-1";
const CLIENT_ID = process.env.COGNITO_CLIENT_ID;
const ALLOWED_ORIGIN = process.env.ALLOWED_ORIGIN || "http://localhost:5500";

// sanity
if (!CLIENT_ID) console.warn("Missing env COGNITO_CLIENT_ID");

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
    Vary: "Origin",
  };
}

function json(statusCode, bodyObj, origin, extraHeaders = {}) {
  return {
    statusCode,
    headers: {
      "Content-Type": "application/json",
      ...corsHeaders(origin),
      ...extraHeaders,
    },
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
  if (
    g.includes("lifeguards") ||
    g.includes("lifeguard") ||
    g.includes("guard")
  )
    return "guard";
  return "unknown";
}

function getBearerToken(event) {
  const h = event.headers || {};
  const auth = h.authorization || h.Authorization || "";
  const m = String(auth).match(/^Bearer\s+(.+)$/i);
  return m ? m[1].trim() : "";
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
    AuthParameters: { USERNAME: username, PASSWORD: password },
  });

  try {
    const resp = await cognito.send(cmd);
    const auth = resp.AuthenticationResult || {};

    const accessToken = auth.AccessToken || "";
    const idToken = auth.IdToken || "";
    const refreshToken = auth.RefreshToken || "";
    const expiresIn = Number(auth.ExpiresIn || 3600);

    if (!accessToken || !idToken) {
      return json(
        401,
        { message: "Login failed (no tokens returned)" },
        origin,
      );
    }

    const payload = decodeJwtPayload(idToken) || {};
    const groups = Array.isArray(payload["cognito:groups"])
      ? payload["cognito:groups"]
      : [];
    const role = getRoleFromGroups(groups);

    return json(
      200,
      {
        ok: true,
        role,
        username: payload["cognito:username"] || username,
        email: payload["email"] || "",
        groups,
        expiresIn,
        accessToken,
        idToken,
        refreshToken, // Optional, if no ans it will be = ""
      },
      origin,
    );
  } catch (err) {
    const detail = err?.name || "AuthError";
    return json(401, { message: "Invalid credentials", detail }, origin);
  }
}

async function routeMe(event, origin) {
  const token = getBearerToken(event); //  idToken or accessToken
  if (!token)
    return json(401, { ok: false, message: "Missing Bearer token" }, origin);

  const payload = decodeJwtPayload(token);
  if (!payload)
    return json(401, { ok: false, message: "Invalid token" }, origin);

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
    origin,
  );
}

async function routeLogout(event, origin) {
  return json(200, { ok: true }, origin);
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
    return { statusCode: 204, headers: corsHeaders(origin), body: "" };
  }

  if (method === "POST" && path.endsWith("/auth/login"))
    return routeLogin(event, origin);
  if (method === "GET" && path.endsWith("/auth/me"))
    return routeMe(event, origin);
  if (method === "POST" && path.endsWith("/auth/logout"))
    return routeLogout(event, origin);

  return json(404, { message: "Not found", path, method }, origin);
};
