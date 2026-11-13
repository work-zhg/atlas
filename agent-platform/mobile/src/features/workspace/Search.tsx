/**
 * 搜索工作空间里的文件（按名字，含子目录；同网页 /v1/workspace/search）。边输入边出结果。
 */
import { useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { FlatList, Pressable, StyleSheet, Text, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useWorkspaceSearch } from "@/api/queries";
import { Empty, Icon, IconButton, Loading } from "@/components/ui";
import { FileIcon, fmtSize } from "@/features/files/look";
import { C, F, R } from "@/theme";

/** 最近搜索（这次打开 App 期间） */
const recent: string[] = [];

export function Search() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);
  const searchQ = useWorkspaceSearch(debounced);
  const remember = () => {
    const k = debounced;
    if (!k) return;
    const i = recent.indexOf(k);
    if (i >= 0) recent.splice(i, 1);
    recent.unshift(k);
    recent.splice(8);
  };
  const results = [
    ...(searchQ.data?.dirs ?? []).map((d) => ({ key: `d:${d.path}`, name: d.name, path: d.path, isDir: true, meta: d.path })),
    ...(searchQ.data?.files ?? []).map((f) => ({ key: `f:${f.path}`, name: f.name, path: f.path, isDir: false, meta: `${f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/")) : "工作空间"} · ${fmtSize(f.size)}` })),
  ];

  return (
    <View style={{ flex: 1, backgroundColor: C.surface, paddingTop: insets.top }}>
      <View style={s.head}>
        <IconButton name="back" label="返回" onPress={() => router.back()} />
        <View style={s.box}>
          <Icon name="search" size={18} color={C.ink4} />
          <TextInput
            style={s.input}
            value={q}
            onChangeText={setQ}
            placeholder="搜索工作空间里的文件"
            placeholderTextColor={C.ink4}
            autoFocus
            returnKeyType="search"
          />
          {q ? <IconButton name="x" label="清空" size={18} color={C.ink3} onPress={() => setQ("")} /> : null}
        </View>
      </View>
      {!debounced ? (
        <View style={{ padding: 16 }}>
          {recent.length > 0 && (
            <>
              <Text style={s.group}>最近搜索</Text>
              <View style={s.chips}>
                {recent.map((k) => (
                  <Text key={k} style={s.chip} onPress={() => setQ(k)}>
                    {k}
                  </Text>
                ))}
              </View>
            </>
          )}
          <Text style={[s.group, { marginTop: 16 }]}>按文件名搜索整个工作空间，包括所有子目录</Text>
        </View>
      ) : searchQ.isPending ? (
        <Loading />
      ) : results.length === 0 ? (
        <Empty icon="search" title={`没有找到「${debounced}」`}>换个关键词试试</Empty>
      ) : (
        <FlatList
          data={results}
          keyExtractor={(r) => r.key}
          keyboardShouldPersistTaps="handled"
          contentContainerStyle={{ paddingBottom: insets.bottom + 24 }}
          ListHeaderComponent={<Text style={[s.group, { paddingHorizontal: 16, paddingTop: 12 }]}>{`${results.length} 个结果${searchQ.data?.truncated ? "（只列出了一部分）" : ""}`}</Text>}
          renderItem={({ item: r }) => (
            <Pressable
              style={({ pressed }) => [s.row, pressed && { backgroundColor: C.hover }]}
              onPress={() => {
                remember();
                router.push(r.isDir ? { pathname: "/folder", params: { dir: r.path } } : { pathname: "/preview", params: { src: "ws", path: r.path } });
              }}
            >
              <FileIcon path={r.path} dir={r.isDir} />
              <View style={{ flex: 1, minWidth: 0 }}>
                <Text style={s.name} numberOfLines={1}>
                  {r.name}
                </Text>
                <Text style={s.meta} numberOfLines={1}>
                  {r.meta}
                </Text>
              </View>
            </Pressable>
          )}
        />
      )}
    </View>
  );
}

const s = StyleSheet.create({
  head: { flexDirection: "row", alignItems: "center", paddingHorizontal: 6, paddingRight: 12, height: 56 },
  box: { flex: 1, height: 40, borderRadius: R.md, backgroundColor: C.hover, flexDirection: "row", alignItems: "center", paddingLeft: 12, gap: 8 },
  input: { flex: 1, fontSize: F.input, color: C.ink, paddingVertical: 0 },
  group: { fontSize: F.xs, color: C.ink4 },
  chips: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginTop: 8 },
  chip: { height: 36, lineHeight: 34, paddingHorizontal: 14, borderRadius: 18, borderWidth: 1, borderColor: C.line, fontSize: F.md, color: C.ink2, overflow: "hidden" },
  row: { flexDirection: "row", alignItems: "center", gap: 12, minHeight: 64, paddingHorizontal: 16 },
  name: { fontSize: F.body, color: C.ink },
  meta: { fontSize: 12.5, color: C.ink3, marginTop: 1 },
});
