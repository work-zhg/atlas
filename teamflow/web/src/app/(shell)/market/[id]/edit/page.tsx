"use client";

/** 流程市场模板的编排（运营管理员）：草稿存在模板行上，发布出新版本（第一次发布即上架） */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Input, Modal, Select, Tag } from "antd";
import { useParams } from "next/navigation";
import { useState } from "react";

import { MARKET_CATEGORIES } from "@/components/shell/AppShell";
import { TemplateEditor } from "@/components/TemplateEditor";
import { Icon, LoadError, Loading } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { useMarketTemplate } from "@/lib/queries";
import type { Issue, MarketCategory, MarketTemplate, Structure } from "@/lib/types";

const BLANK: Structure = {
  roles: {},
  flow: [{ type: "node", id: "n1" }],
  nodes: { n1: { name: "步骤 1", instructions: "", exec_role: "", admit: null, exit: { roles: [], rule: "any" } } },
};

export default function MarketEditor() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const m = useMarketTemplate(id);
  const [meta, setMeta] = useState(false);

  if (m.error) return <LoadError error={m.error} />;
  if (!m.data) return <Loading />;
  const t = m.data;
  if (t.draft === undefined) return <LoadError error={new Error("只有运营管理员能编辑流程市场的模板")} />;

  const save = async (structure: Structure) => {
    const r = await api.put<MarketTemplate & { errors: Issue[]; warnings: Issue[] }>(`/market/${id}/draft`, { structure });
    return { errors: r.errors, warnings: r.warnings };
  };
  const publish = async (note: string) => {
    await api.post(`/market/${id}/publish`, { note: note || null });
    await qc.invalidateQueries({ queryKey: ["market"] });
  };

  return (
    <>
      <TemplateEditor
        key={t.id}
        title={t.name}
        backHref={`/market/${t.id}`}
        structure={t.draft ?? t.structure ?? BLANK}
        nextVersion={t.latest_version + 1}
        save={save}
        publish={publish}
        roleHint="角色只是占位：租户拷贝后在项目配置里分配成员和数字员工"
        publishHint={`发布后：各租户在流程市场看到 v${t.latest_version + 1}，之后拷贝的拿到这一版；已经拷走的是租户自己的，不受影响${t.status === "draft" ? "。这是第一个版本，发布即上架" : ""}`}
        tags={
          <>
            <Tag color="purple">流程市场</Tag>
            {t.latest_version > 0 && <Tag>v{t.latest_version}</Tag>}
            <Button size="small" type="text" icon={<Icon name="pencil" size={13} />} onClick={() => setMeta(true)}>
              基本信息
            </Button>
          </>
        }
      />
      {meta && <MetaModal t={t} onClose={() => setMeta(false)} />}
    </>
  );
}

function MetaModal({ t, onClose }: { t: MarketTemplate; onClose: () => void }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const [name, setName] = useState(t.name);
  const [category, setCategory] = useState<MarketCategory>(t.category);
  const [description, setDescription] = useState(t.description ?? "");
  const [who, setWho] = useState(t.who ?? "");
  const [color, setColor] = useState(t.color ?? "");
  const ok = async () => {
    try {
      await api.put(`/market/${t.id}/draft`, { name: name.trim(), category, description, who, color: color || null });
      await qc.invalidateQueries({ queryKey: ["market"] });
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  };
  return (
    <Modal open title="基本信息" onCancel={onClose} onOk={() => void ok()} okText="保存" okButtonProps={{ disabled: !name.trim() }}>
      <div className="col" style={{ gap: 10 }}>
        <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={64} placeholder="名称" />
        <Select value={category} onChange={setCategory} options={MARKET_CATEGORIES.filter((c) => c.key !== "all").map((c) => ({ value: c.key, label: c.label }))} />
        <Input value={who} onChange={(e) => setWho(e.target.value)} maxLength={64} placeholder="适合谁，如：研发团队" />
        <Input.TextArea value={description} onChange={(e) => setDescription(e.target.value)} maxLength={500} autoSize={{ minRows: 3 }} placeholder="说明" />
        <Input value={color} onChange={(e) => setColor(e.target.value)} maxLength={16} placeholder="卡片颜色，如 #6366f1（可选）" />
      </div>
    </Modal>
  );
}
