"use client";

import { Suspense, type ReactNode } from "react";

import { AppShell } from "@/components/shell/AppShell";
import { Loading } from "@/components/ui";
import { TeamProvider } from "@/lib/team";

export default function ShellLayout({ children }: { children: ReactNode }) {
  return (
    <Suspense fallback={<Loading />}>
      <TeamProvider>
        <AppShell>{children}</AppShell>
      </TeamProvider>
    </Suspense>
  );
}
