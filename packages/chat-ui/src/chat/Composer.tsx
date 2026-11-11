"use client";

/**
 * 输入框：首页与对话页共用。
 *
 * ★ 添加的文件只收集，不在这里上传：首页发第一句话时会话还没建，上传要等建好之后；对话页则
 *   在发送前上传。都交给 onSend 去做 —— 它返回 Promise 时这里显示「发送中」，失败就保留
 *   输入的文字和文件，用户不用重打。
 * ★ Enter 发送，Shift+Enter 换行；输入法选词时的 Enter 不算发送（中文用户选词会误发）。
 */
import { useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { Icon } from "../components/Icon";
import styles from "./chat.module.css";

interface Props {
  disabled?: boolean;
  /** 正在运行 —— 发送按钮变成"停止" */
  running: boolean;
  onSend: (text: string, files: File[]) => void | Promise<void>;
  onCancel?: () => void;
  /** 底栏里添加文件按钮之后的控件（选智能体、执行前确认……） */
  toolbar?: ReactNode;
  placeholder?: string;
  /** 显示「添加文件」按钮 */
  acceptFiles?: boolean;
  autoFocus?: boolean;
  /** 首页用大号：两行起步 */
  size?: "large" | "normal";
  /** 不要外层留白（首页、调试预览自己排版） */
  bare?: boolean;
  /** 输入框下面的一行说明 */
  foot?: ReactNode;
  /** 外部要往输入框里填字（快捷问题）：值变了就填进去 */
  fill?: { text: string; seq: number };
}

export function Composer({
  disabled = false,
  running,
  onSend,
  onCancel,
  toolbar,
  placeholder = "输入消息…",
  acceptFiles = false,
  autoFocus = false,
  size = "normal",
  bare = false,
  foot,
  fill,
}: Props) {
  const [value, setValue] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const filled = useRef(0);

  if (fill && fill.seq !== filled.current) {
    filled.current = fill.seq;
    // 渲染期间同步一次（不用 effect：避免先渲染旧值再闪成新值）
    if (value !== fill.text) setValue(fill.text);
    queueMicrotask(() => ref.current?.focus());
  }

  const grow = (el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  };

  const submit = async () => {
    const text = value.trim();
    if (!text || running || disabled || busy) return;
    const result = onSend(text, files);
    if (result instanceof Promise) {
      setBusy(true);
      try {
        await result;
      } catch {
        return; // 调用方已提示错误；保留输入
      } finally {
        setBusy(false);
      }
    }
    setValue("");
    setFiles([]);
    // 高度是随内容长起来的，清空后要手动收回去
    if (ref.current) ref.current.style.height = "auto";
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void submit();
    }
  };

  const box = (
    <div className={styles.composerInner}>
      {files.length > 0 && (
        <div className={styles.attachList}>
          {files.map((f, i) => (
            <span key={`${f.name}-${i}`} className={styles.attachChip} title={f.name}>
              <Icon name="file" size={13} />
              <span>{f.name}</span>
              <button
                type="button"
                className={styles.iconBtn}
                style={{ width: 18, height: 18 }}
                onClick={() => setFiles(files.filter((_, j) => j !== i))}
                aria-label={`移除 ${f.name}`}
              >
                <Icon name="x" size={12} />
              </button>
            </span>
          ))}
        </div>
      )}
      <textarea
        ref={ref}
        className={styles.composerInput}
        rows={size === "large" ? 2 : 1}
        style={size === "large" ? { minHeight: 56 } : undefined}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        autoFocus={autoFocus}
        onChange={(e) => {
          setValue(e.target.value);
          grow(e.target);
        }}
        onKeyDown={onKeyDown}
        aria-label="消息输入框"
      />
      <div className={styles.composerBar}>
        {acceptFiles && (
          <>
            <button
              type="button"
              className={styles.iconBtn}
              title="添加文件"
              aria-label="添加文件"
              disabled={disabled || busy}
              onClick={() => fileRef.current?.click()}
            >
              <Icon name="clip" size={17} />
            </button>
            <input
              ref={fileRef}
              type="file"
              multiple
              hidden
              onChange={(e) => {
                const picked = Array.from(e.target.files ?? []);
                if (picked.length) setFiles((prev) => [...prev, ...picked]);
                e.target.value = "";
              }}
            />
          </>
        )}
        {toolbar}
        {running && onCancel ? (
          <button type="button" className={`${styles.send} ${styles.stopBtn}`} onClick={onCancel} title="停止" aria-label="停止">
            <Icon name="stop" size={14} />
          </button>
        ) : (
          <button
            type="button"
            className={styles.send}
            disabled={disabled || busy || running || !value.trim()}
            onClick={() => void submit()}
            title="发送"
            aria-label="发送"
          >
            {busy ? <span className={styles.spin} style={{ borderColor: "rgba(255,255,255,.4)", borderTopColor: "#fff" }} /> : <Icon name="up" size={16} />}
          </button>
        )}
      </div>
    </div>
  );

  if (bare) return box;
  return (
    <div className={styles.composer}>
      {box}
      {foot && <div className={styles.composerFoot}>{foot}</div>}
    </div>
  );
}
