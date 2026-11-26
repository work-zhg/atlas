/**
 * 请求核心：访问令牌过期（401）先单飞刷新、重放一次；刷新不了就把 401 交给调用方。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, request } from "./http";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("request", () => {
  it("401 后刷新成功就重放一次", async () => {
    const statuses = [401, 200];
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: statuses.shift()! }));
    vi.stubGlobal("fetch", fetchMock);
    const refresh = vi.fn(async () => true);
    const out = await request<{ ok: boolean }>({ apiBase: "http://atlas.test", refresh }, "/v1/me", {
      method: "POST",
      body: { a: 1 },
    });
    expect(out).toEqual({ ok: true });
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // 重放带着同样的请求体
    const second = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(second[1].body).toBe(JSON.stringify({ a: 1 }));
  });

  it("刷新不了就抛 401；没给 refresh 也不重放", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
    await expect(request({ apiBase: "http://atlas.test", refresh: async () => false }, "/v1/me")).rejects.toMatchObject({
      status: 401,
    });
    const fetchMock = vi.fn(async () => new Response("{}", { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(request({ apiBase: "http://atlas.test" }, "/v1/me")).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
