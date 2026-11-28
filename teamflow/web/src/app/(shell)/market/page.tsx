"use client";

/**
 * 流程市场：产品自带的流程模板，不属于任何租户（同技能市场）。运营管理员（系统租户 · 流程市场读写）
 * 新建、编辑、发布、上下架；租户的团队管理员只能拷贝到自己的团队再改。
 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Input, Modal, Select, Tag } from "antd";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { MARKET_CATEGORIES } from "@/components/shell/AppShell";
import { Empty, Icon, Loading, Page, PageHead } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { MARKET_STATUS } from "@/lib/format";
import { useMarket, useTemplates } from "@/lib/queries";
import { summary } from "@/lib/structure";
import { useCurrentTeam } from "@/lib/team";
import type { MarketCategory, MarketTemplate } from "@/lib/types";

export default function MarketPage() {
  const cat = useSearchParams().get("cat") ?? "all";
  const router = useRouter();
  const { message } = App.useApp();
  const qc = useQueryClient();
  const market = useMarket(cat);
  const { team } = useCurrentTeam();
  const mine = useTemplates(team?.id);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [category, setCategory] = useState<MarketCategory>("rd");
  const maintainer = !!market.data?.maintainer;
  const copied = (mid: string) => (mine.data ?? []).filter((t) => t.origin?.market_template_id === mid);
  const label = MARKET_CATEGORIES.find((c) => c.key === cat)?.label;

  const create = async () => {
    try {
      const t = await api.post<MarketTemplate>("/market", { name: name.trim(), category });
      await qc.invalidateQueries({ queryKey: ["market"] });
      router.push(`/market/${t.id}/edit`);
    } catch (e) {
      message.error(errorText(e));
    }
  };

  return (
    <Page>
      <PageHead
        title={`流程市场${cat === "all" ? "" : ` · ${label}`}`}
        sub={
          maintainer
            ? "产品自带的流程模板，不属于任何租户。你在这里新建、编辑、发布、上下架；租户拷走的是拷贝，之后和市场无关"
            : "产品自带的流程模板。团队管理员拷贝到本团队后再改；市场里的模板不能改"
        }
        extra={
          maintainer && (
            <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setCreating(true)}>
              新建模板
            </Button>
          )
        }
      />
      {market.isPending ? (
        <Loading />
      ) : !market.data?.items.length ? (
        <div className="card">
          <Empty icon="grid" title="这个分类还没有模板" />
        </div>
      ) : (
        <div className="cgrid">
          {market.data.items.map((m) => (
            <Link key={m.id} className="tcard" href={`/market/${m.id}`}>
              <div className="row">
                <span className="ic2" style={{ background: m.color || "var(--primary)" }}>
                  <Icon name="share" size={16} />
                </span>
                <div className="grow">
                  <b>{m.name}</b>
                  <div className="xs faint">
                    {m.category_name}
                    {m.who ? ` · ${m.who}` : ""}
                  </div>
                </div>
                {maintainer && <Tag color={MARKET_STATUS[m.status][1]}>{MARKET_STATUS[m.status][0]}</Tag>}
              </div>
              {m.description && <div className="sm muted" style={{ marginTop: 8 }}>{m.description}</div>}
              {m.structure && <div className="mini">{summary(m.structure)}</div>}
              <div className="row xs muted" style={{ marginTop: 10 }}>
                <span>{m.latest_version ? `v${m.latest_version}` : "草稿"}</span>
                {maintainer && m.copies != null && (
                  <span>
                    · {m.copies} 次拷贝（{m.tenants} 个租户）
                  </span>
                )}
                <span className="sp" />
                {!maintainer && copied(m.id).length > 0 && <Tag color="success">{team?.name}已拷贝</Tag>}
              </div>
            </Link>
          ))}
        </div>
      )}
      <Modal open={creating} title="新建市场模板" onCancel={() => setCreating(false)} onOk={() => void create()} okText="新建" okButtonProps={{ disabled: !name.trim() }}>
        <div className="col" style={{ gap: 10 }}>
          <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={64} placeholder="模板名称，市场里唯一" />
          <Select
            value={category}
            onChange={setCategory}
            options={MARKET_CATEGORIES.filter((c) => c.key !== "all").map((c) => ({ value: c.key, label: c.label }))}
          />
        </div>
      </Modal>
    </Page>
  );
}
