"use client";

/**
 * 技能管理（prototype/atlas-v2.html「技能」页；doc/skill-mcp-backend-design.html §5–§7）。
 *
 * 数据来自两边，各管各的：
 *   技能应用（/api/v1/skills）   内容、版本、扫描结果、审查、状态 —— 可写（按技能应用的操作码）
 *   运行时（/v1/skills/*）       被谁引用、近 7 天加载 —— 只读
 */
import {
  Alert,
  App as AntdApp,
  Button,
  Drawer,
  Empty,
  Input,
  Modal,
  Segmented,
  Select,
  Skeleton,
  Table,
  Tag,
  Upload,
} from "antd";
import Link from "next/link";
import { useMemo, useState } from "react";

import { useSkillCatalog, useSkillReferences, useSkillUsage } from "@/api/catalog";
import {
  type SkillAction,
  useReviewSkill,
  useSkMe,
  useSkillAction,
  useSkillFile,
  useSkills,
  useUploadSkill,
} from "@/api/skill-app";
import type { ManagedSkill, ManagedSkillVersion } from "@/api/skill-types";
import { Icon } from "@/components/Icon";
import { relativeTime } from "@/lib/format";
import { ApiError } from "@/lib/http";
import pageStyles from "@/features/agents/agents.module.css";
import styles from "./catalog.module.css";

export const SKILL_STATUS: Record<string, { label: string; color: string }> = {
  pending_review: { label: "待审查", color: "warning" },
  rejected: { label: "已拒绝", color: "default" },
  published: { label: "已发布", color: "success" },
  disabled: { label: "已停用", color: "default" },
  revoked: { label: "已下架", color: "error" },
};

const SOURCE_LABEL: Record<string, string> = {
  builtin: "内置",
  tenant: "团队",
  imported: "第三方",
};

/** 左侧菜单的三个入口：技能广场（已发布）/ 我上传的 / 待审查 */
export type SkillsView = "market" | "mine" | "review";

const VIEW_TITLE: Record<SkillsView, string> = { market: "技能广场", mine: "我上传的", review: "待审查" };
const VIEW_EMPTY: Record<SkillsView, string> = {
  market: "还没有发布的技能",
  mine: "你还没有上传过技能",
  review: "没有等待审查的技能",
};

interface Row {
  skill: ManagedSkill;
  latest?: ManagedSkillVersion;
  pending?: ManagedSkillVersion;
  references: number;
  loads7d: number;
}

function latestReleased(skill: ManagedSkill): ManagedSkillVersion | undefined {
  return [...skill.versions]
    .filter((v) => v.version != null)
    .sort((a, b) => (b.version ?? 0) - (a.version ?? 0))[0];
}

export function SkillsPage({ view = "market" }: { view?: SkillsView }) {
  const skillsQ = useSkills();
  const { data: me } = useSkMe();
  const runtimeQ = useSkillCatalog();
  const [keyword, setKeyword] = useState("");
  const [source, setSource] = useState("");
  const [sort, setSort] = useState<"uses" | "updated">("uses");
  /** 打开详情抽屉的技能 */
  const [selected, setSelected] = useState<string>();
  const [uploading, setUploading] = useState(false);

  const rows = useMemo<Row[]>(() => {
    const runtime = new Map((runtimeQ.data?.data ?? []).map((s) => [s.slug, s]));
    return (skillsQ.data ?? []).map((skill) => ({
      skill,
      latest: latestReleased(skill),
      pending: skill.versions.find((v) => v.status === "pending_review"),
      references: runtime.get(skill.slug)?.references ?? 0,
      loads7d: runtime.get(skill.slug)?.loads_7d ?? 0,
    }));
  }, [skillsQ.data, runtimeQ.data]);

  const visible = rows.filter((r) => {
    const kw = keyword.trim().toLowerCase();
    if (kw && ![r.skill.slug, r.latest?.description ?? r.pending?.description ?? ""].some((s) => s.toLowerCase().includes(kw))) {
      return false;
    }
    if (view === "market") return r.latest?.status === "published" && (!source || r.skill.source === source);
    if (view === "review") return !!r.pending;
    return r.skill.created_by === me?.uid || r.skill.versions.some((v) => v.uploaded_by === me?.uid);
  });
  // 广场默认按使用量排：用的智能体越多越靠前
  if (view === "market")
    visible.sort((a, b) =>
      sort === "uses"
        ? b.references - a.references || b.loads7d - a.loads7d
        : (b.latest?.published_at ?? "").localeCompare(a.latest?.published_at ?? ""),
    );

  const current = rows.find((r) => r.skill.slug === selected);

  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <h1 className={pageStyles.title}>{VIEW_TITLE[view]}</h1>
          {me?.can_upload && (
            <Button
              type="primary"
              className={pageStyles.action}
              icon={<Icon name="upload" size={14} />}
              onClick={() => setUploading(true)}
            >
              上传技能
            </Button>
          )}
        </div>
        <div className={pageStyles.toolbar}>
          {view === "market" && (
            <Segmented
              value={source}
              onChange={(v) => setSource(v as string)}
              options={[
                { value: "", label: "全部" },
                { value: "tenant", label: "团队" },
                { value: "builtin", label: "内置" },
                { value: "imported", label: "第三方" },
              ]}
            />
          )}
          <Input
            allowClear
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索技能"
            prefix={<Icon name="search" size={14} style={{ color: "var(--fg-4)" }} />}
            style={{ width: 260 }}
          />
          {view === "market" && (
            <>
              <span style={{ flex: 1 }} />
              <Select
                style={{ width: 120 }}
                value={sort}
                onChange={setSort}
                options={[
                  { value: "uses", label: "最多使用" },
                  { value: "updated", label: "最近更新" },
                ]}
              />
            </>
          )}
        </div>
      </header>

      <div className={pageStyles.body}>
        {skillsQ.error && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message="暂时连不上技能服务"
            description={skillsQ.error.message}
          />
        )}
        {runtimeQ.error && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message="暂时看不到技能的使用情况"
            description={(runtimeQ.error as ApiError).message}
          />
        )}
        {runtimeQ.data && !runtimeQ.data.directory_configured && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message="技能目录还没有接入"
            description="接入之前，智能体里暂时不能选择技能。请联系管理员。"
          />
        )}
        {skillsQ.isPending && <Skeleton active paragraph={{ rows: 6 }} />}
        {skillsQ.data && visible.length === 0 && (
          <Empty
            style={{ marginTop: 64 }}
            description={keyword ? "没有找到，换个关键词试试" : VIEW_EMPTY[view]}
          />
        )}
        {view === "market" && visible.length > 0 && (
          <div className={pageStyles.grid}>
            {visible.map((r) => (
              <div
                key={r.skill.slug}
                className={pageStyles.card}
                role="button"
                tabIndex={0}
                onClick={() => setSelected(r.skill.slug)}
                onKeyDown={(e) => e.key === "Enter" && setSelected(r.skill.slug)}
              >
                <div className={pageStyles.cardHead}>
                  <span className={pageStyles.avatar} style={{ background: "#fef3c7", color: "#b45309" }}>
                    <Icon name="spark" size={18} />
                  </span>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div className={pageStyles.cardName}>
                      {r.skill.slug}
                      {r.latest?.has_scripts && <Tag style={{ marginInlineEnd: 0 }}>含脚本</Tag>}
                    </div>
                    <div className={pageStyles.cardSub}>{SOURCE_LABEL[r.skill.source] ?? r.skill.source}</div>
                  </div>
                </div>
                <div className={pageStyles.cardDesc}>{r.latest?.description || "（没有描述）"}</div>
                <div className={pageStyles.cardFoot}>
                  <Icon name="bot" size={13} />
                  <span>{r.references} 个智能体在用</span>
                  <span style={{ flex: 1 }} />
                  <span style={{ fontFamily: "var(--mono)" }}>v{r.latest?.version}</span>
                </div>
              </div>
            ))}
          </div>
        )}
        {view !== "market" && visible.length > 0 && (
          <Table<Row>
            size="middle"
            rowKey={(r) => r.skill.slug}
            dataSource={visible}
            pagination={false}
            onRow={(r) => ({ onClick: () => setSelected(r.skill.slug), style: { cursor: "pointer" } })}
            columns={[
              {
                title: "技能",
                render: (_, r) => (
                  <div>
                    <b>{r.skill.slug}</b>{" "}
                    {(r.latest ?? r.pending)?.has_scripts && <Tag>含脚本</Tag>}
                    <div className={styles.desc2}>
                      {(r.latest ?? r.pending)?.description ?? "—"}
                    </div>
                  </div>
                ),
              },
              {
                title: "最新版本",
                width: 130,
                render: (_, r) => (
                  <span style={{ fontFamily: "var(--mono)" }}>
                    {r.latest ? `v${r.latest.version}` : "—"}
                    {r.pending && (
                      <Tag color="warning" style={{ marginLeft: 6 }}>
                        新上传待审
                      </Tag>
                    )}
                  </span>
                ),
              },
              {
                title: "状态",
                width: 90,
                render: (_, r) => {
                  const st = SKILL_STATUS[(r.latest ?? r.pending)?.status ?? ""];
                  return st ? <Tag color={st.color}>{st.label}</Tag> : null;
                },
              },
              {
                title: "来源",
                width: 80,
                render: (_, r) => <Tag>{SOURCE_LABEL[r.skill.source] ?? r.skill.source}</Tag>,
              },
              { title: "使用中", width: 100, render: (_, r) => `${r.references} 个智能体` },
              {
                title: "7 天加载",
                width: 90,
                render: (_, r) => (
                  // 长期为 0：不报错，但一直占着索引预算 —— 标出来该复查描述或下掉
                  <span style={{ color: r.loads7d === 0 && r.references > 0 ? "var(--warning)" : undefined }}>
                    {r.loads7d}
                  </span>
                ),
              },
            ]}
          />
        )}
      </div>

      <Drawer open={!!current} width={520} title={null} onClose={() => setSelected(undefined)} destroyOnClose>
        {current && <SkillDetail key={current.skill.slug} row={current} />}
      </Drawer>
      <UploadModal open={uploading} onClose={() => setUploading(false)} onDone={(slug) => setSelected(slug)} />
    </div>
  );
}

// ═══════════════════════════════════════════════ 详情

function SkillDetail({ row }: { row: Row }) {
  const { skill } = row;
  const { modal, message } = AntdApp.useApp();
  const ordered = [...skill.versions].sort((a, b) =>
    (b.version ?? Number.MAX_SAFE_INTEGER) - (a.version ?? Number.MAX_SAFE_INTEGER) ||
    b.created_at.localeCompare(a.created_at),
  );
  const [versionId, setVersionId] = useState<string>(row.pending?.id ?? row.latest?.id ?? ordered[0]?.id ?? "");
  const version = skill.versions.find((v) => v.id === versionId) ?? ordered[0];
  const [path, setPath] = useState<string>("SKILL.md");
  const fileQ = useSkillFile(version, version?.files.some((f) => f.path === path) ? path : undefined);
  const refsQ = useSkillReferences(skill.slug);
  const usageQ = useSkillUsage(7);
  const review = useReviewSkill();
  const action = useSkillAction();
  // 审查、停用、下架、重新检查都要 skill:review（只是体验，权限在技能应用的接口上）
  const canReview = !!useSkMe().data?.can_review;

  if (!version) return null;
  const st = SKILL_STATUS[version.status];
  const usage = (usageQ.data?.data ?? []).filter((u) => u.slug === skill.slug);
  const loads = usage.reduce((n, u) => n + u.loads, 0);
  const completed = usage.reduce((n, u) => n + u.completed_runs, 0);

  const ask = (title: string, required: boolean, onOk: (text: string) => Promise<unknown>) => {
    let text = "";
    modal.confirm({
      title,
      icon: null,
      content: (
        <Input.TextArea
          autoFocus
          rows={3}
          placeholder={required ? "必填" : "可选"}
          onChange={(e) => {
            text = e.target.value;
          }}
        />
      ),
      onOk: async () => {
        if (required && !text.trim()) {
          message.warning("请填写");
          throw new Error("required");
        }
        try {
          await onOk(text.trim());
        } catch (err) {
          message.error(err instanceof ApiError ? err.message : String(err));
          throw err;
        }
      },
    });
  };

  const doReview = (approve: boolean) =>
    ask(approve ? "批准并发布" : "拒绝这次上传", true, (note) =>
      review.mutateAsync({ uploadId: version.id, approve, note }).then((v) => {
        message.success(approve ? `已发布 v${v.version}` : "已拒绝");
        setVersionId(v.id);
      }),
    );

  const doAction = (act: SkillAction, label: string, required = false) =>
    ask(label, required, (reason) =>
      action
        .mutateAsync({ slug: skill.slug, version: version.version!, action: act, reason: reason || undefined })
        .then(() => message.success(`${label}：完成`)),
    );

  const scan = version.scan_result as Record<string, { result?: string; hits?: string[]; by?: string; note?: string } | null>;

  return (
    <div>
      <div className={styles.detailHead}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className={styles.detailTitle}>{skill.slug}</h2>
          <div className={styles.sub}>
            {SOURCE_LABEL[skill.source] ?? skill.source} · {skill.created_by} · 创建于{" "}
            {relativeTime(skill.created_at)}
          </div>
        </div>
        {st && <Tag color={st.color}>{st.label}</Tag>}
      </div>

      <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
        <Select
          size="small"
          style={{ width: 190 }}
          value={version.id}
          onChange={setVersionId}
          options={ordered.map((v) => ({
            value: v.id,
            label: `${v.version != null ? `v${v.version}` : "上传"} · ${SKILL_STATUS[v.status]?.label ?? v.status}`,
          }))}
        />
        {canReview && version.status === "pending_review" && (
          <>
            <Button size="small" type="primary" onClick={() => doReview(true)}>
              批准
            </Button>
            <Button size="small" danger onClick={() => doReview(false)}>
              拒绝
            </Button>
          </>
        )}
        {canReview && version.status === "published" && (
          <Button size="small" onClick={() => doAction("disable", "停用（不可被新引用）")}>
            停用
          </Button>
        )}
        {canReview && version.status === "disabled" && (
          <Button size="small" onClick={() => doAction("enable", "重新启用")}>
            启用
          </Button>
        )}
        {canReview && (version.status === "published" || version.status === "disabled") && (
          <Button size="small" danger onClick={() => doAction("revoke", "紧急下架（不可撤销，写明原因）", true)}>
            紧急下架
          </Button>
        )}
        {canReview && version.version != null && version.status !== "rejected" && (
          <Button size="small" type="text" onClick={() => doAction("rescan", "按当前规则重新检查")}>
            重新检查
          </Button>
        )}
      </div>

      {version.status === "revoked" && (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 12 }}
          message="已紧急下架"
          description={`${version.status_reason ?? ""}。用到它的智能体之后都不会再加载它。`}
        />
      )}

      <div className={styles.secTitle}>描述</div>
      <pre className={styles.code}>{version.description}</pre>
      <div className={styles.muted} style={{ marginTop: 4 }}>
        {version.description.length} 字符
      </div>

      <div className={styles.secTitle}>
        文件 · {version.file_count} 个 · {(version.size_bytes / 1024).toFixed(1)} KB
      </div>
      {version.files.map((f) => (
        <button
          key={f.path}
          type="button"
          className={styles.fileRow}
          aria-pressed={f.path === path}
          onClick={() => setPath(f.path)}
        >
          <Icon name="file" size={13} />
          <span style={{ flex: 1 }}>{f.path}</span>
          {f.executable && <Tag color="warning">可执行</Tag>}
          <span className={styles.muted}>{f.size} B</span>
        </button>
      ))}
      <div style={{ marginTop: 8 }}>
        {version.status === "rejected" ? (
          <span className={styles.muted}>被拒的上传已删除包内容</span>
        ) : fileQ.isPending && fileQ.fetchStatus !== "idle" ? (
          <Skeleton active paragraph={{ rows: 3 }} />
        ) : fileQ.error ? (
          <span className={styles.muted}>{fileQ.error.message}</span>
        ) : fileQ.data !== undefined ? (
          <pre className={styles.code}>{fileQ.data}</pre>
        ) : null}
      </div>
      <div className={styles.muted} style={{ marginTop: 4, fontFamily: "var(--mono)" }}>
        {version.content_hash}
      </div>

      <div className={styles.secTitle}>
        <Icon name="shield" size={13} />
        安全检查
      </div>
      {(
        [
          ["archive", "压缩包安全"],
          ["structure", "文件结构"],
          ["content", "没有敏感内容"],
          ["similarity", "与已有技能不冲突"],
          ["review", "人工审查"],
        ] as const
      ).map(([key, label]) => {
        const layer = scan[key];
        const result = layer?.result ?? (key === "review" ? "pending" : "pass");
        const color = result === "pass" || result === "adopted" ? "success" : result === "warn" ? "warning" : result === "reject" ? "error" : "default";
        return (
          <div key={key} className={styles.scan}>
            <Tag color={color} style={{ minWidth: 56, textAlign: "center" }}>
              {{ pass: "通过", warn: "提示", reject: "拒绝", skipped: "免审", adopted: "登记", pending: "待审" }[result] ?? result}
            </Tag>
            <div style={{ flex: 1 }}>
              <b>{label}</b>
              {key === "review" && layer?.by && (
                <span className={styles.muted}>
                  {" "}
                  · {layer.by}
                  {layer.note ? ` · ${layer.note}` : ""}
                </span>
              )}
              {!!layer?.hits?.length && (
                <ul className={styles.hits}>
                  {layer.hits.slice(0, 20).map((h) => (
                    <li key={h}>{h}</li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        );
      })}

      <div className={styles.secTitle}>在用的智能体</div>
      {refsQ.data?.data.length ? (
        refsQ.data.data.map((r) => (
          <div key={`${r.agent_id}-${r.subagent ?? ""}`} className={styles.kv}>
            <span>
              <Link href={`/agents/${r.agent_id}`}>{r.agent_name}</Link> v{r.agent_version}
              {r.subagent && ` → ${r.subagent}`}
            </span>
            <span>
              v{r.skill_version} · {r.active_threads} 个会话
            </span>
          </div>
        ))
      ) : (
        <div className={styles.muted}>{refsQ.isPending ? "加载中…" : "没有智能体引用它"}</div>
      )}

      <div className={styles.secTitle}>效果 · 近 7 天</div>
      <div className={styles.kv}>
        <span>加载过它的运行</span>
        <span>{loads}</span>
      </div>
      <div className={styles.kv}>
        <span>其中正常完成</span>
        <span>
          {completed}
          {loads > 0 && `（${Math.round((completed / loads) * 100)}%）`}
        </span>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════ 上传

function UploadModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  onDone: (slug: string) => void;
}) {
  const { message } = AntdApp.useApp();
  const upload = useUploadSkill();
  const [file, setFile] = useState<File>();
  const [source, setSource] = useState("builtin");
  const [origin, setOrigin] = useState("");
  const err = upload.error as ApiError | null;
  const hits = (err?.details?.hits as string[] | undefined) ?? [];

  const close = () => {
    upload.reset();
    setFile(undefined);
    onClose();
  };

  return (
    <Modal
      title="上传技能"
      open={open}
      onCancel={close}
      okText="上传"
      okButtonProps={{ disabled: !file || (source === "imported" && !origin), loading: upload.isPending }}
      onOk={() =>
        file &&
        upload.mutate(
          { file, source, originUrl: origin || undefined },
          {
            onSuccess: (v) => {
              message.success(
                v.status === "published" ? `已发布 ${v.slug} v${v.version}` : `已上传 ${v.slug}，等待审查`,
              );
              onDone(v.slug);
              close();
            },
          },
        )
      }
    >
      <Upload.Dragger
        accept=".zip"
        maxCount={1}
        beforeUpload={(f) => {
          setFile(f);
          upload.reset();
          return false; // 不自动上传：和来源一起提交
        }}
        onRemove={() => setFile(undefined)}
        fileList={file ? [{ uid: "1", name: file.name, status: "done" }] : []}
      >
        <p style={{ margin: "8px 0" }}>把技能包（zip，根目录或唯一子目录里有 SKILL.md）拖到这里</p>
        <p className={styles.muted}>≤ 20MB · ≤ 500 个文件 · 不允许符号链接与二进制可执行文件</p>
      </Upload.Dragger>
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <Select
          value={source}
          onChange={setSource}
          style={{ width: 200 }}
          options={[
            { value: "builtin", label: "内置" },
            { value: "tenant", label: "团队" },
            { value: "imported", label: "第三方" },
          ]}
        />
        {source === "imported" && (
          <Input value={origin} onChange={(e) => setOrigin(e.target.value)} placeholder="出处 URL（必填）" />
        )}
      </div>
      {err && (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 12 }}
          message={err.kind === "scan_blocked" ? "包没有通过检查" : "上传失败"}
          description={
            hits.length ? (
              <ul className={styles.hits}>
                {hits.map((h) => (
                  <li key={h}>{h}</li>
                ))}
              </ul>
            ) : (
              err.message
            )
          }
        />
      )}
    </Modal>
  );
}
