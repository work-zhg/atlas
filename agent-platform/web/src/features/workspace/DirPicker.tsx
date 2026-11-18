"use client";

/**
 * 新对话的工作目录：首页输入框里选（原生智能体、会话 Pod 里的 CLI 智能体；本机 CLI 选的是电脑上的目录，
 * 见 features/home/DevicePickers）。对话里去工作空间的入口在文件抽屉的头部。
 *
 * ★ 默认是新目录：后端建对话时给 {日期}/{对话 id}。也可以选工作空间里已有的目录 —— 菜单里列第一层，
 *   更深的在「浏览更多目录」里一层层点进去选。
 * ★ 建对话时定下，之后不能换（它是沙箱里的 cwd）。不能选根目录、隐藏目录（后端也校验）。
 */
import { Alert, Dropdown, Modal, Spin } from "antd";
import { useState } from "react";

import { useWorkspaceDir } from "@/api/workspace";
import { Icon } from "@/components/Icon";
import chat from "@atlas/chat-ui/src/chat/chat.module.css";

import styles from "./workspace.module.css";

export const workspaceHref = (dir: string) => `/workspace?dir=${encodeURIComponent(dir)}`;

/**
 * 菜单里不是目录的几项的 key（目录的 key 是它的路径）。
 * 都以 . 开头：列出来的目录不含隐藏目录，不会和这几项撞名。
 */
const NEW_DIR = ".new-dir";
const BROWSE = ".browse";
const GROUP = ".workspace";

/** value 为空串 = 新目录 */
export function DirPicker({ value, onChange }: { value: string; onChange: (dir: string) => void }) {
  const [listing, setListing] = useState(false);
  const top = useWorkspaceDir("", false, listing);
  const [browsing, setBrowsing] = useState<string | null>(null);

  const folders = top.data?.dirs ?? [];
  const paths = new Set(folders.map((f) => f.path));
  // 选中的是更深的目录（从「浏览」或工作空间页来的）：放在第一层前面，菜单里才显示得出选中
  const deeper = value && !paths.has(value) ? [value] : [];

  return (
    <>
      <Dropdown
        trigger={["click"]}
        onOpenChange={(o) => o && setListing(true)}
        menu={{
          selectable: true,
          selectedKeys: [value || NEW_DIR],
          style: { maxHeight: 420, overflowY: "auto" },
          items: [
            {
              key: NEW_DIR,
              icon: <Icon name="plus" size={14} />,
              label: (
                <span>
                  新目录 <small style={{ color: "var(--fg-3)", fontFamily: "var(--mono)" }}>日期/对话 id</small>
                </span>
              ),
            },
            { type: "divider" },
            top.isPending
              ? { key: ".loading", disabled: true, label: "正在列工作空间里的目录…" }
              : top.error
                ? { key: ".error", disabled: true, label: top.error.message }
                : {
                    key: GROUP,
                    type: "group" as const,
                    label: folders.length || deeper.length ? "工作空间" : "工作空间里还没有目录",
                    children: [...deeper, ...folders.map((f) => f.path)].map((path) => ({
                      key: path,
                      icon: <Icon name="folder" size={14} />,
                      label: path,
                    })),
                  },
            { type: "divider" },
            { key: BROWSE, icon: <Icon name="search" size={14} />, label: "浏览更多目录…" },
          ],
          onClick: ({ key }) => {
            if (key === NEW_DIR) onChange("");
            else if (key === BROWSE) setBrowsing(value);
            else if (paths.has(key) || deeper.includes(key)) onChange(key);
          },
        }}
      >
        <button type="button" className={chat.pill} aria-label="选择工作目录" title={value || "新目录：日期/对话 id"}>
          <Icon name="folder" size={14} />
          <span style={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis" }}>{value || "新目录"}</span>
          <Icon name="down" size={13} />
        </button>
      </Dropdown>

      {browsing !== null && (
        <DirBrowser
          start={browsing}
          onCancel={() => setBrowsing(null)}
          onPick={(dir) => {
            onChange(dir);
            setBrowsing(null);
          }}
        />
      )}
    </>
  );
}

/** 一层层点进去选目录；当前所在的目录就是要用的那个 */
function DirBrowser({
  start,
  onCancel,
  onPick,
}: {
  start: string;
  onCancel: () => void;
  onPick: (dir: string) => void;
}) {
  const [dir, setDir] = useState(start);
  const q = useWorkspaceDir(dir, false);
  const parts = dir ? dir.split("/") : [];

  return (
    <Modal
      open
      title="选择工作目录"
      okText="使用这个目录"
      cancelText="取消"
      okButtonProps={{ disabled: !dir }}
      onCancel={onCancel}
      onOk={() => onPick(dir)}
    >
      <nav className={styles.pickCrumbs} aria-label="位置">
        <button type="button" onClick={() => setDir("")}>
          我的工作空间
        </button>
        {parts.map((p, i) => (
          <span key={i} style={{ display: "contents" }}>
            <Icon name="right" size={13} />
            <button type="button" onClick={() => setDir(parts.slice(0, i + 1).join("/"))}>
              {p}
            </button>
          </span>
        ))}
      </nav>
      <div className={styles.pickList}>
        {q.isPending ? (
          <Spin size="small" style={{ margin: 16 }} />
        ) : q.error ? (
          <Alert type="error" showIcon message={q.error.message} />
        ) : q.data.dirs.length ? (
          q.data.dirs.map((f) => (
            <button key={f.path} type="button" className={styles.pickRow} onClick={() => setDir(f.path)}>
              <Icon name="folder" size={15} />
              <span className={styles.pickName}>{f.name}</span>
              <span className={styles.count}>{f.files} 项</span>
              <Icon name="right" size={14} />
            </button>
          ))
        ) : (
          <div className={styles.pickEmpty}>没有子目录</div>
        )}
      </div>
      <div className={styles.hint} style={{ marginTop: 10 }}>
        {dir ? (
          <span>
            用 <code>{dir}</code> 作为工作目录
          </span>
        ) : (
          "点进一个目录再使用：不能用工作空间的根目录"
        )}
      </div>
    </Modal>
  );
}

/** 新对话输入框下面的一行：选了目录时说明它是什么 */
export function DirHint({ dir }: { dir: string }) {
  return (
    <span className={styles.hint}>
      <Icon name="folder" size={13} />
      <span>
        工作目录 <code>{dir}</code>：智能体能读写这个目录里的文件，看不到工作空间的其他目录
      </span>
    </span>
  );
}
