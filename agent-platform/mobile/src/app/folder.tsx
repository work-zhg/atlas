import { useLocalSearchParams } from "expo-router";

import { Workspace } from "@/features/workspace/Workspace";

/** 工作空间里的某个文件夹（逐层推进来，返回键回上一级） */
export default function FolderRoute() {
  const { dir } = useLocalSearchParams<{ dir: string }>();
  return <Workspace dir={dir ?? ""} />;
}
