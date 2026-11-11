import { redirect } from "next/navigation";

/** 新建改成智能体列表里的弹窗（选名称和模板），建好直接进编辑页 */
export default function NewAgentPage() {
  redirect("/agents?create=1");
}
