# 原生沙箱的 egress sidecar：上游镜像 + 平台底线 deny.always（拒绝集群网段）。
#
# ★ deny.always 优先级高于逐沙箱的出站策略，sidecar 每分钟热加载；路径是上游写死的
#   （components/egress/pkg/policy/always_rules.go）。sidecar 由 lifecycle server 注入，挂不进 ConfigMap，
#   只能打进镜像。
# ★ deny.always 按目标集群生成（deploy/local/egress-rules.sh），不进仓库：集群网段因集群而异。
#   集群网段变了就重新生成、重建镜像、滚动重启 opensandbox-sbx。
FROM sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/egress:release-1.1.1
COPY deny.always /var/egress/rules/deny.always
