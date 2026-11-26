/**
 * 统一登录的前端约定（c4-model/apigateway/dynamic-web-jwt.html）——Atlas、AI TeamFlow、运营中心共用。
 *
 *   · 访问 JWT 在 HttpOnly Cookie uc_access 里，15 分钟过期；网关本地验签。
 *   · 接口回 401：先单飞刷新（POST /uc/api/v1/auth/refresh，只有这个路径会带上 uc_refresh），
 *     成功后重放原请求；刷新也 401（14 天到期、账号或租户停用）才整页跳登录页。
 *   · 写请求带双提交的 CSRF 头 X-UC-CSRF（值取自可读的 uc_csrf Cookie），网关校验。
 *
 * 地址都是同源路径，构建时可换：NEXT_PUBLIC_UC_API_BASE（用户中心接口，经网关 /uc）、
 * NEXT_PUBLIC_LOGIN_PATH（统一登录页，用户中心前端 /uc-ui/login）。
 */

export const UC_API_BASE = (process.env.NEXT_PUBLIC_UC_API_BASE ?? "/uc").replace(/\/+$/, "");
export const LOGIN_PATH = process.env.NEXT_PUBLIC_LOGIN_PATH ?? "/uc-ui/login";

export function readCookie(name: string): string | undefined {
  if (typeof document === "undefined") return undefined;
  const hit = document.cookie.split("; ").find((c) => c.startsWith(`${name}=`));
  return hit ? decodeURIComponent(hit.slice(name.length + 1)) : undefined;
}

/** 写请求的 CSRF 双提交头（Cookie 里没有时为空：没登录，接口自然 401）。 */
export function csrfHeaders(): Record<string, string> {
  const csrf = readCookie("uc_csrf");
  return csrf ? { "X-UC-CSRF": csrf } : {};
}

let inflight: Promise<boolean> | null = null;

/**
 * 用刷新令牌换一个新的访问令牌。单飞：同一时刻多个请求撞上 401 只刷新一次，大家等同一个结果。
 * 返回 false = 刷新令牌也不能用了，要重新登录。
 */
export function refreshSession(): Promise<boolean> {
  if (!inflight) {
    inflight = fetch(`${UC_API_BASE}/api/v1/auth/refresh`, {
      method: "POST",
      credentials: "same-origin",
      headers: csrfHeaders(),
    })
      .then((res) => res.ok)
      .catch(() => false)
      .finally(() => {
        // 留一小会儿：紧跟着撞上 401 的请求直接用这次的结果，不再发第二次
        setTimeout(() => {
          inflight = null;
        }, 1000);
      });
  }
  return inflight;
}

/** 统一登录页的地址。next = 登录后回到的页面（含 basePath，同源路径）。 */
export function loginUrl(next?: string): string {
  const here =
    next ?? (typeof window === "undefined" ? "/" : window.location.pathname + window.location.search);
  return `${LOGIN_PATH}?next=${encodeURIComponent(here)}`;
}

/** 登出：作废这次登录（用户中心删刷新令牌、清 Cookie），然后去登录页。 */
export async function logout(): Promise<void> {
  await fetch(`${UC_API_BASE}/api/v1/auth/logout`, {
    method: "POST",
    credentials: "same-origin",
    headers: csrfHeaders(),
  }).catch(() => undefined);
  window.location.assign(LOGIN_PATH);
}
