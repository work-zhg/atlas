"use client";

import { useParams } from "next/navigation";

import { GwGate } from "@/features/catalog/McpPage";
import { ServiceDetail } from "@/features/catalog/McpService";

export default function Page() {
  const { name } = useParams<{ name: string }>();
  return <GwGate>{(me) => <ServiceDetail name={decodeURIComponent(name)} me={me} />}</GwGate>;
}
