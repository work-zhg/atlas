/**
 * 登录会话：服务器地址、访问令牌、刷新令牌（c4-model/apigateway/dynamic-mobile-jwt.html）。
 *
 * ★ 和网页同一套令牌，载体不同：令牌登录接口 POST /uc/api/v1/auth/token 用 JSON 返回访问 JWT（15 分钟）
 *   和刷新令牌（14 天）；业务请求带 Authorization: Bearer，不带 Cookie、不用 CSRF 头。API 网关本地验签，
 *   写 X-Uid —— 后端不用为 App 改任何东西。
 * ★ 访问令牌过期（接口 401）：单飞刷新（POST /uc/api/v1/auth/token/refresh，每次都换一对新的），成功后重放；
 *   刷新也 401（14 天到期、账号或租户停用）才回登录页。
 * ★ 刷新令牌放 SecureStore；访问令牌只在内存里（App 重启后第一个请求 401，自动刷新一次）。
 * ★ 服务器地址让用户填（企业自己部署），记在 SecureStore 里。
 * ★ 企业代码（多租户）：和网页登录页同一套规则 —— 部署里只有一家企业时不用填，多家时要填，
 *   记住上次填的（GET /uc/api/v1/auth/tenant-required 判断）。
 */
import * as SecureStore from "expo-secure-store";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { AtlasClientConfig } from "@atlas/chat-ui/src/lib/http";

const KEY_SERVER = "atlas.server";
/** 升级前存的 CSRF 值（Cookie 会话时代）：读到就删 */
const KEY_LEGACY_CSRF = "atlas.csrf";
const KEY_REFRESH = "atlas.refresh";
const KEY_ACCOUNT = "atlas.account";
const KEY_TENANT = "atlas.tenant";

export const DEFAULT_SERVER = "https://atlas.techstz.com";
/** 0.1.0 的默认地址是 HTTP；App 已不允许明文，装过 0.1.0 的手机里记着它，读出来换成 DEFAULT_SERVER */
const LEGACY_DEFAULT_SERVER = "http://atlas.techstz.com";

interface Tokens {
  access_token: string;
  refresh_token: string;
  must_change_password?: boolean;
}

export interface Session {
  /** 还在读本地存的登录信息 */
  loading: boolean;
  server: string;
  /** 上次登录用的账号（登录页预填） */
  account: string;
  /** 上次登录用的企业代码（登录页预填；只有一家企业时为空） */
  tenant: string;
  signedIn: boolean;
  /** Atlas 接口（chat-ui 的 hooks 用它） */
  client: AtlasClientConfig;
  /** tenant：企业代码，只有一家企业时不传 */
  login: (server: string, account: string, password: string, tenant?: string) => Promise<{ mustChangePassword: boolean }>;
  logout: () => Promise<void>;
  /** 刷新也 401：登录过期，回登录页 */
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

export interface TenantRequired {
  /** 要不要填企业代码 */
  required: boolean;
  /** 只有一家企业时是它（只给名称）；多家时为空（登录前不透露有哪些企业） */
  tenant: { name: string } | null;
}

/** 登录页要不要显示企业代码。连不上返回 null（登录时照常提交，由用户中心报错） */
export async function fetchTenantRequired(rawServer: string): Promise<TenantRequired | null> {
  try {
    const res = await fetch(`${normalizeServer(rawServer)}/uc/api/v1/auth/tenant-required`);
    return res.ok ? ((await res.json()) as TenantRequired) : null;
  } catch {
    return null;
  }
}

async function errorMessage(res: Response): Promise<string> {
  try {
    // 用户中心 {code, message}；网关 {code, message, error: {message}}（如 IP_BLOCKED）
    const body = (await res.json()) as { message?: string; error?: { message?: string }; detail?: string };
    return body.message ?? body.error?.message ?? body.detail ?? `登录失败（${res.status}）`;
  } catch {
    return res.status >= 500 ? "系统忙不过来了，请稍后再试" : `登录失败（${res.status}）`;
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [server, setServer] = useState(DEFAULT_SERVER);
  const [account, setAccount] = useState("");
  const [tenant, setTenant] = useState("");
  const [refreshToken, setRefreshToken] = useState<string | null>(null);
  // 访问令牌只放内存：请求头每次现取（刷新后重放的请求要带新的）
  const access = useRef<string | null>(null);
  const refreshing = useRef<Promise<boolean> | null>(null);

  useEffect(() => {
    void (async () => {
      const [s, r, a, t] = await Promise.all([
        SecureStore.getItemAsync(KEY_SERVER),
        SecureStore.getItemAsync(KEY_REFRESH),
        SecureStore.getItemAsync(KEY_ACCOUNT),
        SecureStore.getItemAsync(KEY_TENANT),
      ]);
      void SecureStore.deleteItemAsync(KEY_LEGACY_CSRF);
      if (s) setServer(s === LEGACY_DEFAULT_SERVER ? DEFAULT_SERVER : s);
      if (a) setAccount(a);
      if (t) setTenant(t);
      setRefreshToken(r);
      setLoading(false);
    })();
  }, []);

  const keep = useCallback(async (tokens: Tokens) => {
    access.current = tokens.access_token;
    await SecureStore.setItemAsync(KEY_REFRESH, tokens.refresh_token);
    setRefreshToken(tokens.refresh_token);
  }, []);

  const login = useCallback(
    async (rawServer: string, acct: string, password: string, rawTenant?: string) => {
      const base = normalizeServer(rawServer);
      // 企业代码不分大小写（用户中心按小写存）
      const code = rawTenant?.trim().toLowerCase() || undefined;
      const res = await fetch(`${base}/uc/api/v1/auth/token`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account: acct.trim(), password, tenant: code }),
      });
      if (!res.ok) throw new Error(await errorMessage(res));
      const tokens = (await res.json()) as Tokens;
      await Promise.all([
        SecureStore.setItemAsync(KEY_SERVER, base),
        SecureStore.setItemAsync(KEY_ACCOUNT, acct.trim()),
        code ? SecureStore.setItemAsync(KEY_TENANT, code) : SecureStore.deleteItemAsync(KEY_TENANT),
      ]);
      setServer(base);
      setAccount(acct.trim());
      setTenant(code ?? "");
      await keep(tokens);
      return { mustChangePassword: Boolean(tokens.must_change_password) };
    },
    [keep],
  );

  const expire = useCallback(() => {
    access.current = null;
    setRefreshToken(null);
    void SecureStore.deleteItemAsync(KEY_REFRESH);
  }, []);

  /** 用刷新令牌换一对新的。单飞：同一时刻多个请求撞上 401 只换一次 */
  const refresh = useCallback((): Promise<boolean> => {
    if (!refreshing.current) {
      refreshing.current = (async () => {
        const rt = await SecureStore.getItemAsync(KEY_REFRESH);
        if (!rt) return false;
        try {
          const res = await fetch(`${server}/uc/api/v1/auth/token/refresh`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ refresh_token: rt }),
          });
          if (res.status === 401) {
            expire();
            return false;
          }
          if (!res.ok) return false;
          await keep((await res.json()) as Tokens);
          return true;
        } catch {
          return false; // 网络不通：不算登录过期
        }
      })().finally(() => {
        refreshing.current = null;
      });
    }
    return refreshing.current;
  }, [server, keep, expire]);

  const logout = useCallback(async () => {
    // 先让用户中心作废刷新令牌；失败也照样退出本地
    try {
      if (refreshToken) {
        await fetch(`${server}/uc/api/v1/auth/token/revoke`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
      }
    } catch {
      // 网络不通时也要能退出
    }
    expire();
  }, [server, refreshToken, expire]);

  const client = useMemo<AtlasClientConfig>(
    () => ({
      // 经 API 网关：/atlas/… 是 Atlas 的接口
      apiBase: `${server}/atlas`,
      headers: (): Record<string, string> => (access.current ? { Authorization: `Bearer ${access.current}` } : {}),
      refresh,
    }),
    [server, refresh],
  );

  const value = useMemo<Session>(
    () => ({ loading, server, account, tenant, signedIn: Boolean(refreshToken), client, login, logout, expire }),
    [loading, server, account, tenant, refreshToken, client, login, logout, expire],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
