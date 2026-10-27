"use client";

/**
 * 一条记忆的演变过程（doc/memory-design.html §6.5、§7「查看」）：取代链上的全部条目，
 * 旧的在前。删除的条目不再加载，但留在这里可以追溯、可以恢复。
 */
import { Drawer, Empty, Skeleton, Tag, Timeline } from "antd";

import { SOURCE_LABEL, useMemoryEntry } from "@/api/memories";
import type { MemoryEntry } from "@/api/types";

import { localDateTime } from "./time";

function stateTag(e: MemoryEntry) {
  if (e.status === "active") return <Tag color="green">有效</Tag>;
  if (e.superseded_by) return <Tag>被 #{e.superseded_by} 取代</Tag>;
  return <Tag>已遗忘</Tag>;
}

export function EntryHistoryDrawer({ number, onClose }: { number: number | null; onClose: () => void }) {
  const { data, isPending, error } = useMemoryEntry(number);
  return (
    <Drawer title={`#${number ?? ""} 的演变`} open={number !== null} onClose={onClose} width={480}>
      {error && <Empty description={(error as Error).message} />}
      {isPending && number !== null && <Skeleton active />}
      {data && (
        <Timeline
          items={data.chain.map((e) => ({
            color: e.status === "active" ? "green" : "gray",
            children: (
              <div style={{ opacity: e.status === "active" ? 1 : 0.75 }}>
                <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 4 }}>
                  <span style={{ fontFamily: "var(--mono)", color: "var(--fg-3)" }}>#{e.number}</span>
                  {stateTag(e)}
                  <Tag>{SOURCE_LABEL[e.source] ?? e.source}</Tag>
                </div>
                <div style={{ lineHeight: 1.6, fontWeight: e.number === number ? 600 : 400 }}>{e.content}</div>
                <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 2 }}>
                  主题 {e.topic ?? "—"} · 成立于 {localDateTime(e.occurred_at)}
                  {e.retired_at ? ` · ${localDateTime(e.retired_at)} 删除` : ""}
                </div>
              </div>
            ),
          }))}
        />
      )}
    </Drawer>
  );
}
