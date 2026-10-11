"use client";

import { useParams } from "next/navigation";

import { DocumentPage } from "@/features/knowledge/DocumentPage";

export default function Page() {
  const { datasetId, documentId } = useParams<{ datasetId: string; documentId: string }>();
  return <DocumentPage datasetId={datasetId} documentId={documentId} />;
}
