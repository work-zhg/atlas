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
  Switch,
  Tag,
  App as AntdApp,
} from "antd";
import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { useAgent, useAgents, useCreateAgent, useSetAgentStatus, useUpdateAgent } from "@/api/agents";
import { useModels, useTools } from "@/api/meta";
import type { AgentDetail, AgentStatus } from "@/api/types";
import { qk } from "@/api/keys";
import { http } from "@/lib/http";
import { Icon } from "@/components/Icon";
import { ApiError } from "@/lib/http";
import { AVATAR_KEYS, avatarBackground, avatarLetter } from "@/features/agents/avatar";
import { AGENT_STATUS_META } from "@/features/agents/status";
import styles from "./editor.module.css";
import {
  defaultSpec,
  effortForbidsThinkingOff,
  reconcileModelSpec,
  type ResolvedSpec,
} from "./spec-defaults";

const EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;

interface Draft {
  slug: string;
  name: string;
  description: string;
  avatar_key: string;
  spec: ResolvedSpec;
}

function Section({
  title,
  desc,
  children,
}: {
  title: string;
  desc?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={styles.section}>
      <h2 className={styles.sectionTitle}>{title}</h2>
      {desc && <p className={styles.sectionDesc}>{desc}</p>}
      {children}
    </div>
  );
}

export function AgentEditor({ agentId }: { agentId?: string }) {
  const router = useRouter();
  const { message } = AntdApp.useApp();
  const isNew = !agentId;

  const detailQ = useAgent(agentId);
  const modelsQ = useModels();
  const toolsQ = useTools();

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
  });

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
      spec: d.spec as unknown as ResolvedSpec,
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
   * 审批候选项：只列**已勾选**工具的模型侧名字 —— 没勾的工具列出来也拦不到。
   * tags 模式允许自由输入，MCP 工具（mcp:server:tool）走那条路。
   */
  const approvalOptions = useMemo(() => {
    const picked = new Set(draft.spec.tool_names);
    return (toolsQ.data?.data ?? [])
      .filter((t) => picked.has(t.name))
      .flatMap((t) =>
        (t.model_tool_names ?? [t.name]).map((n) => ({
          value: n,
          label: n === t.name ? n : `${n}（来自 ${t.name}）`,
        })),
      );
  }, [toolsQ.data, draft.spec.tool_names]);

  /** 后端 schema 校验会强制加入的项（bash→execute），UI 上标出来且不可取消。 */
  const forcedApprovals = useMemo(() => {
    const out: string[] = [];
    if (draft.spec.tool_names.includes("bash")) out.push("execute");
    return out;
  }, [draft.spec.tool_names]);

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
              // ★ 剔除 task：子智能体不能再委派（深度结构性封顶在 1），
              //   留着只会让人以为它能，实际是个哑参数
              tool_names: (spec.tool_names ?? []).filter((n) => n !== "task"),
              // ★ 与后端 SubAgentSpec 的默认值对齐。两者在生成类型里是必填
              //   （后端有默认值 ⇒ 响应里必然存在），少写就编译不过。
              //   persistent：子智能体持有自己的会话，第二次委派恢复上下文。
              session_mode: "persistent",
              // 从已有 agent 派生出来的子智能体恒为 native —— 编辑器里挑的
              //   都是平台自己的 agent；acp 子智能体目前只能经 API 配置。
              kind: "native",
              source_agent_id: src.id,
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
  const readOnly = detail?.is_builtin ?? false;

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
          {detail && !detail.is_builtin && detail.status !== "enabled" && (
            <Button
              type={detail.status === "draft" ? "primary" : "default"}
              ghost={detail.status === "draft"}
              onClick={() => setStatus.mutate("enabled" as AgentStatus)}
              loading={setStatus.isPending}
            >
              {detail.status === "draft" ? "启用" : "恢复启用"}
            </Button>
          )}
          {detail && !detail.is_builtin && detail.status !== "archived" && (
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

      <div className={styles.body}>
        <div className={styles.form}>
          {readOnly && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message="这是内置智能体，不可修改"
              description="如需定制，请新建一个智能体。"
            />
          )}

          {saveError && (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 16 }}
              message={
                saveError.kind === "invalid_spec" ? "配置不合法，已被拒绝" : "保存失败"
              }
              // engine 的 spec.validate() 会明确指出哪个参数不被该模型接受
              // （§3 D3：不静默降级），message 已经是可读的中文
              description={saveError.message}
            />
          )}

          <Section title="基本信息">
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

          <Section title="系统提示词" desc="决定这个智能体的角色与行为准则。">
            <Input.TextArea
              value={draft.spec.system_prompt}
              disabled={readOnly}
              rows={8}
              onChange={(e) => patchSpec({ system_prompt: e.target.value })}
              placeholder="你是……"
            />
          </Section>

          <Section
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

          <Section title="工具">
            {toolsQ.data?.data.map((t) => {
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

          <Section
            title="子智能体"
            desc="主智能体通过 task 工具把子任务整体委派出去，在独立上下文里执行，只拿回结论。"
          >
            {/* task 与 subagents 是一对：缺任何一半，委派都不会发生，
                而且是静默的 —— 所以两种不匹配都要当场说清楚。 */}
            <Select
              style={{ width: "100%", marginBottom: 12 }}
              placeholder={readOnly ? "内置智能体不可修改" : "选择一个已启用的智能体加入…"}
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
                  委派深度上限 {draft.spec.limits.max_subagent_depth}
                  ；子智能体不能再委派子智能体（结构性封顶），因此加入时会自动剔除它的 task 工具。
                  <br />
                  加入的是来源智能体<b>当时的配置副本</b> —— 之后改动来源智能体不会影响这里，
                  历史会话的可复现性也不受影响。需要更新时，移除后重新加入即可。
                </p>
              </>
            )}
          </Section>

          <Section
            title="人工确认"
            desc="列出的工具在执行前会挂起，等用户点头。拒绝不终止本轮 —— 作为工具结果回给智能体，它可以换个方案继续。"
          >
            <Select
              mode="tags"
              style={{ width: "100%" }}
              disabled={readOnly}
              placeholder="选择需要人工确认的工具（可直接输入 MCP 工具名，如 mcp:server:tool）"
              value={draft.spec.limits.require_approval_for}
              onChange={(v: string[]) =>
                // 强制项去不掉：后端 schema 校验会再加回来，UI 上先兜住，
                // 免得用户以为取消成功了、保存后又冒出来
                patchLimits({ require_approval_for: [...new Set([...v, ...forcedApprovals])] })
              }
              options={approvalOptions}
            />
            {forcedApprovals.length > 0 && (
              <p style={{ margin: "8px 0 0", fontSize: 12, color: "var(--fg-3)" }}>
                {forcedApprovals.map((n) => (
                  <Tag key={n} color="warning" style={{ marginInlineEnd: 4 }}>
                    {n}
                  </Tag>
                ))}
                由所勾工具<b>强制进入</b>审批，不可取消 —— 这条写在 schema 层而不是编辑器默认值，
                绕过编辑器直接调 API 同样会被加上。
              </p>
            )}
            <p style={{ margin: "8px 0 0", fontSize: 12, color: "var(--fg-3)" }}>
              匹配的是<b>模型侧</b>的工具名：勾选 <code>bash</code> 时模型看到的是{" "}
              <code>execute</code>；<code>filesystem</code> 展开成 7 个文件工具，可只拦写入类。
              等待超时按 expired 处理，同样作为工具结果返回。
            </p>
          </Section>

          <Section title="限制" desc="超出即中断本轮运行（§13）。">
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
                <label style={{ fontSize: 12, color: "var(--fg-2)" }}>超时（秒）</label>
                <InputNumber
                  style={{ width: "100%" }}
                  min={10}
                  disabled={readOnly}
                  value={draft.spec.limits.timeout_s}
                  onChange={(v) => v && patchLimits({ timeout_s: v })}
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
            <p style={{ fontSize: 12, color: "var(--fg-3)" }}>
              <Icon name="layers" size={13} style={{ verticalAlign: -2, marginRight: 4 }} />
              保存会生成新版本，历史会话仍使用当时的版本快照，不受影响。
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
