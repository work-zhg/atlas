"use client";

/**
 * 编辑抽屉：当前值与版本 → 按类型编辑新值 → 修改前后对比 → 修改说明 → 保存 / 恢复默认。
 *
 * ★ 期望版本取打开抽屉时的那一版（base），不跟着列表的后台刷新走 —— 否则别人先改了，这里会悄悄把
 *   别人的改动覆盖掉。版本对不上（409）时不覆盖：取最新的一版给人看，由人决定「用最新值重新编辑」。
 * ★ 恢复默认 = 值写成 null，以后跟着代码里的默认值走；同样记一个新版本。
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Descriptions, Drawer, Flex, Input, Tag, Typography, theme } from "antd";
import { type ReactNode, useMemo, useState } from "react";

import { type AppConfigApi, AppConfigError, type ConfigOut, qk } from "./api";
import { ValueEditor, draftOf, readDraft } from "./ValueEditor";
import { ChangePreview, StateTag, ValueView, useMono } from "./ValueView";
import { type ConfigValue, KIND_LABEL, type ValueType, compactText, formatTime, sameValue } from "./value";

function hint(type: ValueType, def: ConfigValue): string {
  switch (type.kind) {
    case "list":
      return `一行一项，元素类型 ${type.element}；默认：${compactText(type, def)}`;
    case "map":
      return `键不能重复，值的类型 ${type.element}；默认：${compactText(type, def)}`;
    default:
      return `${KIND_LABEL[type.kind]} · 默认：${compactText(type, def)}`;
  }
}

export interface EditDrawerProps {
  api: AppConfigApi;
  /** 打开时的样子：期望版本以它为准 */
  entry: ConfigOut;
  type: ValueType;
  open: boolean;
  readOnly: boolean;
  renderUser: (by: string) => ReactNode;
  notify: (text: string) => void;
  onClose: () => void;
  /** 关闭动画结束 */
  onClosed: () => void;
  onHistory: () => void;
}

export function EditDrawer({ api, entry, type, open, readOnly, renderUser, notify, onClose, onClosed, onHistory }: EditDrawerProps) {
  const qc = useQueryClient();
  const { token } = theme.useToken();
  const mono = useMono();
  const [base, setBase] = useState(entry);
  const [draft, setDraft] = useState(() => draftOf(type, entry.effective));
  const [note, setNote] = useState("");
  /** 409 之后取到的最新一版 */
  const [latest, setLatest] = useState<ConfigOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const result = useMemo(() => readDraft(type, draft), [type, draft]);
  const changed = result.ok && !sameValue(result.value, base.effective);

  const write = useMutation({
    mutationFn: (value: ConfigValue | null) =>
      api.set(base.key, { value, expected_version: base.version, note: note.trim() }),
    onSuccess: (out, value) => {
      qc.setQueryData<ConfigOut[]>(qk.list(api.endpoint), (list) => list?.map((c) => (c.key === out.key ? out : c)));
      void qc.invalidateQueries({ queryKey: qk.all(api.endpoint) });
      notify(value === null ? `已恢复默认值，记为第 ${out.version} 版` : `已保存为第 ${out.version} 版；下一次读取就生效`);
      onClose();
    },
    onError: async (e) => {
      if (!(e instanceof AppConfigError && e.conflict)) {
        setError(e.message);
        return;
      }
      try {
        setLatest(await api.get(base.key));
        setError(null);
      } catch (again) {
        setError((again as Error).message);
      }
    },
  });

  function reloadLatest(fresh: ConfigOut) {
    setBase(fresh);
    setDraft(draftOf(type, fresh.effective));
    setLatest(null);
  }

  const label = (text: ReactNode, sub?: string) => (
    <div style={{ marginBottom: 6, fontWeight: 500 }}>
      {text}
      {sub && <Typography.Text type="secondary" style={{ fontWeight: "normal" }}> · {sub}</Typography.Text>}
    </div>
  );

  return (
    <Drawer
      open={open}
      width={560}
      onClose={onClose}
      afterOpenChange={(visible) => !visible && onClosed()}
      title={
        <div>
          <div style={{ ...mono, fontSize: 15 }}>{base.key}</div>
          <div style={{ marginTop: 4, fontWeight: "normal", fontSize: 12 }}>
            <Tag style={mono}>{base.type}</Tag>
            <Typography.Text type="secondary">{base.description}</Typography.Text>
          </div>
        </div>
      }
      footer={
        readOnly ? (
          <Flex justify="end">
            <Button onClick={onClose}>关闭</Button>
          </Flex>
        ) : (
          <Flex align="center" gap={8}>
            {base.overridden && (
              <Button
                disabled={latest !== null}
                loading={write.isPending && write.variables === null}
                onClick={() => write.mutate(null)}
              >
                恢复默认
              </Button>
            )}
            <span style={{ flex: 1 }} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              保存后对所有实例立即生效
            </Typography.Text>
            <Button onClick={onClose}>取消</Button>
            <Button
              type="primary"
              disabled={!changed || latest !== null}
              loading={write.isPending && write.variables !== null}
              onClick={() => result.ok && write.mutate(result.value)}
            >
              保存
            </Button>
          </Flex>
        )
      }
    >
      {latest && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="没有保存：别人先改了这个配置"
          description={
            <div>
              <div>
                {renderUser(latest.updated_by)} 在 {formatTime(latest.updated_at)} 改成了第 {latest.version} 版，你是基于第{" "}
                {base.version} 版改的。最新的值：
              </div>
              <div style={{ margin: "6px 0 8px" }}>
                <ValueView type={type} value={latest.effective} /> <StateTag overridden={latest.overridden} />
              </div>
              <Button size="small" onClick={() => reloadLatest(latest)}>
                用最新值重新编辑
              </Button>
            </div>
          }
        />
      )}
      {error && (
        <Alert type="error" showIcon closable style={{ marginBottom: 16 }} message={error} onClose={() => setError(null)} />
      )}
      {readOnly && <Alert type="info" showIcon style={{ marginBottom: 16 }} message="你没有修改配置的权限，只能查看。" />}

      <Descriptions
        column={1}
        size="small"
        style={{ marginBottom: 20 }}
        items={[
          {
            key: "current",
            label: "当前",
            children: (
              <span>
                <ValueView type={type} value={base.effective} /> <StateTag overridden={base.overridden} />
              </span>
            ),
          },
          {
            key: "version",
            label: "版本",
            children: (
              <span>
                第 {base.version} 版 · {renderUser(base.updated_by)} · {formatTime(base.updated_at)}
                <Button type="text" size="small" onClick={onHistory}>
                  看历史
                </Button>
              </span>
            ),
          },
        ]}
      />

      {!readOnly && (
        <>
          {label("新的值")}
          <ValueEditor type={type} draft={draft} result={result} onChange={setDraft} />
          <div style={{ marginTop: 6, fontSize: 12 }}>
            {result.ok ? (
              <Typography.Text type="secondary">{hint(type, base.default)}</Typography.Text>
            ) : (
              <Typography.Text type="danger">{result.error}</Typography.Text>
            )}
          </div>

          {result.ok && (
            <div
              style={{
                marginTop: 16,
                padding: "10px 12px",
                borderRadius: token.borderRadius,
                background: token.colorFillQuaternary,
                border: `1px solid ${token.colorBorderSecondary}`,
              }}
            >
              <div style={{ marginBottom: 6, fontWeight: 500 }}>修改前后</div>
              <ChangePreview type={type} before={base.effective} after={result.value} />
            </div>
          )}

          <div style={{ marginTop: 20 }}>
            {label("修改说明", "可选，会记进历史")}
            <Input
              value={note}
              maxLength={500}
              showCount
              placeholder="为什么改？比如：评测期间临时放宽"
              onChange={(e) => setNote(e.target.value)}
            />
          </div>
        </>
      )}
    </Drawer>
  );
}
