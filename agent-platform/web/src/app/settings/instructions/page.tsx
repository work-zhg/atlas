import { redirect } from "next/navigation";

/** 个人指令并入「个性化」，平台指令在「系统 › 平台设置」 */
export default function Page() {
  redirect("/me?tab=instructions");
}
