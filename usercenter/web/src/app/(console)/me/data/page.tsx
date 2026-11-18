"use client";

import { PageHead } from "@/components/common";
import { DataList } from "@/components/data";

/** 个人中心 › 我的数据：每个人都可能是某条数据（智能体、知识库）的 Owner，在这里管谁能用。 */
export default function MyDataPage() {
  return (
    <>
      <PageHead title="我的数据" sub="你创建的智能体、知识库等数据：在这里决定谁能查看、谁能修改。" />
      <DataList where="me" />
    </>
  );
}
