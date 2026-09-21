"use client";

import { Button } from "antd";
import { useRef, useState, type KeyboardEvent } from "react";

import { Icon } from "@/components/Icon";
import styles from "./chat.module.css";

interface Props {
  disabled: boolean;
  /** 正在运行 —— 发送按钮变成"停止" */
  running: boolean;
  onSend: (text: string) => void;
  onCancel: () => void;
}

export function Composer({ disabled, running, onSend, onCancel }: Props) {
  const [value, setValue] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  const submit = () => {
    const text = value.trim();
    if (!text || running || disabled) return;
    onSend(text);
    setValue("");
    // 高度是随内容长起来的，清空后要手动收回去
    if (ref.current) ref.current.style.height = "auto";
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter 发送，Shift+Enter 换行。输入法组合中的 Enter 不能当发送 ——
    // 中文用户选词时会误发。
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className={styles.composer}>
      <div className={styles.composerInner}>
        <textarea
          ref={ref}
          className={styles.composerInput}
          rows={1}
          value={value}
          disabled={disabled}
          placeholder={disabled ? "请先选择或新建会话" : "输入消息，Enter 发送，Shift+Enter 换行"}
          onChange={(e) => {
            setValue(e.target.value);
            e.target.style.height = "auto";
            e.target.style.height = `${Math.min(e.target.scrollHeight, 180)}px`;
          }}
          onKeyDown={onKeyDown}
          aria-label="消息输入框"
        />
        <div className={styles.composerBar}>
          <span className={styles.composerHint}>
            {running ? "正在生成…" : "Enter 发送 · Shift+Enter 换行"}
          </span>
          {running ? (
            <Button size="small" danger icon={<Icon name="stop" size={13} />} onClick={onCancel}>
              停止
            </Button>
          ) : (
            <Button
              type="primary"
              size="small"
              icon={<Icon name="send" size={13} />}
              disabled={disabled || !value.trim()}
              onClick={submit}
            >
              发送
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
