"use client";

/**
 * 上传：排队、同名冲突（替换 / 保留两者 / 跳过，可以一并处理其余的）、进度面板。
 *
 * ★ 一个一个传：上传经 server 中转，并发多了只会互相抢带宽、占 server 内存。
 * ★ 同名按目标目录现有的文件判断：每个涉及的目录列一次（文件夹上传会涉及好几层）。
 */
import { Button, Checkbox, Modal } from "antd";
import { useCallback, useRef, useState } from "react";

import { uploadWorkspaceFile, type WorkspaceDir } from "@/api/workspace";
import { Icon } from "@/components/Icon";
import { formatBytes } from "@/features/files/format";
import { http } from "@/lib/http";

import styles from "./workspace.module.css";

export interface PickedFile {
  file: File;
  /** 相对上传目标目录的路径（上传文件夹时带子目录） */
  rel: string;
}

type How = "replace" | "keep" | "skip";

interface Item {
  id: number;
  path: string;
  size: number;
  state: "up" | "done" | "fail" | "skip";
  pct: number;
  error?: string;
  replaced?: boolean;
}

const join = (a: string, b: string) => (a ? `${a}/${b}` : b);
const parentOf = (p: string) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");

function uniqueName(path: string, taken: Set<string>): string {
  const dir = parentOf(path);
  const name = path.slice(dir ? dir.length + 1 : 0);
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : "";
  for (let i = 1; ; i++) {
    const candidate = join(dir, `${stem} (${i})${ext}`);
    if (!taken.has(candidate)) return candidate;
  }
}

export function useUploads({ maxBytes, onChanged }: { maxBytes: number; onChanged: () => void }) {
  const [items, setItems] = useState<Item[]>([]);
  const [collapsed, setCollapsed] = useState(false);
  const [ask, setAsk] = useState<{ path: string; more: number; resolve: (how: How, all: boolean) => void }>();
  const seq = useRef(0);
  const busy = useRef(false);

  const patch = (id: number, p: Partial<Item>) =>
    setItems((list) => list.map((it) => (it.id === id ? { ...it, ...p } : it)));

  const enqueue = useCallback(
    async (picked: PickedFile[], dir: string) => {
      if (!picked.length) return;
      setCollapsed(false);
      // 涉及的每个目录列一次，得到已有的文件
      const taken = new Set<string>();
      const dirs = new Set(picked.map((p) => parentOf(join(dir, p.rel))));
      await Promise.all(
        [...dirs].map(async (d) => {
          try {
            const res = await http.get<WorkspaceDir>("/v1/workspace/entries", { dir: d });
            res.files.forEach((f) => taken.add(f.path));
          } catch {
            // 目录还不存在：没有同名
          }
        }),
      );
      let all: How | null = null;
      const plan: { file: File; path: string; replaced: boolean }[] = [];
      for (const [i, one] of picked.entries()) {
        const target = join(dir, one.rel);
        const existed = taken.has(target);
        let how: How = "replace";
        if (existed) {
          if (all) how = all;
          else {
            const more = picked.slice(i + 1).filter((p) => taken.has(join(dir, p.rel))).length;
            const [h, applyAll] = await new Promise<[How, boolean]>((resolve) =>
              setAsk({ path: target, more, resolve: (h2, a) => resolve([h2, a]) }),
            );
            setAsk(undefined);
            how = h;
            if (applyAll) all = h;
          }
        }
        if (how === "skip") {
          const id = ++seq.current;
          setItems((list) => [...list, { id, path: target, size: one.file.size, state: "skip", pct: 0 }]);
          continue;
        }
        const path = how === "keep" ? uniqueName(target, taken) : target;
        taken.add(path);
        plan.push({ file: one.file, path, replaced: existed && how === "replace" });
      }

      const queued = plan.map((p) => ({ ...p, id: ++seq.current }));
      setItems((list) => [
        ...list,
        ...queued.map((q): Item => {
          const over = maxBytes > 0 && q.file.size > maxBytes;
          return {
            id: q.id,
            path: q.path,
            size: q.file.size,
            state: over ? "fail" : "up",
            pct: 0,
            replaced: q.replaced,
            error: over ? `超过单个文件 ${formatBytes(maxBytes)} 的上限` : undefined,
          };
        }),
      ]);
      // 一个一个传（多次 enqueue 也排在一起）
      while (busy.current) await new Promise((r) => setTimeout(r, 200));
      busy.current = true;
      try {
        for (const q of queued) {
          if (maxBytes > 0 && q.file.size > maxBytes) continue;
          try {
            await uploadWorkspaceFile(q.path, q.file, (pct) => patch(q.id, { pct }));
            patch(q.id, { state: "done", pct: 1 });
          } catch (e) {
            patch(q.id, { state: "fail", error: (e as Error).message });
          }
        }
      } finally {
        busy.current = false;
        onChanged();
      }
    },
    [maxBytes, onChanged],
  );

  const panel = (
    <UploadPanel
      items={items}
      collapsed={collapsed}
      onToggle={() => setCollapsed((v) => !v)}
      onClose={() => setItems([])}
    />
  );
  const conflict = <ConflictModal ask={ask} />;
  return { enqueue, panel, conflict };
}

function ConflictModal({
  ask,
}: {
  ask?: { path: string; more: number; resolve: (how: How, all: boolean) => void };
}) {
  const [all, setAll] = useState(false);
  const decide = (how: How) => {
    ask?.resolve(how, all);
    setAll(false);
  };
  return (
    <Modal
      open={!!ask}
      title="已有同名文件"
      closable={false}
      maskClosable={false}
      footer={[
        <Button key="skip" onClick={() => decide("skip")}>
          跳过
        </Button>,
        <Button key="keep" onClick={() => decide("keep")}>
          保留两者
        </Button>,
        <Button key="replace" type="primary" onClick={() => decide("replace")}>
          替换
        </Button>,
      ]}
    >
      <p>「{ask?.path}」已经存在，要怎么处理？</p>
      {!!ask?.more && (
        <Checkbox checked={all} onChange={(e) => setAll(e.target.checked)}>
          其余 {ask.more} 个同名文件也这样处理
        </Checkbox>
      )}
    </Modal>
  );
}

function UploadPanel({
  items,
  collapsed,
  onToggle,
  onClose,
}: {
  items: Item[];
  collapsed: boolean;
  onToggle: () => void;
  onClose: () => void;
}) {
  if (!items.length) return null;
  const count = (s: Item["state"]) => items.filter((i) => i.state === s).length;
  const up = count("up");
  const fail = count("fail");
  const skip = count("skip");
  const title = up
    ? `正在上传 ${up} 项`
    : `上传完成 ${count("done")} 项${fail ? `，${fail} 项失败` : ""}${skip ? `，跳过 ${skip} 项` : ""}`;
  return (
    <div className={styles.upq}>
      <div className={styles.upqHead}>
        <Icon
          name={up ? "upload" : fail ? "alert" : "check"}
          size={16}
          style={{ color: up ? "var(--primary)" : fail ? "var(--danger)" : "var(--success)" }}
        />
        <span>{title}</span>
        <span style={{ flex: 1 }} />
        <Button size="small" type="text" icon={<Icon name={collapsed ? "up" : "down"} size={14} />} onClick={onToggle} />
        {!up && <Button size="small" type="text" icon={<Icon name="x" size={14} />} onClick={onClose} />}
      </div>
      {!collapsed && (
        <div className={styles.upqBody}>
          {items.map((it) => (
            <div key={it.id} className={styles.upi}>
              <Icon name="file" size={14} style={{ color: "var(--fg-3)", flex: "none" }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {it.path.split("/").pop()}
                  {it.replaced && <span style={{ color: "var(--fg-4)" }}>（已替换）</span>}
                </div>
                <div className={styles.upiSub} style={it.state === "fail" ? { color: "var(--danger)" } : undefined}>
                  {it.state === "fail" ? it.error : `${formatBytes(it.size)} · ${parentOf(it.path) || "我的工作空间"}`}
                </div>
              </div>
              <span style={{ flex: "none", fontSize: 12, color: "var(--fg-3)" }}>
                {it.state === "up"
                  ? `${Math.floor(it.pct * 100)}%`
                  : it.state === "done"
                    ? "完成"
                    : it.state === "skip"
                      ? "已跳过"
                      : "失败"}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** 拖进来的东西 → 文件列表（文件夹递归展开）。★ 条目要在第一个 await 之前取出来，事件结束后就失效了 */
export async function filesFromDrop(dt: DataTransfer): Promise<PickedFile[]> {
  const entries = [...dt.items]
    .map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null))
    .filter((e): e is FileSystemEntry => !!e);
  const plain = [...dt.files];
  if (!entries.length) return plain.map((file) => ({ file, rel: file.name }));
  const out: PickedFile[] = [];
  const walk = async (entry: FileSystemEntry, prefix: string): Promise<void> => {
    if (entry.isFile) {
      const file = await new Promise<File>((res, rej) => (entry as FileSystemFileEntry).file(res, rej));
      out.push({ file, rel: prefix + file.name });
      return;
    }
    const reader = (entry as FileSystemDirectoryEntry).createReader();
    for (;;) {
      const batch = await new Promise<FileSystemEntry[]>((res, rej) => reader.readEntries(res, rej));
      if (!batch.length) break;
      for (const child of batch) await walk(child, `${prefix}${entry.name}/`);
    }
  };
  for (const e of entries) await walk(e, "");
  return out;
}
