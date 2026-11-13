"use client";

/**
 * 我的设备（agent-platform/doc/design/device-cli-design.html §2.2 · §3.4）。
 *
 * 一个人配对到平台的电脑：本机 CLI 智能体跑在这里。卡片上是连接器上线时报告的系统、版本、装了哪些 CLI、
 * 本机允许平台使用的目录（只读：电脑主人在电脑上改）。
 *
 * ★ 添加设备 = 一次性配对码（10 分钟、只能用一次）+ 在电脑上运行的命令 + 等电脑连上（3 秒刷新一次）。
 *   第一台配对的自动成为默认设备：被委派、调试预览、新建对话时预选它。
 * ★ 吊销立即生效：电脑断开、令牌作废，在这台电脑上进行中的对话中断。
 */
import { Alert, App as AntdApp, Button, Dropdown, Empty, Input, Modal, Skeleton, Tag, Typography } from "antd";
import { useCallback, useEffect, useState } from "react";

import {
  type Device,
  type PairingCode,
  deviceStatusText,
  useDeviceActions,
  useDevices,
  useStartPairing,
} from "@/api/devices";
import { Icon } from "@/components/Icon";
import pageStyles from "@/features/agents/agents.module.css";
import { relativeTime } from "@/lib/format";

/** 权限模式上限的中文（连接器报告的是 ACP modeId） */
const MODE_NAMES: Record<string, string> = {
  auto: "自动",
  acceptEdits: "接受编辑",
  default: "手动",
  plan: "仅规划",
};

export function DevicesPage() {
  const { modal, message } = AntdApp.useApp();
  const [pairing, setPairing] = useState(false);
  const devicesQ = useDevices({ pairing });
  const actions = useDeviceActions();
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);
  const devices = devicesQ.data?.data ?? [];

  const revoke = (d: Device) =>
    modal.confirm({
      title: `吊销「${d.name}」？`,
      content:
        "吊销后这台电脑立即断开，令牌作废；在它上面进行中的对话会中断，已有对话的文件也看不到了。" +
        "以后要再用，需要重新配对。",
      okText: "吊销",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () =>
        actions.revoke.mutateAsync(d.id).then(
          () => message.success("已吊销"),
          (e: Error) => {
            message.error(e.message);
          },
        ),
    });

  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <div>
            <h1 className={pageStyles.title}>我的设备</h1>
            <p className={pageStyles.desc}>
              本机 CLI 智能体跑在你自己的电脑上：在电脑上装好连接器、配对后，就能在网页上让它改你电脑上的项目。
            </p>
          </div>
          <Button
            className={pageStyles.action}
            type="primary"
            icon={<Icon name="plus" size={14} />}
            onClick={() => setPairing(true)}
          >
            添加设备
          </Button>
        </div>
      </header>
      <div className={pageStyles.body}>
        {devicesQ.isPending && <Skeleton active paragraph={{ rows: 4 }} />}
        {devicesQ.error && <Alert type="error" showIcon message={devicesQ.error.message} />}
        {devicesQ.data && devices.length === 0 && (
          <Empty description="还没有配对电脑" image={Empty.PRESENTED_IMAGE_SIMPLE}>
            <Button type="primary" onClick={() => setPairing(true)}>
              添加设备
            </Button>
          </Empty>
        )}
        <div className={pageStyles.grid}>
          {devices.map((d) => (
            <DeviceCard
              key={d.id}
              device={d}
              onRename={() => setRenaming({ id: d.id, name: d.name })}
              onDefault={() =>
                actions.setDefault.mutate(d.id, {
                  onSuccess: () => message.success(`「${d.name}」已设为默认设备`),
                  onError: (e) => message.error(e.message),
                })
              }
              onRevoke={() => revoke(d)}
            />
          ))}
        </div>
      </div>

      {pairing && <PairingModal devices={devices} onClose={() => setPairing(false)} />}

      <Modal
        open={renaming !== null}
        title="重命名设备"
        okText="保存"
        cancelText="取消"
        confirmLoading={actions.rename.isPending}
        onCancel={() => setRenaming(null)}
        onOk={() =>
          renaming &&
          actions.rename.mutate(
            { id: renaming.id, name: renaming.name.trim() },
            { onSuccess: () => setRenaming(null), onError: (e) => message.error(e.message) },
          )
        }
      >
        <Input
          value={renaming?.name ?? ""}
          maxLength={128}
          autoFocus
          onChange={(e) => renaming && setRenaming({ ...renaming, name: e.target.value })}
        />
      </Modal>
    </div>
  );
}

function DeviceCard({
  device: d,
  onRename,
  onDefault,
  onRevoke,
}: {
  device: Device;
  onRename: () => void;
  onDefault: () => void;
  onRevoke: () => void;
}) {
  const online = d.status === "online";
  const policy = d.policy as {
    allowed_roots?: string[];
    max_mode?: string;
    max_sessions?: number;
    remote_files?: boolean;
  };
  const clis = Object.entries(d.clis ?? {});
  return (
    <div className={pageStyles.card} style={{ cursor: "default" }}>
      <div className={pageStyles.cardHead}>
        <span className={pageStyles.avatar} style={{ background: online ? "var(--primary)" : "var(--fg-4, #b5b5c3)" }}>
          <Icon name="laptop" size={20} />
        </span>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className={pageStyles.cardName}>
            {d.name}
            {d.is_default && <Tag color="purple">默认</Tag>}
          </div>
          <div className={pageStyles.cardSub}>
            <StatusDot online={online} />
            {online ? deviceStatusText(d.status) : `不在线 · 最近在线 ${relativeTime(d.last_seen_at)}`}
          </div>
        </div>
        <Dropdown
          trigger={["click"]}
          placement="bottomRight"
          menu={{
            items: [
              { key: "rename", icon: <Icon name="pencil" size={14} />, label: "重命名" },
              ...(d.is_default ? [] : [{ key: "default", icon: <Icon name="star" size={14} />, label: "设为默认" }]),
              { type: "divider" as const },
              { key: "revoke", icon: <Icon name="trash" size={14} />, label: "吊销", danger: true },
            ],
            onClick: ({ key }) => (key === "rename" ? onRename() : key === "default" ? onDefault() : onRevoke()),
          }}
        >
          <Button size="small" type="text" icon={<Icon name="more" size={16} />} aria-label={`${d.name} 的操作`} />
        </Dropdown>
      </div>

      <dl style={{ margin: 0, display: "grid", gridTemplateColumns: "84px 1fr", rowGap: 6, fontSize: 12.5 }}>
        <dt style={{ color: "var(--fg-3)" }}>系统</dt>
        <dd style={{ margin: 0 }}>{d.os || "—"}</dd>
        <dt style={{ color: "var(--fg-3)" }}>连接器</dt>
        <dd style={{ margin: 0 }}>{d.connector_version || "—"}</dd>
        <dt style={{ color: "var(--fg-3)" }}>CLI</dt>
        <dd style={{ margin: 0 }}>
          {!online ? (
            <span style={{ color: "var(--fg-3)" }}>电脑在线时显示</span>
          ) : clis.length ? (
            clis.map(([name, version]) => (
              <Tag key={name} style={{ marginBottom: 2 }}>
                {name} <span style={{ color: "var(--fg-3)" }}>{version}</span>
              </Tag>
            ))
          ) : (
            <span style={{ color: "var(--warning, #b45309)" }}>一个都没装：在电脑上运行 atlas-connect doctor</span>
          )}
        </dd>
        {online && (
          <>
            <dt style={{ color: "var(--fg-3)" }}>允许的目录</dt>
            <dd style={{ margin: 0, fontFamily: "var(--mono)", wordBreak: "break-all" }}>
              {(policy.allowed_roots ?? []).join("、") || "—"}
            </dd>
            <dt style={{ color: "var(--fg-3)" }}>本机上限</dt>
            <dd style={{ margin: 0 }}>
              权限模式最高「{MODE_NAMES[policy.max_mode ?? ""] ?? policy.max_mode ?? "—"}」 · 同时{" "}
              {policy.max_sessions ?? "—"} 个对话 · 远程查看文件{policy.remote_files === false ? "已关闭" : "已开启"}
            </dd>
          </>
        )}
      </dl>
      <div className={pageStyles.cardFoot}>
        <span>配对于 {relativeTime(d.paired_at)}</span>
        {online && <span style={{ marginLeft: "auto" }}>本机策略在电脑上改</span>}
      </div>
    </div>
  );
}

function StatusDot({ online }: { online: boolean }) {
  return (
    <span
      aria-hidden="true"
      style={{
        display: "inline-block",
        width: 7,
        height: 7,
        borderRadius: "50%",
        marginRight: 6,
        verticalAlign: "middle",
        background: online ? "var(--success, #16a34a)" : "var(--fg-4, #b5b5c3)",
      }}
    />
  );
}

/** 平台的对外地址：连接器连 {它}/device-api */
function platformUrl(): string {
  return typeof window === "undefined" ? "" : window.location.origin;
}

function PairingModal({ devices, onClose }: { devices: Device[]; onClose: () => void }) {
  const start = useStartPairing();
  const [code, setCode] = useState<PairingCode>();
  const [now, setNow] = useState(() => Date.now());
  // 打开时已有的设备：之后出现的新设备就是这次配对的
  const [before] = useState(() => new Set(devices.map((d) => d.id)));
  const paired = devices.find((d) => !before.has(d.id));

  const { mutate } = start;
  const issue = useCallback(() => mutate(undefined, { onSuccess: setCode }), [mutate]);
  useEffect(() => issue(), [issue]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const left = code ? Math.max(0, Math.round((new Date(code.expires_at).getTime() - now) / 1000)) : 0;
  const command = code ? `atlas-connect pair ${platformUrl()} --code ${code.code}` : "";

  return (
    <Modal open title="添加设备" footer={<Button onClick={onClose}>{paired ? "完成" : "关闭"}</Button>} onCancel={onClose} width={560}>
      {paired ? (
        <Alert
          type="success"
          showIcon
          message={`「${paired.name}」已配对${paired.is_default ? "，并设为默认设备" : ""}`}
          description={
            paired.status === "online"
              ? "电脑已经连上，可以新建本机 CLI 对话了。"
              : "在电脑上运行 atlas-connect run 让它连上平台（之后开机运行它即可）。"
          }
        />
      ) : (
        <ol style={{ paddingLeft: 18, margin: 0, display: "grid", gap: 14, fontSize: 13 }}>
          <li>
            在电脑上安装连接器 <b>atlas-connect</b>（macOS、Linux；Windows 请在 WSL 里装），以及要用的 CLI
            和它的适配器。装好后运行 <code>atlas-connect doctor</code> 检查缺什么。
          </li>
          <li>
            在电脑上运行下面的命令配对，然后运行 <code>atlas-connect run</code> 连上平台：
            {start.isPending && <Skeleton active paragraph={{ rows: 1 }} />}
            {start.error && <Alert type="error" showIcon message={start.error.message} style={{ marginTop: 8 }} />}
            {code && (
              <>
                <Typography.Paragraph
                  copyable={{ text: command }}
                  style={{
                    margin: "8px 0 4px",
                    padding: "8px 10px",
                    background: "var(--bg-2)",
                    borderRadius: 8,
                    fontFamily: "var(--mono)",
                    fontSize: 12.5,
                  }}
                >
                  {command}
                </Typography.Paragraph>
                <div style={{ color: "var(--fg-3)", fontSize: 12 }}>
                  配对码 <b style={{ fontFamily: "var(--mono)", color: "var(--fg-1)" }}>{code.code}</b>
                  {left > 0 ? (
                    <> · {Math.floor(left / 60)} 分 {left % 60} 秒内有效，只能用一次</>
                  ) : (
                    <>
                      {" "}
                      · 已过期{" "}
                      <Button size="small" type="link" onClick={issue}>
                        换一个
                      </Button>
                    </>
                  )}
                </div>
              </>
            )}
          </li>
          <li style={{ color: "var(--fg-3)" }}>
            <Icon name="clock" size={13} style={{ verticalAlign: -2 }} /> 等电脑配对……配对成功后这里会自动更新。
          </li>
        </ol>
      )}
    </Modal>
  );
}
