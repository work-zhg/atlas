"use client";

/**
 * 每条回答下的操作：复制 / 重新生成 / 有帮助 / 没帮助。
 *
 * ★ 只挂在**已落库**的助手消息上：反馈要指向一条消息 id，实时区的回答还没有 id。
 * ★ 重新生成只在最后一条回答上、这一轮结束后出现：用同一个问题再问一次（新的一轮）。
 */
import { App as AntdApp, Button, Input, Popover, Tooltip } from "antd";
import { useState } from "react";

import { useSetFeedback, useThreadFeedback, type Rating } from "../api/feedback";
import { Icon } from "../components/Icon";
import { copyText } from "../lib/clipboard";
import styles from "./chat.module.css";

const REASONS = ["内容不准确", "没理解我的意思", "太啰嗦", "操作有误", "其他"];

export interface MessageActionsProps {
  threadId: string;
  messageId: string;
  text: string;
  /** 打开反馈按钮（嵌入方没有反馈接口时关掉） */
  feedback: boolean;
  /** 只在最后一条回答、且这一轮已结束时给 */
  onRegenerate?: () => void;
}

export function MessageActions({ threadId, messageId, text, feedback, onRegenerate }: MessageActionsProps) {
  const { message } = AntdApp.useApp();
  const fbQ = useThreadFeedback(threadId, feedback);
  const setFb = useSetFeedback(threadId);
  const mine = fbQ.data?.[messageId];
  const [askOpen, setAskOpen] = useState(false);
  const [reasons, setReasons] = useState<string[]>([]);
  const [comment, setComment] = useState("");

  const rate = (rating: Rating | null, extra?: { reasons: string[]; comment: string }) =>
    setFb.mutate(
      { messageId, rating, ...extra },
      { onError: (e) => message.error(`没有提交成功：${(e as Error).message}`) },
    );

  const copy = async () => {
    if (await copyText(text)) message.success("已复制");
    else message.warning("复制失败，请手动选中文字复制");
  };

  const dislikeForm = (
    <div style={{ width: 300 }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {REASONS.map((r) => {
          const on = reasons.includes(r);
          return (
            <Button
              key={r}
              size="small"
              type={on ? "primary" : "default"}
              ghost={on}
              onClick={() => setReasons(on ? reasons.filter((x) => x !== r) : [...reasons, r])}
            >
              {r}
            </Button>
          );
        })}
      </div>
      <Input.TextArea
        rows={2}
        style={{ marginTop: 10 }}
        placeholder="补充说明（可选）"
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        maxLength={2000}
      />
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 10 }}>
        <Button size="small" onClick={() => setAskOpen(false)}>
          跳过
        </Button>
        <Button
          size="small"
          type="primary"
          onClick={() => {
            rate("down", { reasons, comment: comment.trim() });
            setAskOpen(false);
            message.success("感谢反馈");
          }}
        >
          提交
        </Button>
      </div>
    </div>
  );

  return (
    <div className={styles.msgActions}>
      <Tooltip title="复制">
        <button type="button" className={styles.iconBtn} onClick={() => void copy()} aria-label="复制">
          <Icon name="copy" size={15} />
        </button>
      </Tooltip>
      {onRegenerate && (
        <Tooltip title="用同一个问题再问一次">
          <button type="button" className={styles.iconBtn} onClick={onRegenerate} aria-label="重新生成">
            <Icon name="refresh" size={15} />
          </button>
        </Tooltip>
      )}
      {feedback && (
        <>
          <Tooltip title="有帮助">
            <button
              type="button"
              className={`${styles.iconBtn} ${mine?.rating === "up" ? styles.iconBtnOn : ""}`}
              aria-pressed={mine?.rating === "up"}
              onClick={() => rate(mine?.rating === "up" ? null : "up")}
              aria-label="有帮助"
            >
              <Icon name="like" size={15} />
            </button>
          </Tooltip>
          <Popover
            open={askOpen}
            onOpenChange={(o) => !o && setAskOpen(false)}
            trigger="click"
            title="哪里不满意？"
            content={dislikeForm}
          >
            <Tooltip title="没帮助">
              <button
                type="button"
                className={`${styles.iconBtn} ${mine?.rating === "down" ? styles.iconBtnOn : ""}`}
                aria-pressed={mine?.rating === "down"}
                onClick={() => {
                  if (mine?.rating === "down") return rate(null);
                  // 先记下「没帮助」，原因可选：用户直接关掉弹层也不丢这次反馈
                  rate("down");
                  setReasons([]);
                  setComment("");
                  setAskOpen(true);
                }}
                aria-label="没帮助"
              >
                <Icon name="dislike" size={15} />
              </button>
            </Tooltip>
          </Popover>
        </>
      )}
    </div>
  );
}
