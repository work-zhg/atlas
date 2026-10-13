"use client";

import { useParams } from "next/navigation";

import { DatasetPage } from "@/features/knowledge/DatasetPage";

export default function Page() {
  const { datasetId } = useParams<{ datasetId: string }>();
  return <DatasetPage datasetId={datasetId} />;
}
