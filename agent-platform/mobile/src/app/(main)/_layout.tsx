/** 主区：侧边菜单（新对话、工作空间、最近对话）。页面自己画顶栏，不用抽屉自带的。 */
import { Drawer } from "expo-router/drawer";

import { DrawerMenu } from "@/features/shell/DrawerMenu";
import { C } from "@/theme";

export default function MainLayout() {
  return (
    <Drawer
      drawerContent={(props) => <DrawerMenu {...props} />}
      screenOptions={{
        headerShown: false,
        drawerType: "front",
        drawerStyle: { width: "84%", backgroundColor: C.surface },
        overlayColor: C.mask,
        swipeEdgeWidth: 32,
      }}
    >
      <Drawer.Screen name="index" />
      <Drawer.Screen name="workspace" />
      <Drawer.Screen name="chat/[threadId]" />
    </Drawer>
  );
}
