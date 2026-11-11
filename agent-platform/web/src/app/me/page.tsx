"use client";

/** 个性化：记忆 + 自定义指令（原来是两个一级菜单，内容都是「智能体怎么对待我」） */
import { Tabs } from "antd";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import pageStyles from "@/features/agents/agents.module.css";
import { InstructionEditor } from "@/features/me/InstructionEditor";
import { MemoryPage } from "@/features/memory/MemoryPage";

function Personalize() {
  const router = useRouter();
  const tab = useSearchParams().get("tab") === "instructions" ? "instructions" : "memory";
  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <h1 className={pageStyles.title}>个性化</h1>
        </div>
      </header>
      <div className={pageStyles.body}>
        <Tabs
          activeKey={tab}
          onChange={(k) => router.replace(k === "memory" ? "/me" : `/me?tab=${k}`)}
          items={[
            { key: "memory", label: "记忆", children: <MemoryPage embedded /> },
            {
              key: "instructions",
              label: "自定义指令",
              children: (
                <div style={{ maxWidth: 760 }}>
                  <p style={{ margin: "0 0 10px", fontWeight: 600, color: "var(--fg-2)" }}>
                    希望智能体了解你哪些情况、怎样回答？
                  </p>
                  <InstructionEditor
                    scope="me"
                    placeholder="例如：我是后端工程师，主要用 Python；回答尽量简洁，先给结论；代码注释用中文。"
                    hint="对你的所有对话生效"
                  />
                </div>
              ),
            },
          ]}
        />
      </div>
    </div>
  );
}

export default function Page() {
  return (
    <Suspense>
      <Personalize />
    </Suspense>
  );
}
