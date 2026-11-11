"use client";

/**
 * 修改历史：每个版本一行（版本、值、修改说明、修改人、时间），最新的在前。
 *
 * ★ 回滚 = 把那一版的值写成一个新版本，不删除任何历史；期望版本取历史里最新的一版（就是人眼前看到的
 *   「当前」）。别人刚改过（409）就刷新历史，让人再确认一次。
 * ★ 值为 null 的版本（登记、恢复默认）显示「默认」；回滚到它就是恢复默认值。
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Descriptions, Drawer, type Modal, Table, Tag, Typography, theme } from "antd";
import type { ReactNode } from "react";

import { type AppConfigApi, AppConfigError, type ConfigOut, type RevisionOut, qk, useRevisions } from "./api";
import { ChangePreview, ValueView, useMono } from "./ValueView";
import { type ValueType, formatTime } from "./value";

type ModalApi = ReturnType<typeof Modal.useModal>[0];

export interface HistoryDrawerProps {
  api: AppConfigApi;
  entry: ConfigOut;
  type: ValueType | null;
  open: boolean;
  readOnly: boolean;
  renderUser: (by: string) => ReactNode;
  modal: ModalApi;
  notify: (text: string) => void;
  notifyError: (text: string) => void;
  onClose: () => void;
}

function RollbackPreview({ entry, type, current, target }: { entry: ConfigOut; type: ValueType | null; current: RevisionOut; target: RevisionOut }) {
  const { token } = theme.useToken();
  const mono = useMono();
  const before = current.value ?? entry.default;
  const after = target.value ?? entry.default;
  const show = (rev: RevisionOut, value: typeof before) => (
    <span>
      <ValueView type={type} value={value} /> {rev.value === null && <Tag>默认</Tag>}
    </span>
  );
  return (
    <div>
      <Descriptions
        column={1}
        size="small"
        items={[
          { key: "key", label: "配置项", children: <span style={mono}>{entry.key}</span> },
          { key: "current", label: `当前（第 ${current.version} 版）`, children: show(current, before) },
          { key: "target", label: "回滚后", children: show(target, after) },
        ]}
      />
      {type && (
        <div
          style={{
            marginTop: 8,
            padding: "8px 12px",
            borderRadius: token.borderRadius,
            background: token.colorFillQuaternary,
          }}
        >
          <ChangePreview type={type} before={before} after={after} />
        </div>
      )}
      <Alert
        type="info"
        style={{ marginTop: 12 }}
        message={`会记为第 ${current.version + 1} 版，保存后对所有实例立即生效。`}
      />
    </div>
  );
}

export function HistoryDrawer({ api, entry, type, open, readOnly, renderUser, modal, notify, notifyError, onClose }: HistoryDrawerProps) {
  const qc = useQueryClient();
  const mono = useMono();
  const q = useRevisions(api, entry.key, open);
  const revisions = q.data ?? [];

  const rollback = useMutation({
    mutationFn: ({ to, expected }: { to: number; expected: number }) =>
      api.rollback(entry.key, { to_version: to, expected_version: expected }),
    onSuccess: (out, { to }) => notify(`已回滚到第 ${to} 版，记为第 ${out.version} 版；下一次读取就生效`),
    onError: (e) =>
      notifyError(e instanceof AppConfigError && e.conflict ? "别人刚改过这个配置，历史已刷新，请再确认一次" : e.message),
    onSettled: () => qc.invalidateQueries({ queryKey: qk.all(api.endpoint) }),
  });

  function confirmRollback(target: RevisionOut) {
    const current = revisions[0];
    if (!current) return;
    modal.confirm({
      title: `回滚到第 ${target.version} 版`,
      icon: null,
      width: 520,
      content: <RollbackPreview entry={entry} type={type} current={current} target={target} />,
      okText: "回滚",
      cancelText: "取消",
      // 出错已在 onError 里提示；吞掉以便弹窗照常关闭
      onOk: () => rollback.mutateAsync({ to: target.version, expected: current.version }).catch(() => undefined),
    });
  }

  return (
    <Drawer
      open={open}
      width={760}
      onClose={onClose}
      title={
        <div>
          <div>修改历史</div>
          <div style={{ ...mono, marginTop: 4, fontWeight: "normal", fontSize: 12 }}>{entry.key}</div>
        </div>
      }
      footer={<Button onClick={onClose}>关闭</Button>}
    >
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        最新的在前。回滚 = 把那一版的值写成一个新版本，不删除任何历史。
      </Typography.Paragraph>
      {q.error ? (
        <Alert type="error" showIcon message={`读取历史失败：${q.error.message}`} />
      ) : (
        <Table<RevisionOut>
          rowKey="version"
          size="small"
          loading={q.isLoading}
          pagination={false}
          dataSource={revisions}
          columns={[
            {
              title: "版本",
              width: 80,
              render: (_, r, i) => (
                <div>
                  第 {r.version} 版
                  {i === 0 && (
                    <div>
                      <Tag color="success">当前</Tag>
                    </div>
                  )}
                </div>
              ),
            },
            {
              title: "值",
              render: (_, r) => (r.value === null ? <Tag>默认</Tag> : <ValueView type={type} value={r.value} short />),
            },
            {
              title: "修改说明",
              render: (_, r) => (r.note ? r.note : <Typography.Text type="secondary">—</Typography.Text>),
            },
            {
              title: "修改人 · 时间",
              width: 150,
              render: (_, r) => (
                <div>
                  <Typography.Text ellipsis={{ tooltip: r.changed_by }} style={{ maxWidth: 140 }}>
                    {renderUser(r.changed_by)}
                  </Typography.Text>
                  <div>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {formatTime(r.created_at)}
                    </Typography.Text>
                  </div>
                </div>
              ),
            },
            {
              title: "",
              width: 110,
              render: (_, r, i) =>
                i === 0 || readOnly ? null : (
                  <Button type="text" size="small" onClick={() => confirmRollback(r)}>
                    回滚到这一版
                  </Button>
                ),
            },
          ]}
        />
      )}
    </Drawer>
  );
}
