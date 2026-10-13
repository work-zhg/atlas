"use client";

import { Alert, Button, Empty, Input, Segmented, Skeleton } from "antd";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { useAgents } from "@/api/agents";
import type { AgentStatus } from "@/api/types";
import { Icon } from "@/components/Icon";
import { AgentCard } from "@/features/agents/AgentCard";
import styles from "@/features/agents/agents.module.css";

type Scope = AgentStatus | "all";
type Kind = "all" | "native" | "acp";

const SCOPES: { value: Scope; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "enabled", label: "已启用" },
  { value: "draft", label: "草稿" },
  { value: "archived", label: "已归档" },
];

export default function AgentsPage() {
  const router = useRouter();
  const [scope, setScope] = useState<Scope>("all");
  const [keyword, setKeyword] = useState("");
  const [kind, setKind] = useState<Kind>("all");

  // 搜索走本地过滤而不是每次敲键都打后端：智能体是百量级，
  // 一次拉全量再本地筛，比 debounce + 请求风暴简单也更跟手。
  const { data, isPending, error } = useAgents({ status: scope });

  const agents = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return (data?.data ?? []).filter(
      (a) =>
        (kind === "all" || a.kind === kind) &&
        (!kw || [a.name, a.slug, a.description].some((s) => s.toLowerCase().includes(kw))),
    );
  }, [data, keyword, kind]);

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div className={styles.titleRow}>
          <div>
            <h1 className={styles.title}>智能体</h1>
            <p className={styles.desc}>
              {isPending ? "加载中…" : `共 ${data?.data.length ?? 0} 个`}
              {" · "}Native 在平台内运行、可以委派；ACP 在会话 Pod 里驱动 CLI
            </p>
          </div>
          <Button
            type="primary"
            className={styles.action}
            icon={<Icon name="plus" size={14} />}
            onClick={() => router.push("/agents/new")}
          >
            新建智能体
          </Button>
        </div>

        <div className={styles.filterRow}>
          <Input
            allowClear
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索名称、标识或描述…"
            prefix={<Icon name="search" size={14} />}
            style={{ width: 260 }}
          />
          <Segmented
            value={kind}
            onChange={(v) => setKind(v as Kind)}
            options={[
              { value: "all", label: "全部类型" },
              { value: "native", label: "Native" },
              { value: "acp", label: "ACP · CLI" },
            ]}
          />
          <Segmented
            value={scope}
            onChange={(v) => setScope(v as Scope)}
            options={SCOPES}
          />
        </div>
      </header>

      <div className={styles.body}>
        {error && (
          <Alert
            type="error"
            showIcon
            message="加载智能体失败"
            description={error.message}
            style={{ marginBottom: 16 }}
          />
        )}

        {isPending && <Skeleton active paragraph={{ rows: 6 }} />}

        {!isPending && agents.length === 0 && (
          <Empty
            description={keyword ? "没有匹配的智能体" : "还没有智能体"}
            style={{ marginTop: 64 }}
          />
        )}

        {agents.length > 0 && (
          <div className={styles.grid}>
            {agents.map((a) => (
              <AgentCard key={a.id} agent={a} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
