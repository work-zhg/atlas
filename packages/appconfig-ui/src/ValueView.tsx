"use client";

/**
 * 值的展示：列表里的「当前值」、抽屉里的「当前」、历史里每一版的值，以及修改前后的差异。
 * 样式只用 antd 的主题变量，跟着宿主的主题走（深色 / 浅色都行）。
 */
import { Tag, Typography, theme } from "antd";
import type { CSSProperties, ReactNode } from "react";

import { type ConfigValue, type Scalar, type ScalarKind, type ValueType, diffList, diffMap, sameValue, scalarLabel } from "./value";

/** 列表、映射在表格里最多显示几项 */
const SHORT_ITEMS = 3;

export function useMono(): CSSProperties {
  const { token } = theme.useToken();
  return { fontFamily: token.fontFamilyCode };
}

export function StateTag({ overridden }: { overridden: boolean }) {
  return overridden ? <Tag color="blue">已修改</Tag> : <Tag>默认</Tag>;
}

function Muted({ children }: { children: ReactNode }) {
  return <Typography.Text type="secondary">{children}</Typography.Text>;
}

function More({ total }: { total: number }) {
  return total > SHORT_ITEMS ? <Muted>等 {total} 项</Muted> : null;
}

function ScalarView({ kind, value }: { kind: ScalarKind; value: Scalar }) {
  const mono = useMono();
  switch (kind) {
    case "bool":
      return value ? <Tag color="success">开</Tag> : <Tag>关</Tag>;
    case "string":
      return value === "" ? <Muted>（空）</Muted> : <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{value}</span>;
    default:
      return <span style={mono}>{String(value)}</span>;
  }
}

/** short：表格里用，列表、映射只显示前几项，长文本截两行 */
export function ValueView({ type, value, short = false }: { type: ValueType | null; value: ConfigValue; short?: boolean }) {
  const mono = useMono();
  if (type === null) return <span style={mono}>{JSON.stringify(value)}</span>;

  switch (type.kind) {
    case "list": {
      const items = value as Scalar[];
      if (items.length === 0) return <Muted>（空列表）</Muted>;
      const shown = short ? items.slice(0, SHORT_ITEMS) : items;
      return (
        <span>
          {shown.map((item, i) => (
            <Tag key={i} style={{ ...mono, marginBottom: 4 }}>
              {scalarLabel(type.element, item)}
            </Tag>
          ))}
          {short && <More total={items.length} />}
        </span>
      );
    }
    case "map": {
      const entries = Object.entries(value as Record<string, Scalar>);
      if (entries.length === 0) return <Muted>（空）</Muted>;
      const shown = short ? entries.slice(0, SHORT_ITEMS) : entries;
      return (
        <div>
          {shown.map(([k, v]) => (
            <div key={k} style={{ ...mono, fontSize: 12, wordBreak: "break-all" }}>
              {k} → {scalarLabel(type.element, v)}
            </div>
          ))}
          {short && <More total={entries.length} />}
        </div>
      );
    }
    default:
      if (short && type.kind === "string" && value !== "") {
        return (
          <Typography.Paragraph ellipsis={{ rows: 2, tooltip: String(value) }} style={{ margin: 0 }}>
            {String(value)}
          </Typography.Paragraph>
        );
      }
      return <ScalarView kind={type.kind} value={value as Scalar} />;
  }
}

/** 修改前后：基本类型「旧 → 新」；列表、映射逐项标出增加、删除、改动的，避免误删一项还不知道。 */
export function ChangePreview({ type, before, after }: { type: ValueType; before: ConfigValue; after: ConfigValue }) {
  const { token } = theme.useToken();
  const mono = useMono();
  if (sameValue(before, after)) return <Muted>（没有变化）</Muted>;

  const line = (sign: string, color: string | undefined, text: ReactNode, key: string | number) => (
    <div key={key} style={{ ...mono, fontSize: 12, color, whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
      {sign} {text}
    </div>
  );

  switch (type.kind) {
    case "list": {
      const label = (v: Scalar) => scalarLabel(type.element, v);
      return (
        <div>
          {diffList(before as Scalar[], after as Scalar[]).map((d, i) =>
            d.op === "add"
              ? line("+", token.colorSuccess, label(d.value), i)
              : d.op === "del"
                ? line("−", token.colorError, label(d.value), i)
                : line(" ", undefined, label(d.value), i),
          )}
        </div>
      );
    }
    case "map": {
      const label = (v: Scalar | undefined) => (v === undefined ? "" : scalarLabel(type.element, v));
      return (
        <div>
          {diffMap(before as Record<string, Scalar>, after as Record<string, Scalar>).map((d) =>
            d.op === "add"
              ? line("+", token.colorSuccess, `${d.key} → ${label(d.after)}`, d.key)
              : d.op === "del"
                ? line("−", token.colorError, `${d.key} → ${label(d.before)}`, d.key)
                : d.op === "change"
                  ? line("~", token.colorWarning, `${d.key} → ${label(d.before)} ⇒ ${label(d.after)}`, d.key)
                  : line(" ", undefined, `${d.key} → ${label(d.after)}`, d.key),
          )}
        </div>
      );
    }
    default:
      return (
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <ScalarView kind={type.kind} value={before as Scalar} />
          <span>→</span>
          <span style={{ color: token.colorPrimaryText }}>
            <ScalarView kind={type.kind} value={after as Scalar} />
          </span>
        </div>
      );
  }
}
