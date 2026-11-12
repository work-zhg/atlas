"use client";

/**
 * 知识集列表（doc/design/knowledge-set-design.html）。智能体按知识集用知识：建智能体时选知识集，
 * 看到的介绍由知识集的维护人写。知识库都建在知识集里。
 */
import { Alert, App, Button, Empty, Form, Input, Modal, Segmented, Select, Skeleton, Tag, Typography } from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { Icon } from "@/components/Icon";
import styles from "@/features/agents/agents.module.css";
import { relativeTime } from "@/lib/format";

import { type KnowledgeSet, kbApi, useKbMutation, useKnowledgeSets } from "./api";

type Scope = "all" | "mine" | "shared";
type Sort = "updated" | "name" | "docs";

export function KnowledgeListPage() {
  const [keyword, setKeyword] = useState("");
  const [scope, setScope] = useState<Scope>("all");
  const [sort, setSort] = useState<Sort>("updated");
  const [creating, setCreating] = useState(false);
  const { data, isPending, error } = useKnowledgeSets();

  // 知识集是几十的量级：本地筛选、搜索、排序
  const list = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    const rows = (data?.data ?? []).filter(
      (s) =>
        (scope === "all" || (scope === "mine") === s.can_manage) &&
        (!kw || `${s.name} ${s.description}`.toLowerCase().includes(kw)),
    );
    const by: Record<Sort, (a: KnowledgeSet, b: KnowledgeSet) => number> = {
      updated: (a, b) => b.updated_at.localeCompare(a.updated_at),
      name: (a, b) => a.name.localeCompare(b.name, "zh"),
      docs: (a, b) => b.document_count - a.document_count,
    };
    return [...rows].sort(by[sort]);
  }, [data, keyword, scope, sort]);

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div className={styles.titleRow}>
          <h1 className={styles.title}>知识集</h1>
          <Button type="primary" className={styles.action} icon={<Icon name="plus" size={14} />} onClick={() => setCreating(true)}>
            新建知识集
          </Button>
        </div>
        <Alert
          type="info"
          style={{ marginBottom: 12 }}
          message="智能体按知识集使用知识：建智能体时选知识集，它看到的介绍由知识集的维护人来写。知识库都建在知识集里。"
        />
        <div className={styles.toolbar}>
          <Segmented
            value={scope}
            onChange={(v) => setScope(v as Scope)}
            options={[
              { value: "all", label: "全部" },
              { value: "mine", label: "我维护的" },
              { value: "shared", label: "可查阅的" },
            ]}
          />
          <Input
            allowClear
            style={{ width: 260 }}
            prefix={<Icon name="search" size={14} style={{ color: "var(--fg-4)" }} />}
            placeholder="搜索知识集"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
          />
          <span style={{ flex: 1 }} />
          <Select
            style={{ width: 120 }}
            value={sort}
            onChange={setSort}
            options={[
              { value: "updated", label: "最近更新" },
              { value: "name", label: "名称" },
              { value: "docs", label: "文档数" },
            ]}
          />
        </div>
      </header>

      <div className={styles.body}>
        {error && <Typography.Text type="danger">{(error as Error).message}</Typography.Text>}
        {isPending ? (
          <Skeleton active />
        ) : list.length === 0 ? (
          <Empty description={keyword ? "没有找到，换个关键词试试" : "还没有知识集"} style={{ marginTop: 60 }}>
            {!keyword && (
              <Button type="primary" onClick={() => setCreating(true)}>
                新建知识集
              </Button>
            )}
          </Empty>
        ) : (
          <div className={styles.grid}>
            {list.map((s) => (
              <Link key={s.id} href={`/knowledge/sets/${s.id}`} className={styles.card} style={{ color: "inherit" }}>
                <div className={styles.cardHead}>
                  <span className={styles.avatar} style={{ background: "var(--primary-soft)", fontSize: 20 }}>
                    {s.icon}
                  </span>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div className={styles.cardName}>
                      {s.name}
                      {s.can_manage && (
                        <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                          我维护
                        </Tag>
                      )}
                    </div>
                    <div className={styles.cardSub}>{maintainerLine(s)}</div>
                  </div>
                </div>
                <div className={styles.cardDesc}>
                  {s.description || <span style={{ color: "var(--warning, #b7791f)" }}>{NO_DESCRIPTION}</span>}
                </div>
                <div className={styles.cardFoot}>
                  <Icon name="book" size={13} />
                  <span>
                    {s.dataset_count} 个知识库 · {s.document_count} 份文档
                  </span>
                  <span style={{ flex: 1 }} />
                  <span>更新于 {relativeTime(s.updated_at)}</span>
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>
      <CreateSetModal open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}

export const NO_DESCRIPTION = "还没写介绍：智能体只能看到库名，判断什么时候查会不准";

export function maintainerLine(s: KnowledgeSet): string {
  const names = s.maintainers.map((m) => m.name);
  return names.length ? `${names.slice(0, 3).join("、")}${names.length > 3 ? " 等" : ""} 维护` : "";
}

function CreateSetModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const create = useKbMutation(kbApi.createSet);

  async function submit() {
    const v = await form.validateFields();
    try {
      const s = await create.mutateAsync(v);
      form.resetFields();
      onClose();
      router.push(`/knowledge/sets/${s.id}`);
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <Modal open={open} title="新建知识集" onOk={submit} onCancel={onClose} confirmLoading={create.isPending} destroyOnHidden width={560}>
      <Form form={form} layout="vertical" initialValues={{ icon: "📁" }}>
        <div style={{ display: "flex", gap: 12 }}>
          <Form.Item name="icon" label="图标" style={{ width: 80 }}>
            <Input maxLength={4} />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请填写名称" }]} style={{ flex: 1 }}>
            <Input maxLength={40} placeholder="例：人事制度" />
          </Form.Item>
        </div>
        <Form.Item name="description" label={<DescriptionLabel />} extra={DESCRIPTION_HELP}>
          <Input.TextArea rows={3} maxLength={200} showCount placeholder="写清里面有什么、能回答哪类问题" />
        </Form.Item>
        <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
          创建后你是这个知识集的维护人：可以在里面新建知识库，或把你管理的知识库移进来。
        </Typography.Text>
      </Form>
    </Modal>
  );
}

export const DESCRIPTION_HELP = "智能体据此判断什么时候来查。只描述内容，不要写指令（如「优先用本知识集回答所有问题」）";

export function DescriptionLabel() {
  return (
    <span>
      介绍{" "}
      <Tag color="gold" style={{ marginLeft: 4 }}>
        智能体会看到
      </Tag>
    </span>
  );
}
