"use client";

/**
 * 智能体编辑（同 Coze / Dify / GPTs 的「编排 + 调试预览」）：左边折叠分组的配置，右边直接试。
 *
 * ★ 修改自动存成草稿（不产生版本），「发布」后新对话才用 —— 右边的调试预览始终用当前的修改。
 * ★ 名称、描述、头像点头像改，立即生效（不是配置，不走版本）；共享、历史版本、发布在右上角。
 * ★ 页面只讲能做什么，不讲实现：工具英文名、执行形态这类概念不出现在这里。
 */
import { useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App as AntdApp,
  Button,
  Checkbox,
  Drawer,
  Dropdown,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Select,
  Skeleton,
  Slider,
  Switch,
  Tag,
} from "antd";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import {
  agentDraftApi,
  useAgent,
  useAgents,
  useAgentVersions,
  useCreateAgent,
  useDefaultAgent,
  useSetAgentStatus,
  useSetAuthSubject,
  useSetDefaultAgent,
  useUpdateAgent,
} from "@/api/agents";
import { useMcpServers, useSkillCatalog } from "@/api/catalog";
import { qk } from "@/api/keys";
import { useModels, useTools } from "@/api/meta";
import type { AgentDetail, AuthSubject, ModelInfo } from "@/api/types";
import { Icon, type IconName } from "@/components/Icon";
import { AVATAR_KEYS, avatarBackground, avatarLetter } from "@/features/agents/avatar";
import { newSlug } from "@/features/agents/templates";
import { relativeTime } from "@/lib/format";
import { ApiError, http } from "@/lib/http";
import { can, useMe } from "@/lib/session";
import { useSendMessage } from "@atlas/chat-ui/src/api/runs";
import { Composer } from "@atlas/chat-ui/src/chat/Composer";
import { MODE_TEXT } from "@atlas/chat-ui/src/chat/ModePicker";
import { ThreadConversation } from "@atlas/chat-ui/src/chat/ThreadConversation";
import type { ApprovalMode } from "@atlas/chat-ui/src/lib/events";
import { newIdempotencyKey } from "@atlas/chat-ui/src/lib/id";

import { SharingPanel } from "./AccessSections";
import { McpToolsSection, SkillsSection } from "./CatalogSections";
import { DEFAULT_KNOWLEDGE, KnowledgeSection } from "./KnowledgeSection";
import { defaultSpec, effortForbidsThinkingOff, reconcileModelSpec, type ResolvedSpec } from "./spec-defaults";
import styles from "./studio.module.css";

const EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;
const SUBAGENT_TOOL = "task";
const SAVE_DELAY_MS = 800;

/** 平台自带的能力：用能做什么来描述，不露工具名 */
const CAPS: Record<string, [IconName, string, string]> = {
  bash: ["terminal", "执行命令", "在隔离的工作环境里运行命令和脚本"],
  filesystem: ["file", "读写文件", "读取你上传的文件，把结果保存成文件"],
  write_todos: ["todo", "制定计划", "复杂任务先列出步骤，边做边更新进度"],
  ask_user_question: ["help", "向你提问", "信息不够时先问清楚再动手"],
  memory: ["bookmark", "记忆", "记住你的偏好和背景，下次对话自动带上"],
};
const CAP_ORDER = ["bash", "filesystem", "write_todos", "ask_user_question", "memory"];

const CLI_MODES: [string, string, string][] = [
  ["auto", "自动", "常规操作直接做，有风险的会拒绝"],
  ["accept_edits", "接受编辑", "改文件不用问，运行命令前先问你"],
  ["manual", "手动", "每次操作前都先问你"],
  ["plan", "仅规划", "只分析和出方案，不改任何东西"],
];

const MODE_ORDER: ApprovalMode[] = ["auto", "manual", "plan"];

/** 后端存的 spec → 编辑器的 spec。旧快照没有后加的字段：补默认值，免得各处判空 */
function normalizeSpec(raw: unknown): ResolvedSpec {
  const spec = raw as Partial<ResolvedSpec>;
  return {
    ...(spec as ResolvedSpec),
    tool_names: spec.tool_names ?? [],
    subagents: spec.subagents ?? [],
    skills: spec.skills ?? [],
    mcp_tool_digests: spec.mcp_tool_digests ?? {},
    mcp_drift_policy: spec.mcp_drift_policy ?? "warn",
    knowledge: { ...DEFAULT_KNOWLEDGE, ...(spec.knowledge ?? {}) },
    approval: spec.approval ?? defaultSpec().approval,
    limits: spec.limits ?? defaultSpec().limits,
    compaction: spec.compaction ?? defaultSpec().compaction,
  };
}

/** 委派要靠 task 工具：有协作智能体就带上，没有就去掉（作者不用知道这个工具） */
function withTask(spec: ResolvedSpec): ResolvedSpec {
  const has = spec.tool_names.includes(SUBAGENT_TOOL);
  if (spec.subagents.length > 0 && !has) return { ...spec, tool_names: [...spec.tool_names, SUBAGENT_TOOL] };
  if (spec.subagents.length === 0 && has) return { ...spec, tool_names: spec.tool_names.filter((n) => n !== SUBAGENT_TOOL) };
  return spec;
}

function modelLabel(m: ModelInfo): string {
  return m.display_name || m.model;
}

// ───────────────────────────────────────────── 小组件

function Group({
  title,
  summary,
  open,
  onToggle,
  children,
}: {
  title: string;
  summary?: ReactNode;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <section className={`${styles.group} ${open ? styles.open : ""}`}>
      <button type="button" className={styles.groupHead} onClick={onToggle} aria-expanded={open}>
        <Icon name="right" size={14} className={styles.chev} />
        <h3>{title}</h3>
        {summary !== undefined && <span className={styles.groupSum}>{summary}</span>}
      </button>
      {open && <div className={styles.groupBody}>{children}</div>}
    </section>
  );
}

function Choice({
  checked,
  title,
  desc,
  disabled,
  onClick,
  children,
}: {
  checked: boolean;
  title: string;
  desc?: string;
  disabled?: boolean;
  onClick: () => void;
  children?: ReactNode;
}) {
  return (
    <div
      role="radio"
      aria-checked={checked}
      tabIndex={disabled ? -1 : 0}
      className={styles.choice}
      onClick={disabled ? undefined : onClick}
      onKeyDown={(e) => !disabled && (e.key === "Enter" || e.key === " ") && onClick()}
      style={disabled ? { cursor: "not-allowed", opacity: 0.6 } : undefined}
    >
      <span className={styles.radio} />
      <div className={styles.choiceText}>
        <b>{title}</b>
        {desc && <div>{desc}</div>}
        {children}
      </div>
    </div>
  );
}

// ───────────────────────────────────────────── 弹窗 / 抽屉

function BasicInfoModal({ detail, onClose }: { detail: AgentDetail; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const update = useUpdateAgent(detail.id);
  const [name, setName] = useState(detail.name);
  const [description, setDescription] = useState(detail.description);
  const [avatar, setAvatar] = useState(detail.avatar_key);
  return (
    <Modal
      open
      title="基本信息"
      okText="保存"
      cancelText="取消"
      confirmLoading={update.isPending}
      okButtonProps={{ disabled: !name.trim() }}
      onCancel={onClose}
      onOk={() =>
        update.mutate(
          { name: name.trim(), description, avatar_key: avatar },
          { onSuccess: onClose, onError: (e) => message.error(e.message) },
        )
      }
    >
      <span className={styles.label}>头像</span>
      <div className={styles.row} style={{ marginBottom: 16 }}>
        {AVATAR_KEYS.map((k) => (
          <button
            key={k}
            type="button"
            aria-label={`头像 ${k}`}
            aria-pressed={avatar === k}
            onClick={() => setAvatar(k)}
            className={styles.avatar}
            style={{
              background: avatarBackground(k),
              outline: avatar === k ? "2px solid var(--primary)" : "none",
              outlineOffset: 2,
            }}
          >
            {avatarLetter(name || "新")}
          </button>
        ))}
      </div>
      <span className={styles.label}>名称</span>
      <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={64} style={{ marginBottom: 16 }} />
      <span className={styles.label}>描述</span>
      <Input.TextArea
        rows={3}
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        placeholder="一句话说明它擅长什么"
      />
      <p className={styles.hint} style={{ marginTop: 6 }}>
        显示在智能体列表里，帮别人判断要不要用它
      </p>
    </Modal>
  );
}

function VersionsDrawer({
  detail,
  hasDraft,
  onClose,
  onRestore,
}: {
  detail: AgentDetail;
  hasDraft: boolean;
  onClose: () => void;
  onRestore: (spec: unknown, version: number) => void;
}) {
  const versionsQ = useAgentVersions(detail.id);
  const versions = [...(versionsQ.data?.data ?? [])].sort((a, b) => b.version - a.version);
  return (
    <Drawer open onClose={onClose} title="历史版本" width={460}>
      {versionsQ.isPending && <Skeleton active />}
      {hasDraft && (
        <div className={styles.version}>
          <b style={{ width: 52 }}>草稿</b>
          <div style={{ flex: 1 }}>
            <div>未发布的修改</div>
            <div className={styles.saveState}>{detail.draft_updated_at ? relativeTime(detail.draft_updated_at) : ""}</div>
          </div>
          <Tag color="warning">未发布</Tag>
        </div>
      )}
      {versions.map((v) => (
        <div key={v.id} className={styles.version}>
          <b style={{ width: 52 }}>v{v.version}</b>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div>{v.note || (v.version === 1 ? "首次保存" : "（没有说明）")}</div>
            <div className={styles.saveState}>{relativeTime(v.created_at)}</div>
          </div>
          {v.version === detail.version ? (
            <Tag color="success">使用中</Tag>
          ) : (
            <Button size="small" onClick={() => onRestore(v.spec, v.version)}>
              恢复
            </Button>
          )}
        </div>
      ))}
    </Drawer>
  );
}

function PublishModal({
  onClose,
  onPublish,
  busy,
  firstTime,
}: {
  onClose: () => void;
  onPublish: (note: string) => void;
  busy: boolean;
  firstTime: boolean;
}) {
  const [note, setNote] = useState("");
  return (
    <Modal
      open
      title={firstTime ? "发布智能体" : "发布新版本"}
      okText="发布"
      cancelText="取消"
      confirmLoading={busy}
      onCancel={onClose}
      onOk={() => onPublish(note.trim())}
    >
      <span className={styles.label}>这次改了什么（可选）</span>
      <Input.TextArea
        rows={3}
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="例如：开启了记忆，增加了网页搜索"
        maxLength={500}
      />
      <p className={styles.hint} style={{ marginTop: 8 }}>
        {firstTime
          ? "发布后就能在对话里使用它；共享给别人后，别人也能用。"
          : "发布后新对话使用新版本，进行中的对话不受影响。随时可以在历史版本里恢复。"}
      </p>
    </Modal>
  );
}

// ───────────────────────────────────────────── 调试预览

function Preview({ agentId, flush, name }: { agentId: string; flush: () => Promise<void>; name: string }) {
  const { message } = AntdApp.useApp();
  const sendMessage = useSendMessage();
  const [threadId, setThreadId] = useState<string | null>(null);
  const [fill, setFill] = useState<{ text: string; seq: number }>();

  const start = async (text: string) => {
    try {
      await flush();
      const thread = await agentDraftApi.previewThread(agentId);
      await sendMessage.mutateAsync({
        threadId: thread.id,
        content: [{ type: "text", text }],
        idempotencyKey: newIdempotencyKey(),
      });
      setThreadId(thread.id);
    } catch (e) {
      message.error((e as Error).message);
      throw e;
    }
  };

  return (
    <aside className={styles.preview} aria-label="调试预览">
      <div className={styles.previewHead}>
        <b>调试预览</b>
        <Tag style={{ marginInlineEnd: 0 }}>使用当前修改</Tag>
        <span style={{ flex: 1 }} />
        <Button
          size="small"
          type="text"
          icon={<Icon name="refresh" size={14} />}
          disabled={!threadId}
          onClick={() => setThreadId(null)}
          title="清空对话"
        />
      </div>
      <div className={styles.previewBody}>
        {threadId ? (
          <ThreadConversation
            key={threadId}
            threadId={threadId}
            hideHeader
            beforeSend={flush}
            placeholder="输入消息试一试"
          />
        ) : (
          <>
            <div className={styles.previewEmpty}>
              <Icon name="chat" size={28} style={{ color: "var(--primary)" }} />
              <b style={{ color: "var(--fg)", marginTop: 10 }}>和「{name}」试一试</b>
              <div style={{ fontSize: 13, marginTop: 4 }}>修改立即生效，不用先发布</div>
              <div className={styles.chips}>
                {["你能做什么？", "帮我写一份周报", "总结一下这段话"].map((q) => (
                  <button
                    key={q}
                    type="button"
                    className={styles.chip}
                    onClick={() => setFill({ text: q, seq: (fill?.seq ?? 0) + 1 })}
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
            <div className={styles.previewFoot}>
              <Composer bare running={false} placeholder="输入消息试一试" fill={fill} onSend={(t) => start(t)} />
            </div>
          </>
        )}
      </div>
    </aside>
  );
}

// ───────────────────────────────────────────── 编辑页

type SaveState = "idle" | "saving" | "saved" | "error";

export function AgentStudio({ agentId }: { agentId: string }) {
  const router = useRouter();
  const qc = useQueryClient();
  const { message, modal } = AntdApp.useApp();
  const { data: me } = useMe();
  const detailQ = useAgent(agentId);
  const detail = detailQ.data;
  const modelsQ = useModels({ agentId });
  const toolsQ = useTools({ agentId, authSubject: detail?.auth_subject ?? "user" });
  const skillCatalogQ = useSkillCatalog();
  const mcpServersQ = useMcpServers();
  const candidatesQ = useAgents({ status: "enabled" });
  const defaultQ = useDefaultAgent();
  const setDefault = useSetDefaultAgent();
  const setStatus = useSetAgentStatus(agentId);
  const setSubject = useSetAuthSubject(agentId);
  const createAgent = useCreateAgent();

  const [spec, setSpecState] = useState<ResolvedSpec | null>(null);
  // 最新的配置：同一个事件里连着改两处时，第二处要基于第一处改完的结果
  const specRef = useRef<ResolvedSpec | null>(null);
  const [hasDraft, setHasDraft] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [saveError, setSaveError] = useState<string>();
  const [publishError, setPublishError] = useState<ApiError | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [open, setOpen] = useState<Record<string, boolean>>({
    prompt: true,
    model: true,
    caps: true,
    knowledge: true,
    skills: false,
    tools: false,
    collab: false,
    advanced: false,
  });
  const [knowMode, setKnowMode] = useState<"all" | "pick" | "none">("none");
  const [showParams, setShowParams] = useState(false);
  const [dialog, setDialog] = useState<"basic" | "share" | "versions" | "publish" | null>(null);

  // 载入：草稿优先（上次没发布的修改）。只在换了智能体时载入 —— 自动保存后的刷新不能把正在打的字冲掉
  const loadedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!detail || loadedFor.current === detail.id) return;
    loadedFor.current = detail.id;
    const s = normalizeSpec(detail.draft_spec ?? detail.spec);
    specRef.current = s;
    setSpecState(s);
    setHasDraft(detail.has_draft ?? false);
    const k = s.knowledge ?? DEFAULT_KNOWLEDGE;
    setKnowMode(k.scope === "all_readable" ? "all" : (k.dataset_ids ?? []).length > 0 ? "pick" : "none");
  }, [detail]);

  // ── 自动保存草稿 ──
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pending = useRef<ResolvedSpec | null>(null);
  const saveNow = useCallback(async () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    const s = pending.current;
    if (!s) return;
    pending.current = null;
    setSaveState("saving");
    try {
      await agentDraftApi.save(agentId, s);
      setSaveState("saved");
      setSaveError(undefined);
      setHasDraft(true);
    } catch (e) {
      setSaveState("error");
      setSaveError((e as Error).message);
      throw e;
    }
  }, [agentId]);
  const flush = useCallback(async () => {
    if (pending.current) await saveNow();
  }, [saveNow]);
  useEffect(() => () => void flush().catch(() => undefined), [flush]);

  const setSpec = (next: ResolvedSpec | ((s: ResolvedSpec) => ResolvedSpec)) => {
    const prev = specRef.current;
    if (!prev) return;
    const value = withTask(typeof next === "function" ? next(prev) : next);
    specRef.current = value;
    setSpecState(value);
    // 停手一小会儿再存：打字时不每个字都发请求
    pending.current = value;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void saveNow().catch(() => undefined), SAVE_DELAY_MS);
    setSaveState("saving");
  };
  const patch = (p: Partial<ResolvedSpec>) => setSpec((s) => ({ ...s, ...p }));
  const patchModel = (p: Partial<ResolvedSpec["model"]>) => setSpec((s) => ({ ...s, model: { ...s.model, ...p } }));
  const patchLimits = (p: Partial<ResolvedSpec["limits"]>) => setSpec((s) => ({ ...s, limits: { ...s.limits, ...p } }));

  const models = modelsQ.data?.data ?? [];
  const modelInfo = useMemo(() => models.find((m) => m.model === spec?.model.model), [models, spec?.model.model]);

  if (detailQ.isPending || !spec) {
    return (
      <div className={styles.studio} style={{ padding: 32 }}>
        <Skeleton active paragraph={{ rows: 10 }} />
      </div>
    );
  }
  if (!detail) {
    return (
      <div className={styles.studio} style={{ padding: 32 }}>
        <Alert type="error" showIcon message="智能体不存在，或你没有权限查看" />
      </div>
    );
  }

  const isOwner = detail.my_level === "OWNER";
  const canEdit = isOwner || (detail.my_level === "WRITE" && detail.auth_subject === "user");
  const readOnly = !canEdit;
  const cli = spec.kind === "cli";
  const isDefault = defaultQ.data?.id === detail.id;

  const toggle = (k: string) => setOpen((o) => ({ ...o, [k]: !o[k] }));

  const publish = async (note: string) => {
    setPublishing(true);
    setPublishError(null);
    try {
      await flush();
      const out = await agentDraftApi.publish(agentId, note);
      qc.setQueryData(qk.agents.detail(agentId), out);
      void qc.invalidateQueries({ queryKey: qk.agents.all });
      setHasDraft(false);
      setDialog(null);
      message.success(`已发布 v${out.version}`);
    } catch (e) {
      setPublishError(e as ApiError);
      setDialog(null);
    } finally {
      setPublishing(false);
    }
  };

  const discard = () =>
    modal.confirm({
      title: "放弃未发布的修改？",
      content: "回到当前发布的版本，草稿里的修改会丢失。",
      okText: "放弃修改",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        if (timer.current) clearTimeout(timer.current);
        pending.current = null;
        await agentDraftApi.discard(agentId);
        const s = normalizeSpec(detail.spec);
        specRef.current = s;
        setSpecState(s);
        setHasDraft(false);
        setSaveState("idle");
      },
    });

  const duplicate = () =>
    createAgent.mutate(
      {
        slug: newSlug("agent"),
        name: `${detail.name} 副本`,
        description: detail.description,
        avatar_key: detail.avatar_key,
        spec,
        auth_subject: "user",
      },
      {
        onSuccess: (a) => {
          message.success("已复制为新的智能体（草稿）");
          router.push(`/agents/${a.id}`);
        },
        onError: (e) => message.error(e.message),
      },
    );

  const archive = () =>
    modal.confirm({
      title: `归档「${detail.name}」？`,
      content: "归档后不能再发起新对话，已有的对话仍然保留。可以随时在「已归档」里恢复。",
      okText: "归档",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () =>
        setStatus.mutateAsync("archived").then(() => {
          message.success("已归档");
          router.push("/agents");
        }),
    });

  const switchIdentity = (next: AuthSubject) => {
    if (next === detail.auth_subject) return;
    modal.confirm({
      title: next === "agent" ? "改为以智能体自己的身份访问资料？" : "改为以使用者的身份访问资料？",
      content:
        next === "agent"
          ? "之后所有使用者经它拿到的结果都一样，与本人权限无关；它没有被授权的知识库、外部工具将不可用。"
          : "之后每个人只能经它访问自己本来就有权限的资料。",
      okText: "确定",
      cancelText: "取消",
      onOk: () =>
        setSubject.mutateAsync(next).then(
          () => message.success("已修改"),
          (e: Error) => {
            message.error(e.message);
          },
        ),
    });
  };

  // 能力：平台自带、能用的内置工具（不含委派 task：由协作智能体自动管）
  const builtin = (toolsQ.data?.data ?? []).filter((t) => t.kind !== "mcp" && t.available && t.name !== SUBAGENT_TOOL);
  const rank = (name: string) => (CAP_ORDER.includes(name) ? CAP_ORDER.indexOf(name) : CAP_ORDER.length);
  const capsList = [...builtin].sort((a, b) => rank(a.name) - rank(b.name));
  const capOn = capsList.filter((t) => spec.tool_names.includes(t.name)).length;

  const k = spec.knowledge ?? DEFAULT_KNOWLEDGE;
  const knowSummary = knowMode === "all" ? "使用者能查阅的全部" : knowMode === "pick" ? `${(k.dataset_ids ?? []).length} 个` : "不使用";
  const mcpPicked = spec.tool_names.filter((n) => n.startsWith("mcp:"));
  const status =
    detail.status === "draft" ? (
      <Tag color="warning">草稿</Tag>
    ) : detail.status === "archived" ? (
      <Tag>已归档</Tag>
    ) : (
      <Tag color="success">已发布 v{detail.version}</Tag>
    );
  const saveText =
    saveState === "saving"
      ? "正在保存…"
      : saveState === "error"
        ? `没有保存成功：${saveError ?? ""}`
        : hasDraft
          ? "有未发布的修改 · 已自动保存"
          : detail.status === "draft"
            ? "还没有发布"
            : "所有修改已发布";
  const canPublish = !readOnly && (hasDraft || detail.status === "draft" || saveState === "saving");

  const moreItems = [
    { key: "dup", icon: <Icon name="copy" size={14} />, label: "复制为新智能体" },
    ...(can(me, "config:manage") && detail.status === "enabled" && !isDefault
      ? [{ key: "default", icon: <Icon name="star" size={14} />, label: "设为默认智能体" }]
      : []),
    ...(hasDraft && !readOnly ? [{ key: "discard", icon: <Icon name="retry" size={14} />, label: "放弃未发布的修改" }] : []),
    ...(isOwner && detail.status !== "archived"
      ? [{ type: "divider" as const }, { key: "archive", icon: <Icon name="archive" size={14} />, label: "归档", danger: true }]
      : []),
  ];

  return (
    <div className={styles.studio}>
      <header className={styles.head}>
        <Button type="text" icon={<Icon name="left" size={16} />} onClick={() => router.push("/agents")} aria-label="返回" />
        <button
          type="button"
          className={styles.avatar}
          style={{ background: avatarBackground(detail.avatar_key) }}
          onClick={() => !readOnly && setDialog("basic")}
          title={readOnly ? detail.name : "修改名称、描述、头像"}
        >
          {avatarLetter(detail.name)}
        </button>
        <div style={{ minWidth: 0 }}>
          <div className={styles.row} style={{ gap: 6 }}>
            <span className={styles.name}>{detail.name}</span>
            {!readOnly && (
              <Button size="small" type="text" icon={<Icon name="pencil" size={13} />} onClick={() => setDialog("basic")} aria-label="修改基本信息" />
            )}
            {status}
            {isDefault && <Tag color="processing">默认</Tag>}
          </div>
          <div className={styles.saveState} style={saveState === "error" ? { color: "var(--danger)" } : undefined}>
            {readOnly ? "你可以使用它，但不能修改" : saveText}
          </div>
        </div>
        <span style={{ flex: 1 }} />
        {isOwner && (
          <Button icon={<Icon name="share" size={14} />} onClick={() => setDialog("share")}>
            共享
          </Button>
        )}
        <Button type="text" icon={<Icon name="clock" size={16} />} onClick={() => setDialog("versions")} title="历史版本" />
        <Dropdown
          trigger={["click"]}
          placement="bottomRight"
          menu={{
            items: moreItems,
            onClick: ({ key }) => {
              if (key === "dup") duplicate();
              if (key === "default")
                setDefault.mutate(detail.id, {
                  onSuccess: () => message.success("已设为默认智能体"),
                  onError: (e) => message.error(e.message),
                });
              if (key === "discard") discard();
              if (key === "archive") archive();
            },
          }}
        >
          <Button type="text" icon={<Icon name="more" size={16} />} aria-label="更多" />
        </Dropdown>
        {readOnly ? (
          <Button type="primary" onClick={() => router.push(`/?agent=${detail.id}`)}>
            开始对话
          </Button>
        ) : (
          <Button type="primary" disabled={!canPublish} onClick={() => setDialog("publish")}>
            发布
          </Button>
        )}
      </header>

      <div className={styles.body}>
        <div className={styles.left}>
          <div className={styles.leftIn}>
            {publishError && (
              <Alert
                type="error"
                showIcon
                closable
                onClose={() => setPublishError(null)}
                message={
                  publishError.kind === "invalid_spec"
                    ? "配置有问题，没有发布"
                    : publishError.kind === "invalid_reference"
                      ? "用到的技能或工具有问题，没有发布"
                      : "没有发布成功"
                }
                description={publishError.message}
              />
            )}

            <Group
              title="人设与指令"
              summary={spec.system_prompt ? `${spec.system_prompt.length} 字` : "未填写"}
              open={open.prompt!}
              onToggle={() => toggle("prompt")}
            >
              <Input.TextArea
                rows={9}
                value={spec.system_prompt}
                disabled={readOnly}
                onChange={(e) => patch({ system_prompt: e.target.value })}
                placeholder="告诉它扮演什么角色、怎么工作、有哪些要求…"
              />
            </Group>

            <Group
              title="模型"
              summary={modelInfo ? modelLabel(modelInfo) : spec.model.model}
              open={open.model!}
              onToggle={() => toggle("model")}
            >
              <div className={styles.row}>
                <Select
                  style={{ width: 300 }}
                  value={spec.model.model}
                  disabled={readOnly}
                  loading={modelsQ.isPending}
                  onChange={(model: string) => {
                    const info = models.find((m) => m.model === model);
                    setSpec((s) => ({ ...s, model: reconcileModelSpec({ ...s.model, model }, info) }));
                  }}
                  options={models.map((m) => ({
                    value: m.model,
                    label: modelLabel(m),
                    disabled: !m.is_available || !m.selectable,
                  }))}
                />
                <Button type="link" onClick={() => setShowParams((v) => !v)}>
                  {showParams ? "收起生成参数" : "生成参数"}
                </Button>
              </div>
              {modelsQ.data?.key && modelsQ.data.gateway_reachable && !modelInfo && (
                <Alert
                  type="error"
                  showIcon
                  style={{ marginTop: 10 }}
                  message="当前模型用不了（没有分配给这个智能体，或已经下线），请换一个"
                />
              )}
              {modelsQ.data?.key?.status === "pending_models" && (
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginTop: 10 }}
                  message="还没有给它分配模型，分配之前不能对话"
                  description="请模型管理员在「模型 › 客户端 Key」里给它分配模型。"
                />
              )}
              {showParams && (
                <div style={{ marginTop: 14 }}>
                  <div className={styles.grid2}>
                    <div>
                      <span className={styles.label}>回答长度上限（token）</span>
                      <InputNumber
                        style={{ width: "100%" }}
                        min={256}
                        max={modelInfo?.max_output_tokens ?? 128_000}
                        step={1024}
                        disabled={readOnly}
                        value={spec.model.max_output_tokens}
                        onChange={(v) => v && patchModel({ max_output_tokens: v })}
                      />
                    </div>
                    <div>
                      <span className={styles.label}>深度思考</span>
                      <Select
                        style={{ width: "100%" }}
                        disabled={readOnly}
                        value={spec.model.thinking}
                        onChange={(v) => patchModel({ thinking: v })}
                        options={[
                          { value: "auto", label: "自动" },
                          { value: "adaptive", label: "按需思考", disabled: !modelInfo?.supports_adaptive_thinking },
                          { value: "off", label: "关闭" },
                        ]}
                      />
                    </div>
                  </div>
                  {modelInfo?.supports_effort && (
                    <div style={{ marginTop: 12 }}>
                      <span className={styles.label}>推理强度</span>
                      <Segmented
                        disabled={readOnly}
                        value={spec.model.effort ?? "high"}
                        onChange={(v) => patchModel({ effort: v as (typeof EFFORTS)[number] })}
                        options={EFFORTS.map((e) => ({ value: e, label: e }))}
                      />
                      {effortForbidsThinkingOff(spec.model.effort) && spec.model.thinking === "off" && (
                        <p className={styles.hint} style={{ color: "var(--warning)" }}>
                          这个推理强度下不能关闭深度思考
                        </p>
                      )}
                    </div>
                  )}
                  {modelInfo?.supports_temperature && (
                    <div style={{ marginTop: 12 }}>
                      <span className={styles.label}>随机性（越高越有创意，越低越稳定）</span>
                      <Slider
                        min={0}
                        max={1}
                        step={0.05}
                        disabled={readOnly}
                        value={spec.model.temperature ?? 0}
                        onChange={(v) => patchModel({ temperature: v })}
                      />
                    </div>
                  )}
                </div>
              )}
            </Group>

            {cli ? (
              <Group
                title="操作权限"
                summary={CLI_MODES.find(([v]) => v === (spec.cli?.permission_mode ?? "auto"))?.[1]}
                open={open.caps!}
                onToggle={() => toggle("caps")}
              >
                <div className={styles.choices}>
                  {CLI_MODES.map(([v, t, d]) => (
                    <Choice
                      key={v}
                      checked={(spec.cli?.permission_mode ?? "auto") === v}
                      title={t}
                      desc={d}
                      disabled={readOnly}
                      onClick={() =>
                        spec.cli && patch({ cli: { ...spec.cli, permission_mode: v as "auto" | "accept_edits" | "manual" | "plan" } })
                      }
                    />
                  ))}
                </div>
              </Group>
            ) : (
              <Group title="能力" summary={`已开启 ${capOn} 项`} open={open.caps!} onToggle={() => toggle("caps")}>
                {toolsQ.isPending && <Skeleton active paragraph={{ rows: 3 }} />}
                {capsList.map((t) => {
                  const [icon, label, desc] = CAPS[t.name] ?? ["wrench", t.display_name || t.name, t.description];
                  const on = spec.tool_names.includes(t.name);
                  return (
                    <div key={t.name} className={styles.cap}>
                      <span className={styles.capIcon}>
                        <Icon name={icon} size={16} />
                      </span>
                      <div className={styles.capText}>
                        <b>{label}</b>
                        <div>{desc}</div>
                      </div>
                      <Switch
                        checked={on}
                        disabled={readOnly}
                        onChange={(v) =>
                          patch({
                            tool_names: v ? [...spec.tool_names, t.name] : spec.tool_names.filter((n) => n !== t.name),
                          })
                        }
                      />
                    </div>
                  );
                })}
              </Group>
            )}

            <Group title="知识库" summary={knowSummary} open={open.knowledge!} onToggle={() => toggle("knowledge")}>
              <div className={styles.choices}>
                <Choice
                  checked={knowMode === "all"}
                  title="使用者能查阅的全部知识库"
                  desc="每个人只会查到自己有权限的内容"
                  disabled={readOnly || detail.auth_subject === "agent"}
                  onClick={() => {
                    setKnowMode("all");
                    patch({ knowledge: { ...k, scope: "all_readable", dataset_ids: [], mode: cli ? "tool" : k.mode } });
                  }}
                />
                <Choice
                  checked={knowMode === "pick"}
                  title="指定知识库"
                  desc="只在选定的知识库里查找"
                  disabled={readOnly}
                  onClick={() => {
                    setKnowMode("pick");
                    patch({ knowledge: { ...k, scope: "selected" } });
                  }}
                >
                  {knowMode === "pick" && (
                    <div
                      style={{ marginTop: 10 }}
                      onClick={(e) => e.stopPropagation()}
                      onKeyDown={(e) => e.stopPropagation()}
                    >
                      <KnowledgeSection
                        bare
                        value={{ ...k, scope: "selected" }}
                        kind={spec.kind ?? "native"}
                        readOnly={readOnly}
                        agentId={agentId}
                        authSubject={detail.auth_subject}
                        onChange={(knowledge) => patch({ knowledge })}
                      />
                    </div>
                  )}
                </Choice>
                <Choice
                  checked={knowMode === "none"}
                  title="不使用知识库"
                  disabled={readOnly}
                  onClick={() => {
                    setKnowMode("none");
                    patch({ knowledge: { ...k, scope: "selected", dataset_ids: [] } });
                  }}
                />
              </div>
            </Group>

            <Group
              title="技能"
              summary={spec.skills.length ? spec.skills.map((s) => s.slug).join("、") : "未添加"}
              open={open.skills!}
              onToggle={() => toggle("skills")}
            >
              <p className={styles.hint}>让它按固定的方法完成某类工作</p>
              <SkillsSection
                bare
                skills={spec.skills}
                catalog={skillCatalogQ.data}
                readOnly={readOnly}
                onChange={(skills) => patch({ skills })}
              />
            </Group>

            <Group
              title="工具"
              summary={mcpPicked.length ? `已选 ${mcpPicked.length} 个` : "未添加"}
              open={open.tools!}
              onToggle={() => toggle("tools")}
            >
              <p className={styles.hint}>连接外部系统，例如搜索网页、查询客户、创建工单</p>
              <McpToolsSection
                bare
                tools={(toolsQ.data?.data ?? []).filter((t) => t.kind === "mcp")}
                subject={toolsQ.data?.mcp_subject ?? null}
                servers={mcpServersQ.data?.data ?? []}
                picked={spec.tool_names}
                digests={spec.mcp_tool_digests ?? {}}
                policy={spec.mcp_drift_policy ?? "warn"}
                readOnly={readOnly}
                onToggle={(name, on) =>
                  patch({ tool_names: on ? [...spec.tool_names, name] : spec.tool_names.filter((n) => n !== name) })
                }
                onPolicy={(mcp_drift_policy) => patch({ mcp_drift_policy })}
              />
            </Group>

            {!cli && (
              <Group
                title="协作智能体"
                summary={spec.subagents.length ? spec.subagents.map((s) => s.name).join("、") : "未添加"}
                open={open.collab!}
                onToggle={() => toggle("collab")}
              >
                <p className={styles.hint}>遇到合适的子任务，交给这些智能体去完成</p>
                <SubagentPicker
                  spec={spec}
                  agentId={agentId}
                  readOnly={readOnly}
                  candidates={candidatesQ.data?.data ?? []}
                  onChange={(subagents) => patch({ subagents })}
                />
              </Group>
            )}

            <Group title="高级设置" open={open.advanced!} onToggle={() => toggle("advanced")}>
              {!cli && (
                <div style={{ marginBottom: 18 }}>
                  <span className={styles.label}>执行前确认</span>
                  <p className={styles.hint}>新对话的默认方式；对话时可以在允许的范围里切换</p>
                  <Checkbox.Group
                    disabled={readOnly}
                    value={spec.approval.allowed_modes}
                    onChange={(picked) => {
                      const allowed = MODE_ORDER.filter((m) => (picked as ApprovalMode[]).includes(m));
                      if (!allowed.some((m) => m !== "plan")) return;
                      const current = spec.approval.default_mode;
                      patch({
                        approval: {
                          allowed_modes: allowed,
                          default_mode: allowed.includes(current) ? current : allowed[0]!,
                        },
                      });
                    }}
                    style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 10 }}
                  >
                    {MODE_ORDER.map((m) => (
                      <Checkbox key={m} value={m}>
                        <b>{MODE_TEXT[m].label}</b>
                        <span style={{ marginLeft: 8, fontSize: 12.5, color: "var(--fg-3)" }}>{MODE_TEXT[m].hint}</span>
                      </Checkbox>
                    ))}
                  </Checkbox.Group>
                  <span className={styles.label}>默认</span>
                  <Segmented
                    disabled={readOnly}
                    value={spec.approval.default_mode}
                    options={MODE_ORDER.filter((m) => (spec.approval.allowed_modes ?? []).includes(m)).map((m) => ({
                      value: m,
                      label: MODE_TEXT[m].label,
                    }))}
                    onChange={(v) => patch({ approval: { ...spec.approval, default_mode: v as ApprovalMode } })}
                  />
                </div>
              )}

              <div style={{ marginBottom: 18 }}>
                <span className={styles.label}>访问权限</span>
                <div className={styles.choices}>
                  <Choice
                    checked={detail.auth_subject === "user"}
                    title="以使用者的身份"
                    desc="每个人只能经它访问自己本来就有权限的资料，适合个人助手"
                    disabled={!isOwner || setSubject.isPending}
                    onClick={() => switchIdentity("user")}
                  />
                  <Choice
                    checked={detail.auth_subject === "agent"}
                    title="以智能体自己的身份"
                    desc="所有使用者拿到相同的结果，适合岗位型、流程型智能体"
                    disabled={!isOwner || setSubject.isPending}
                    onClick={() => switchIdentity("agent")}
                  />
                </div>
                {!isOwner && <p className={styles.hint} style={{ marginTop: 6 }}>只有所有者能修改</p>}
              </div>

              <div className={styles.grid2}>
                {!cli && (
                  <div>
                    <span className={styles.label}>工作环境</span>
                    <Input
                      disabled={readOnly}
                      value={spec.sandbox?.image ?? ""}
                      placeholder="默认环境"
                      onChange={(e) => patch({ sandbox: { profile: spec.sandbox?.profile ?? "strict", image: e.target.value } })}
                    />
                  </div>
                )}
                <div>
                  <span className={styles.label}>单次对话最多步数</span>
                  <InputNumber
                    style={{ width: "100%" }}
                    min={1}
                    disabled={readOnly}
                    value={spec.limits.max_steps}
                    onChange={(v) => v && patchLimits({ max_steps: v })}
                  />
                </div>
                <div>
                  <span className={styles.label}>单次对话最多用量（token）</span>
                  <InputNumber
                    style={{ width: "100%" }}
                    min={1000}
                    step={10_000}
                    disabled={readOnly}
                    value={spec.limits.max_total_tokens}
                    onChange={(v) => v && patchLimits({ max_total_tokens: v })}
                  />
                </div>
              </div>
            </Group>
          </div>
        </div>

        {readOnly ? (
          <aside className={styles.preview}>
            <div className={styles.previewEmpty}>
              <Icon name="lock" size={24} />
              <div style={{ marginTop: 8 }}>只有能编辑的人可以在这里试</div>
            </div>
          </aside>
        ) : (
          <Preview agentId={agentId} flush={flush} name={detail.name} />
        )}
      </div>

      {dialog === "basic" && <BasicInfoModal detail={detail} onClose={() => setDialog(null)} />}
      {dialog === "share" && (
        <Modal open title="共享" footer={null} width={640} onCancel={() => setDialog(null)}>
          <SharingPanel detail={detail} />
        </Modal>
      )}
      {dialog === "versions" && (
        <VersionsDrawer
          detail={detail}
          hasDraft={hasDraft}
          onClose={() => setDialog(null)}
          onRestore={(raw, v) => {
            setSpec(normalizeSpec(raw));
            setDialog(null);
            message.success(`已恢复 v${v} 的配置为草稿，发布后生效`);
          }}
        />
      )}
      {dialog === "publish" && (
        <PublishModal
          busy={publishing}
          firstTime={detail.status === "draft"}
          onClose={() => setDialog(null)}
          onPublish={(note) => void publish(note)}
        />
      )}
    </div>
  );
}

// ───────────────────────────────────────────── 协作智能体

function SubagentPicker({
  spec,
  agentId,
  readOnly,
  candidates,
  onChange,
}: {
  spec: ResolvedSpec;
  agentId: string;
  readOnly: boolean;
  candidates: { id: string; name: string; description: string }[];
  onChange: (subagents: ResolvedSpec["subagents"]) => void;
}) {
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);

  /**
   * 选中一个已有智能体 → 把它当时的配置**拷贝**进来。
   * ★ 拷贝而不是存 id：配置是版本快照，存活引用的话来源一改，历史对话的含义都跟着变（§5.4）。
   */
  const add = async (id: string) => {
    setAdding(true);
    try {
      const src = await qc.fetchQuery({
        queryKey: qk.agents.detail(id),
        queryFn: () => http.get<AgentDetail>(`/v1/agents/${id}`),
      });
      const s = src.spec as unknown as ResolvedSpec;
      onChange([
        ...spec.subagents,
        {
          name: src.slug,
          description: src.description,
          system_prompt: s.system_prompt ?? "",
          model: s.model,
          tool_names: s.tool_names ?? [],
          session_mode: "persistent",
          kind: s.kind ?? "native",
          ...(s.kind === "cli" && s.cli ? { cli: s.cli } : {}),
          source_agent_id: src.id,
          skills: s.skills ?? [],
          mcp_tool_digests: {},
          mcp_drift_policy: s.mcp_drift_policy ?? "warn",
        },
      ]);
    } finally {
      setAdding(false);
    }
  };

  const names = new Map(candidates.map((c) => [c.id, c.name]));
  return (
    <>
      {spec.subagents.map((sub, i) => (
        <div key={sub.name} className={styles.sub}>
          <span className={styles.capIcon} style={{ background: "var(--info-soft)", color: "var(--info)" }}>
            <Icon name="bot" size={16} />
          </span>
          <div className={styles.capText}>
            <b>{(sub.source_agent_id && names.get(sub.source_agent_id)) || sub.name}</b>
            <div>{sub.description || "没有描述：主智能体不知道什么时候该交给它"}</div>
          </div>
          {!readOnly && (
            <Button size="small" type="text" danger onClick={() => onChange(spec.subagents.filter((_, j) => j !== i))}>
              移除
            </Button>
          )}
        </div>
      ))}
      {!readOnly && (
        <Select
          style={{ width: "100%", marginTop: spec.subagents.length ? 10 : 0 }}
          placeholder="添加一个已发布的智能体"
          value={null}
          loading={adding}
          showSearch
          optionFilterProp="label"
          onChange={(id: string) => void add(id)}
          options={candidates
            .filter((a) => a.id !== agentId && !spec.subagents.some((s) => s.source_agent_id === a.id))
            .map((a) => ({ value: a.id, label: a.name }))}
          notFoundContent="没有可添加的智能体"
        />
      )}
      {spec.subagents.length > 0 && (
        <p className={styles.hint} style={{ marginTop: 10 }}>
          加入的是它现在的配置；之后它再修改，这里不会跟着变，需要时移除后重新添加。
        </p>
      )}
    </>
  );
}
