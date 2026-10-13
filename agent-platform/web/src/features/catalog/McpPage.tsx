"use client";

/**
 * MCP 管理（prototype/atlas-v2.html「MCP」页；doc/skill-mcp-backend-design.html §8–§10）。
 *
 *   运行时（/v1/mcp/servers）   server 此刻的状态、工具定义、与上一版的差异、被谁引用
 *   配置服务（/config/mcp/*）   注册表的增改、启停、工具复核结论
 *
 * ★ 运行时用哪份注册表由 server 的 MCP_REGISTRY 决定：env 时这里的「注册 / 停用」
 *   写进配置服务，但要切到 config 之后才生效 —— 页面上要说清楚，不能让人以为改了就生效。
 */
import {
  Alert,
  App as AntdApp,
  Button,
  Checkbox,
  Empty,
  Input,
  InputNumber,
  Modal,
  Select,
  Skeleton,
  Switch,
  Table,
  Tag,
} from "antd";
import Link from "next/link";
import { useState } from "react";

import { useMcpServer, useMcpServers, useRefreshMcpServer } from "@/api/catalog";
import { useConfigMcpServers, useCreateMcpServer, usePatchMcpServer, useReviewTool } from "@/api/config";
import type { McpServerSummary, McpTool } from "@/api/types";
import { Icon } from "@/components/Icon";
import { relativeTime } from "@/lib/format";
import { ApiError } from "@/lib/http";
import pageStyles from "@/features/agents/agents.module.css";
import styles from "./catalog.module.css";

export const MCP_STATUS: Record<string, { label: string; color: string }> = {
  ok: { label: "正常", color: "success" },
  changed: { label: "定义有变化", color: "warning" },
  stale: { label: "刷新失败·用缓存", color: "warning" },
  error: { label: "未发现", color: "error" },
  disabled: { label: "已停用", color: "default" },
};

export const REVIEW_STATUS: Record<string, { label: string; color: string }> = {
  ok: { label: "可用", color: "success" },
  invalid: { label: "定义不合规", color: "error" },
  pending_review: { label: "待复核", color: "warning" },
  rejected: { label: "复核拒绝", color: "error" },
};

export function McpPage() {
  const serversQ = useMcpServers();
  const registry = serversQ.data?.registry ?? "env";
  const configQ = useConfigMcpServers();
  const [selected, setSelected] = useState<string>();
  const [creating, setCreating] = useState(false);
  const servers = serversQ.data?.data ?? [];
  const current = servers.find((s) => s.name === selected) ?? servers[0];

  return (
    <div className={styles.split}>
      <div className={styles.list}>
        <header className={pageStyles.head}>
          <div className={pageStyles.titleRow}>
            <div>
              <h1 className={pageStyles.title}>MCP</h1>
              <p className={pageStyles.desc}>
                外部服务提供的工具。工具定义由运行时发现并缓存；模型侧名为 server__tool。
              </p>
            </div>
            <Button
              type="primary"
              className={pageStyles.action}
              icon={<Icon name="plus" size={14} />}
              onClick={() => setCreating(true)}
            >
              注册 server
            </Button>
          </div>
        </header>

        <div className={pageStyles.body}>
          {registry === "env" && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message="运行时当前使用环境变量里的 server（MCP_REGISTRY=env）"
              description={
                <>
                  这里列出的是 MCP_SERVERS 里的配置，只读。在配置服务注册的 server
                  {configQ.data ? `（已有 ${configQ.data.length} 个）` : ""}要等运行时切到
                  MCP_REGISTRY=config 才生效；切换前先执行 <code>python -m atlas_config mcp import-env</code>{" "}
                  导入现有配置。
                </>
              }
            />
          )}
          {serversQ.error && (
            <Alert
              type="error"
              showIcon
              message="加载失败"
              description={
                (serversQ.error as ApiError).status === 404
                  ? "运行时没有 /v1/mcp/servers —— 正在运行的 server 是旧版本，重新部署后可用。"
                  : serversQ.error.message
              }
            />
          )}
          {serversQ.isPending && <Skeleton active paragraph={{ rows: 5 }} />}
          {serversQ.data && servers.length === 0 && (
            <Empty style={{ marginTop: 64 }} description="还没有 MCP server" />
          )}
          {servers.length > 0 && (
            <Table<McpServerSummary>
              size="middle"
              rowKey="name"
              dataSource={servers}
              pagination={false}
              onRow={(r) => ({ onClick: () => setSelected(r.name), style: { cursor: "pointer" } })}
              rowClassName={(r) => (r.name === current?.name ? styles.rowSel! : "")}
              columns={[
                {
                  title: "Server（工具前缀）",
                  render: (_, s) => (
                    <div>
                      <b>{s.name}</b>
                      <div className={styles.desc2} style={{ fontFamily: "var(--mono)" }}>
                        {s.url}
                      </div>
                    </div>
                  ),
                },
                { title: "传输", width: 130, dataIndex: "transport" },
                {
                  title: "凭据",
                  width: 90,
                  render: (_, s) =>
                    s.credential_scope === "user" ? <Tag color="blue">用户授权</Tag> : <Tag>平台</Tag>,
                },
                {
                  title: "状态",
                  width: 140,
                  render: (_, s) => {
                    const st = MCP_STATUS[s.status];
                    return <Tag color={st?.color}>{st?.label ?? s.status}</Tag>;
                  },
                },
                {
                  title: "工具",
                  width: 90,
                  render: (_, s) => (
                    <span>
                      {s.tools_count}
                      {s.pending_review > 0 && (
                        <Tag color="warning" style={{ marginLeft: 4 }}>
                          {s.pending_review} 待复核
                        </Tag>
                      )}
                    </span>
                  ),
                },
                {
                  title: "最近发现",
                  width: 110,
                  render: (_, s) => <span className={styles.muted}>{relativeTime(s.fetched_at)}</span>,
                },
                { title: "引用", width: 60, dataIndex: "references" },
              ]}
            />
          )}
          {servers.length > 0 && (
            <p className={styles.muted} style={{ marginTop: 8 }}>
              发现失败时继续用缓存的工具清单 —— 一个 server 抖动不该让整轮对话发不出去。
            </p>
          )}
        </div>
      </div>

      {current && <McpDetail key={current.name} name={current.name} registry={registry} />}
      <CreateServerModal open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}

// ═══════════════════════════════════════════════ 详情

function McpDetail({ name, registry }: { name: string; registry: string }) {
  const { message } = AntdApp.useApp();
  const detailQ = useMcpServer(name);
  const refresh = useRefreshMcpServer();
  const patch = usePatchMcpServer();
  const review = useReviewTool(name);
  const configQ = useConfigMcpServers();
  const registered = configQ.data?.find((s) => s.name === name);
  const d = detailQ.data;

  if (!d) {
    return (
      <aside className={styles.detail}>
        <Skeleton active paragraph={{ rows: 8 }} />
      </aside>
    );
  }

  const decide = (tool: McpTool, decision: "approved" | "rejected") =>
    review.mutate(
      { tool_name: tool.name, digest: tool.digest, decision, note: "" },
      {
        onSuccess: () => message.success(decision === "approved" ? "已确认这一版定义" : "已拒绝这一版定义"),
        onError: (e) => message.error(e.message),
      },
    );

  return (
    <aside className={styles.detail}>
      <div className={styles.detailHead}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className={styles.detailTitle}>{d.name}</h2>
          <div className={styles.sub}>模型侧名 {d.name}__&lt;tool&gt;</div>
        </div>
        <Button
          size="small"
          icon={<Icon name="retry" size={13} />}
          loading={refresh.isPending}
          disabled={!d.enabled}
          onClick={() =>
            refresh.mutate(name, {
              onSuccess: () => message.success("已重新发现"),
              onError: (e) => message.error((e as ApiError).message),
            })
          }
        >
          重新发现
        </Button>
      </div>

      {d.error && (
        <Alert type="warning" showIcon style={{ marginTop: 12 }} message="最近一次发现失败" description={d.error} />
      )}

      <div className={styles.secTitle}>连接</div>
      <div className={styles.kv}>
        <span>端点</span>
        <span>{d.url}</span>
      </div>
      <div className={styles.kv}>
        <span>传输</span>
        <span>{d.transport}</span>
      </div>
      {Object.entries(d.headers).map(([k, v]) => (
        <div key={k} className={styles.kv}>
          <span>{k}</span>
          <span>{v}</span>
        </div>
      ))}
      <div className={styles.kv}>
        <span>单次调用超时</span>
        <span>{d.call_timeout_s ? `${d.call_timeout_s}s` : "全局默认"}</span>
      </div>
      <div className={styles.kv}>
        <span>工具定义需复核</span>
        <span>{d.review_required ? "是" : "否"}</span>
      </div>
      {registered && (
        <div className={styles.kv}>
          <span>注册表中启用</span>
          <span>
            <Switch
              size="small"
              checked={registered.enabled}
              loading={patch.isPending}
              onChange={(on) =>
                patch.mutate(
                  { name, patch: { status: on ? "enabled" : "disabled" } },
                  {
                    onSuccess: () =>
                      message.info(
                        on
                          ? "已启用"
                          : "已停用。★ ACP 会话的 MCP 是建会话时的快照，进行中的会话不受影响",
                      ),
                  },
                )
              }
            />
          </span>
        </div>
      )}
      {registry === "env" && !registered && (
        <p className={styles.muted} style={{ marginTop: 6 }}>
          这台 server 来自环境变量，不在配置服务的注册表里。
        </p>
      )}

      <div className={styles.secTitle}>指纹</div>
      <div className={styles.kv}>
        <span>完整定义</span>
        <span>{d.content_hash ? `${d.content_hash.slice(0, 12)}…` : "—"}</span>
      </div>
      <div className={styles.kv}>
        <span>最近变化</span>
        <span>{d.changed_at ? relativeTime(d.changed_at) : "—"}</span>
      </div>
      {d.status === "changed" && (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 8 }}
          message="工具定义有变化"
          description="只改描述不改名字是注入风险最高的变更 —— 下面标出了改了什么。引用它的智能体保存时记录过旧指纹，运行时会提示。"
        />
      )}

      <div className={styles.secTitle}>工具（{d.tools?.length ?? 0}）</div>
      {(d.tools ?? []).map((t) => {
        const rs = REVIEW_STATUS[t.review_status];
        const canReview = d.review_required && registry === "config" && t.review_status !== "invalid";
        return (
          <div key={t.name} className={styles.tool}>
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <span className={styles.toolName}>{t.name}</span>
              <Tag color={rs?.color}>{rs?.label ?? t.review_status}</Tag>
              {t.change && <Tag color="warning">{{ added: "新增", description: "描述变更", schema: "参数变更" }[t.change] ?? t.change}</Tag>}
              <span style={{ flex: 1 }} />
              {canReview && t.review_status !== "ok" && (
                <Button size="small" type="link" onClick={() => decide(t, "approved")}>
                  确认
                </Button>
              )}
              {canReview && t.review_status !== "rejected" && (
                <Button size="small" type="link" danger onClick={() => decide(t, "rejected")}>
                  拒绝
                </Button>
              )}
            </div>
            <div className={styles.muted} style={{ marginTop: 4 }}>
              {t.previous_description ? (
                <>
                  <span className={styles.del}>{t.previous_description}</span>{" "}
                  <span className={styles.add}>{t.description}</span>
                </>
              ) : (
                t.description
              )}
            </div>
            {t.issues && t.issues.length > 0 && (
              <ul className={styles.hits}>
                {t.issues.map((i) => (
                  <li key={i}>{i}</li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
      {(d.removed_tools ?? []).length > 0 && (
        <p className={styles.muted}>
          已删除：<span className={styles.del}>{d.removed_tools!.join("、")}</span>
          （依赖它们的智能体运行时会因缺工具而失败）
        </p>
      )}

      <div className={styles.secTitle}>被引用</div>
      {(d.referenced_by ?? []).length === 0 ? (
        <div className={styles.muted}>没有智能体引用它</div>
      ) : (
        (d.referenced_by as Array<Record<string, unknown>>).map((r) => (
          <div key={`${r.agent_id}-${r.subagent ?? ""}`} className={styles.kv}>
            <span>
              <Link href={`/agents/${String(r.agent_id)}`}>{String(r.agent_name)}</Link> v{String(r.agent_version)}
              {r.subagent ? ` → ${String(r.subagent)}` : ""}
            </span>
            <span>{(r.tools as string[]).join(", ")}</span>
          </div>
        ))
      )}
    </aside>
  );
}

// ═══════════════════════════════════════════════ 注册

function parseHeaders(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const i = line.indexOf(":");
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

function CreateServerModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const create = useCreateMcpServer();
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [transport, setTransport] = useState<"streamable_http" | "sse">("streamable_http");
  const [headers, setHeaders] = useState("");
  const [timeout, setTimeoutS] = useState<number | null>(null);
  const [reviewRequired, setReviewRequired] = useState(true);
  const err = create.error as ApiError | null;

  const close = () => {
    create.reset();
    onClose();
  };

  return (
    <Modal
      title="注册 MCP server"
      open={open}
      onCancel={close}
      okText="注册"
      okButtonProps={{ disabled: !name || !url, loading: create.isPending }}
      onOk={() =>
        create.mutate(
          {
            name,
            display_name: "",
            description: "",
            credential_scope: "platform",
            status: "enabled",
            url,
            transport,
            headers: parseHeaders(headers),
            call_timeout_s: timeout,
            review_required: reviewRequired,
          },
          {
            onSuccess: () => {
              message.success(`已注册 ${name}`);
              close();
            },
          },
        )
      }
    >
      <div style={{ display: "grid", gap: 10 }}>
        <Input
          value={name}
          onChange={(e) => setName(e.target.value.toLowerCase())}
          placeholder="名字 = 工具前缀，如 github（小写字母开头，不含下划线，≤24）"
        />
        <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://higress.internal/mcp/github" />
        <Select
          value={transport}
          onChange={setTransport}
          options={[
            { value: "streamable_http", label: "streamable_http（推荐）" },
            { value: "sse", label: "sse" },
          ]}
        />
        <Input.TextArea
          rows={3}
          value={headers}
          onChange={(e) => setHeaders(e.target.value)}
          placeholder={"每行一个 header，凭据只写占位符：\nAuthorization: Bearer ${GITHUB_TOKEN}"}
          style={{ fontFamily: "var(--mono)" }}
        />
        <InputNumber
          style={{ width: "100%" }}
          min={1}
          value={timeout}
          onChange={setTimeoutS}
          placeholder="单次调用超时（秒），留空用全局默认"
        />
        <Checkbox checked={reviewRequired} onChange={(e) => setReviewRequired(e.target.checked)}>
          工具定义经人工复核后才进模型上下文
        </Checkbox>
        <p className={styles.muted} style={{ margin: 0 }}>
          配置服务不保存真凭据：Authorization / *Key / *Token 等 header 的值必须是 ${"{ENV}"}{" "}
          占位符，真值放在运行时的环境变量里。
        </p>
        {err && (
          <Alert
            type="error"
            showIcon
            message="注册失败"
            description={
              err.kind === "validation_error" ? JSON.stringify(err.details.detail) : err.message
            }
          />
        )}
      </div>
    </Modal>
  );
}
