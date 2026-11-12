"use client";

import { useParams } from "next/navigation";

import { SetPage } from "@/features/knowledge/SetPage";

export default function Page() {
  const { setId } = useParams<{ setId: string }>();
  return <SetPage setId={setId} />;
}
