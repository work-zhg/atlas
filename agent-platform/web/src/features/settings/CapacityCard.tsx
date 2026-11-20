"use client";

/**
 * 份额与用量：本租户的份额（运营中心定，只读）、现在用了多少，以及每人上限（在「配置项」里改）。
 *
 * ★ 每人上限高于份额不报错：准入先判租户份额，多出来的那部分用不上 —— 这里只提示，不拦。
 */
import { Alert, Card, Progress, Skeleton, Tooltip } from "antd";
import Link from "next/link";

import { useKbCapacity, useTenantSettings } from "@/api/tenant-settings";
import { formatBytes } from "@/features/files/format";

interface Row {
  label: string;
  used: number;
  /** null = 不限 */
  quota: number | null;
  /** 0 = 不限；undefined = 这一项没有每人上限 */
  perPerson?: number;
  fmt: (n: number) => string;
  hint: string;
}

const count = (n: number) => String(n);

function Meter({ row }: { row: Row }) {
  const pct = row.quota ? Math.min(100, Math.round((row.used / row.quota) * 100)) : 0;
  const over = row.quota !== null && row.perPerson !== undefined && row.perPerson > row.quota;
  return (
    <div style={{ padding: "12px 0", borderTop: "1px solid var(--line)" }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <Tooltip title={row.hint}>
          <span style={{ fontWeight: 600 }}>{row.label}</span>
        </Tooltip>
        <span style={{ marginLeft: "auto", fontVariantNumeric: "tabular-nums" }}>
          {row.fmt(row.used)} / {row.quota === null ? "不限" : row.fmt(row.quota)}
        </span>
      </div>
      {row.quota !== null && (
        <Progress
          percent={pct}
          size="small"
          showInfo={false}
          status={pct >= 100 ? "exception" : pct >= 80 ? "active" : "normal"}
        />
      )}
      {row.perPerson !== undefined && (
        <div style={{ fontSize: 12.5, color: over ? "var(--warning)" : "var(--fg-3)", marginTop: 2 }}>
          每人上限：{row.perPerson === 0 ? "不限" : row.fmt(row.perPerson)}
          {over && "（高于本租户份额，多出来的用不上）"}
        </div>
      )}
    </div>
  );
}

export function CapacityCard() {
  const q = useTenantSettings();
  const kb = useKbCapacity();
  const data = q.data;

  return (
    <Card>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 16, marginBottom: 4 }}>
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 600 }}>份额与用量</div>
          <div style={{ fontSize: 13, color: "var(--fg-3)", marginTop: 2 }}>
            份额由平台运营方分配；每人上限在<Link href="/settings/configs">「配置项」</Link>里设置（capacity.*）
          </div>
        </div>
      </div>
      {q.error && <Alert type="error" showIcon message={`读取失败：${(q.error as Error).message}`} />}
      {q.isPending && <Skeleton active paragraph={{ rows: 3 }} />}
      {data && (
        <>
          <Meter
            row={{
              label: "同时运行的任务",
              used: data.usage.running_runs,
              quota: data.quota.running_runs,
              perPerson: data.per_person.running_runs,
              fmt: count,
              hint: "超出时新任务排队，前面的跑完再开始，不算失败",
            }}
          />
          <Meter
            row={{
              label: "沙箱",
              used: data.usage.sandboxes,
              quota: data.quota.sandboxes,
              perPerson: data.per_person.sandboxes,
              fmt: count,
              hint: "原生沙箱与会话 Pod 合计；满了提示稍后再试",
            }}
          />
          <Meter
            row={{
              label: "工作空间存储",
              used: data.usage.storage_bytes,
              quota: data.quota.storage_bytes,
              perPerson: data.per_person.storage_bytes,
              fmt: formatBytes,
              hint: "所有人的工作空间合计；满了拒绝上传",
            }}
          />
          {kb.data && (
            <Meter
              row={{
                label: "知识库文件",
                used: kb.data.used_bytes,
                quota: kb.data.quota_bytes,
                fmt: formatBytes,
                hint: "上传到知识库的原始文件合计；满了拒绝上传",
              }}
            />
          )}
          {data.quota.is_default && (
            <div style={{ fontSize: 12.5, color: "var(--fg-3)", marginTop: 8 }}>当前用的是平台的默认份额</div>
          )}
        </>
      )}
    </Card>
  );
}
