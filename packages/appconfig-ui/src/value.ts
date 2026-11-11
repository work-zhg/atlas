/**
 * 配置值的类型与取值 —— 与 atlas_appconfig/types.py 一一对应。
 *
 * 接口给的类型是 ValueType.describe() 的文本（int、list<string>、map<int>……），进来时用 parseType
 * 解析成 {kind, element} 一次，之后只按属性判断。
 *
 * ★ 校验与后端一致：bool 不算数字；int 可以写进 float；list / map 的元素只能是基本类型、不能为空、
 *   不能嵌套；map 的 key 是字符串。前端校验只为就地提示，最终以后端的 422 为准。
 */

export type ScalarKind = "string" | "int" | "float" | "bool";

export type ValueType =
  | { kind: ScalarKind }
  | { kind: "list"; element: ScalarKind }
  | { kind: "map"; element: ScalarKind };

export type Scalar = string | number | boolean;
export type ConfigValue = Scalar | Scalar[] | { [key: string]: Scalar };

const SCALAR_KINDS: readonly string[] = ["string", "int", "float", "bool"] satisfies ScalarKind[];
const CONTAINER = /^(list|map)<(string|int|float|bool)>$/;

/** 类型文本 → ValueType；不认识（库比组件新）返回 null，界面只读展示。 */
export function parseType(text: string): ValueType | null {
  if (SCALAR_KINDS.includes(text)) return { kind: text as ScalarKind };
  const m = CONTAINER.exec(text);
  if (!m) return null;
  return { kind: m[1] as "list" | "map", element: m[2] as ScalarKind };
}

export const KIND_LABEL: Record<ScalarKind, string> = {
  string: "文本",
  int: "整数",
  float: "小数",
  bool: "开关",
};

// ─────────────────────────────────────────────── 基本类型：文本 ↔ 值

const INT = /^[-+]?\d+$/;
const FLOAT = /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/;

export type Checked<T> = { ok: true; value: T } | { ok: false; error: string };

/** 编辑框里的文本 → 值。开关的文本是 "true" / "false"。 */
export function parseScalar(kind: ScalarKind, text: string): Checked<Scalar> {
  switch (kind) {
    case "string":
      return { ok: true, value: text };
    case "bool":
      return text === "true" || text === "false" ? { ok: true, value: text === "true" } : { ok: false, error: "要选开或关" };
    case "int": {
      const t = text.trim();
      if (!INT.test(t)) return { ok: false, error: "要一个整数" };
      const n = Number(t);
      return Number.isSafeInteger(n) ? { ok: true, value: n } : { ok: false, error: "整数太大" };
    }
    case "float": {
      const t = text.trim();
      const n = Number(t);
      return FLOAT.test(t) && Number.isFinite(n) ? { ok: true, value: n } : { ok: false, error: "要一个数" };
    }
  }
}

/** 值 → 给人看的文字 */
export function scalarLabel(kind: ScalarKind, value: Scalar): string {
  if (kind === "bool") return value ? "开" : "关";
  if (kind === "string" && value === "") return "（空）";
  return String(value);
}

function checkScalar(kind: ScalarKind, value: unknown, where: string): string | null {
  const ok =
    kind === "bool"
      ? typeof value === "boolean"
      : kind === "int"
        ? Number.isSafeInteger(value)
        : kind === "float"
          ? typeof value === "number" && Number.isFinite(value)
          : typeof value === "string";
  return ok ? null : `${where}要是 ${kind}，拿到的是 ${JSON.stringify(value)}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 任意 JSON（JSON 编辑模式的输入）按类型校验；合规返回 null，否则返回错误文案。 */
export function checkValue(type: ValueType, value: unknown): string | null {
  switch (type.kind) {
    case "list":
      if (!Array.isArray(value)) return `要一个列表（list<${type.element}>）`;
      for (const [i, item] of value.entries()) {
        const error = checkScalar(type.element, item, `第 ${i + 1} 项`);
        if (error) return error;
      }
      return null;
    case "map":
      if (!isRecord(value)) return `要一个映射（map<${type.element}>）`;
      for (const [k, item] of Object.entries(value)) {
        const error = checkScalar(type.element, item, `键 ${JSON.stringify(k)} 的值`);
        if (error) return error;
      }
      return null;
    default:
      return checkScalar(type.kind, value, "值");
  }
}

/** 值相等：映射不看键的顺序（PG 的 JSONB 会重排键）。 */
export function sameValue(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => sameValue(x, b[i]));
  if (isRecord(a) && isRecord(b)) {
    const keys = Object.keys(a);
    return keys.length === Object.keys(b).length && keys.every((k) => Object.hasOwn(b, k) && sameValue(a[k], b[k]));
  }
  return a === b;
}

// ─────────────────────────────────────────────── 修改前后的差异

export interface ListOp {
  op: "keep" | "add" | "del";
  value: Scalar;
}

/** 列表按最长公共子序列对齐：删掉中间一项只标那一项，不会把后面全标成改动。 */
export function diffList(before: readonly Scalar[], after: readonly Scalar[]): ListOp[] {
  const n = before.length;
  const m = after.length;
  const w = m + 1;
  // lcs[i * w + j] = before[i..] 与 after[j..] 的最长公共子序列长度
  const lcs = new Uint32Array((n + 1) * w);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i * w + j] =
        before[i] === after[j] ? (lcs[(i + 1) * w + j + 1] ?? 0) + 1 : Math.max(lcs[(i + 1) * w + j] ?? 0, lcs[i * w + j + 1] ?? 0);
    }
  }
  const ops: ListOp[] = [];
  let i = 0;
  let j = 0;
  while (i < n || j < m) {
    const b = before[i];
    const a = after[j];
    if (i < n && j < m && b === a) {
      ops.push({ op: "keep", value: b as Scalar });
      i++;
      j++;
    } else if (j >= m || (i < n && (lcs[(i + 1) * w + j] ?? 0) >= (lcs[i * w + j + 1] ?? 0))) {
      ops.push({ op: "del", value: b as Scalar });
      i++;
    } else {
      ops.push({ op: "add", value: a as Scalar });
      j++;
    }
  }
  return ops;
}

export interface MapOp {
  op: "keep" | "add" | "del" | "change";
  key: string;
  before?: Scalar;
  after?: Scalar;
}

/** 映射按键对齐：先按修改后的顺序列出保留 / 改动 / 新增的键，再列删掉的键。 */
export function diffMap(before: Readonly<Record<string, Scalar>>, after: Readonly<Record<string, Scalar>>): MapOp[] {
  const ops: MapOp[] = [];
  for (const [key, value] of Object.entries(after)) {
    if (!Object.hasOwn(before, key)) ops.push({ op: "add", key, after: value });
    else if (before[key] === value) ops.push({ op: "keep", key, after: value });
    else ops.push({ op: "change", key, before: before[key], after: value });
  }
  for (const [key, value] of Object.entries(before)) {
    if (!Object.hasOwn(after, key)) ops.push({ op: "del", key, before: value });
  }
  return ops;
}

// ─────────────────────────────────────────────── 展示

/** 修改时间：今年的只显示月日时分 */
export function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  const md = `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return d.getFullYear() === new Date().getFullYear() ? md : `${d.getFullYear()}-${md}`;
}

/** 一行写得下的文字（列表下面的「默认：…」）：列表、映射用 JSON */
export function compactText(type: ValueType | null, value: ConfigValue): string {
  if (type === null || type.kind === "list" || type.kind === "map") return JSON.stringify(value);
  return scalarLabel(type.kind, value as Scalar);
}
