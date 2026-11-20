import type { MgGrant, MgKey, MgPrice } from "@/api/model-gateway";

/** 131072 → 128k；空 → — */
export function fmtTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000 && n % 1_000_000 === 0) return `${n / 1_000_000}M`;
  if (n >= 1024 && n % 1024 === 0) return `${n / 1024}k`;
  if (n >= 1000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

/** 后端的 Decimal 是字符串：¥12.34；空 → — */
export function money(v: string | number | null | undefined, digits = 2): string {
  if (v == null || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  // 很小的金额（单次调用）多留几位，免得都显示成 ¥0.00
  const d = n !== 0 && Math.abs(n) < 0.01 ? 4 : digits;
  return `¥${n.toFixed(d)}`;
}

/** 元 / 百万 token：输入 / 输出（有缓存命中价时带上） */
export function priceText(p: MgPrice): string {
  const [i, c, o] = [Number(p.input), Number(p.cache_hit), Number(p.output)];
  if (!i && !c && !o) return "不计费";
  return `¥${i} / ¥${o}${c ? `（缓存命中 ¥${c}）` : ""}`;
}

export function grantLabel(g: MgGrant): string {
  const name = g.subject_name || g.subject_id;
  return g.subject_type === "dept" ? `${name}${g.include_sub ? "（含下级）" : ""}` : name;
}

export const fmtTime = (at: string | null | undefined) => (at ? at.slice(0, 16).replace("T", " ") : "—");

/** Key 现在能不能用：停用、过期、待分配都算不能 */
export function keyExpired(k: Pick<MgKey, "expires_at">): boolean {
  return !!k.expires_at && new Date(k.expires_at).getTime() < Date.now();
}

const base = (gatewayUrl: string | null) => (gatewayUrl || "<模型网关地址，请咨询管理员>").replace(/\/+$/, "");

export type SnippetKind = "claude-code" | "universal" | "anthropic" | "openai";

export const FORMAT_HINT =
  "通用接口按 Anthropic 格式传，模型的线路是哪种格式网关都会转换；Anthropic 格式、OpenAI 格式的接口不转换，模型没有这种格式的线路时会报错。拿不准就用通用接口。";

/**
 * 调用示例：同一把 Key。
 *   universal   通用接口，按 Anthropic 格式传、网关按线路转换：base_url = 网关地址 + /universal
 *   anthropic   只走 Anthropic 格式的线路（不转换）：base_url = 网关地址
 *   openai      只走 OpenAI 格式的线路（不转换）；Cursor、OpenAI SDK：base_url = 网关地址 + /v1
 *   claude-code 环境变量，连通用接口
 */
export function snippet(kind: SnippetKind, gatewayUrl: string | null, model: string, key = "$MODEL_API_KEY"): string {
  const b = base(gatewayUrl);
  if (kind === "claude-code") {
    return [`export ANTHROPIC_BASE_URL=${b}/universal`, `export ANTHROPIC_AUTH_TOKEN=${key}`, `export ANTHROPIC_MODEL=${model}`, "claude"].join("\n");
  }
  if (kind === "openai") {
    return [
      `curl ${b}/v1/chat/completions \\`,
      `  -H "Authorization: Bearer ${key}" \\`,
      `  -H "Content-Type: application/json" \\`,
      `  -d '{"model": "${model}", "messages": [{"role": "user", "content": "你好"}]}'`,
    ].join("\n");
  }
  return [
    `curl ${b}${kind === "universal" ? "/universal" : ""}/v1/messages \\`,
    `  -H "x-api-key: ${key}" \\`,
    `  -H "anthropic-version: 2023-06-01" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '{"model": "${model}", "max_tokens": 1024, "messages": [{"role": "user", "content": "你好"}]}'`,
  ].join("\n");
}
