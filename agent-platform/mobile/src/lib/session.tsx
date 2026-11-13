/**
 * 登录会话：服务器地址、用户中心的会话、CSRF 头。
 *
 * ★ 和网页同一套登录（doc/identity-propagation-design.html §4）：调用户中心的 POST /uc/api/v1/auth/login，
 *   会话在 HttpOnly Cookie 里。App 的 fetch（expo/fetch）用系统的 CookieManager，之后的请求自动带上，
 *   网关照常校验、写 X-Uid —— 后端不用为 App 改任何东西。
 * ★ 写请求要带 CSRF 双提交头：值等于 uc_csrf Cookie。App 读不到 Cookie 存储，登录时从 Set-Cookie 里取出来存好。
 * ★ 服务器地址让用户填（企业自己部署），记在 SecureStore 里；CSRF 值也在那里。
 */
import * as SecureStore from "expo-secure-store";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import type { AtlasClientConfig } from "@atlas/chat-ui/src/lib/http";

const KEY_SERVER = "atlas.server";
const KEY_CSRF = "atlas.csrf";
const KEY_ACCOUNT = "atlas.account";

export const DEFAULT_SERVER = "https://atlas.techstz.com";
/** 0.1.0 的默认地址是 HTTP；App 已不允许明文，装过 0.1.0 的手机里记着它，读出来换成 DEFAULT_SERVER */
const LEGACY_DEFAULT_SERVER = "http://atlas.techstz.com";

export interface Session {
  /** 还在读本地存的登录信息 */
  loading: boolean;
  server: string;
  /** 上次登录用的账号（登录页预填） */
  account: string;
  signedIn: boolean;
  /** Atlas 接口（chat-ui 的 hooks 用它） */
  client: AtlasClientConfig;
  login: (server: string, account: string, password: string) => Promise<{ mustChangePassword: boolean }>;
  logout: () => Promise<void>;
  /** 接口回 401：会话过期，回登录页 */
  expire: () => void;
}

const Ctx = createContext<Session | null>(null);

export function useSession(): Session {
  const s = useContext(Ctx);
  if (!s) throw new Error("缺少 <SessionProvider>");
  return s;
}

/** 去掉末尾的 /，没写协议的补 https://（App 不允许明文 HTTP） */
export function normalizeServer(raw: string): string {
  const s = raw.trim().replace(/\/+$/, "");
  return /^https?:\/\//i.test(s) ? s : `https://${s}`;
}

/** Set-Cookie 里取某个 Cookie 的值。多条 Set-Cookie 会被合成一行（逗号分隔），按名字找即可 */
function cookieFrom(setCookie: string | null, name: string): string | null {
  if (!setCookie) return null;
  const m = new RegExp(`(?:^|[;,]\\s*)${name}=([^;,]+)`).exec(setCookie);
  return m ? m[1]! : null;
}

async function errorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { error?: { message?: string }; detail?: string };
    return body.error?.message ?? body.detail ?? `登录失败（${res.status}）`;
  } catch {
    return res.status >= 500 ? "系统忙不过来了，请稍后再试" : `登录失败（${res.status}）`;
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [server, setServer] = useState(DEFAULT_SERVER);
  const [account, setAccount] = useState("");
  const [csrf, setCsrf] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      const [s, c, a] = await Promise.all([
        SecureStore.getItemAsync(KEY_SERVER),
        SecureStore.getItemAsync(KEY_CSRF),
        SecureStore.getItemAsync(KEY_ACCOUNT),
      ]);
      if (s) setServer(s === LEGACY_DEFAULT_SERVER ? DEFAULT_SERVER : s);
      if (a) setAccount(a);
      setCsrf(c);
      setLoading(false);
    })();
  }, []);

  const login = useCallback(async (rawServer: string, acct: string, password: string) => {
    const base = normalizeServer(rawServer);
    const res = await fetch(`${base}/uc/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account: acct.trim(), password }),
      credentials: "include",
    });
    if (!res.ok) throw new Error(await errorMessage(res));
    const token = cookieFrom(res.headers.get("set-cookie"), "uc_csrf");
    if (!token) throw new Error("登录成功了，但没有拿到校验信息（uc_csrf），请联系管理员");
    const body = (await res.json()) as { must_change_password?: boolean };
    await Promise.all([
      SecureStore.setItemAsync(KEY_SERVER, base),
      SecureStore.setItemAsync(KEY_CSRF, token),
      SecureStore.setItemAsync(KEY_ACCOUNT, acct.trim()),
    ]);
    setServer(base);
    setAccount(acct.trim());
    setCsrf(token);
    return { mustChangePassword: Boolean(body.must_change_password) };
  }, []);

  const expire = useCallback(() => {
    setCsrf(null);
    void SecureStore.deleteItemAsync(KEY_CSRF);
  }, []);

  const logout = useCallback(async () => {
    // 先让用户中心作废会话（顺带清掉 Cookie）；失败也照样退出本地
    try {
      await fetch(`${server}/uc/api/v1/auth/logout`, {
        method: "POST",
        headers: csrf ? { "X-UC-CSRF": csrf } : {},
        credentials: "include",
      });
    } catch {
      // 网络不通时也要能退出
    }
    expire();
  }, [server, csrf, expire]);

  const client = useMemo<AtlasClientConfig>(
    () => ({
      apiBase: `${server}/atlas-api`,
      headers: (): Record<string, string> => (csrf ? { "X-UC-CSRF": csrf } : {}),
    }),
    [server, csrf],
  );

  const value = useMemo<Session>(
    () => ({ loading, server, account, signedIn: Boolean(csrf), client, login, logout, expire }),
    [loading, server, account, csrf, client, login, logout, expire],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
