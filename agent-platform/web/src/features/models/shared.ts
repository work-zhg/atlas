import type { MgGrant, MgModel } from "@/api/model-gateway";

/** 131072 → 128k；空 → — */
export function fmtTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000 && n % 1_000_000 === 0) return `${n / 1_000_000}M`;
  if (n >= 1024 && n % 1024 === 0) return `${n / 1024}k`;
  if (n >= 1000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

/** 美元 / 百万 token：输入 / 输出 */
export function fmtPrice(m: Pick<MgModel, "input_price" | "output_price">): string {
  if (m.input_price == null && m.output_price == null) return "—";
  return `$${m.input_price ?? 0} / $${m.output_price ?? 0}`;
}

export function grantLabel(g: MgGrant): string {
  const name = g.subject_name || g.subject_id;
  return g.subject_type === "dept" ? `${name}${g.include_sub ? "（含下级）" : ""}` : name;
}

/** 调用示例：OpenAI 兼容入口。地址空 = LiteLLM 还没对集群外开放 */
export function curlFor(litellmUrl: string | null, model: string, key = "$MODEL_API_KEY"): string {
  const base = (litellmUrl || "<LiteLLM 地址，请咨询管理员>").replace(/\/+$/, "");
  return [
    `curl ${base}/v1/chat/completions \\`,
    `  -H "Authorization: Bearer ${key}" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '{`,
    `    "model": "${model}",`,
    `    "messages": [{"role": "user", "content": "你好"}]`,
    `  }'`,
  ].join("\n");
}
