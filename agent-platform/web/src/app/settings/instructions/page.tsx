import { redirect } from "next/navigation";

/** 个人指令并入「个性化」，组织指令在「租户设置」 */
export default function Page() {
  redirect("/me?tab=instructions");
}
