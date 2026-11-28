"use client";

/** 流程市场 · 模板详情：流程图、节点说明、角色；运营管理员上下架、编辑，团队管理员拷贝到团队 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Tag } from "antd";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { FlowGraph } from "@/components/FlowGraph";
import { Crumb, Icon, LoadError, Loading, Page, PageHead } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { MARKET_STATUS, RULE } from "@/lib/format";
import { useMarketTemplate, useTemplates } from "@/lib/queries";
import { nodeIds, roleDuties, roleNames } from "@/lib/structure";
import { useCan, useCurrentTeam } from "@/lib/team";
import type { Template } from "@/lib/types";

export default function MarketDetail() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { message } = App.useApp();
  const qc = useQueryClient();
  const m = useMarketTemplate(id);
  const { team } = useCurrentTeam();
  const can = useCan();
  const mine = useTemplates(team?.id);
  const [sel, setSel] = useState<string>();
  const [busy, setBusy] = useState(false);

  if (m.error) return <LoadError error={m.error} />;
  if (!m.data) return <Loading />;
  const t = m.data;
  const maintainer = t.draft !== undefined;
  const s = t.structure ?? t.draft ?? null;
  const nid = sel && s?.nodes[sel] ? sel : s ? nodeIds(s)[0] : undefined;
  const n = nid && s ? s.nodes[nid] : undefined;
  const copies = (mine.data ?? []).filter((x) => x.origin?.market_template_id === t.id);

  const copy = async () => {
    if (!team) return;
    setBusy(true);
    try {
      const tpl = await api.post<Template>(`/teams/${team.id}/templates`, { market_template_id: t.id });
      await qc.invalidateQueries({ queryKey: ["team", team.id, "templates"] });
      message.success(`已拷贝为「${tpl.name}」的草稿`);
      router.push(`/team/templates/${tpl.id}`);
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const toggle = async () => {
    try {
      await api.post(`/market/${t.id}/listing`, { listed: t.status !== "listed" });
      await qc.invalidateQueries({ queryKey: ["market"] });
    } catch (e) {
      message.error(errorText(e));
    }
  };

  return (
    <Page>
      <Crumb items={[["流程市场", "/market"], [t.name, undefined]]} />
      <PageHead
        title={
          <>
            {t.name}
            {maintainer && <Tag color={MARKET_STATUS[t.status][1]}>{MARKET_STATUS[t.status][0]}</Tag>}
          </>
        }
        sub={
          <>
            {t.category_name}
            <span>·</span>
            {t.latest_version ? `v${t.latest_version}` : "草稿"}
            {t.who && (
              <>
                <span>·</span>
                {t.who}
              </>
            )}
            {maintainer && t.copies != null && (
              <>
                <span>·</span>
                {t.copies} 次拷贝（{t.tenants} 个租户）
              </>
            )}
          </>
        }
        extra={
          maintainer ? (
            <>
              <Button disabled={t.status === "draft"} title={t.status === "draft" ? "先发布第一个版本" : undefined} onClick={() => void toggle()}>
                {t.status === "listed" ? "下架" : "上架"}
              </Button>
              <Button type="primary" icon={<Icon name="pencil" size={14} />} onClick={() => router.push(`/market/${t.id}/edit`)}>
                编辑
              </Button>
            </>
          ) : (
            <Button type="primary" icon={<Icon name="copy" size={14} />} disabled={!can.configFlow || !team} loading={busy} onClick={() => void copy()} title={can.configFlow ? undefined : "只有团队管理员能拷贝"}>
              拷贝到{team?.name ?? "本团队"}
            </Button>
          )
        }
      />
      {!maintainer && copies.length > 0 && (
        <div className="banner info" style={{ marginBottom: 12 }}>
          <Icon name="check" size={15} />
          <div>
            {team?.name}已经拷贝过：
            {copies.map((c, i) => (
              <span key={c.id}>
                {i > 0 && "、"}
                <Link href={`/team/templates/${c.id}`}>「{c.name}」</Link>（拷自 v{c.origin!.version}）
              </span>
            ))}
            。拷过去的是团队自己的，市场后来的更新不会带过去；要新版本就再拷一份
          </div>
        </div>
      )}
      {t.description && (
        <div className="card pad" style={{ marginBottom: 14 }}>
          <div className="sm">{t.description}</div>
        </div>
      )}
      {s ? (
        <>
          <div className="card">
            <FlowGraph structure={s} selected={nid} onSelect={setSel} />
            <div className="legend">
              <span>点节点看工作说明</span>
              <span className="sp" />
              <span className="rtag admit">准入</span>
              <span>开工前审上游</span>
              <span className="rtag exit">准出</span>
              <span>交付前审本节点</span>
            </div>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", gap: 16, marginTop: 16 }}>
            <div className="card pad">
              {n && (
                <>
                  <h2 style={{ marginBottom: 10 }}>{n.name}</h2>
                  <dl className="kv" style={{ gridTemplateColumns: "82px 1fr" }}>
                    <dt>工作说明</dt>
                    <dd className="sm" style={{ whiteSpace: "pre-wrap" }}>
                      {n.instructions || "—"}
                    </dd>
                    <dt>执行</dt>
                    <dd className="sm">{s.roles[n.exec_role] ?? "—"}</dd>
                    {n.admit && (
                      <>
                        <dt>准入</dt>
                        <dd className="sm">
                          {roleNames(s, n.admit.roles)} · {RULE[n.admit.rule]}
                        </dd>
                      </>
                    )}
                    <dt>准出</dt>
                    <dd className="sm">
                      {roleNames(s, n.exit.roles)} · {RULE[n.exit.rule]}
                    </dd>
                  </dl>
                </>
              )}
            </div>
            <div className="card pad">
              <h2 style={{ marginBottom: 10 }}>角色</h2>
              {Object.entries(s.roles).map(([rk, rn]) => {
                const d = roleDuties(s, rk);
                return (
                  <div key={rk} className="row" style={{ padding: "6px 0", borderBottom: "1px solid var(--tf-line-soft)", alignItems: "flex-start" }}>
                    <b className="sm" style={{ width: 72, flex: "none" }}>
                      {rn}
                    </b>
                    <span className="sm muted">
                      执行 {d.exec.join("、") || "—"}；评审 {[...new Set([...d.admit, ...d.exit])].join("、") || "—"}
                    </span>
                  </div>
                );
              })}
              <div className="xs faint" style={{ marginTop: 8 }}>
                角色是占位：拷到团队后，在项目配置里给每个角色分配成员和数字员工；产出物的格式看数字员工的技能
              </div>
            </div>
          </div>
        </>
      ) : (
        <div className="card pad sm muted">还没有内容</div>
      )}
    </Page>
  );
}
