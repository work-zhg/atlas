"use client";

/**
 * 技能市场：产品自带的技能（浏览器、computer use……）发布在这里，不属于任何企业。企业从市场拷贝；
 * 标成「默认安装」的，开通租户时自动装进去。下架只影响以后的安装，已经装进企业的是企业自己的。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Checkbox, Empty, Input, Modal, Switch, Table, Tag, Tooltip, Upload } from "antd";
import { useState } from "react";

import { ErrorBox, OBSERVER_TIP, PageHead, SquareIcon, colorOf, errorText } from "@/components/common";
import { Icon } from "@/components/Icon";
import { sys } from "@/lib/api";
import { bytes, dateTime } from "@/lib/format";
import { useMe, useSystems, withCapability } from "@/lib/session";
import type { MarketSkill, MarketVersion } from "@/lib/types";

export default function SkillsPage() {
  const { data: systems } = useSystems();
  const market = withCapability(systems, "skill_market")[0];
  if (systems && !market)
    return (
      <>
        <PageHead title="技能市场" />
        <Empty description="没有登记技能应用" />
      </>
    );
  return market ? <Market code={market.code} /> : null;
}

function Market({ code }: { code: string }) {
  const ops = sys(code);
  const qc = useQueryClient();
  const { message } = App.useApp();
  const { data: me } = useMe();
  const admin = me?.role === "ADMIN";
  const key = ["market", code];
  const q = useQuery({ queryKey: key, queryFn: () => ops.get<MarketSkill[]>("/market/skills") });
  const [upload, setUpload] = useState(false);
  const [revoking, setRevoking] = useState<MarketVersion | null>(null);

  async function act(fn: () => Promise<unknown>, ok: string) {
    try {
      await fn();
      message.success(ok);
      await qc.invalidateQueries({ queryKey: key });
      return true;
    } catch (e) {
      message.error(errorText(e));
      return false;
    }
  }

  return (
    <>
      <PageHead
        title="技能市场"
        sub="平台提供给所有企业的技能。企业管理员在自己的技能里安装；开了「开通时默认安装」的，新开通的企业自动装上"
        extra={
          <Tooltip title={admin ? undefined : OBSERVER_TIP}>
            <Button type="primary" icon={<Icon name="upload" />} disabled={!admin} onClick={() => setUpload(true)}>
              发布技能
            </Button>
          </Tooltip>
        }
      />
      <ErrorBox error={q.error} />
      <Table<MarketSkill>
        rowKey="slug"
        loading={q.isLoading}
        dataSource={q.data ?? []}
        pagination={false}
        expandable={{
          expandRowByClick: true,
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
                    v.status === "published" ? (
                      <Tag color="success">已发布</Tag>
                    ) : (
                      <div>
                        <Tag>已下架</Tag>
                        {v.status_reason && <div className="ops-sub">{v.status_reason}</div>}
                      </div>
                    ),
                },
                {
                  title: "内容",
                  render: (_, v) => (
                    <span className="ops-muted">
                      {bytes(v.size_bytes)} · {v.file_count} 个文件{v.has_scripts && " · 含脚本"}
                    </span>
                  ),
                },
                { title: "发布", render: (_, v) => <span className="ops-muted">{v.published_by} · {dateTime(v.published_at)}</span> },
                {
                  title: "",
                  align: "right",
                  render: (_, v) =>
                    v.status === "published" && (
                      <Button size="small" type="link" danger disabled={!admin} onClick={() => setRevoking(v)}>
                        下架
                      </Button>
                    ),
                },
              ]}
            />
          ),
        }}
        columns={[
          {
            title: "技能",
            render: (_, s) => (
              <div className="ops-row" style={{ gap: 10 }}>
                <SquareIcon icon="zap" color={colorOf(s.slug)} size={30} />
                <div>
                  <b className="ops-mono">{s.slug}</b>
                  <div className="ops-sub">{s.description || "没有说明"}</div>
                </div>
              </div>
            ),
          },
          {
            title: "最新版本",
            render: (_, s) => {
              const latest = s.versions.find((v) => v.version === s.latest);
              return s.latest ? (
                <span className="ops-nw">
                  v{s.latest} {latest?.has_scripts && <Tag color="warning">含脚本</Tag>}
                </span>
              ) : (
                <span className="ops-muted">都已下架</span>
              );
            },
          },
          {
            title: "开通时默认安装",
            width: 150,
            render: (_, s) => (
              <span onClick={(e) => e.stopPropagation()}>
                <Tooltip title={admin ? undefined : OBSERVER_TIP}>
                  <Switch
                    checked={s.install_by_default}
                    disabled={!admin}
                    onChange={(on) => void act(() => ops.patch(`/market/skills/${s.slug}`, { install_by_default: on }), on ? "新开通的企业会自动装上" : "已取消默认安装")}
                  />
                </Tooltip>
              </span>
            ),
          },
        ]}
      />
      {upload && <Publish system={code} onClose={() => setUpload(false)} onDone={() => qc.invalidateQueries({ queryKey: key })} />}
      {revoking && (
        <Revoke
          version={revoking}
          onClose={() => setRevoking(null)}
          onOk={(reason) => act(() => ops.post(`/market/skills/${revoking.slug}/versions/${revoking.version}/revoke`, { reason }), "已下架")}
        />
      )}
    </>
  );
}

function Revoke({ version: v, onClose, onOk }: { version: MarketVersion; onClose: () => void; onOk: (reason: string) => Promise<boolean> }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <Modal
      open
      title={`下架 ${v.slug} v${v.version}`}
      okText="下架"
      okButtonProps={{ danger: true, disabled: !reason.trim() }}
      confirmLoading={busy}
      onCancel={onClose}
      onOk={async () => {
        setBusy(true);
        if (await onOk(reason.trim())) onClose();
        setBusy(false);
      }}
    >
      <Alert type="warning" showIcon style={{ marginBottom: 14 }} message="已经装了这个版本的企业照常能用；市场里不再提供，以后安装会装别的版本" />
      <div className="ops-sub" style={{ marginBottom: 6 }}>
        原因（必填，企业管理员看得到）
      </div>
      <Input.TextArea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
    </Modal>
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
    <Modal open title="发布技能" okText="发布" onCancel={onClose} onOk={() => void submit()} okButtonProps={{ disabled: !file }} confirmLoading={busy}>
      <ErrorBox error={error} />
      <Upload.Dragger
        accept=".zip"
        maxCount={1}
        beforeUpload={(f) => {
          setFile(f);
          return false;
        }}
        onRemove={() => setFile(null)}
      >
        <p style={{ color: "var(--primary)" }}>
          <Icon name="upload" size={26} />
        </p>
        <p>把技能包（.zip）拖到这里，或点击选择</p>
        <p className="ops-sub">包里要有 SKILL.md；同名技能发成新版本，旧版本留着</p>
      </Upload.Dragger>
      <Checkbox checked={byDefault} onChange={(e) => setByDefault(e.target.checked)} style={{ marginTop: 14 }}>
        开通时默认安装
      </Checkbox>
      <div className="ops-sub" style={{ marginTop: 8 }}>
        发布前会做安全扫描；含可执行脚本的会标「含脚本」，企业管理员安装时看得到
      </div>
    </Modal>
  );
}
