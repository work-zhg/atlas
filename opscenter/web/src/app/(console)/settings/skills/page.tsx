"use client";

/**
 * 技能市场：产品自带的技能（浏览器、computer use……）发布在这里，不属于任何租户。租户从市场拷贝；
 * 标成「默认安装」的，开通租户时自动装进去。下架只影响以后的安装，已经装进租户的是租户自己的。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Checkbox, Input, Modal, Space, Switch, Table, Tag, Typography, Upload } from "antd";
import { useState } from "react";

import { ErrorBox, PageHead, errorText } from "@/components/common";
import { sys } from "@/lib/api";
import { bytes, dateTime } from "@/lib/format";
import { useMe, useSystems, withCapability } from "@/lib/session";
import type { MarketSkill, MarketVersion } from "@/lib/types";

export default function SkillsPage() {
  const { data: systems } = useSystems();
  const market = withCapability(systems, "skill_market")[0];
  if (!market) return <PageHead title="技能市场" sub="没有登记技能应用" />;
  return <Market code={market.code} />;
}

function Market({ code }: { code: string }) {
  const ops = sys(code);
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const { data: me } = useMe();
  const admin = me?.role === "ADMIN";
  const key = ["market", code];
  const q = useQuery({ queryKey: key, queryFn: () => ops.get<MarketSkill[]>("/market/skills") });
  const [upload, setUpload] = useState(false);

  async function act(fn: () => Promise<unknown>, ok: string) {
    try {
      await fn();
      message.success(ok);
      await qc.invalidateQueries({ queryKey: key });
    } catch (e) {
      message.error(errorText(e));
    }
  }

  function revoke(v: MarketVersion) {
    let reason = "";
    modal.confirm({
      title: `下架 ${v.slug} v${v.version}`,
      content: <Input.TextArea rows={3} placeholder="原因（必填）" onChange={(e) => (reason = e.target.value)} />,
      okButtonProps: { danger: true },
      onOk: () => act(() => ops.post(`/market/skills/${v.slug}/versions/${v.version}/revoke`, { reason }), "已下架"),
    });
  }

  return (
    <>
      <PageHead
        title="技能市场"
        sub="产品自带的技能，不属于任何租户；租户从这里拷贝，开通租户时自动装「默认安装」的"
        extra={
          admin && (
            <Button type="primary" onClick={() => setUpload(true)}>
              发布技能
            </Button>
          )
        }
      />
      <ErrorBox error={q.error} />
      <Table<MarketSkill>
        rowKey="slug"
        loading={q.isLoading}
        dataSource={q.data ?? []}
        pagination={false}
        expandable={{
          expandedRowRender: (s) => (
            <Table<MarketVersion>
              rowKey="version"
              size="small"
              pagination={false}
              dataSource={s.versions}
              columns={[
                { title: "版本", dataIndex: "version", render: (v: number) => `v${v}` },
                {
                  title: "状态",
                  render: (_, v) =>
                    v.status === "published" ? <Tag color="green">已发布</Tag> : <Tag title={v.status_reason ?? ""}>已下架</Tag>,
                },
                { title: "大小", render: (_, v) => `${bytes(v.size_bytes)} · ${v.file_count} 个文件${v.has_scripts ? " · 含脚本" : ""}` },
                { title: "发布", render: (_, v) => `${v.published_by} · ${dateTime(v.published_at)}` },
                {
                  title: "",
                  render: (_, v) =>
                    admin &&
                    v.status === "published" && (
                      <Button size="small" type="link" danger onClick={() => revoke(v)}>
                        下架
                      </Button>
                    ),
                },
              ]}
            />
          ),
        }}
        columns={[
          { title: "技能", dataIndex: "slug", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
          { title: "说明", dataIndex: "description" },
          { title: "最新版本", dataIndex: "latest", render: (v: number | null) => (v ? `v${v}` : "—") },
          {
            title: "开通租户时默认安装",
            render: (_, s) => (
              <Switch
                size="small"
                checked={s.install_by_default}
                disabled={!admin}
                onChange={(on) => act(() => ops.patch(`/market/skills/${s.slug}`, { install_by_default: on }), "已保存")}
              />
            ),
          },
        ]}
      />
      {upload && <Publish system={code} onClose={() => setUpload(false)} onDone={() => qc.invalidateQueries({ queryKey: key })} />}
    </>
  );
}

function Publish({ system, onClose, onDone }: { system: string; onClose: () => void; onDone: () => Promise<unknown> }) {
  const [file, setFile] = useState<File | null>(null);
  const [byDefault, setByDefault] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("install_by_default", String(byDefault));
      await sys(system).post("/market/skills", form);
      await onDone();
      onClose();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open title="发布技能到市场" onCancel={onClose} onOk={submit} okButtonProps={{ disabled: !file }} confirmLoading={busy}>
      <ErrorBox error={error} />
      <Space direction="vertical" style={{ width: "100%" }}>
        <Upload.Dragger
          accept=".zip"
          maxCount={1}
          beforeUpload={(f) => {
            setFile(f);
            return false;
          }}
          onRemove={() => setFile(null)}
        >
          <p>把技能包（zip，含 SKILL.md）拖到这里，或点击选择</p>
        </Upload.Dragger>
        <Checkbox checked={byDefault} onChange={(e) => setByDefault(e.target.checked)}>
          开通租户时默认安装
        </Checkbox>
        <Typography.Text type="secondary">同名技能会发布成新版本；发布前会做安全扫描。</Typography.Text>
      </Space>
    </Modal>
  );
}
