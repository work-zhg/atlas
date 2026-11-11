"use client";

/**
 * 按类型编辑一个值。编辑中的内容是草稿（Draft，保留原始文本，「1.」这种输入到一半的也留得住），
 * readDraft 把草稿读成值或错误；草稿不合规时不能保存。
 *
 *   整数、小数   数字框，按类型校验
 *   开关         开关控件
 *   文本         多行输入框
 *   列表         一行一项，可以增删、上下移动
 *   映射         键 + 值两列，键不能为空、不能重复
 *   列表和映射都可以切到 JSON 编辑；从 JSON 切回逐项编辑前 JSON 必须合规。
 */
import { Button, Flex, Input, Segmented, Select, Switch, Typography } from "antd";
import { useState } from "react";

import { useMono } from "./ValueView";
import { type ConfigValue, type Scalar, type ScalarKind, type ValueType, checkValue, parseScalar } from "./value";

export interface Row {
  id: number;
  /** 映射的键；列表不用 */
  key: string;
  /** 元素的文本（开关是 "true" / "false"） */
  text: string;
}

export interface Draft {
  mode: "form" | "json";
  /** 基本类型的文本（开关是 "true" / "false"） */
  text: string;
  /** 列表、映射逐项编辑的行 */
  rows: Row[];
  /** JSON 编辑的文本 */
  json: string;
}

/** 逐项编辑时某一行错在哪一格 */
export interface RowError {
  field: "key" | "value";
  message: string;
}

export type ReadResult =
  | { ok: true; value: ConfigValue }
  | { ok: false; error: string; rowErrors: ReadonlyMap<number, RowError> };

let seq = 0;
const newRow = (key: string, text: string): Row => ({ id: ++seq, key, text });
const blank = (kind: ScalarKind) => (kind === "bool" ? "true" : "");

export function draftOf(type: ValueType, value: ConfigValue): Draft {
  const json = JSON.stringify(value, null, 2);
  switch (type.kind) {
    case "list":
      return { mode: "form", text: "", rows: (value as Scalar[]).map((v) => newRow("", String(v))), json };
    case "map":
      return {
        mode: "form",
        text: "",
        rows: Object.entries(value as Record<string, Scalar>).map(([k, v]) => newRow(k, String(v))),
        json,
      };
    default:
      return { mode: "form", text: String(value), rows: [], json };
  }
}

function fail(error: string, rowErrors: ReadonlyMap<number, RowError> = new Map()): ReadResult {
  return { ok: false, error, rowErrors };
}

function fromRows(rowErrors: Map<number, RowError>, value: ConfigValue): ReadResult {
  const first = rowErrors.values().next();
  return first.done ? { ok: true, value } : fail(first.value.message, rowErrors);
}

export function readDraft(type: ValueType, draft: Draft): ReadResult {
  if (draft.mode === "json") {
    let parsed: unknown;
    try {
      parsed = JSON.parse(draft.json);
    } catch {
      return fail("不是合法的 JSON");
    }
    const error = checkValue(type, parsed);
    return error ? fail(error) : { ok: true, value: parsed as ConfigValue };
  }

  switch (type.kind) {
    case "list": {
      const rowErrors = new Map<number, RowError>();
      const out: Scalar[] = [];
      draft.rows.forEach((r, i) => {
        const c = parseScalar(type.element, r.text);
        // ★ 空项多半是「加一项」后忘了填；白名单这类配置里空前缀等于全放行。真要空字符串就切到 JSON
        if (r.text === "") rowErrors.set(r.id, { field: "value", message: `第 ${i + 1} 项是空的：不要就删掉，确实要空字符串请切到 JSON 编辑` });
        else if (c.ok) out.push(c.value);
        else rowErrors.set(r.id, { field: "value", message: `第 ${i + 1} 项：${c.error}` });
      });
      return fromRows(rowErrors, out);
    }
    case "map": {
      const rowErrors = new Map<number, RowError>();
      const out: Record<string, Scalar> = {};
      const seen = new Set<string>();
      for (const r of draft.rows) {
        const c = parseScalar(type.element, r.text);
        if (r.key === "") rowErrors.set(r.id, { field: "key", message: "键不能为空" });
        else if (seen.has(r.key)) rowErrors.set(r.id, { field: "key", message: `键 ${r.key} 重复了` });
        else if (!c.ok) rowErrors.set(r.id, { field: "value", message: `${r.key}：${c.error}` });
        else out[r.key] = c.value;
        seen.add(r.key);
      }
      return fromRows(rowErrors, out);
    }
    default: {
      const c = parseScalar(type.kind, draft.text);
      return c.ok ? { ok: true, value: c.value } : fail(c.error);
    }
  }
}

function ElementInput({
  kind,
  value,
  invalid,
  placeholder,
  onChange,
}: {
  kind: ScalarKind;
  value: string;
  invalid: boolean;
  placeholder?: string;
  onChange: (text: string) => void;
}) {
  const mono = useMono();
  if (kind === "bool") {
    return (
      <Select
        value={value}
        onChange={onChange}
        style={{ width: 96 }}
        options={[
          { value: "true", label: "开" },
          { value: "false", label: "关" },
        ]}
      />
    );
  }
  return (
    <Input
      value={value}
      placeholder={placeholder}
      status={invalid ? "error" : undefined}
      // 文本元素和键平分一行；数字元素不需要那么宽
      style={kind === "string" ? { ...mono, flex: 1, minWidth: 0 } : { ...mono, flex: "none", width: 200 }}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

export function ValueEditor({
  type,
  draft,
  result,
  onChange,
}: {
  type: ValueType;
  draft: Draft;
  result: ReadResult;
  onChange: (draft: Draft) => void;
}) {
  const mono = useMono();
  const [switchError, setSwitchError] = useState<string | null>(null);
  const invalid = !result.ok;
  const rowErrors: ReadonlyMap<number, RowError> = result.ok ? new Map() : result.rowErrors;

  if (type.kind !== "list" && type.kind !== "map") {
    switch (type.kind) {
      case "bool":
        return (
          <Switch
            checked={draft.text === "true"}
            checkedChildren="开"
            unCheckedChildren="关"
            onChange={(checked) => onChange({ ...draft, text: String(checked) })}
          />
        );
      case "string":
        return (
          <Input.TextArea
            value={draft.text}
            autoSize={{ minRows: 2, maxRows: 12 }}
            onChange={(e) => onChange({ ...draft, text: e.target.value })}
          />
        );
      default:
        return (
          <Input
            value={draft.text}
            status={invalid ? "error" : undefined}
            style={{ ...mono, maxWidth: 240 }}
            onChange={(e) => onChange({ ...draft, text: e.target.value })}
          />
        );
    }
  }

  const element = type.element;
  const setRows = (rows: Row[]) => onChange({ ...draft, rows });
  const patch = (id: number, change: Partial<Row>) => setRows(draft.rows.map((r) => (r.id === id ? { ...r, ...change } : r)));
  const remove = (id: number) => setRows(draft.rows.filter((r) => r.id !== id));
  const move = (i: number, by: -1 | 1) => {
    const rows = [...draft.rows];
    const [r] = rows.splice(i, 1);
    if (r) rows.splice(i + by, 0, r);
    setRows(rows);
  };

  function switchTo(mode: Draft["mode"]) {
    if (mode === draft.mode) return;
    if (mode === "json") {
      // 逐项 → JSON 总能切：读不成值的格子按原文本写进去，到 JSON 里再改
      const cell = (r: Row) => {
        const c = parseScalar(element, r.text);
        return c.ok ? c.value : r.text;
      };
      const value = type.kind === "list" ? draft.rows.map(cell) : Object.fromEntries(draft.rows.map((r) => [r.key, cell(r)]));
      setSwitchError(null);
      onChange({ ...draft, mode, json: JSON.stringify(value, null, 2) });
      return;
    }
    if (!result.ok) {
      setSwitchError("先改正 JSON 里的错误再切换");
      return;
    }
    setSwitchError(null);
    onChange(draftOf(type, result.value));
  }

  return (
    <div>
      <Flex align="center" gap={8} style={{ marginBottom: 8 }}>
        <Segmented
          size="small"
          value={draft.mode}
          onChange={(v) => switchTo(v as Draft["mode"])}
          options={[
            { value: "form", label: "逐项编辑" },
            { value: "json", label: "JSON" },
          ]}
        />
        {switchError && <Typography.Text type="danger">{switchError}</Typography.Text>}
      </Flex>

      {draft.mode === "json" ? (
        <Input.TextArea
          value={draft.json}
          status={invalid ? "error" : undefined}
          autoSize={{ minRows: 4, maxRows: 16 }}
          style={{ ...mono, fontSize: 12 }}
          onChange={(e) => {
            setSwitchError(null);
            onChange({ ...draft, json: e.target.value });
          }}
        />
      ) : (
        <>
          {draft.rows.map((r, i) => (
            <Flex key={r.id} gap={6} style={{ marginBottom: 6 }}>
              {type.kind === "map" && (
                <Input
                  value={r.key}
                  placeholder="键"
                  status={rowErrors.get(r.id)?.field === "key" ? "error" : undefined}
                  style={{ ...mono, flex: 1, minWidth: 0 }}
                  onChange={(e) => patch(r.id, { key: e.target.value })}
                />
              )}
              <ElementInput
                kind={element}
                value={r.text}
                placeholder={type.kind === "map" ? "值" : undefined}
                invalid={rowErrors.get(r.id)?.field === "value"}
                onChange={(text) => patch(r.id, { text })}
              />
              {type.kind === "list" && (
                <>
                  <Button disabled={i === 0} title="上移" onClick={() => move(i, -1)}>
                    ↑
                  </Button>
                  <Button disabled={i === draft.rows.length - 1} title="下移" onClick={() => move(i, 1)}>
                    ↓
                  </Button>
                </>
              )}
              <Button danger title="删除这一项" onClick={() => remove(r.id)}>
                ✕
              </Button>
            </Flex>
          ))}
          <Button size="small" onClick={() => setRows([...draft.rows, newRow("", blank(element))])}>
            ＋ 加一项
          </Button>
        </>
      )}
    </div>
  );
}
