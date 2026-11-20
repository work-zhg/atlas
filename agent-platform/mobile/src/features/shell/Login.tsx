/**
 * 登录：用户中心的账号密码（和网页同一个账号）。服务器地址可以改（企业自己部署）。
 *
 * ★ 企业代码：和网页登录页一样，部署里只有一家企业时不用填（只显示企业名），多家时要填，预填上次的。
 *   换了服务器地址（输完）重新判断一次。
 */
import { useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { Button, Icon } from "@/components/ui";
import { fetchTenantRequired, type TenantRequired, useSession } from "@/lib/session";
import { C, F, R } from "@/theme";

export function Login() {
  const insets = useSafeAreaInsets();
  const session = useSession();
  const [server, setServer] = useState(session.server);
  const [account, setAccount] = useState(session.account);
  const [tenant, setTenant] = useState(session.tenant);
  const [password, setPassword] = useState("");
  /** 要不要填企业代码；null = 还不知道（连不上时带上次的企业代码提交，由用户中心判断） */
  const [need, setNeed] = useState<TenantRequired | null>(null);
  const [showServer, setShowServer] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const checkTenant = (target: string) => {
    if (target.trim()) void fetchTenantRequired(target).then(setNeed);
  };
  // 打开登录页时按记住的服务器判断一次；之后改了地址在输完时再判断
  useEffect(() => checkTenant(session.server), [session.server]);

  const needTenant = need?.required ?? false;
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await session.login(server, account, password, need?.required === false ? undefined : tenant);
      if (r.mustChangePassword) setError("这个账号要先修改密码：请在电脑上登录一次，改完再回来");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <KeyboardAvoidingView behavior="padding" style={{ flex: 1, backgroundColor: C.surface }}>
      <ScrollView contentContainerStyle={[s.page, { paddingTop: insets.top + 64, paddingBottom: insets.bottom + 24 }]} keyboardShouldPersistTaps="handled">
        <View style={s.logo}>
          <Text style={s.logoText}>A</Text>
        </View>
        <Text style={s.title}>登录 Atlas</Text>
        <Text style={s.sub}>和电脑版用同一个账号</Text>

        {needTenant ? (
          <>
            <Text style={s.label}>企业代码</Text>
            <TextInput style={s.input} value={tenant} onChangeText={setTenant} autoCapitalize="none" autoCorrect={false} placeholder="企业代码" placeholderTextColor={C.ink4} />
          </>
        ) : need?.tenant ? (
          <>
            <Text style={s.label}>企业</Text>
            <Text style={s.tenant}>{need.tenant.name}</Text>
          </>
        ) : null}
        <Text style={s.label}>账号</Text>
        <TextInput style={s.input} value={account} onChangeText={setAccount} autoCapitalize="none" autoCorrect={false} placeholder="账号" placeholderTextColor={C.ink4} />
        <Text style={s.label}>密码</Text>
        <TextInput
          style={s.input}
          value={password}
          onChangeText={setPassword}
          secureTextEntry
          placeholder="密码"
          placeholderTextColor={C.ink4}
          onSubmitEditing={() => void submit()}
        />

        {showServer ? (
          <>
            <Text style={s.label}>服务器地址</Text>
            <TextInput
              style={s.input}
              value={server}
              onChangeText={setServer}
              onEndEditing={() => checkTenant(server)}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              placeholder="https://atlas.example.com"
              placeholderTextColor={C.ink4}
            />
          </>
        ) : (
          <Pressable style={s.serverRow} onPress={() => setShowServer(true)} hitSlop={8}>
            <Icon name="server" size={15} color={C.ink3} />
            <Text style={s.serverText} numberOfLines={1}>
              {server.replace(/^https?:\/\//, "")}
            </Text>
            <Text style={s.change}>更换</Text>
          </Pressable>
        )}

        {error ? <Text style={s.error}>{error}</Text> : null}
        <Button kind="primary" style={{ marginTop: 20 }} loading={busy} disabled={!account.trim() || !password || !server.trim() || (needTenant && !tenant.trim())} onPress={() => void submit()}>
          登录
        </Button>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const s = StyleSheet.create({
  page: { paddingHorizontal: 28 },
  logo: { width: 56, height: 56, borderRadius: 16, backgroundColor: C.primary2, alignItems: "center", justifyContent: "center", marginBottom: 20 },
  logoText: { color: "#fff", fontSize: 26, fontWeight: "800" },
  title: { fontSize: F.h1, fontWeight: "700", color: C.ink },
  sub: { fontSize: F.body, color: C.ink3, marginTop: 4, marginBottom: 24 },
  label: { fontSize: F.sm, color: C.ink2, marginTop: 14, marginBottom: 6, fontWeight: "600" },
  tenant: { fontSize: F.input, color: C.ink, fontWeight: "600" },
  input: { height: 48, borderWidth: 1, borderColor: C.line, borderRadius: R.md, paddingHorizontal: 14, fontSize: F.input, color: C.ink },
  serverRow: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 16 },
  serverText: { flex: 1, fontSize: F.sm, color: C.ink3 },
  change: { fontSize: F.sm, color: C.primary, fontWeight: "600" },
  error: { color: C.danger, fontSize: F.sm, marginTop: 14, lineHeight: 20 },
});
