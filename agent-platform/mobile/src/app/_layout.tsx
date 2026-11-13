/**
 * 根布局：登录会话 → 接口（react-query + chat-ui 的 AtlasClientProvider）→ 页面。
 *
 * ★ 没登录只看得到登录页（Stack.Protected）；接口回 401 = 会话过期，回到登录页。
 * ★ 换账号 / 退出时清空缓存，不让上一个人的数据留在内存里。
 */
import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { KeyboardProvider } from "react-native-keyboard-controller";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { AtlasClientProvider } from "@atlas/chat-ui/src/client";
import { ApiError } from "@atlas/chat-ui/src/lib/http";
import { Loading, ToastProvider } from "@/components/ui";
import { SessionProvider, useSession } from "@/lib/session";

function Api({ children }: { children: ReactNode }) {
  const session = useSession();
  const expire = useRef(session.expire);
  expire.current = session.expire;
  const [qc] = useState(() => {
    const onError = (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) expire.current();
    };
    return new QueryClient({
      queryCache: new QueryCache({ onError }),
      mutationCache: new MutationCache({ onError }),
      defaultOptions: { queries: { retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 2 } },
    });
  });
  useEffect(() => {
    if (!session.signedIn) qc.clear();
  }, [session.signedIn, qc]);
  return (
    <QueryClientProvider client={qc}>
      <AtlasClientProvider config={session.client}>{children}</AtlasClientProvider>
    </QueryClientProvider>
  );
}

function Routes() {
  const { loading, signedIn } = useSession();
  if (loading) return <Loading style={{ flex: 1, justifyContent: "center" }} />;
  return (
    <Stack screenOptions={{ headerShown: false, animation: "slide_from_right" }}>
      <Stack.Protected guard={signedIn}>
        <Stack.Screen name="(main)" />
        <Stack.Screen name="preview" />
        <Stack.Screen name="folder" />
        <Stack.Screen name="search" />
      </Stack.Protected>
      <Stack.Protected guard={!signedIn}>
        <Stack.Screen name="login" />
      </Stack.Protected>
    </Stack>
  );
}

export default function RootLayout() {
  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <SafeAreaProvider>
        <KeyboardProvider>
          <SessionProvider>
            <Api>
              <ToastProvider>
                <StatusBar style="dark" />
                <Routes />
              </ToastProvider>
            </Api>
          </SessionProvider>
        </KeyboardProvider>
      </SafeAreaProvider>
    </GestureHandlerRootView>
  );
}
