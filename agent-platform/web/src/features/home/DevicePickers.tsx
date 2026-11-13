"use client";

/**
 * 新建本机 CLI 对话时选电脑和项目目录（agent-platform/doc/design/device-cli-design.html §2.3）。
 *
 * ★ 电脑只能选自己的、在线的、装了这种 CLI 的；默认选中默认设备。
 * ★ 目录默认是「默认项目地址 / 对话 id」（一个新的空目录）；也可以改选电脑上已有的项目（每个允许目录
 *   下一层的子目录名，标出 git 仓库），或手动输入。都必须在本机允许的目录里，最终由电脑上的连接器判。
 * ★ 建对话时定下，之后不能换：CLI 的会话记录在那台电脑上，换电脑或目录就接不上之前的上下文。
 */
import { Alert, Dropdown, Input, Modal, Spin } from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { type DeviceChoice, useDeviceChoices, useDeviceProjects } from "@/api/devices";
import { Icon } from "@/components/Icon";
import chat from "@atlas/chat-ui/src/chat/chat.module.css";

/** 选中的电脑和目录。dir 为空 = 新目录（默认项目地址 / 对话 id） */
export interface DevicePick {
  deviceId?: string;
  dir?: string;
}

/** 首选：默认设备能选就是它，否则第一台能选的 */
export function preferredDevice(choices: DeviceChoice[]): DeviceChoice | undefined {
  return choices.find((c) => c.is_default && c.selectable) ?? choices.find((c) => c.selectable);
}

/** 为什么选不了（给出去哪里处理） */
function blocked(c: DeviceChoice, cliType: string): string {
  if (!c.online) return "不在线";
  if (!c.has_cli) return `没装 ${cliType}`;
  return "";
}

/** 路径在不在某个允许目录里：按路径段比（只是提示，连接器才是权威：它展开 ~、解析符号链接后再判） */
function within(path: string, roots: string[]): boolean {
  const parts = (p: string) => p.trim().split("/").filter(Boolean);
  const target = parts(path);
  return roots.some((r) => {
    const root = parts(r);
    return root.length <= target.length && root.every((seg, i) => seg === target[i]);
  });
}

/** 菜单里几项操作的 key（项目目录的 key 是它的路径） */
const MANAGE_DEVICES = "manage-devices";
const NEW_DIR = "new-dir";
const TYPE_DIR = "type-dir";

export function DevicePickers({
  cliType,
  defaultProjectDir,
  value,
  onChange,
}: {
  cliType: string;
  defaultProjectDir: string;
  value: DevicePick;
  onChange: (pick: DevicePick) => void;
}) {
  const router = useRouter();
  const choicesQ = useDeviceChoices(cliType);
  const choices = choicesQ.data ?? [];
  const device = choices.find((c) => c.id === value.deviceId);
  const [listing, setListing] = useState(false);
  const projectsQ = useDeviceProjects(device?.id, listing);
  const [typing, setTyping] = useState<string | null>(null);

  // 还没选、或选的那台不能用了：换成首选
  useEffect(() => {
    if (choicesQ.data && !device?.selectable) {
      const next = preferredDevice(choicesQ.data);
      if (next?.id !== value.deviceId) onChange({ deviceId: next?.id, dir: undefined });
    }
  }, [choicesQ.data, device?.selectable, value.deviceId, onChange]);

  if (choicesQ.isPending) return <Spin size="small" />;

  const roots = device?.allowed_roots ?? [];
  const projectPaths = new Set((projectsQ.data?.roots ?? []).flatMap((r) => r.entries.map((e) => e.path)));
  const typedOk = typing === null || !typing.trim() || within(typing, roots);

  return (
    <>
      <Dropdown
        trigger={["click"]}
        menu={{
          selectable: true,
          selectedKeys: device ? [device.id] : [],
          items: [
            { key: "title", type: "group", label: "在哪台电脑上运行" },
            ...choices.map((c) => ({
              key: c.id,
              disabled: !c.selectable,
              label: (
                <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                  <Dot on={c.online} />
                  {c.name}
                  {c.is_default && <small style={{ color: "var(--fg-3)" }}>默认</small>}
                  {!c.selectable && <small style={{ color: "var(--fg-3)" }}>{blocked(c, cliType)}</small>}
                </span>
              ),
            })),
            { type: "divider" },
            { key: MANAGE_DEVICES, icon: <Icon name="laptop" size={14} />, label: "管理我的设备" },
          ],
          onClick: ({ key }) => {
            if (key === MANAGE_DEVICES) router.push("/devices");
            else onChange({ deviceId: key, dir: undefined });
          },
        }}
      >
        <button type="button" className={chat.pill} aria-label="选择电脑">
          <Icon name="laptop" size={14} />
          {device ? (
            <>
              <Dot on={device.online} />
              {device.name}
            </>
          ) : (
            "没有可用的电脑"
          )}
          <Icon name="down" size={13} />
        </button>
      </Dropdown>

      {device?.selectable && (
        <Dropdown
          trigger={["click"]}
          onOpenChange={(o) => o && setListing(true)}
          menu={{
            selectable: true,
            selectedKeys: [value.dir ?? NEW_DIR],
            items: [
              {
                key: NEW_DIR,
                icon: <Icon name="plus" size={14} />,
                label: (
                  <span>
                    新目录 <small style={{ color: "var(--fg-3)", fontFamily: "var(--mono)" }}>{defaultProjectDir}/对话 id</small>
                  </span>
                ),
              },
              { type: "divider" },
              ...(projectsQ.isPending
                ? [{ key: "loading", disabled: true, label: "正在列电脑上的项目…" }]
                : projectsQ.error
                  ? [{ key: "error", disabled: true, label: projectsQ.error.message }]
                  : (projectsQ.data?.roots ?? []).map((r) => ({
                      key: `root:${r.root}`,
                      type: "group" as const,
                      label: r.root + (r.exists ? "" : "（不存在）"),
                      children: r.entries.map((e) => ({
                        key: e.path,
                        icon: <Icon name="folder" size={14} />,
                        label: (
                          <span>
                            {e.name}
                            {e.is_repo && <small style={{ color: "var(--fg-3)", marginLeft: 6 }}>git</small>}
                          </span>
                        ),
                      })),
                    }))),
              { type: "divider" },
              { key: TYPE_DIR, icon: <Icon name="pencil" size={14} />, label: "输入路径…" },
            ],
            onClick: ({ key }) => {
              if (key === NEW_DIR) onChange({ ...value, dir: undefined });
              else if (key === TYPE_DIR) setTyping(value.dir ?? "");
              else if (projectPaths.has(key)) onChange({ ...value, dir: key });
            },
          }}
        >
          <button type="button" className={chat.pill} aria-label="选择项目目录" title={value.dir ?? `${defaultProjectDir}/对话 id`}>
            <Icon name="folder" size={14} />
            <span style={{ fontFamily: "var(--mono)", maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis" }}>
              {value.dir ?? "新目录"}
            </span>
            <Icon name="down" size={13} />
          </button>
        </Dropdown>
      )}

      <Modal
        open={typing !== null}
        title="输入项目目录"
        okText="使用"
        cancelText="取消"
        okButtonProps={{ disabled: !typing?.trim() || !typedOk }}
        onCancel={() => setTyping(null)}
        onOk={() => {
          onChange({ ...value, dir: typing?.trim().replace(/\/+$/, "") || undefined });
          setTyping(null);
        }}
      >
        <div style={{ fontSize: 12.5, color: "var(--fg-3)", marginBottom: 6 }}>在 {device?.name} 上的路径</div>
        <Input
          autoFocus
          style={{ fontFamily: "var(--mono)" }}
          value={typing ?? ""}
          placeholder="~/code/my-project"
          onChange={(e) => setTyping(e.target.value)}
        />
        {!typedOk && (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: 10 }}
            message={`不在这台电脑允许的目录里（${roots.join("、") || "没有配置"}）`}
            description="要用这个目录，先在电脑上把它加进本机策略（~/.atlas/connect.toml 的 allowed_roots），再重启连接器。"
          />
        )}
      </Modal>
    </>
  );
}

/** 输入框下面的一行：没有可用的电脑时说明原因；有的话提示 CLI 以谁的身份在哪里工作 */
export function DeviceHint({ cliType, pick }: { cliType: string; pick: DevicePick }) {
  const choicesQ = useDeviceChoices(cliType);
  const choices = choicesQ.data ?? [];
  const device = choices.find((c) => c.id === pick.deviceId);
  if (choicesQ.isPending) return null;
  if (!choices.length)
    return (
      <Alert
        type="info"
        showIcon
        message="这个智能体在你自己的电脑上运行，你还没有配对电脑"
        action={<Link href="/devices">去添加</Link>}
      />
    );
  if (!device?.selectable)
    return (
      <Alert
        type="warning"
        showIcon
        message={
          choices.some((c) => c.online)
            ? `在线的电脑上没有装 ${cliType}：在电脑上运行 atlas-connect doctor 查看怎么装`
            : "你的电脑都不在线：在电脑上运行 atlas-connect run 启动连接器"
        }
        action={<Link href="/devices">我的设备</Link>}
      />
    );
  return (
    <span style={{ fontSize: 12, color: "var(--fg-3)", display: "inline-flex", gap: 6, alignItems: "center" }}>
      <Icon name="laptop" size={13} />
      CLI 将以你的身份在「{device.name}」上工作，能读写这台电脑上你能访问的文件；电脑和目录建好对话后不能换
    </span>
  );
}

function Dot({ on }: { on: boolean }) {
  return (
    <span
      aria-hidden="true"
      style={{
        width: 7,
        height: 7,
        borderRadius: "50%",
        display: "inline-block",
        background: on ? "var(--success, #16a34a)" : "var(--fg-4, #b5b5c3)",
      }}
    />
  );
}
