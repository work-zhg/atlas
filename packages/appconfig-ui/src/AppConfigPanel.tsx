"use client";

/**
 * 应用配置的管理面板：列表（按配置项前缀分组）→「修改」打开编辑抽屉、「历史」打开修改历史抽屉，
 * 回滚是确认弹窗。没有「新建」：配置项由宿主在代码里声明（ConfigKey），这里只能改值。
 *
 * 页面标题、导航由宿主自己画；这里只管面板本身。能不能看、能不能改由宿主的 ConfigGuard 决定，
 * 前端的 readOnly 只是体验（没有改的权限时不出现「修改」「回滚」）。
 */
import { Alert, Button, Flex, Input, Modal, Segmented, Table, type TableColumnType, Tag, Typography, message, theme } from "antd";
import { type ReactNode, type TdHTMLAttributes, useMemo, useState } from "react";

import { type AppConfigClient, type ConfigOut, SYSTEM, appConfigApi, useConfigs } from "./api";
import { EditDrawer } from "./EditDrawer";
import { HistoryDrawer } from "./HistoryDrawer";
import { StateTag, ValueView, useMono } from "./ValueView";
import { type ValueType, compactText, parseType } from "./value";

export interface AppConfigPanelProps extends AppConfigClient {
  /** 只能看、不能改（对应宿主 ConfigGuard.edit 不通过的人） */
  readOnly?: boolean;
  /** 修订历史里记的是宿主 ConfigGuard 返回的操作人标识（如用户 id），宿主可以换成姓名 */
  renderUser?: (by: string) => ReactNode;
}

type Filter = "all" | "changed" | "default";

type Row =
  | { kind: "group"; id: string; label: string }
  | { kind: "item"; id: string; entry: ConfigOut; type: ValueType | null };

const defaultUser = (by: string): ReactNode => (by === SYSTEM ? "系统" : by);

/** 按第一个点前面的部分分组：sandbox.quota_per_user → sandbox.* */
function groupLabel(key: string): string {
  const dot = key.indexOf(".");
  return dot > 0 ? `${key.slice(0, dot)}.*` : "其他";
}

export function AppConfigPanel({ endpoint, headers, readOnly = false, renderUser = defaultUser }: AppConfigPanelProps) {
  const api = useMemo(() => appConfigApi({ endpoint, headers }), [endpoint, headers]);
  const q = useConfigs(api);
  const { token } = theme.useToken();
  const mono = useMono();
  const [messageApi, messageHolder] = message.useMessage();
  const [modalApi, modalHolder] = Modal.useModal();
  const [filter, setFilter] = useState<Filter>("all");
  const [keyword, setKeyword] = useState("");
  /** 编辑抽屉：entry 是打开时的快照；nonce 让同一项再打开时重置草稿 */
  const [edit, setEdit] = useState<{ entry: ConfigOut; type: ValueType; open: boolean; nonce: number } | null>(null);
  const [history, setHistory] = useState<{ key: string; open: boolean } | null>(null);

  const list = useMemo(() => q.data ?? [], [q.data]);
  const rows = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    const out: Row[] = [];
    let group: string | null = null;
    for (const entry of list) {
      if (filter === "changed" && !entry.overridden) continue;
      if (filter === "default" && entry.overridden) continue;
      if (kw && !`${entry.key}\n${entry.description}`.toLowerCase().includes(kw)) continue;
      const label = groupLabel(entry.key);
      if (label !== group) {
        group = label;
        out.push({ kind: "group", id: `group:${label}`, label });
      }
      out.push({ kind: "item", id: entry.key, entry, type: parseType(entry.type) });
    }
    return out;
  }, [list, filter, keyword]);

  function openEdit(entry: ConfigOut, type: ValueType | null) {
    if (type === null) return; // 组件不认识的类型（库比组件新）：只能看历史
    setHistory((h) => h && { ...h, open: false });
    setEdit({ entry, type, open: true, nonce: Date.now() });
  }

  function openHistory(key: string) {
    setEdit((e) => e && { ...e, open: false });
    setHistory({ key, open: true });
  }

  const historyEntry = history ? list.find((c) => c.key === history.key) : undefined;
  const notify = (text: string) => void messageApi.success(text);
  const notifyError = (text: string) => void messageApi.error(text);

  // 分组行占满一整行：第一列 colSpan=4，其余列 colSpan=0
  const cell =
    (first: boolean) =>
    (r: Row): TdHTMLAttributes<HTMLTableCellElement> =>
      r.kind === "group"
        ? { colSpan: first ? 4 : 0, style: { background: token.colorFillAlter, padding: "6px 12px" } }
        : {};

  const columns: TableColumnType<Row>[] = [
    {
      title: "配置项",
      width: "38%",
      onCell: cell(true),
      render: (_, r) =>
        r.kind === "group" ? (
          <Typography.Text type="secondary" style={{ ...mono, fontSize: 12 }}>
            {r.label}
          </Typography.Text>
        ) : (
          <div>
            <div style={{ ...mono, fontSize: 13, wordBreak: "break-all" }}>{r.entry.key}</div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {r.entry.description}
            </Typography.Text>
          </div>
        ),
    },
    {
      title: "类型",
      width: "13%",
      onCell: cell(false),
      render: (_, r) => r.kind === "item" && <Tag style={mono}>{r.entry.type}</Tag>,
    },
    {
      title: "当前值",
      width: "33%",
      onCell: cell(false),
      render: (_, r) =>
        r.kind === "item" && (
          <div>
            <ValueView type={r.type} value={r.entry.effective} short /> <StateTag overridden={r.entry.overridden} />
            {r.entry.overridden && (
              <div style={{ marginTop: 4, fontSize: 12, wordBreak: "break-all" }}>
                <Typography.Text type="secondary">默认：{compactText(r.type, r.entry.default)}</Typography.Text>
              </div>
            )}
          </div>
        ),
    },
    {
      title: "操作",
      onCell: cell(false),
      render: (_, r) =>
        r.kind === "item" && (
          <Flex gap={0} onClick={(e) => e.stopPropagation()}>
            {!readOnly && r.type !== null && (
              <Button type="text" size="small" onClick={() => openEdit(r.entry, r.type)}>
                修改
              </Button>
            )}
            <Button type="text" size="small" onClick={() => openHistory(r.entry.key)}>
              历史
            </Button>
          </Flex>
        ),
    },
  ];

  return (
    <div>
      {messageHolder}
      {modalHolder}

      <Flex align="center" gap={12} wrap style={{ marginBottom: 12 }}>
        <Segmented<Filter>
          value={filter}
          onChange={setFilter}
          options={[
            { value: "all", label: "全部" },
            { value: "changed", label: "已修改" },
            { value: "default", label: "默认值" },
          ]}
        />
        <Input
          allowClear
          value={keyword}
          placeholder="搜索配置项或说明"
          style={{ width: 260 }}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <span style={{ flex: 1 }} />
        <Button loading={q.isFetching} onClick={() => void q.refetch()}>
          刷新
        </Button>
      </Flex>

      {q.error ? (
        <Alert type="error" showIcon message="读取配置失败" description={q.error.message} />
      ) : (
        <Table<Row>
          rowKey="id"
          size="middle"
          loading={q.isLoading}
          pagination={false}
          dataSource={rows}
          columns={columns}
          locale={{ emptyText: list.length === 0 ? "这个应用还没有声明配置项" : "没有符合条件的配置项" }}
          onRow={(r) =>
            r.kind === "item"
              ? { onClick: () => (r.type ? openEdit(r.entry, r.type) : openHistory(r.entry.key)), style: { cursor: "pointer" } }
              : {}
          }
        />
      )}

      {edit && (
        <EditDrawer
          key={edit.nonce}
          api={api}
          entry={edit.entry}
          type={edit.type}
          open={edit.open}
          readOnly={readOnly}
          renderUser={renderUser}
          notify={notify}
          onClose={() => setEdit((e) => e && { ...e, open: false })}
          onClosed={() => setEdit((e) => (e?.open ? e : null))}
          onHistory={() => openHistory(edit.entry.key)}
        />
      )}
      {history && historyEntry && (
        <HistoryDrawer
          api={api}
          entry={historyEntry}
          type={parseType(historyEntry.type)}
          open={history.open}
          readOnly={readOnly}
          renderUser={renderUser}
          modal={modalApi}
          notify={notify}
          notifyError={notifyError}
          onClose={() => setHistory((h) => h && { ...h, open: false })}
        />
      )}
    </div>
  );
}
