"use client";

import {
  Alert,
  Button,
  Checkbox,
  Input,
  InputNumber,
  Segmented,
  Select,
  Skeleton,
  Slider,
  Space,
  Switch,
  Tag,
  App as AntdApp,
} from "antd";
import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import {
  useAgent,
  useAgents,
  useAgentVersions,
  useCreateAgent,
  useSetAgentStatus,
  useUpdateAgent,
} from "@/api/agents";
import { useMcpServers, useSkillCatalog } from "@/api/catalog";
import { useModels, useTools } from "@/api/meta";
import type { AgentDetail, AgentStatus, AgentVersion, AuthSubject } from "@/api/types";
import { relativeTime } from "@/lib/format";
import { qk } from "@/api/keys";
import { http } from "@/lib/http";
import { Icon } from "@/components/Icon";
import { ApiError } from "@/lib/http";
import { AVATAR_KEYS, avatarBackground, avatarLetter } from "@/features/agents/avatar";
import { AGENT_STATUS_META } from "@/features/agents/status";
import styles from "./editor.module.css";
import { AuthSubjectPicker, AuthSubjectSwitch, IdentityPanel, SharingPanel } from "./AccessSections";
import { McpToolsSection, SkillsSection } from "./CatalogSections";
import { DEFAULT_KNOWLEDGE, KnowledgeSection } from "./KnowledgeSection";
import {
  ASK_USER_TOOL,
  CLARIFY_TEMPLATE,
  defaultSpec,
  effortForbidsThinkingOff,
  reconcileModelSpec,
  type ResolvedSpec,
} from "./spec-defaults";

const EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;

/** 左侧锚点导航（prototype/atlas-v2.html 编辑器） */
const NAV: [string, string][] = [
  ["e-basic", "基本信息"],
  ["e-access", "权限类型"],
  ["e-kind", "执行形态"],
  ["e-prompt", "系统提示词"],
  ["e-model", "模型"],
  ["e-tools", "内置工具"],
  ["e-skills", "技能"],
  ["e-mcp", "MCP"],
  ["e-knowledge", "知识库"],
  ["e-subs", "子智能体"],
  ["e-approval", "审批模式"],
  ["e-limits", "限制"],
  ["e-share", "共享"],
];

/** 后端存的 spec → 编辑器的 spec。旧快照没有后加的字段：补默认值，免得各处判空 */
function normalizeSpec(raw: unknown): ResolvedSpec {
  const spec = raw as Partial<ResolvedSpec>;
  return {
    ...(spec as ResolvedSpec),
    skills: spec.skills ?? [],
    mcp_tool_digests: spec.mcp_tool_digests ?? {},
    mcp_drift_policy: spec.mcp_drift_policy ?? "warn",
    knowledge: spec.knowledge ?? DEFAULT_KNOWLEDGE,
    // 旧快照没有审批模式：后端按「默认自动、允许全部」处理（设计 D8），这里与之一致
    approval: spec.approval ?? defaultSpec().approval,
  };
}

/**
 * 版本历史。★ 「载入」只把那一版的配置放进编辑区，不直接回滚 —— 保存后成为
 *   一个**新**版本（版本只增不改，历史 run 仍指向各自当时的版本）。
 */
function VersionPanel({
  agentId,
  current,
  onRestore,
}: {
  agentId: string;
  current?: number;
  onRestore: (v: AgentVersion) => void;
}) {
  const versionsQ = useAgentVersions(agentId);
  const versions = [...(versionsQ.data?.data ?? [])].sort((a, b) => b.version - a.version);
  return (
    <aside className={styles.side} aria-label="版本历史">
      <p className={styles.sideTitle}>
        <Icon name="clock" size={13} /> 版本
        <span className={styles.sideHint}>新会话用当前版本</span>
      </p>
      {versionsQ.isPending && <Skeleton active paragraph={{ rows: 4 }} />}
      {versions.map((v) => (
        <div key={v.id} className={styles.version} aria-current={v.version === current ? "true" : undefined}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <b>v{v.version}</b>
            {v.version === current && (
              <Tag color="success" style={{ marginInlineEnd: 0 }}>
                当前
              </Tag>
            )}
            <span className={styles.sideHint}>{relativeTime(v.created_at)}</span>
          </div>
          <div className={styles.versionMeta}>
            {(v.spec as { model?: { model?: string } }).model?.model ?? ""}
            {" · "}
            {((v.spec as { tool_names?: string[] }).tool_names ?? []).length} 工具
            {" · "}
            {((v.spec as { skills?: unknown[] }).skills ?? []).length} 技能
          </div>
          {v.version !== current && (
            <Button size="small" type="link" style={{ padding: 0 }} onClick={() => onRestore(v)}>
              载入到编辑区
            </Button>
          )}
        </div>
      ))}
    </aside>
  );
}

interface Draft {
  slug: string;
  name: string;
  description: string;
  avatar_key: string;
  spec: ResolvedSpec;
  /** 权限类型：只在新建时随表单提交；之后由所有者单独切换（数字员工设计 §7.1） */
  auth_subject: AuthSubject;
}

function Section({
  id,
  title,
  desc,
  children,
}: {
  id?: string;
  title: string;
  desc?: string;
  children: React.ReactNode;
}) {
  return (
    <div id={id} className={styles.section}>
      <h2 className={styles.sectionTitle}>{title}</h2>
      {desc && <p className={styles.sectionDesc}>{desc}</p>}
      {children}
    </div>
  );
}

type PermissionModeValue = "manual" | "accept_edits" | "auto" | "plan";

/** ACP CLI 的权限模式（doc/acp-permission-mode-design.html）。不提供 bypass。 */
const PERMISSION_MODE_OPTIONS: { value: PermissionModeValue; label: string }[] = [
  { value: "auto", label: "Auto（默认）· CLI 自己判断，有风险的直接拒绝" },
  { value: "accept_edits", label: "自动接受编辑 · 执行命令仍要确认" },
  { value: "manual", label: "手动确认 · 编辑和命令都要确认" },
  { value: "plan", label: "仅规划 · 不执行任何操作" },
];

type ApprovalModeValue = "plan" | "manual" | "auto";

/**
 * Native 的审批模式（doc/native-approval-mode-design.html §4）。顺序 = 严格程度从高到低，
 * 与后端 APPROVAL_MODES 一致。只读与写待办在任何模式下都不问。
 */
const APPROVAL_MODE_OPTIONS: { value: ApprovalModeValue; label: string; desc: string }[] = [
  { value: "plan", label: "规划", desc: "只读调研，提交计划经用户批准后再执行" },
  { value: "manual", label: "手动", desc: "写文件、删除、执行命令、外部工具、委派，每次都问" },
  {
    value: "auto",
    label: "自动",
    desc: "工作区内读写与委派直接执行；执行命令、删除、外部工具先请示（判断器上线后由它判断）",
  },
];

export function AgentEditor({ agentId }: { agentId?: string }) {
  const router = useRouter();
  const { message } = AntdApp.useApp();
  const isNew = !agentId;

  const detailQ = useAgent(agentId);
  const modelsQ = useModels();
  const skillCatalogQ = useSkillCatalog();
  const mcpServersQ = useMcpServers();

  const createAgent = useCreateAgent();
  const updateAgent = useUpdateAgent(agentId ?? "");
  const setStatus = useSetAgentStatus(agentId ?? "");

  // 可被选作子智能体的候选：已启用的其它智能体（草稿还没定型，不该被别人依赖）
  const candidatesQ = useAgents({ status: "enabled" });
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);

  const [draft, setDraft] = useState<Draft>({
    slug: "",
    name: "",
    description: "",
    avatar_key: "general",
    spec: defaultSpec(),
    auth_subject: "user",
  });
  // MCP 工具按权限主体列出：编辑已有的按 agentId；新建的随「权限类型」切换（以 Agent 为权限、
  // 没保存时还没有数字员工，MCP 部分为空 —— doc/mcp-design.html §9.2）
  const toolsQ = useTools({ agentId, authSubject: draft.auth_subject });

  // 载入已有智能体。spec 在 OpenAPI 里是自由 dict（后端存的是 JSONB），
  // 这里断言成 ResolvedSpec —— 它由同一份 pydantic 模型序列化而来，结构一致。
  useEffect(() => {
    const d = detailQ.data;
    if (!d) return;
    setDraft({
      slug: d.slug,
      name: d.name,
      description: d.description,
      avatar_key: d.avatar_key,
      spec: normalizeSpec(d.spec),
      auth_subject: d.auth_subject,
    });
  }, [detailQ.data]);

  const models = modelsQ.data?.data ?? [];
  const modelInfo = useMemo(
    () => models.find((m) => m.model === draft.spec.model.model),
    [models, draft.spec.model.model],
  );

  const saving = createAgent.isPending || updateAgent.isPending;
  const saveError = (createAgent.error ?? updateAgent.error) as ApiError | null;

  const patchSpec = (patch: Partial<ResolvedSpec>) =>
    setDraft((d) => ({ ...d, spec: { ...d.spec, ...patch } }));

  const patchModel = (patch: Partial<ResolvedSpec["model"]>) =>
    setDraft((d) => ({ ...d, spec: { ...d.spec, model: { ...d.spec.model, ...patch } } }));

  const patchLimits = (patch: Partial<ResolvedSpec["limits"]>) =>
    setDraft((d) => ({ ...d, spec: { ...d.spec, limits: { ...d.spec.limits, ...patch } } }));

  /**
   * 改允许的模式。默认模式被去掉时改成剩下里最宽松的那个（列表按严格程度排，取最后一个）——
   * 默认不在允许范围里后端会拒绝保存。
   */
  const setAllowedModes = (picked: ApprovalModeValue[]) => {
    const allowed = APPROVAL_MODE_OPTIONS.map((o) => o.value).filter((m) => picked.includes(m));
    // 至少留一种能执行的模式（复选框已禁用最后一项，这里再兜一次）
    if (!allowed.some((m) => m !== "plan")) return;
    setDraft((d) => {
      const current = d.spec.approval.default_mode;
      const fallback = allowed[allowed.length - 1] ?? "auto";
      return {
        ...d,
        spec: {
          ...d.spec,
          approval: {
            allowed_modes: allowed,
            default_mode: allowed.includes(current) ? current : fallback,
          },
        },
      };
    });
  };

  const onModelChange = (model: string) => {
    const info = models.find((m) => m.model === model);
    setDraft((d) => ({
      ...d,
      spec: { ...d.spec, model: reconcileModelSpec({ ...d.spec.model, model }, info) },
    }));
  };

  /**
   * 选中一个已有智能体 → 把它当时的配置**拷贝**进 subagents。
   *
   * ★ 为什么是拷贝而不是存 id 在运行时解引用：spec 是版本快照，
   *   run 绑 agent_version_id 才能回答"当时用的哪份配置"（§5.4）。
   *   存活引用的话，来源智能体一改，所有历史 run 的含义都跟着变。
   */
  const addSubagentFrom = async (id: string) => {
    setAdding(true);
    try {
      const src = await qc.fetchQuery({
        queryKey: qk.agents.detail(id),
        queryFn: () => http.get<AgentDetail>(`/v1/agents/${id}`),
      });
      const spec = src.spec as unknown as ResolvedSpec;
      setDraft((d) => ({
        ...d,
        spec: {
          ...d.spec,
          subagents: [
            ...d.spec.subagents,
            {
              // name 就是模型调用 task 时传的 subagent_type，用 slug 而非展示名：
              // 它是标识符，且 slug 的唯一性由库约束保证（spec.validate 要求名称唯一）
              name: src.slug,
              description: src.description,
              system_prompt: spec.system_prompt ?? "",
              model: spec.model,
              // SubAgent 可以继续派 SubAgent（共用这份名录，层数不设上限），task 照原样保留
              tool_names: spec.tool_names ?? [],
              // ★ 与后端 SubAgentSpec 的默认值对齐。两者在生成类型里是必填
              //   （后端有默认值 ⇒ 响应里必然存在），少写就编译不过。
              //   persistent：子智能体持有自己的会话，第二次委派恢复上下文。
              session_mode: "persistent",
              // 从已有 agent 派生出来的子智能体恒为 native —— 编辑器里挑的
              //   都是平台自己的 agent；acp 子智能体目前只能经 API 配置。
              kind: "native",
              source_agent_id: src.id,
              // 技能随配置一起拷贝（版本已钉死）；MCP 指纹由服务端保存时重新记录
              skills: spec.skills ?? [],
              mcp_tool_digests: {},
              mcp_drift_policy: spec.mcp_drift_policy ?? "warn",
            },
          ],
        },
      }));
    } finally {
      setAdding(false);
    }
  };

  const handleSave = () => {
    if (isNew) {
      createAgent.mutate(
        {
          slug: draft.slug,
          name: draft.name,
          description: draft.description,
          avatar_key: draft.avatar_key,
          spec: draft.spec,
          auth_subject: draft.auth_subject,
        },
        {
          onSuccess: (a) => {
            message.success("已创建");
            router.replace(`/agents/${a.id}`);
          },
        },
      );
    } else {
      updateAgent.mutate(
        {
          name: draft.name,
          description: draft.description,
          avatar_key: draft.avatar_key,
          spec: draft.spec,
        },
        // 保存 = 新建版本，不是原地覆盖（§5.4）
        { onSuccess: (a) => message.success(`已保存为 v${a.version}`) },
      );
    }
  };

  if (detailQ.isPending && !isNew) {
    return (
      <div className={styles.page}>
        <div className={styles.body}>
          <Skeleton active paragraph={{ rows: 8 }} />
        </div>
      </div>
    );
  }

  const detail = detailQ.data;
  // 编辑以人为权限的要「编辑」，以 Agent 为权限的要「所有者」（谁能改它，谁就间接握有数字员工
  // 的全部权限，数字员工设计 §8.2）；启用 / 归档、共享只有所有者
  const isOwner = detail?.my_level === "OWNER";
  const canEdit = isOwner || (detail?.my_level === "WRITE" && detail.auth_subject === "user");
  const readOnly = !!detail && !canEdit;

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <span
          style={{
            width: 40,
            height: 40,
            borderRadius: 10,
            display: "grid",
            placeItems: "center",
            color: "#fff",
            fontWeight: 700,
            flex: "none",
            background: avatarBackground(draft.avatar_key),
          }}
          aria-hidden="true"
        >
          {avatarLetter(draft.name || "新")}
        </span>
        <div style={{ minWidth: 0 }}>
          <h1 className={styles.title}>{isNew ? "新建智能体" : draft.name || "未命名"}</h1>
          <p className={styles.subtitle}>
            {draft.slug || "尚未设置标识"}
            {detail && ` · v${detail.version}`}
            {detail && (
              <Tag
                color={AGENT_STATUS_META[detail.status].color}
                style={{ marginLeft: 8, transform: "translateY(-1px)" }}
              >
                {AGENT_STATUS_META[detail.status].label}
              </Tag>
            )}
          </p>
        </div>

        <div className={styles.actions}>
          {/* ★ 三态各有各的出口，不能写成「archived ? 恢复 : 归档」的二态开关 ——
              那样 draft 只会看到「归档」，新建的智能体就永远出不了草稿态：
              既不能启用，也就选不进新建会话的下拉。 */}
          {detail && isOwner && detail.status !== "enabled" && (
            <Button
              type={detail.status === "draft" ? "primary" : "default"}
              ghost={detail.status === "draft"}
              onClick={() => setStatus.mutate("enabled" as AgentStatus)}
              loading={setStatus.isPending}
            >
              {detail.status === "draft" ? "启用" : "恢复启用"}
            </Button>
          )}
          {detail && isOwner && detail.status !== "archived" && (
            <Button onClick={() => setStatus.mutate("archived" as AgentStatus)} loading={setStatus.isPending}>
              归档
            </Button>
          )}
          <Button onClick={() => router.push("/agents")}>返回</Button>
          <Button
            type="primary"
            loading={saving}
            disabled={readOnly || !draft.name || (isNew && !draft.slug)}
            onClick={handleSave}
          >
            {isNew ? "创建" : "保存新版本"}
          </Button>
        </div>
      </header>

      <div className={styles.layout}>
        <nav className={styles.nav} aria-label="配置分节">
          {NAV.filter(([id]) => detail || id !== "e-share").map(([id, label]) => (
            <a key={id} href={`#${id}`} className={styles.navLink}>
              {label}
            </a>
          ))}
        </nav>
      <div className={styles.body}>
        <div className={styles.form}>
          {readOnly && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message="你可以使用这个智能体，但不能修改它"
              description={
                detail?.auth_subject === "agent"
                  ? "以 Agent 为权限的智能体只有所有者能修改。"
                  : "需要所有者共享「编辑」权限。"
              }
            />
          )}

          {saveError && (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 16 }}
              message={
                saveError.kind === "invalid_spec"
                  ? "配置不合法，已被拒绝"
                  : saveError.kind === "invalid_reference"
                    ? "引用的技能或 MCP 工具有问题"
                    : saveError.kind === "dependency_unavailable"
                      ? "配置服务暂时不可用，无法确认引用 —— 稍后再保存"
                      : "保存失败"
              }
              // engine 的 spec.validate() 会明确指出哪个参数不被该模型接受
              // （§3 D3：不静默降级），message 已经是可读的中文
              description={saveError.message}
            />
          )}

          <Section id="e-basic" title="基本信息">
            <div className={styles.grid2}>
              <div>
                <label style={{ fontSize: 12, color: "var(--fg-2)" }}>名称</label>
                <Input
                  value={draft.name}
                  disabled={readOnly}
                  onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
                  placeholder="数据分析师"
                />
              </div>
              <div>
                <label style={{ fontSize: 12, color: "var(--fg-2)" }}>
                  标识 slug{!isNew && "（创建后不可改）"}
                </label>
                <Input
                  value={draft.slug}
                  disabled={!isNew || readOnly}
                  onChange={(e) => setDraft((d) => ({ ...d, slug: e.target.value }))}
                  placeholder="data-analyst"
                  className={styles.mono}
                />
              </div>
            </div>

            <div style={{ marginTop: "var(--s-4)" }}>
              <label style={{ fontSize: 12, color: "var(--fg-2)" }}>描述</label>
              <Input.TextArea
                value={draft.description}
                disabled={readOnly}
                rows={2}
                onChange={(e) => setDraft((d) => ({ ...d, description: e.target.value }))}
                placeholder="一句话说明它擅长什么"
              />
            </div>

            <div style={{ marginTop: "var(--s-4)" }}>
              <label style={{ fontSize: 12, color: "var(--fg-2)", display: "block" }}>头像</label>
              <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
                {AVATAR_KEYS.map((k) => (
                  <button
                    key={k}
                    type="button"
                    disabled={readOnly}
                    aria-label={`头像 ${k}`}
                    aria-pressed={draft.avatar_key === k}
                    onClick={() => setDraft((d) => ({ ...d, avatar_key: k }))}
                    style={{
                      width: 28,
                      height: 28,
                      borderRadius: 8,
                      background: avatarBackground(k),
                      border:
                        draft.avatar_key === k
                          ? "2px solid var(--primary-text)"
                          : "2px solid transparent",
                      cursor: readOnly ? "not-allowed" : "pointer",
                    }}
                  />
                ))}
              </div>
            </div>
          </Section>

          <Section
            id="e-access"
            title="权限类型"
            desc="决定运行时按谁的权限访问知识库、MCP 等资源。能不能用这个智能体，另由「共享」决定。"
          >
            {isNew || !detail ? (
              <AuthSubjectPicker
                value={draft.auth_subject}
                onChange={(v) => setDraft((d) => ({ ...d, auth_subject: v }))}
              />
            ) : (
              <Space direction="vertical" size={12} style={{ width: "100%" }}>
                <AuthSubjectSwitch detail={detail} />
                <IdentityPanel agentId={detail.id} />
              </Space>
            )}
          </Section>

          <Section
            id="e-kind"
            title="执行形态"
            desc="type 是机制不是角色：Native 在平台内运行、可以委派子智能体；ACP 在会话 Pod 里驱动 CLI，工具由 CLI 自带。"
          >
            <Segmented
              disabled
              value={draft.spec.kind ?? "native"}
              options={[
                { value: "native", label: "Native · 平台内运行" },
                { value: "acp", label: "ACP · CLI" },
              ]}
            />
            {draft.spec.kind === "acp" && draft.spec.cli ? (
              <div style={{ marginTop: 12, fontSize: 12.5, color: "var(--fg-2)" }}>
                CLI <code>{draft.spec.cli.cli_type}</code> · 权限模式{" "}
                <Select
                  size="small"
                  style={{ width: 300 }}
                  disabled={readOnly}
                  value={draft.spec.cli.permission_mode ?? "auto"}
                  options={PERMISSION_MODE_OPTIONS}
                  onChange={(mode: PermissionModeValue) =>
                    draft.spec.cli && patchSpec({ cli: { ...draft.spec.cli, permission_mode: mode } })
                  }
                />
              </div>
            ) : (
              <p className={styles.capNote}>执行形态在创建时确定；ACP 智能体目前经 API 创建。</p>
            )}
          </Section>

          <Section id="e-prompt" title="系统提示词" desc="决定这个智能体的角色与行为准则。">
            <Input.TextArea
              value={draft.spec.system_prompt}
              disabled={readOnly}
              rows={8}
              onChange={(e) => patchSpec({ system_prompt: e.target.value })}
              placeholder="你是……"
            />
            {draft.spec.tool_names.includes(ASK_USER_TOOL) && !readOnly && (
              <p className={styles.capNote}>
                已开启「向用户提问」：何时问、怎么问写在工具说明里；这个领域该问哪几项，建议在提示词里列出来。{" "}
                <Button
                  size="small"
                  type="link"
                  style={{ padding: 0 }}
                  onClick={() =>
                    patchSpec({
                      system_prompt: `${draft.spec.system_prompt.trimEnd()}\n\n${CLARIFY_TEMPLATE}`.trim(),
                    })
                  }
                >
                  插入澄清清单模板
                </Button>
              </p>
            )}
          </Section>

          <Section
            id="e-model"
            title="模型"
            desc="下方控件按所选模型的实测能力渲染 —— 不支持的参数不会出现，避免配出会被网关拒绝的组合。"
          >
            <div className={styles.grid2}>
              <div>
                <label style={{ fontSize: 12, color: "var(--fg-2)" }}>模型</label>
                <Select
                  style={{ width: "100%" }}
                  value={draft.spec.model.model}
                  disabled={readOnly}
                  loading={modelsQ.isPending}
                  onChange={onModelChange}
                  options={models.map((m) => ({
                    value: m.model,
                    label: m.display_name || m.model,
                    disabled: !m.is_available,
                  }))}
                />
                {modelInfo && (
                  <p className={styles.capNote}>
                    上下文 {(modelInfo.context_window / 1000).toFixed(0)}k · 最大输出{" "}
                    {(modelInfo.max_output_tokens / 1000).toFixed(0)}k
                  </p>
                )}
              </div>

              <div>
                <label style={{ fontSize: 12, color: "var(--fg-2)" }}>最大输出 tokens</label>
                <InputNumber
                  style={{ width: "100%" }}
                  min={256}
                  max={modelInfo?.max_output_tokens ?? 128_000}
                  step={1024}
                  disabled={readOnly}
                  value={draft.spec.model.max_output_tokens}
                  onChange={(v) => v && patchModel({ max_output_tokens: v })}
                />
              </div>
            </div>

            {/* ★ 能力矩阵联动（§3 D3）：effort 与 temperature 互斥地出现 */}
            {modelInfo?.supports_effort && (
              <div style={{ marginTop: "var(--s-4)" }}>
                <label style={{ fontSize: 12, color: "var(--fg-2)", display: "block" }}>
                  推理强度 effort
                </label>
                <Segmented
                  style={{ marginTop: 4 }}
                  disabled={readOnly}
                  value={draft.spec.model.effort ?? "high"}
                  onChange={(v) => patchModel({ effort: v as (typeof EFFORTS)[number] })}
                  options={EFFORTS.map((e) => ({ value: e, label: e }))}
                />
                {effortForbidsThinkingOff(draft.spec.model.effort) &&
                  draft.spec.model.thinking === "off" && (
                    <p className={styles.capNote} style={{ color: "var(--warning)" }}>
                      effort 为 {draft.spec.model.effort} 时不能关闭 thinking，保存会被拒绝。
                    </p>
                  )}
              </div>
            )}

            {modelInfo?.supports_temperature && (
              <div style={{ marginTop: "var(--s-4)" }}>
                <label style={{ fontSize: 12, color: "var(--fg-2)" }}>
                  temperature（留空则用模型默认）
                </label>
                <Slider
                  min={0}
                  max={1}
                  step={0.05}
                  disabled={readOnly}
                  value={draft.spec.model.temperature ?? 0}
                  onChange={(v) => patchModel({ temperature: v })}
                />
              </div>
            )}

            <div className={styles.grid2} style={{ marginTop: "var(--s-4)" }}>
              <div>
                <label style={{ fontSize: 12, color: "var(--fg-2)" }}>thinking</label>
                <Select
                  style={{ width: "100%" }}
                  disabled={readOnly}
                  value={draft.spec.model.thinking}
                  onChange={(v) => patchModel({ thinking: v })}
                  options={[
                    { value: "auto", label: "auto（按模型能力自动决定）" },
                    {
                      value: "adaptive",
                      label: "adaptive",
                      disabled: !modelInfo?.supports_adaptive_thinking,
                    },
                    { value: "off", label: "off" },
                  ]}
                />
                {modelInfo && !modelInfo.supports_adaptive_thinking && (
                  <p className={styles.capNote}>该模型不支持 adaptive thinking。</p>
                )}
              </div>

              <div>
                <label style={{ fontSize: 12, color: "var(--fg-2)", display: "block" }}>
                  提示词缓存
                </label>
                <Switch
                  style={{ marginTop: 6 }}
                  disabled={readOnly || !modelInfo?.supports_cache}
                  checked={draft.spec.model.prompt_cache}
                  onChange={(v) => patchModel({ prompt_cache: v })}
                />
                {modelInfo && (
                  <p className={styles.capNote}>
                    最小可缓存 {modelInfo.min_cacheable_tokens} tokens
                  </p>
                )}
              </div>
            </div>
          </Section>

          <Section id="e-tools" title="内置工具" desc="平台自己实现的工具。勾选后才可在「人工确认」里选择。">
            {toolsQ.data?.data
              .filter((t) => t.kind !== "mcp")
              .map((t) => {
                const checked = draft.spec.tool_names.includes(t.name);
                return (
                  <div
                    key={t.name}
                    className={`${styles.toolRow} ${t.available ? "" : styles.toolRowDisabled}`}
                  >
                    <Checkbox
                      checked={checked}
                      disabled={readOnly || !t.available}
                      onChange={(e) =>
                        patchSpec({
                          tool_names: e.target.checked
                            ? [...draft.spec.tool_names, t.name]
                            : draft.spec.tool_names.filter((n) => n !== t.name),
                        })
                      }
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <span className={styles.toolName}>{t.name}</span>{" "}
                      <span style={{ fontSize: 12.5 }}>{t.display_name}</span>
                      {!t.available && (
                        <Tag color="default" style={{ marginLeft: 6 }}>
                          本期未接入
                        </Tag>
                      )}
                      <p className={styles.toolDesc}>{t.note || t.description}</p>
                    </div>
                  </div>
                );
              })}
          </Section>

          <SkillsSection
            skills={draft.spec.skills}
            catalog={skillCatalogQ.data}
            readOnly={readOnly}
            onChange={(skills) => patchSpec({ skills })}
          />

          <McpToolsSection
            tools={(toolsQ.data?.data ?? []).filter((t) => t.kind === "mcp")}
            subject={toolsQ.data?.mcp_subject ?? null}
            servers={mcpServersQ.data?.data ?? []}
            picked={draft.spec.tool_names}
            digests={draft.spec.mcp_tool_digests ?? {}}
            policy={draft.spec.mcp_drift_policy ?? "warn"}
            readOnly={readOnly}
            onToggle={(name, on) =>
              patchSpec({
                tool_names: on
                  ? [...draft.spec.tool_names, name]
                  : draft.spec.tool_names.filter((n) => n !== name),
              })
            }
            onPolicy={(mcp_drift_policy) => patchSpec({ mcp_drift_policy })}
          />

          <KnowledgeSection
            value={draft.spec.knowledge ?? DEFAULT_KNOWLEDGE}
            kind={draft.spec.kind ?? "native"}
            readOnly={readOnly}
            onChange={(knowledge) => patchSpec({ knowledge })}
          />

          <Section
            id="e-subs"
            title="子智能体"
            desc="主智能体通过 task 工具把子任务整体委派出去，在独立上下文里执行，只拿回结论。"
          >
            {/* task 与 subagents 是一对：缺任何一半，委派都不会发生，
                而且是静默的 —— 所以两种不匹配都要当场说清楚。 */}
            <Select
              style={{ width: "100%", marginBottom: 12 }}
              placeholder={readOnly ? "没有编辑权限" : "选择一个已启用的智能体加入…"}
              value={null}
              disabled={readOnly}
              loading={candidatesQ.isPending || adding}
              showSearch
              optionFilterProp="label"
              onChange={(id: string) => void addSubagentFrom(id)}
              options={(candidatesQ.data?.data ?? [])
                // 不能选自己（自我委派），也不重复添加
                .filter(
                  (a) =>
                    a.id !== agentId &&
                    !draft.spec.subagents.some((s) => s.source_agent_id === a.id),
                )
                .map((a) => ({
                  value: a.id,
                  label: `${a.name} · ${a.slug} · ${a.model}`,
                }))}
              notFoundContent="没有可选的智能体 —— 先把要复用的那个启用"
            />

            {draft.spec.subagents.length === 0 ? (
              <Alert
                type={draft.spec.tool_names.includes("task") ? "warning" : "info"}
                showIcon
                message={
                  draft.spec.tool_names.includes("task")
                    ? "勾了 task 却没有子智能体 —— 该工具不会生效"
                    : "尚未配置子智能体"
                }
                description={
                  draft.spec.tool_names.includes("task")
                    ? "没有子智能体时 task 不会进入工具表，模型根本看不到它，run.started 会把它列入 unsupported_tools。"
                    : "从上方选一个已启用的智能体加入，再在「工具」里勾选 task，委派才会启用。"
                }
              />
            ) : (
              <>
                {!draft.spec.tool_names.includes("task") && (
                  <Alert
                    type="warning"
                    showIcon
                    style={{ marginBottom: 12 }}
                    message="已配置子智能体，但没有勾选 task"
                    description="主智能体因此无法委派 —— 请在上方「工具」里勾上 task。"
                  />
                )}
                {draft.spec.subagents.map((sub, i) => (
                  <div key={sub.name} className={styles.toolRow}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <span className={styles.toolName}>{sub.name}</span>{" "}
                      <Tag color="default" style={{ marginLeft: 2 }}>
                        {sub.model.model}
                      </Tag>
                      {sub.source_agent_id && (
                        <Tag color="purple" style={{ marginInlineEnd: 4 }}>
                          来自智能体
                        </Tag>
                      )}
                      {/* ACP 子智能体（CLI 跑在会话 Pod 里）：权限模式由作者定，不让模型自己选 */}
                      {sub.kind === "acp" && sub.cli && (
                        <div style={{ margin: "6px 0 2px", fontSize: 12, color: "var(--fg-2)" }}>
                          CLI 权限模式{" "}
                          <Select
                            size="small"
                            style={{ width: 300 }}
                            disabled={readOnly}
                            value={sub.cli.permission_mode ?? "auto"}
                            options={PERMISSION_MODE_OPTIONS}
                            onChange={(mode: PermissionModeValue) =>
                              patchSpec({
                                subagents: draft.spec.subagents.map((s, j) =>
                                  j === i && s.cli ? { ...s, cli: { ...s.cli, permission_mode: mode } } : s,
                                ),
                              })
                            }
                          />
                        </div>
                      )}
                      {!readOnly && (
                        <Button
                          size="small"
                          type="text"
                          danger
                          style={{ float: "right" }}
                          onClick={() =>
                            patchSpec({
                              subagents: draft.spec.subagents.filter((_, j) => j !== i),
                            })
                          }
                        >
                          移除
                        </Button>
                      )}
                      {(sub.tool_names ?? []).map((n) => (
                        <Tag key={n} color="blue" style={{ marginInlineEnd: 4 }}>
                          {n}
                        </Tag>
                      ))}
                      {(sub.tool_names ?? []).length === 0 && (
                        <span style={{ fontSize: 12, color: "var(--fg-3)" }}>无工具</span>
                      )}
                      {(sub.skills ?? []).map((k) => (
                        <Tag key={k.slug} color="purple" style={{ marginInlineEnd: 4 }}>
                          技能 {k.slug}
                          {k.version != null ? ` v${k.version}` : ""}
                        </Tag>
                      ))}
                      {/* description 是主智能体判断「何时委派」的唯一依据，
                          所以它比 system_prompt 更该被看见。 */}
                      <p className={styles.toolDesc}>
                        {sub.description || "未填写委派说明 —— 主智能体将无从判断何时该用它"}
                      </p>
                      {sub.system_prompt && (
                        <details style={{ marginTop: 4 }}>
                          <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--fg-2)" }}>
                            系统提示词
                          </summary>
                          <pre
                            style={{
                              margin: "6px 0 0",
                              padding: 10,
                              borderRadius: 8,
                              background: "var(--bg-2)",
                              fontSize: 12,
                              lineHeight: 1.6,
                              whiteSpace: "pre-wrap",
                              wordBreak: "break-word",
                            }}
                          >
                            {sub.system_prompt}
                          </pre>
                        </details>
                      )}
                    </div>
                  </div>
                ))}
                <p style={{ margin: "10px 0 0", fontSize: 12, color: "var(--fg-3)" }}>
                  子智能体也能继续派这份名录里的子智能体，层数不设上限；一轮累计能派出的数量由服务端配置兜底。
                  <br />
                  加入的是来源智能体<b>当时的配置副本</b> —— 之后改动来源智能体不会影响这里，
                  历史会话的可复现性也不受影响。需要更新时，移除后重新加入即可。
                </p>
              </>
            )}
          </Section>

          <Section
            id="e-approval"
            title="审批模式"
            desc="作者定默认模式和允许的范围，用户在对话里从允许的模式中选。拒绝不终止本轮 —— 连同用户给的原因作为工具结果回给智能体，它可以换个方案继续。"
          >
            {draft.spec.kind === "acp" ? (
              <p className={styles.capNote}>
                ACP 智能体的权限由 CLI 的权限模式决定（见「执行形态」）；作为子智能体时与父智能体的模式取更严格的那个。
              </p>
            ) : (
              <>
                <label style={{ fontSize: 12, color: "var(--fg-2)" }}>允许的模式</label>
                <Checkbox.Group
                  style={{ display: "flex", flexDirection: "column", gap: 6, margin: "6px 0 12px" }}
                  disabled={readOnly}
                  value={draft.spec.approval.allowed_modes}
                  onChange={(v) => setAllowedModes(v as ApprovalModeValue[])}
                >
                  {APPROVAL_MODE_OPTIONS.map((o) => {
                    const allowed = draft.spec.approval.allowed_modes ?? [];
                    // 至少留一种能执行的模式：只剩规划时计划永远批不下来（后端同样会拒绝）
                    const lastExecution =
                      o.value !== "plan" &&
                      allowed.includes(o.value) &&
                      allowed.filter((m) => m !== "plan").length === 1;
                    return (
                      <Checkbox key={o.value} value={o.value} disabled={lastExecution}>
                        <b>{o.label}</b>
                        <span style={{ marginLeft: 8, fontSize: 12, color: "var(--fg-3)" }}>{o.desc}</span>
                      </Checkbox>
                    );
                  })}
                </Checkbox.Group>
                <label style={{ fontSize: 12, color: "var(--fg-2)" }}>默认模式</label>
                <div style={{ marginTop: 6 }}>
                  <Segmented
                    disabled={readOnly}
                    value={draft.spec.approval.default_mode}
                    options={APPROVAL_MODE_OPTIONS.filter((o) =>
                      (draft.spec.approval.allowed_modes ?? []).includes(o.value),
                    ).map((o) => ({ value: o.value, label: o.label }))}
                    onChange={(v) =>
                      patchSpec({
                        approval: { ...draft.spec.approval, default_mode: v as ApprovalModeValue },
                      })
                    }
                  />
                </div>
                <p style={{ margin: "8px 0 0", fontSize: 12, color: "var(--fg-3)" }}>
                  工具属于哪一类由工具自己声明（MCP 工具按 <code>readOnlyHint</code> 注解，没有注解的按外部工具处理），
                  不再逐个勾选。子智能体与父智能体的模式取更严格的那个。审批没有等待上限。
                </p>
              </>
            )}
          </Section>

          <Section
            id="e-limits"
            title="限制"
            desc="只限制「做多少」，不限制「做多久」：一轮运行和审批等待都没有时长上限。"
          >
            <div className={styles.grid2}>
              <div>
                <label style={{ fontSize: 12, color: "var(--fg-2)" }}>最大步数</label>
                <InputNumber
                  style={{ width: "100%" }}
                  min={1}
                  disabled={readOnly}
                  value={draft.spec.limits.max_steps}
                  onChange={(v) => v && patchLimits({ max_steps: v })}
                />
              </div>
              <div>
                <label style={{ fontSize: 12, color: "var(--fg-2)" }}>累计 token 上限</label>
                <InputNumber
                  style={{ width: "100%" }}
                  min={1000}
                  step={10_000}
                  disabled={readOnly}
                  value={draft.spec.limits.max_total_tokens}
                  onChange={(v) => v && patchLimits({ max_total_tokens: v })}
                />
              </div>
              <div>
                <label style={{ fontSize: 12, color: "var(--fg-2)" }}>工具并发</label>
                <InputNumber
                  style={{ width: "100%" }}
                  min={1}
                  max={16}
                  disabled={readOnly}
                  value={draft.spec.limits.tool_concurrency}
                  onChange={(v) => v && patchLimits({ tool_concurrency: v })}
                />
              </div>
            </div>
          </Section>

          {detail && (
            <Section id="e-share" title="共享" desc="谁能使用、编辑这个智能体（用户中心数据权限）。新建后只有你是所有者。">
              <SharingPanel detail={detail} />
            </Section>
          )}

          {detail && (
            <p style={{ fontSize: 12, color: "var(--fg-3)" }}>
              <Icon name="layers" size={13} style={{ verticalAlign: -2, marginRight: 4 }} />
              保存会生成新版本，历史会话仍使用当时的版本快照，不受影响。
            </p>
          )}
        </div>
      </div>
        {agentId && (
          <VersionPanel
            agentId={agentId}
            current={detail?.version}
            onRestore={(v) => {
              setDraft((d) => ({ ...d, spec: normalizeSpec(v.spec) }));
              message.info(`已载入 v${v.version} 的配置到编辑区 —— 保存后成为新版本`);
            }}
          />
        )}
      </div>
    </div>
  );
}
