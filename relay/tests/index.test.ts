/**
 * Tests for Haniel Push Relay Worker.
 *
 * Strategy: directly call the Worker's fetch handler with mocked Env.
 * KV operations are mocked via vi.fn(). APNs fetch is mocked via globalThis.fetch spy.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import worker, { type Env, resetJwtCache } from "../src/index";

// --- Test fixtures ---

// Valid 64-char hex device tokens for testing
const TOKEN_A = "a".repeat(64);
const TOKEN_B = "b".repeat(64);
const TOKEN_STALE = "c".repeat(64);
const TOKEN_OK = "d".repeat(64);
const TOKEN_FAIL = "e".repeat(64);

function createMockKV(store: Map<string, string> = new Map()): KVNamespace {
  return {
    get: vi.fn(async (key: string) => store.get(key) ?? null),
    put: vi.fn(async (key: string, value: string) => {
      store.set(key, value);
    }),
    delete: vi.fn(async (key: string) => {
      store.delete(key);
    }),
    list: vi.fn(async () => ({
      keys: Array.from(store.keys()).map((name) => ({ name })),
      list_complete: true,
      cacheStatus: null,
    })),
    getWithMetadata: vi.fn(),
  } as unknown as KVNamespace;
}

// Generate a test P-256 key pair for APNs JWT signing
async function generateTestKey(): Promise<string> {
  const keyPair = await crypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" },
    true,
    ["sign", "verify"],
  );
  const pkcs8 = await crypto.subtle.exportKey("pkcs8", keyPair.privateKey);
  const b64 = btoa(String.fromCharCode(...new Uint8Array(pkcs8)));
  return `-----BEGIN PRIVATE KEY-----\n${b64}\n-----END PRIVATE KEY-----`;
}

let testPem: string;

function createMockEnv(
  kvStore: Map<string, string> = new Map(),
): Env {
  return {
    DEVICE_TOKENS: createMockKV(kvStore),
    INSTANCE_KEY: "test-instance-key",
    APNS_KEY_ID: "KEYID123",
    APNS_TEAM_ID: "TEAMID456",
    APNS_PRIVATE_KEY: testPem,
    APNS_BUNDLE_ID: "com.haniel.app",
  };
}

function makeRequest(
  method: string,
  path: string,
  body?: unknown,
  headers: Record<string, string> = {},
): Request {
  const init: RequestInit = {
    method,
    headers: {
      "Content-Type": "application/json",
      Authorization: "Bearer test-instance-key",
      ...headers,
    },
  };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
  }
  return new Request(`https://relay.example.com${path}`, init);
}

// --- Tests ---

beforeEach(async () => {
  if (!testPem) {
    testPem = await generateTestKey();
  }
  resetJwtCache();
  vi.restoreAllMocks();
});

describe("Authentication", () => {
  it("returns 401 when Authorization header is missing", async () => {
    const env = createMockEnv();
    const req = new Request("https://relay.example.com/v1/push", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "t", body: "b" }),
    });

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    expect(resp.status).toBe(401);
    const data = await resp.json<{ error: string }>();
    expect(data.error).toBe("unauthorized");
  });

  it("returns 401 when token is wrong", async () => {
    const env = createMockEnv();
    const req = makeRequest("POST", "/v1/push", { title: "t", body: "b" }, {
      Authorization: "Bearer wrong-key",
    });

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    expect(resp.status).toBe(401);
  });
});

describe("POST /v1/devices", () => {
  it("registers a device token in KV", async () => {
    const env = createMockEnv();
    const req = makeRequest("POST", "/v1/devices", {
      token: TOKEN_A,
      device_name: "iPhone 15",
    });

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    expect(resp.status).toBe(200);
    const data = await resp.json<{ registered: boolean }>();
    expect(data.registered).toBe(true);

    expect(env.DEVICE_TOKENS.put).toHaveBeenCalledWith(
      TOKEN_A,
      expect.stringContaining("iPhone 15"),
    );
  });

  it("upserts when same token is registered again", async () => {
    const store = new Map<string, string>();
    store.set(
      TOKEN_A,
      JSON.stringify({ device_name: "Old Phone", registered_at: "2025-01-01" }),
    );
    const env = createMockEnv(store);
    const req = makeRequest("POST", "/v1/devices", {
      token: TOKEN_A,
      device_name: "New Phone",
    });

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    expect(resp.status).toBe(200);

    expect(env.DEVICE_TOKENS.put).toHaveBeenCalledWith(
      TOKEN_A,
      expect.stringContaining("New Phone"),
    );
  });

  it("returns 400 when token or device_name is missing", async () => {
    const env = createMockEnv();
    const req = makeRequest("POST", "/v1/devices", { token: TOKEN_A });

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    expect(resp.status).toBe(400);
  });

  it("returns 400 for invalid token format (not 64-char hex)", async () => {
    const env = createMockEnv();
    const req = makeRequest("POST", "/v1/devices", {
      token: "invalid-token-format",
      device_name: "Test",
    });

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    expect(resp.status).toBe(400);
    const data = await resp.json<{ error: string }>();
    expect(data.error).toContain("invalid token format");
  });

  it("returns 400 for token with wrong length", async () => {
    const env = createMockEnv();
    const req = makeRequest("POST", "/v1/devices", {
      token: "abcdef1234", // too short
      device_name: "Test",
    });

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    expect(resp.status).toBe(400);
  });
});

describe("DELETE /v1/devices/:token", () => {
  it("deletes a device token from KV", async () => {
    const store = new Map<string, string>();
    store.set(TOKEN_A, JSON.stringify({ device_name: "Test", registered_at: "" }));
    const env = createMockEnv(store);
    const req = makeRequest("DELETE", `/v1/devices/${TOKEN_A}`);

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    expect(resp.status).toBe(200);
    const data = await resp.json<{ deleted: boolean }>();
    expect(data.deleted).toBe(true);
    expect(env.DEVICE_TOKENS.delete).toHaveBeenCalledWith(TOKEN_A);
  });
});

describe("POST /v1/push", () => {
  it("returns sent: 0 when no devices registered", async () => {
    const env = createMockEnv();
    const req = makeRequest("POST", "/v1/push", {
      title: "Test",
      body: "Hello",
    });

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    expect(resp.status).toBe(200);
    const data = await resp.json<{ sent: number; failed: number }>();
    expect(data.sent).toBe(0);
    expect(data.failed).toBe(0);
  });

  it("sends push to all registered devices", async () => {
    const store = new Map<string, string>();
    store.set(TOKEN_A, JSON.stringify({ device_name: "D1", registered_at: "" }));
    store.set(TOKEN_B, JSON.stringify({ device_name: "D2", registered_at: "" }));
    const env = createMockEnv(store);

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("", { status: 200 }),
    );

    const req = makeRequest("POST", "/v1/push", {
      title: "Deploy",
      body: "New deploy pending",
      data: { deploy_id: "d1", type: "new_pending" },
    });

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    expect(resp.status).toBe(200);
    const data = await resp.json<{ sent: number; failed: number }>();
    expect(data.sent).toBe(2);
    expect(data.failed).toBe(0);

    expect(fetchSpy).toHaveBeenCalledTimes(2);
    const calls = fetchSpy.mock.calls;
    const urls = calls.map((c) => c[0] as string);
    expect(urls.some((u) => u.includes(TOKEN_A))).toBe(true);
    expect(urls.some((u) => u.includes(TOKEN_B))).toBe(true);

    const init = calls[0][1] as RequestInit;
    expect(init.headers).toHaveProperty("apns-topic", "com.haniel.app");
    expect(init.headers).toHaveProperty("apns-push-type", "alert");
    const apnsBody = JSON.parse(init.body as string);
    expect(apnsBody.aps.alert.title).toBe("Deploy");
    expect(apnsBody.deploy_id).toBe("d1");
  });

  it("deletes stale tokens on APNs 410 Gone", async () => {
    const store = new Map<string, string>();
    store.set(TOKEN_STALE, JSON.stringify({ device_name: "Old", registered_at: "" }));
    const env = createMockEnv(store);

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("", { status: 410 }),
    );

    const req = makeRequest("POST", "/v1/push", {
      title: "Test",
      body: "Test",
    });

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    const data = await resp.json<{ sent: number; failed: number; errors: Array<{ token: string; status: number }> }>();
    expect(data.sent).toBe(0);
    expect(data.failed).toBe(1);
    expect(data.errors[0]).toEqual({ token: TOKEN_STALE, status: 410 });
    expect(env.DEVICE_TOKENS.delete).toHaveBeenCalledWith(TOKEN_STALE);
  });

  it("continues processing when APNs call fails for one token", async () => {
    const store = new Map<string, string>();
    store.set(TOKEN_OK, JSON.stringify({ device_name: "Good", registered_at: "" }));
    store.set(TOKEN_FAIL, JSON.stringify({ device_name: "Bad", registered_at: "" }));
    const env = createMockEnv(store);

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : (input as Request).url;
      if (url.includes(TOKEN_FAIL)) {
        throw new Error("network error");
      }
      return new Response("", { status: 200 });
    });

    const req = makeRequest("POST", "/v1/push", {
      title: "Test",
      body: "Test",
    });

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    const data = await resp.json<{ sent: number; failed: number }>();
    expect(data.sent).toBe(1);
    expect(data.failed).toBe(1);
  });

  it("returns 400 when title or body is missing", async () => {
    const env = createMockEnv();
    const req = makeRequest("POST", "/v1/push", { title: "only title" });

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    expect(resp.status).toBe(400);
  });
});

describe("Unknown routes", () => {
  it("returns 404 for unknown path", async () => {
    const env = createMockEnv();
    const req = makeRequest("GET", "/v1/unknown");

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    expect(resp.status).toBe(404);
  });

  it("returns 404 for wrong method on known path", async () => {
    const env = createMockEnv();
    const req = makeRequest("GET", "/v1/push");

    const resp = await worker.fetch(req, env, {} as ExecutionContext);
    expect(resp.status).toBe(404);
  });
});
