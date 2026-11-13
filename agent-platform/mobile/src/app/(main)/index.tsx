import { useLocalSearchParams, useRouter } from "expo-router";

import { Home } from "@/features/home/Home";

/** 新对话。dir = 从工作空间的文件夹进来（新对话以它为工作目录） */
export default function HomeRoute() {
  const { dir } = useLocalSearchParams<{ dir?: string }>();
  const router = useRouter();
  return <Home dir={dir ?? ""} onDropDir={() => router.setParams({ dir: undefined })} />;
}
