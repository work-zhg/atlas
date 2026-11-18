"use client";

import { PageHead } from "@/components/common";
import { DataList } from "@/components/data";

/** 权限 › 数据：各应用登记的全部数据及其授权（data:view，只读）。授权由每条数据的 Owner 管理。 */
export default function AllDataPage() {
  return (
    <>
      <PageHead title="数据" sub="各应用登记的全部数据及其授权（只读）。授权由每条数据的 Owner 管理。" />
      <DataList where="all" />
    </>
  );
}
