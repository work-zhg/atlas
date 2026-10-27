"use client";

/**
 * MCP 管理（doc/mcp-design.html 第二版 §9）：后端是 MCP 网关。
 *
 *   服务       注册（上游 = Higress 上的服务）、修改、停用；工具定义与上一版的差异；复核；授权
 *   待复核     所有服务里等人工批准的工具定义（批准的是这一版，指纹变了要重新复核）
 *   API Key    发放、重置、停用、启用；有授权但没有 Key 的人
 *   访问日志   经过网关的每个请求，含被拒的
 *
 * MCP 管理员看到全部；服务的所有者只看到自己的服务（授权、访问日志）。
 * ★ 「被哪些智能体引用」只有 Atlas 知道：走 Atlas 的 /v1/mcp/servers/{名字}。
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
  Skeleton,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
} from "antd";
import Link from "next/link";
import { useState } from "react";

import { useMcpServer } from "@/api/catalog";
import {
  type GwService,
  type GwServiceDetail,
  type GwTool,
  useCreateGwService,
  useGwMe,
  useGwPendingReviews,
  useGwService,
  useGwServices,
  usePatchGwService,
  useRefreshGwService,
  useReviewGwTool,
  useSetGwServiceEnabled,
} from "@/api/mcp-gateway";
import { Icon } from "@/components/Icon";
import pageStyles from "@/features/agents/agents.module.css";
import { relativeTime } from "@/lib/format";
import styles from "./catalog.module.css";
import { McpAccessLog } from "./McpAccessLog";
import { McpGrants } from "./McpGrants";
import { McpKeys } from "./McpKeys";

export const REVIEW_STATUS: Record<string, { label: string; color: string }> = {
  ok: { label: "可用", color: "success" },
  invalid: { label: "定义不合规", color: "error" },
  pending_review: { label: "待复核", color: "warning" },
  rejected: { label: "复核拒绝", color: "error" },
};

/** 和上一版比改了什么（指纹覆盖名字、描述、参数定义、只读注解） */
function changeOf(t: GwTool): string | null {
  const p = t.previous;
  if (!p) return null;
  if (p.description !== t.description) return "描述变更";
  if (JSON.stringify(p.input_schema) !== JSON.stringify(t.input_schema)) return "参数变更";
  return "只读注解变更";
}

export function McpPage() {
  const meQ = useGwMe();
  const servicesQ = useGwServices();
  const pendingQ = useGwPendingReviews(!!meQ.data?.can_manage);
  const manage = !!meQ.data?.can_manage;
  const services = servicesQ.data ?? [];
  const pending = pendingQ.data?.length ?? 0;

  if (meQ.error) {
    return (
      <div className={pageStyles.body}>
        <Alert
          type="error"
          showIcon
          message="连不上 MCP 网关"
          description={`MCP 的管理端在 MCP 网关上（atlas-mcpgateway）：${meQ.error.message}`}
        />
      </div>
    );
  }

  const items = [
    { key: "services", label: "服务", children: <ServicesTab services={services} manage={manage} loading={servicesQ.isPending} /> },
    ...(manage
      ? [
          {
            key: "reviews",
            label: pending ? `待复核（${pending}）` : "待复核",
            children: <PendingTab rows={pendingQ.data ?? []} />,
          },
          { key: "keys", label: "API Key", children: <McpKeys services={services} /> },
        ]
      : []),
    { key: "log", label: "访问日志", children: <McpAccessLog services={services} canManage={manage} /> },
  ];

  return (
    <div className={styles.list}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <div>
            <h1 className={pageStyles.title}>MCP</h1>
            <p className={pageStyles.desc}>
              外部服务提供的工具，统一经 MCP 网关调用：网关按 API Key 认人、按授权判权、只放行复核通过的工具。模型看到的名字是
              服务名__工具名。
            </p>
          </div>
        </div>
      </header>
      <div className={pageStyles.body} style={{ paddingTop: 0 }}>
        <Tabs items={items} />
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════ 服务

function ServicesTab({ services, manage, loading }: { services: GwService[]; manage: boolean; loading: boolean }) {
  const [selected, setSelected] = useState<string>();
  const [creating, setCreating] = useState(false);
  const current = services.find((s) => s.name === selected) ?? services[0];

  return (
    <div className={styles.split} style={{ minHeight: 480 }}>
      <div className={styles.list} style={{ paddingRight: 16 }}>
        {manage && (
          <div style={{ marginBottom: 12 }}>
            <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setCreating(true)}>
              注册服务
            </Button>
          </div>
        )}
        {loading && <Skeleton active paragraph={{ rows: 5 }} />}
        {!loading && services.length === 0 && (
          <Empty
            style={{ marginTop: 48 }}
            description={manage ? "还没有 MCP 服务" : "你还不是任何 MCP 服务的所有者"}
          />
        )}
        {services.length > 0 && (
          <Table<GwService>
            size="middle"
            rowKey="name"
            dataSource={services}
            pagination={false}
            onRow={(r) => ({ onClick: () => setSelected(r.name), style: { cursor: "pointer" } })}
            rowClassName={(r) => (r.name === current?.name ? styles.rowSel! : "")}
            columns={[
              {
                title: "服务（工具前缀）",
                render: (_, s) => (
                  <div>
                    <b>{s.name}</b> {s.display_name && s.display_name !== s.name && <span>{s.display_name}</span>}
                    <div className={styles.desc2} style={{ fontFamily: "var(--mono)" }}>
                      上游 Higress:{s.upstream_name}
                    </div>
                  </div>
                ),
              },
              {
                title: "状态",
                width: 160,
                render: (_, s) => (
                  <span>
                    {s.enabled ? <Tag color="success">启用</Tag> : <Tag>已停用</Tag>}
                    {s.access_check.ok === false && <Tag color="error">可直连</Tag>}
                    {s.fetch_error && <Tag color="warning">拉取失败</Tag>}
                  </span>
                ),
              },
              {
                title: "工具",
                width: 120,
                render: (_, s) => (
                  <span>
                    {s.tool_counts.ok ?? 0}
                    {(s.tool_counts.pending_review ?? 0) > 0 && (
                      <Tag color="warning" style={{ marginLeft: 4 }}>
                        {s.tool_counts.pending_review} 待复核
                      </Tag>
                    )}
                  </span>
                ),
              },
              {
                title: "最近拉取",
                width: 100,
                render: (_, s) => <span className={styles.muted}>{relativeTime(s.fetched_at)}</span>,
              },
            ]}
          />
        )}
      </div>
      {current && <ServiceDetail key={current.name} name={current.name} manage={manage} />}
      <RegisterModal open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}

function ServiceDetail({ name, manage }: { name: string; manage: boolean }) {
  const { message } = AntdApp.useApp();
  const detailQ = useGwService(name);
  const refsQ = useMcpServer(name);
  const refresh = useRefreshGwService(name);
  const setEnabled = useSetGwServiceEnabled(name);
  const [editing, setEditing] = useState(false);
  const d = detailQ.data;

  if (!d) {
    return (
      <aside className={styles.detail}>
        {detailQ.error ? <Alert type="error" showIcon message={detailQ.error.message} /> : <Skeleton active paragraph={{ rows: 8 }} />}
      </aside>
    );
  }

  return (
    <aside className={styles.detail}>
      <div className={styles.detailHead}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className={styles.detailTitle}>{d.display_name || d.name}</h2>
          <div className={styles.sub}>{d.name}__&lt;工具名&gt;</div>
        </div>
        {manage && (
          <>
            <Button size="small" onClick={() => setEditing(true)}>
              修改
            </Button>
            <Button
              size="small"
              icon={<Icon name="retry" size={13} />}
              loading={refresh.isPending}
              disabled={!d.enabled}
              onClick={() =>
                refresh.mutate(undefined, {
                  onSuccess: () => message.success("已重新拉取工具定义"),
                  onError: (e) => message.error(e.message),
                })
              }
            >
              刷新
            </Button>
          </>
        )}
      </div>
      {d.description && <p className={styles.muted}>{d.description}</p>}

      {d.access_check.ok === false && (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 12 }}
          message="上游可以直连：绕得过 MCP 网关"
          description={`${d.access_check.detail ?? ""}。到 Higress 上把这个服务的认证改成只允许 mcp-gateway。`}
        />
      )}
      {d.fetch_error && (
        <Alert type="warning" showIcon style={{ marginTop: 12 }} message="最近一次拉取失败，继续用旧定义" description={d.fetch_error} />
      )}

      <div className={styles.secTitle}>接入</div>
      <div className={styles.kv}>
        <span>上游</span>
        <span>Higress:{d.upstream_name}</span>
      </div>
      <div className={styles.kv}>
        <span>凭据</span>
        <span>主体 API Key</span>
      </div>
      <div className={styles.kv}>
        <span>接入检查</span>
        <span>{d.access_check.ok ? "不带网关凭据被拒 ✓" : (d.access_check.detail ?? "—")}</span>
      </div>
      <div className={styles.kv}>
        <span>单次调用超时</span>
        <span>{d.call_timeout_s ? `${d.call_timeout_s}s` : "全局默认"}</span>
      </div>
      <div className={styles.kv}>
        <span>工具定义需复核</span>
        <span>{d.review_required ? "是" : "否"}</span>
      </div>
      {manage && (
        <div className={styles.kv}>
          <span>启用</span>
          <span>
            <Switch
              size="small"
              checked={d.enabled}
              loading={setEnabled.isPending}
              onChange={(on) =>
                setEnabled.mutate(on, {
                  onSuccess: () => message.success(on ? "已启用" : "已停用：所有请求立即被网关拦下，含已打开的 ACP 会话"),
                  onError: (e) => message.error(e.message),
                })
              }
            />
          </span>
        </div>
      )}
      <div className={styles.kv}>
        <span>最近变化</span>
        <span>{d.changed_at ? relativeTime(d.changed_at) : "—"}</span>
      </div>

      <div className={styles.secTitle}>工具（{d.tools.length}）</div>
      {d.tools.map((t) => (
        <ToolRow key={t.name} service={d} tool={t} manage={manage} />
      ))}
      {d.removed_tools.length > 0 && (
        <p className={styles.muted}>
          上游已删除：<span className={styles.del}>{d.removed_tools.join("、")}</span>（依赖它们的智能体运行时会因缺工具而失败）
        </p>
      )}

      <div className={styles.secTitle}>授权（谁能用）</div>
      <McpGrants service={d.name} canGrant />

      <div className={styles.secTitle}>被引用</div>
      {(refsQ.data?.referenced_by ?? []).length === 0 ? (
        <div className={styles.muted}>没有智能体引用它</div>
      ) : (
        (refsQ.data!.referenced_by as Array<Record<string, unknown>>).map((r) => (
          <div key={`${r.agent_id}-${r.subagent ?? ""}`} className={styles.kv}>
            <span>
              <Link href={`/agents/${String(r.agent_id)}`}>{String(r.agent_name)}</Link> v{String(r.agent_version)}
              {r.subagent ? ` → ${String(r.subagent)}` : ""}
            </span>
            <span>{(r.tools as string[]).join(", ")}</span>
          </div>
        ))
      )}
      {editing && <EditModal service={d} onClose={() => setEditing(false)} />}
    </aside>
  );
}

function ToolRow({ service, tool: t, manage }: { service: GwServiceDetail; tool: GwTool; manage: boolean }) {
  const { message } = AntdApp.useApp();
  const review = useReviewGwTool();
  const rs = REVIEW_STATUS[t.status];
  const change = changeOf(t);
  const canReview = manage && service.review_required && t.status !== "invalid";
  const decide = (decision: "approved" | "rejected") =>
    review.mutate(
      { service: service.name, tool: t.name, digest: t.digest, decision },
      {
        onSuccess: () => message.success(decision === "approved" ? "已批准这一版定义" : "已拒绝这一版定义"),
        onError: (e) => message.error(e.message),
      },
    );
  return (
    <div className={styles.tool}>
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <span className={styles.toolName}>{t.name}</span>
        <Tag color={rs?.color}>{rs?.label ?? t.status}</Tag>
        {t.read_only && (
          <Tooltip title="服务声明只读：审批在所有模式下直接放行。复核时确认它确实不改数据">
            <Tag color="blue">只读</Tag>
          </Tooltip>
        )}
        {change && <Tag color="warning">{change}</Tag>}
        <span style={{ flex: 1 }} />
        {canReview && t.status !== "ok" && (
          <Button size="small" type="link" loading={review.isPending} onClick={() => decide("approved")}>
            批准
          </Button>
        )}
        {canReview && t.status !== "rejected" && (
          <Button size="small" type="link" danger loading={review.isPending} onClick={() => decide("rejected")}>
            拒绝
          </Button>
        )}
      </div>
      <div className={styles.muted} style={{ marginTop: 4 }}>
        {t.previous && t.previous.description !== t.description ? (
          <>
            <span className={styles.del}>{t.previous.description}</span> <span className={styles.add}>{t.description}</span>
          </>
        ) : (
          t.description
        )}
      </div>
      {t.issues.length > 0 && (
        <ul className={styles.hits}>
          {t.issues.map((i) => (
            <li key={i}>{i}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════ 待复核

function PendingTab({ rows }: { rows: { service: string; tool: GwTool }[] }) {
  if (rows.length === 0) return <Empty style={{ marginTop: 48 }} description="没有待复核的工具定义" />;
  return (
    <div style={{ maxWidth: 820 }}>
      <p className={styles.muted}>
        工具描述会原样进入模型上下文，它本身就是一段第三方文本。批准的是这一版定义：之后名字、描述、参数或只读注解任何一项变了，都要重新复核。
      </p>
      {rows.map((r) => (
        <PendingRow key={`${r.service}/${r.tool.name}`} service={r.service} tool={r.tool} />
      ))}
    </div>
  );
}

function PendingRow({ service, tool }: { service: string; tool: GwTool }) {
  const detailQ = useGwService(service);
  if (!detailQ.data) return null;
  return (
    <div style={{ marginBottom: 8 }}>
      <b>{service}</b>
      <ToolRow service={detailQ.data} tool={tool} manage />
    </div>
  );
}

// ═══════════════════════════════════════════════ 注册 / 修改

function RegisterModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const create = useCreateGwService();
  const [name, setName] = useState("");
  const [upstream, setUpstream] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [timeout, setTimeoutS] = useState<number | null>(null);
  const [reviewRequired, setReviewRequired] = useState(true);

  const close = () => {
    create.reset();
    onClose();
  };

  return (
    <Modal
      title="注册 MCP 服务"
      open={open}
      onCancel={close}
      okText="注册"
      okButtonProps={{ disabled: !name, loading: create.isPending }}
      onOk={() =>
        create.mutate(
          {
            name,
            display_name: displayName,
            description,
            upstream_name: upstream || null,
            review_required: reviewRequired,
            call_timeout_s: timeout,
          },
          {
            onSuccess: () => {
              message.success(`已注册 ${name}：你是它的所有者`);
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
          placeholder="服务名 = 工具前缀，如 github（小写字母开头，不含下划线，≤24；不能改、不能复用）"
        />
        <Input
          value={upstream}
          onChange={(e) => setUpstream(e.target.value)}
          placeholder="Higress 上 MCP 服务的名字（留空 = 和服务名相同）"
        />
        <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="显示名" />
        <Input.TextArea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="描述（给人看）" />
        <InputNumber
          style={{ width: "100%" }}
          min={1}
          value={timeout}
          onChange={setTimeoutS}
          placeholder="单次调用超时（秒），留空用全局默认"
        />
        <Checkbox checked={reviewRequired} onChange={(e) => setReviewRequired(e.target.checked)}>
          工具定义经人工复核后才能被选用、才会被网关放行
        </Checkbox>
        <p className={styles.muted} style={{ margin: 0 }}>
          先在 Higress 上声明服务（REST 转 MCP 或代理），认证选「只允许 mcp-gateway」。登记时网关会不带凭据试一次，能调通就不允许登记。
        </p>
        {create.error && <Alert type="error" showIcon message="注册失败" description={create.error.message} />}
      </div>
    </Modal>
  );
}

function EditModal({ service, onClose }: { service: GwServiceDetail; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const patch = usePatchGwService(service.name);
  const [displayName, setDisplayName] = useState(service.display_name);
  const [description, setDescription] = useState(service.description);
  const [upstream, setUpstream] = useState(service.upstream_name);
  const [timeout, setTimeoutS] = useState<number | null>(service.call_timeout_s);
  const [reviewRequired, setReviewRequired] = useState(service.review_required);

  return (
    <Modal
      title={`修改 ${service.name}`}
      open
      onCancel={onClose}
      okText="保存"
      okButtonProps={{ loading: patch.isPending }}
      onOk={() =>
        patch.mutate(
          {
            display_name: displayName,
            description,
            upstream_name: upstream,
            call_timeout_s: timeout,
            review_required: reviewRequired,
          },
          {
            onSuccess: () => {
              message.success("已保存");
              onClose();
            },
          },
        )
      }
    >
      <div style={{ display: "grid", gap: 10 }}>
        <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="显示名" />
        <Input.TextArea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="描述" />
        <Input value={upstream} onChange={(e) => setUpstream(e.target.value)} placeholder="Higress 上的名字" />
        <InputNumber
          style={{ width: "100%" }}
          min={1}
          value={timeout}
          onChange={setTimeoutS}
          placeholder="单次调用超时（秒），留空用全局默认"
        />
        <Checkbox checked={reviewRequired} onChange={(e) => setReviewRequired(e.target.checked)}>
          工具定义需人工复核
        </Checkbox>
        <p className={styles.muted} style={{ margin: 0 }}>
          换上游时会重新做接入检查、重新拉取工具定义。服务名不能改。
        </p>
        {patch.error && <Alert type="error" showIcon message="保存失败" description={patch.error.message} />}
      </div>
    </Modal>
  );
}
