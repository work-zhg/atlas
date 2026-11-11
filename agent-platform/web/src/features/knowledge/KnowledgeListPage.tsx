"use client";

/** 知识库列表（doc/atlas-rag-design.html §13）。卡片 + 新建弹窗。 */
import { App, Button, Empty, Form, Input, Modal, Radio, Segmented, Select, Skeleton, Switch, Tag, Typography } from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { Icon } from "@/components/Icon";
import styles from "@/features/agents/agents.module.css";
import { relativeTime } from "@/lib/format";
import { can, useMe } from "@/lib/session";

import {
  type Dataset,
  type DocForm,
  type Permission,
  LEVEL_LABEL,
  PERMISSION_LABEL,
  kbApi,
  useDatasets,
  useKbMutation,
} from "./api";

type Scope = "all" | "mine" | "shared";
type Sort = "created" | "name" | "docs";

export function KnowledgeListPage() {
  const { data: me } = useMe();
  const [keyword, setKeyword] = useState("");
  const [scope, setScope] = useState<Scope>("all");
  const [sort, setSort] = useState<Sort>("created");
  const [creating, setCreating] = useState(false);
  const { data, isPending, error } = useDatasets();

  // 和智能体列表同一套：按「和我的关系」筛，本地搜索和排序（知识库是几十的量级）
  const list = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    const rows = (data?.data ?? []).filter(
      (d) =>
        (scope === "all" || (scope === "mine") === (d.owner_id === me?.id)) &&
        (!kw || `${d.name} ${d.description}`.toLowerCase().includes(kw)),
    );
    const by: Record<Sort, (a: Dataset, b: Dataset) => number> = {
      created: (a, b) => b.created_at.localeCompare(a.created_at),
      name: (a, b) => a.name.localeCompare(b.name, "zh"),
      docs: (a, b) => b.document_count - a.document_count,
    };
    return [...rows].sort(by[sort]);
  }, [data, keyword, scope, sort, me?.id]);

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div className={styles.titleRow}>
          <h1 className={styles.title}>知识库</h1>
          {can(me, "kb:create") && (
            <Button type="primary" className={styles.action} icon={<Icon name="plus" size={14} />} onClick={() => setCreating(true)}>
              新建知识库
            </Button>
          )}
        </div>
        <div className={styles.toolbar}>
          <Segmented
            value={scope}
            onChange={(v) => setScope(v as Scope)}
            options={[
              { value: "all", label: "全部" },
              { value: "mine", label: "我创建的" },
              { value: "shared", label: "共享给我的" },
            ]}
          />
          <Input
            allowClear
            style={{ width: 260 }}
            prefix={<Icon name="search" size={14} style={{ color: "var(--fg-4)" }} />}
            placeholder="搜索知识库"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
          />
          <span style={{ flex: 1 }} />
          <Select
            style={{ width: 120 }}
            value={sort}
            onChange={setSort}
            options={[
              { value: "created", label: "最近创建" },
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
          <Empty description={keyword ? "没有找到，换个关键词试试" : "还没有知识库"} style={{ marginTop: 60 }}>
            {!keyword && can(me, "kb:create") && (
              <Button type="primary" onClick={() => setCreating(true)}>
                新建知识库
              </Button>
            )}
          </Empty>
        ) : (
          <div className={styles.grid}>
            {list.map((ds) => (
              <Link key={ds.id} href={`/knowledge/${ds.id}`} className={styles.card} style={{ color: "inherit" }}>
                <div className={styles.cardHead}>
                  <span className={styles.avatar} style={{ background: "var(--primary-soft)", fontSize: 20 }}>
                    {ds.icon}
                  </span>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div className={styles.cardName}>
                      {ds.name}
                      {ds.review_required && <Tag color="gold" style={{ marginInlineEnd: 0 }}>需审核</Tag>}
                    </div>
                    <div className={styles.cardSub}>
                      {ds.owner_id === me?.id ? "我创建的" : ds.my_level ? LEVEL_LABEL[ds.my_level] : "共享给我的"}
                    </div>
                  </div>
                </div>
                <div className={styles.cardDesc}>{ds.description || "（没有描述）"}</div>
                <div className={styles.cardFoot}>
                  <Icon name="file" size={13} />
                  <span>
                    {ds.document_count} 份文档 · {ds.word_count.toLocaleString()} 字
                  </span>
                  <span style={{ flex: 1 }} />
                  <span>创建于 {relativeTime(ds.created_at)}</span>
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>
      <CreateModal open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}

function CreateModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const create = useKbMutation(kbApi.createDataset);

  async function submit() {
    const v = await form.validateFields();
    try {
      const ds = await create.mutateAsync(v);
      form.resetFields();
      onClose();
      router.push(`/knowledge/${ds.id}`);
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <Modal
      open={open}
      title="新建知识库"
      onOk={submit}
      onCancel={onClose}
      confirmLoading={create.isPending}
      destroyOnHidden
      width={560}
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{ icon: "📚", permission: "only_me" as Permission, doc_form: "text_model" as DocForm, review_required: false }}
      >
        <div style={{ display: "flex", gap: 12 }}>
          <Form.Item name="icon" label="图标" style={{ width: 80 }}>
            <Input maxLength={4} />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请填写名称" }]} style={{ flex: 1 }}>
            <Input maxLength={255} placeholder="如：客服 FAQ" />
          </Form.Item>
        </div>
        <Form.Item
          name="description"
          label="描述"
          extra="智能体会根据描述判断什么时候来这里查"
        >
          <Input.TextArea rows={2} maxLength={2000} />
        </Form.Item>
        <Form.Item name="doc_form" label="引用方式" extra="上传文档后不能再改">
          <Radio.Group>
            <Radio value="text_model">按段落引用（适合大多数文档）</Radio>
            <Radio value="hierarchical_model">按章节引用（适合长文档，上下文更完整）</Radio>
          </Radio.Group>
        </Form.Item>
        <Form.Item name="permission" label="谁能用" extra="之后可以在知识库的「共享」里调整">
          <Radio.Group
            options={(["only_me", "all_team"] as Permission[]).map((v) => ({ value: v, label: PERMISSION_LABEL[v] }))}
          />
        </Form.Item>
        <Form.Item
          name="review_required"
          label="新文档审核后才能被查到"
          valuePropName="checked"
          extra="适合给客户用的知识库，避免未经确认的内容被引用"
        >
          <Switch />
        </Form.Item>
      </Form>
    </Modal>
  );
}
