"use client";

/**
 * 会话后补记的提示（doc/memory-design.html §4.3、§4.7）：「上次会话补记了 N 条［查看］」。
 *
 * ★ 补记写入时用户不在场，所以下次见到用户时要告诉他记了什么、让他能撤销 —— 撤销在记忆页里
 *   （修订历史 / 条目操作）。点「知道了」才算看过，之后不再提示。
 * ★ 记忆没接入（503）或接口出错时什么都不显示：这只是提示，不该挡住对话。
 */
import { Alert, Button, Space } from "antd";
import Link from "next/link";

import { memoryApi, useMemoryBackfills, useMemoryMutation } from "@/api/memories";

export function BackfillNotice({ linkToPage = false }: { linkToPage?: boolean }) {
  const { data } = useMemoryBackfills(true);
  const seen = useMemoryMutation((ids: string[]) => memoryApi.markBackfillsSeen(ids));
  const items = data?.data ?? [];
  if (!items.length) return null;

  const written = items.flatMap((b) => b.written);
  const splits = items.flatMap((b) => b.split);
  const ids = items.map((b) => b.id);
  const parts = [
    written.length ? `补记了 ${written.length} 条记忆` : "",
    splits.length ? `把 ${splits.length} 个超出上限的主题拆小了` : "",
  ].filter(Boolean);

  return (
    <Alert
      type="info"
      showIcon
      style={{ marginBottom: 16 }}
      message={`会话结束后，系统${parts.join("，")}`}
      description={
        linkToPage ? undefined : (
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {written.slice(0, 8).map((w) => (
              <li key={`${String(w.revision)}-${String(w.number)}`}>
                {String(w.content)}
                <span style={{ color: "var(--fg-3)", marginLeft: 6 }}>
                  {String(w.topic)} #{String(w.number)}
                </span>
              </li>
            ))}
            {splits.map((s) => (
              <li key={`split-${String(s.topic)}`}>
                主题「{String(s.topic)}」拆成了 {(s.into as string[]).join("、")}
              </li>
            ))}
          </ul>
        )
      }
      action={
        <Space direction={linkToPage ? "horizontal" : "vertical"}>
          {linkToPage && (
            <Link href="/memories">
              <Button size="small">查看</Button>
            </Link>
          )}
          <Button size="small" type="text" loading={seen.isPending} onClick={() => seen.mutate(ids)}>
            知道了
          </Button>
        </Space>
      }
    />
  );
}
