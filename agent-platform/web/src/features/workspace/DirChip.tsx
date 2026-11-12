"use client";

/**
 * 工作目录标签：新对话的输入框里（可以去掉）、对话标题栏（点了去工作空间的这个目录）。
 */
import Link from "next/link";

import { Icon } from "@/components/Icon";

import styles from "./workspace.module.css";

export const workspaceHref = (dir: string) => `/workspace?dir=${encodeURIComponent(dir)}`;

export function DirChip({ dir, onRemove }: { dir: string; onRemove?: () => void }) {
  if (onRemove) {
    return (
      <span className={styles.dirChip} title={`工作目录：${dir}`}>
        <Icon name="folder" size={14} />
        <span>{dir}</span>
        <span
          role="button"
          tabIndex={0}
          aria-label="不用这个目录"
          className={styles.dirChipX}
          onClick={onRemove}
          onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onRemove()}
        >
          <Icon name="x" size={12} />
        </span>
      </span>
    );
  }
  return (
    <Link href={workspaceHref(dir)} className={styles.dirChip} title={`工作目录：${dir}（在工作空间中打开）`}>
      <Icon name="folder" size={14} />
      <span>{dir}</span>
    </Link>
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
