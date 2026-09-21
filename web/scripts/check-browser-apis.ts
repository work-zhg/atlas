/**
 * 禁止在客户端代码里直接用**只在安全上下文可用**的浏览器 API。
 *
 * 为什么值得单独一条检查：这类错误 TypeScript 查不出来（类型上它们都存在），
 * 而运行期的表现极具误导性 —— 用 http 打开域名时 API 是 undefined，
 * 调用点抛 TypeError，于是"点发送毫无反应"：网络面板空的、后端日志空的，
 * 看起来像输入框坏了。真踩过一次，排查花的时间远超这条检查的成本。
 *
 * 允许的用法是**带回落**（见 src/lib/id.ts）：先 typeof 判断，再退到
 * 不受安全上下文限制的实现。所以这里只拦"裸调用"。
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const SRC = join(import.meta.dirname, "..", "src");

/** 只在安全上下文可用的 API —— 裸用即报错。 */
const FORBIDDEN: { pattern: RegExp; api: string; hint: string }[] = [
  {
    pattern: /(?<!typeof\s)\bcrypto\.randomUUID\s*\(/,
    api: "crypto.randomUUID()",
    hint: "用 @/lib/id 的 randomId() / newIdempotencyKey()",
  },
  {
    pattern: /\bcrypto\.subtle\b/,
    api: "crypto.subtle",
    hint: "安全上下文才有；http 下为 undefined",
  },
  {
    pattern: /\bnavigator\.clipboard\b/,
    api: "navigator.clipboard",
    hint: "需要回落到 document.execCommand('copy') 或提示用户手动复制",
  },
];

/** 已知安全的例外：回落实现本身要提到这些名字。 */
const ALLOWLIST = new Set(["lib/id.ts"]);

function* walk(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (/\.(ts|tsx)$/.test(entry)) yield full;
  }
}

let failed = 0;
for (const file of walk(SRC)) {
  const rel = relative(SRC, file).replaceAll("\\", "/");
  if (ALLOWLIST.has(rel)) continue;

  const lines = readFileSync(file, "utf8").split("\n");
  lines.forEach((line, i) => {
    if (line.trimStart().startsWith("//") || line.trimStart().startsWith("*")) return;
    for (const { pattern, api, hint } of FORBIDDEN) {
      if (pattern.test(line)) {
        console.error(`✗ src/${rel}:${i + 1}  ${api} 只在安全上下文（HTTPS/localhost）可用`);
        console.error(`    ${hint}`);
        console.error(`    ${line.trim()}`);
        failed += 1;
      }
    }
  });
}

if (failed > 0) {
  console.error(`\n共 ${failed} 处。http 下这些调用会抛 TypeError，而症状不指向真因。`);
  process.exit(1);
}
console.log("✓ 未发现裸用安全上下文 API");
