/**
 * Haniel Push Relay — Cloudflare Workers
 *
 * Stateless relay: receives push requests from orch-server,
 * forwards to iOS devices via APNs HTTP/2.
 */

export interface Env {
  DEVICE_TOKENS: KVNamespace;
  INSTANCE_KEY: string;
  APNS_KEY_ID: string;
  APNS_TEAM_ID: string;
  APNS_PRIVATE_KEY: string; // PEM PKCS8 P-256 key
  APNS_BUNDLE_ID: string;
}

interface PushRequest {
  title: string;
  body: string;
  data?: Record<string, unknown>;
}

interface DeviceRegistration {
  token: string;
  device_name: string;
}

interface DeviceRecord {
  device_name: string;
  registered_at: string;
}

// --- JWT cache (module-level, survives across requests within same isolate) ---

let jwtCache: { token: string; expiresAt: number } | null = null;

// --- Helpers ---

function base64url(input: string | ArrayBuffer): string {
  let b64: string;
  if (typeof input === "string") {
    b64 = btoa(input);
  } else {
    const bytes = new Uint8Array(input);
    let binary = "";
    for (let i = 0; i < bytes.length; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    b64 = btoa(binary);
  }
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function pemToDer(pem: string): ArrayBuffer {
  const b64 = pem.replace(/-----[^-]+-----/g, "").replace(/\s/g, "");
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes.buffer;
}

export async function createApnsJwt(env: Env): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  if (jwtCache && jwtCache.expiresAt > now + 60) {
    return jwtCache.token;
  }

  const key = await crypto.subtle.importKey(
    "pkcs8",
    pemToDer(env.APNS_PRIVATE_KEY),
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["sign"],
  );

  const header = base64url(JSON.stringify({ alg: "ES256", kid: env.APNS_KEY_ID }));
  const payload = base64url(JSON.stringify({ iss: env.APNS_TEAM_ID, iat: now }));
  const sigInput = new TextEncoder().encode(`${header}.${payload}`);
  const signature = await crypto.subtle.sign(
    { name: "ECDSA", hash: "SHA-256" },
    key,
    sigInput,
  );

  const jwt = `${header}.${payload}.${base64url(signature)}`;
  jwtCache = { token: jwt, expiresAt: now + 3480 }; // ~58 minutes
  return jwt;
}

/** Reset JWT cache (for testing). */
export function resetJwtCache(): void {
  jwtCache = null;
}

// --- Route handlers ---

async function handlePush(request: Request, env: Env): Promise<Response> {
  const body = await request.json<PushRequest>();
  if (!body.title || !body.body) {
    return Response.json({ error: "title and body are required" }, { status: 400 });
  }

  // List all device tokens from KV
  const listed = await env.DEVICE_TOKENS.list();
  if (listed.keys.length === 0) {
    return Response.json({ sent: 0, failed: 0, errors: [] });
  }

  const jwt = await createApnsJwt(env);
  const apnsPayload = JSON.stringify({
    aps: { alert: { title: body.title, body: body.body }, sound: "default" },
    ...(body.data ?? {}),
  });

  let sent = 0;
  let failed = 0;
  const errors: Array<{ token: string; status: number }> = [];

  const results = await Promise.allSettled(
    listed.keys.map(async (key) => {
      const token = key.name;
      try {
        const resp = await fetch(
          `https://api.push.apple.com/3/device/${token}`,
          {
            method: "POST",
            headers: {
              authorization: `bearer ${jwt}`,
              "apns-topic": env.APNS_BUNDLE_ID,
              "apns-push-type": "alert",
              "content-type": "application/json",
            },
            body: apnsPayload,
          },
        );

        if (resp.status === 410) {
          // Gone — stale token, remove from KV
          await env.DEVICE_TOKENS.delete(token);
          failed++;
          errors.push({ token, status: 410 });
        } else if (!resp.ok) {
          failed++;
          errors.push({ token, status: resp.status });
        } else {
          sent++;
        }
      } catch {
        failed++;
        errors.push({ token, status: 0 });
      }
    }),
  );

  return Response.json({ sent, failed, errors });
}

async function handleRegisterDevice(request: Request, env: Env): Promise<Response> {
  const body = await request.json<DeviceRegistration>();
  if (!body.token || !body.device_name) {
    return Response.json(
      { error: "token and device_name are required" },
      { status: 400 },
    );
  }

  const record: DeviceRecord = {
    device_name: body.device_name,
    registered_at: new Date().toISOString(),
  };
  await env.DEVICE_TOKENS.put(body.token, JSON.stringify(record));

  return Response.json({ registered: true });
}

async function handleDeleteDevice(token: string, env: Env): Promise<Response> {
  await env.DEVICE_TOKENS.delete(token);
  return Response.json({ deleted: true });
}

// --- Auth middleware ---

function authenticate(request: Request, env: Env): Response | null {
  const auth = request.headers.get("Authorization");
  if (!auth || auth !== `Bearer ${env.INSTANCE_KEY}`) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  return null; // auth passed
}

// --- Router ---

function matchRoute(
  method: string,
  pathname: string,
): { handler: string; params?: Record<string, string> } | null {
  if (method === "POST" && pathname === "/v1/push") {
    return { handler: "push" };
  }
  if (method === "POST" && pathname === "/v1/devices") {
    return { handler: "registerDevice" };
  }
  if (method === "DELETE" && pathname.startsWith("/v1/devices/")) {
    const token = pathname.slice("/v1/devices/".length);
    if (token) {
      return { handler: "deleteDevice", params: { token } };
    }
  }
  return null;
}

// --- Worker entry ---

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const route = matchRoute(request.method, url.pathname);

    if (!route) {
      return Response.json({ error: "not found" }, { status: 404 });
    }

    const authError = authenticate(request, env);
    if (authError) return authError;

    switch (route.handler) {
      case "push":
        return handlePush(request, env);
      case "registerDevice":
        return handleRegisterDevice(request, env);
      case "deleteDevice":
        return handleDeleteDevice(route.params!.token, env);
      default:
        return Response.json({ error: "not found" }, { status: 404 });
    }
  },
} satisfies ExportedHandler<Env>;
