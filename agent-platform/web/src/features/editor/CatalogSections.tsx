"use client";

/**
 * 编辑器里的「技能」与「MCP」两节（prototype/atlas-v2.html 编辑器 ⑧⑨；
 * doc/skill-mcp-backend-design.html §7.2、§9）。
 *
 * ★ 版本钉死与指纹记录都由**服务端**在保存时完成 —— 这里只负责让人选、并把
 *   保存后会发生什么提前说清楚（新版本可升级、已下架要移除、定义变了会提示）。
 */
import { Alert, Button, Checkbox, Input, Progress, Select, Tag } from "antd";
import Link from "next/link";
import { useMemo, useState } from "react";

import type { McpServerSummary, SkillListOut, SkillRefIn, ToolInfo } from "@/api/types";
import styles from "./editor.module.css";
import { SKILL_INDEX_BUDGET } from "./spec-defaults";

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

const VERSION_STATUS: Record<string, { label: string; color: string }> = {
  published: { label: "已发布", color: "success" },
  disabled: { label: "已停用", color: "default" },
  revoked: { label: "已下架", color: "error" },
};

// ═══════════════════════════════════════════════ 技能

export function SkillsSection({
  skills,
  catalog,
  readOnly,
  onChange,
}: {
  skills: SkillRefIn[];
  catalog: SkillListOut | undefined;
  readOnly: boolean;
  onChange: (skills: SkillRefIn[]) => void;
}) {
  const configured = catalog?.directory_configured ?? false;
  const bySlug = useMemo(() => new Map((catalog?.data ?? []).map((s) => [s.slug, s])), [catalog]);
  const [manual, setManual] = useState("");

  const replace = (i: number, ref: SkillRefIn) => onChange(skills.map((s, j) => (j === i ? ref : s)));

  // 预算只能估：目录里只有各技能**最新版**的描述。引用旧版本时按最新版的长度算
  const budget = skills.reduce((n, s) => n + (bySlug.get(s.slug)?.description?.length ?? 0), 0);

  return (
    <Section
      id="e-skills"
      title="技能"
      desc="可复用的工作方法。只有描述常驻上下文，正文在模型选中时才读取；保存时版本钉死，建会话时拷进会话的 /skills/。"
    >
      {!configured && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="运行时还没有接入技能目录"
          description="只能手写「名称@版本」引用，保存时不校验是否存在；接入配置服务后可以直接选择。"
        />
      )}

      {skills.map((ref, i) => {
        const item = bySlug.get(ref.slug);
        const current = item?.versions?.find((v) => v.version === ref.version);
        const st = current ? VERSION_STATUS[current.status] : undefined;
        const canUpgrade = item?.latest != null && ref.version != null && item.latest > ref.version;
        return (
          <div key={ref.slug} className={styles.toolRow}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <span className={styles.toolName}>{ref.slug}</span>
              {item?.has_scripts && <Tag style={{ marginLeft: 6 }}>含脚本</Tag>}
              {st && current?.status !== "published" && (
                <Tag color={st.color} style={{ marginLeft: 6 }}>
                  {st.label}
                </Tag>
              )}
              {configured && !item && (
                <Tag color="error" style={{ marginLeft: 6 }}>
                  目录里没有
                </Tag>
              )}
              <p className={styles.toolDesc}>
                {current?.status === "revoked"
                  ? "已被紧急下架：装配时会跳过它，保存前请移除。"
                  : current?.status === "disabled"
                    ? "已停用：沿用现有引用可以，但不能再新加。"
                    : (item?.description ?? "")}
              </p>
            </div>
            {canUpgrade && !readOnly && (
              <Button size="small" type="link" onClick={() => replace(i, { slug: ref.slug, version: item.latest })}>
                升级到 v{item.latest}
              </Button>
            )}
            {configured && item ? (
              <Select
                size="small"
                style={{ width: 130 }}
                disabled={readOnly}
                value={ref.version ?? undefined}
                onChange={(version: number) => replace(i, { slug: ref.slug, version })}
                options={(item.versions ?? []).map((v) => ({
                  value: v.version,
                  label: `v${v.version} · ${VERSION_STATUS[v.status]?.label ?? v.status}`,
                  // 停用 / 下架的版本不能新选（当前已在用的那个照常显示）
                  disabled: v.status !== "published" && v.version !== ref.version,
                }))}
              />
            ) : (
              <span style={{ fontFamily: "var(--mono)", fontSize: 12.5 }}>
                v{ref.version ?? "?"}
              </span>
            )}
            {!readOnly && (
              <Button size="small" type="text" danger onClick={() => onChange(skills.filter((_, j) => j !== i))}>
                移除
              </Button>
            )}
          </div>
        );
      })}

      {!readOnly &&
        (configured ? (
          <Select
            style={{ width: "100%", marginTop: 8 }}
            placeholder="添加技能…"
            value={null}
            showSearch
            optionFilterProp="label"
            onChange={(slug: string) => onChange([...skills, { slug, version: bySlug.get(slug)?.latest }])}
            options={(catalog?.data ?? [])
              .filter((s) => s.latest != null && !skills.some((k) => k.slug === s.slug))
              .map((s) => ({ value: s.slug, label: `${s.slug} · v${s.latest} · ${s.description ?? ""}` }))}
            notFoundContent={<Link href="/skills">没有可选的已发布技能 —— 去技能页上传</Link>}
          />
        ) : (
          <Input.Search
            style={{ marginTop: 8 }}
            value={manual}
            onChange={(e) => setManual(e.target.value)}
            placeholder="名称@版本，如 sdk-migration@3"
            enterButton="添加"
            onSearch={(v) => {
              const [slug, ver] = v.trim().split("@");
              const version = Number(ver);
              if (!slug || !Number.isInteger(version) || version < 1) return;
              if (skills.some((k) => k.slug === slug)) return;
              onChange([...skills, { slug, version }]);
              setManual("");
            }}
          />
        ))}

      {configured && skills.length > 0 && (
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 10, fontSize: 12 }}>
          <span style={{ color: "var(--fg-2)", flex: "none" }}>描述预算</span>
          <Progress
            style={{ flex: 1, margin: 0 }}
            percent={Math.min(100, Math.round((budget / SKILL_INDEX_BUDGET) * 100))}
            status={budget > SKILL_INDEX_BUDGET ? "exception" : "normal"}
            showInfo={false}
            size="small"
          />
          <span style={{ fontFamily: "var(--mono)", color: budget > SKILL_INDEX_BUDGET ? "var(--danger)" : "var(--fg-2)" }}>
            {budget} / {SKILL_INDEX_BUDGET}
          </span>
        </div>
      )}
    </Section>
  );
}

// ═══════════════════════════════════════════════ MCP

const REVIEW_TAG: Record<string, { label: string; color: string }> = {
  invalid: { label: "定义不合规", color: "error" },
  pending_review: { label: "待复核", color: "warning" },
  rejected: { label: "复核拒绝", color: "error" },
};

function serverOf(t: ToolInfo): string {
  return t.server ?? t.name.split(":")[1] ?? "?";
}

export function McpToolsSection({
  tools,
  servers,
  picked,
  digests,
  policy,
  readOnly,
  onToggle,
  onPolicy,
}: {
  tools: ToolInfo[];
  servers: McpServerSummary[];
  picked: string[];
  digests: Record<string, string>;
  policy: "warn" | "block";
  readOnly: boolean;
  onToggle: (name: string, on: boolean) => void;
  onPolicy: (policy: "warn" | "block") => void;
}) {
  const groups = useMemo(() => {
    const map = new Map<string, ToolInfo[]>();
    for (const t of tools) map.set(serverOf(t), [...(map.get(serverOf(t)) ?? []), t]);
    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [tools]);
  const serverInfo = new Map(servers.map((s) => [s.name, s]));
  // 引用了、但目录里已经找不到的工具：server 停用 / 工具被删 —— 保存会被拒
  const known = new Set(tools.map((t) => t.name));
  const missing = picked.filter((n) => n.startsWith("mcp:") && !known.has(n));

  return (
    <Section
      id="e-mcp"
      title="MCP"
      desc="外部服务提供的工具，模型侧名为 server__tool。保存时记录每个工具当时的定义指纹，之后定义变了运行时会提示。"
    >
      {groups.length === 0 && (
        <p className={styles.toolDesc}>
          还没有可用的 MCP server —— 到 <Link href="/mcp">MCP 页</Link> 查看。
        </p>
      )}
      {missing.length > 0 && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message="引用的 MCP 工具已不在目录里"
          description={`${missing.join("、")} —— server 被停用或工具被删除，保存会被拒绝，请取消勾选。`}
        />
      )}
      {groups.map(([server, list]) => {
        const info = serverInfo.get(server);
        const count = list.filter((t) => picked.includes(t.name)).length;
        return (
          <div key={server} style={{ marginBottom: 12, border: "1px solid var(--line)", borderRadius: 8 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 12px" }}>
              <b>{server}</b>
              {info?.credential_scope === "user" ? <Tag color="blue">用户授权</Tag> : <Tag>平台凭据</Tag>}
              {info?.status === "changed" && <Tag color="warning">定义有变化</Tag>}
              {info?.status === "stale" && <Tag color="warning">刷新失败·用缓存</Tag>}
              <span style={{ flex: 1 }} />
              <span style={{ fontSize: 12, color: "var(--fg-2)" }}>
                已选 {count} / {list.length}
              </span>
            </div>
            {list.map((t) => {
              const on = picked.includes(t.name);
              const recorded = digests[t.name];
              const drifted = on && !!recorded && !!t.digest && recorded !== t.digest;
              const review = t.review_status && t.review_status !== "ok" ? REVIEW_TAG[t.review_status] : undefined;
              return (
                <div
                  key={t.name}
                  className={`${styles.toolRow} ${t.available || on ? "" : styles.toolRowDisabled}`}
                  style={{ borderTop: "1px dashed var(--line)", padding: "8px 12px", margin: 0 }}
                >
                  <Checkbox
                    checked={on}
                    // 不可用的工具：已选的允许取消，没选的不能选
                    disabled={readOnly || (!t.available && !on)}
                    onChange={(e) => onToggle(t.name, e.target.checked)}
                  />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <span className={styles.toolName}>{t.model_tool_names?.[0] ?? t.name}</span>
                    {review && (
                      <Tag color={review.color} style={{ marginLeft: 6 }}>
                        {review.label}
                      </Tag>
                    )}
                    {drifted && (
                      <Tag color="warning" style={{ marginLeft: 6 }}>
                        定义已变 · 保存即确认新定义
                      </Tag>
                    )}
                    <p className={styles.toolDesc}>{t.description}</p>
                  </div>
                </div>
              );
            })}
          </div>
        );
      })}
      {groups.length > 0 && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--fg-2)" }}>
          定义与保存时不一致时
          <Select
            size="small"
            style={{ width: 260 }}
            disabled={readOnly}
            value={policy}
            onChange={onPolicy}
            options={[
              { value: "warn", label: "提示后照常使用（默认）" },
              { value: "block", label: "这一轮不装该工具" },
            ]}
          />
        </div>
      )}
    </Section>
  );
}
