import { useLocalSearchParams } from "expo-router";

import { Preview } from "@/features/files/Preview";

/** 文件预览。src = "ws"（工作空间）或会话 id */
export default function PreviewRoute() {
  const { src, path } = useLocalSearchParams<{ src: string; path: string }>();
  return <Preview src={src} path={path} />;
}
