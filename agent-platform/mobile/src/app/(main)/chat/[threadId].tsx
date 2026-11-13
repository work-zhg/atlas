import { useLocalSearchParams } from "expo-router";

import { Conversation } from "@/features/chat/Conversation";

export default function ChatRoute() {
  const { threadId } = useLocalSearchParams<{ threadId: string }>();
  // 换会话时整个对话页重建：流的游标、面板状态都跟着会话走
  return <Conversation key={threadId} threadId={threadId} />;
}
