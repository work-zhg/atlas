#!/usr/bin/env bash
# 在一台新机器上搭单节点 K8s（在那台机器上以 root 运行），组合与 112 一致：
#   containerd（docker 官方源，docker 同时用来在服务器上构建镜像）、kubeadm 1.36、Calico 3.33（tigera-operator，
#   VXLANCrossSubnet、hostPorts）、local-path（local-path / local-path-retain）、metrics-server、
#   traefik（hostPort 80/443，IngressClass traefik）、helm、uv（images.sh 导出依赖用）。
#
#   PUBLIC_IP=34.97.143.215 DOMAIN=apijp.techstz.com SMALL_NODE=1 deploy/server/cluster-setup.sh
#
#   PUBLIC_IP / DOMAIN  写进 apiserver 证书的 SAN（可空）
#   SMALL_NODE=1        2 核的小机器：装 small-node-cpu.yaml（Pod 的 CPU requests 压到 1/4）、CoreDNS 只留 1 个副本
#
# 之后：git clone 仓库到这台机器、放好 .env，在仓库里跑 deploy/server/images.sh 与 deploy/server/deploy.sh
# （两者发现本机是 K8s 节点就不经 ssh）。只在 Debian 13 上跑过（2026-11-11，GCP asia-northeast2）。
# ★ 可重复执行：kubeadm init 只在还没有集群时做。
set -euxo pipefail
cd "$(dirname "$0")/../.."
export DEBIAN_FRONTEND=noninteractive
PUBLIC_IP="${PUBLIC_IP:-}"
DOMAIN="${DOMAIN:-}"
. /etc/os-release

# ── 内核参数
cat >/etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF
modprobe overlay; modprobe br_netfilter
cat >/etc/sysctl.d/99-k8s.conf <<EOF
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
fs.inotify.max_user_instances       = 1024
fs.inotify.max_user_watches         = 1048576
EOF
sysctl --system >/dev/null
swapoff -a

apt-get update -q
apt-get install -y -q ca-certificates curl gpg apt-transport-https git jq socat conntrack ipset ebtables \
  libdigest-sha-perl make python3 nfs-common

# ── docker + containerd（docker 官方源）
install -m 0755 -d /etc/apt/keyrings
curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
  >/etc/apt/sources.list.d/docker.list
# ── K8s 1.36
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.36/deb/Release.key | gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes.gpg] https://pkgs.k8s.io/core:/stable:/v1.36/deb/ /" \
  >/etc/apt/sources.list.d/kubernetes.list
apt-get update -q
apt-get install -y -q containerd.io docker-ce docker-ce-cli docker-buildx-plugin kubelet kubeadm kubectl cri-tools
apt-mark hold kubelet kubeadm kubectl

# containerd 包默认关了 CRI：重新生成默认配置，开 systemd cgroup
containerd config default >/etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
grep -q 'SystemdCgroup = true' /etc/containerd/config.toml || \
  sed -i "/\[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc.options\]/a\            SystemdCgroup = true" /etc/containerd/config.toml
systemctl restart containerd
systemctl enable --now kubelet docker
cat >/etc/crictl.yaml <<EOF
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint: unix:///run/containerd/containerd.sock
EOF

# ── kubeadm
if [ ! -f /etc/kubernetes/admin.conf ]; then
  NODE_IP="$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
  cat >/root/kubeadm.yaml <<EOF
apiVersion: kubeadm.k8s.io/v1beta4
kind: InitConfiguration
localAPIEndpoint:
  advertiseAddress: ${NODE_IP}
nodeRegistration:
  criSocket: unix:///run/containerd/containerd.sock
---
apiVersion: kubeadm.k8s.io/v1beta4
kind: ClusterConfiguration
clusterName: atlas
networking:
  podSubnet: 10.244.0.0/16
  serviceSubnet: 10.96.0.0/12
apiServer:
  certSANs: [$(printf '%s, ' ${NODE_IP} ${PUBLIC_IP} ${DOMAIN})$(hostname)]
---
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: systemd
maxPods: 250
EOF
  kubeadm init --config /root/kubeadm.yaml
fi
mkdir -p /root/.kube && cp /etc/kubernetes/admin.conf /root/.kube/config
export KUBECONFIG=/etc/kubernetes/admin.conf
kubectl taint nodes --all node-role.kubernetes.io/control-plane- 2>/dev/null || true

# ── Calico（与 112 同版本、同网络参数；不装 calico-apiserver / goldmane / whisker，省 CPU）
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.33.0/manifests/operator-crds.yaml 2>/dev/null || true
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.33.0/manifests/tigera-operator.yaml 2>/dev/null || true
kubectl wait --for condition=established --timeout=120s crd/installations.operator.tigera.io
cat <<EOF | kubectl apply -f -
apiVersion: operator.tigera.io/v1
kind: Installation
metadata:
  name: default
spec:
  calicoNetwork:
    hostPorts: Enabled
    ipPools:
      - name: default-ipv4-ippool
        blockSize: 26
        cidr: 10.244.0.0/16
        encapsulation: VXLANCrossSubnet
        natOutgoing: Enabled
        nodeSelector: all()
  controlPlaneReplicas: 1
EOF
for _ in $(seq 60); do kubectl get nodes | grep -q ' Ready ' && break; sleep 5; done
kubectl get nodes

# ── local-path（默认 + retain 两个 StorageClass）
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.37/deploy/local-path-storage.yaml
kubectl patch storageclass local-path -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
cat <<EOF | kubectl apply -f -
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: local-path-retain
provisioner: rancher.io/local-path
reclaimPolicy: Retain
volumeBindingMode: WaitForFirstConsumer
EOF

# ── metrics-server
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.9.0/components.yaml
kubectl -n kube-system patch deploy metrics-server --type=json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' || true

# ── helm + traefik（hostPort 80/443，与 112 同 chart 版本）
if ! command -v helm >/dev/null; then
  curl -fsSL https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz | tar xz -C /tmp linux-amd64/helm
  install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm
fi
helm repo add traefik https://traefik.github.io/charts >/dev/null 2>&1 || true
helm repo update traefik >/dev/null
cat >/root/traefik-values.yaml <<EOF
ingressClass:
  enabled: true
  isDefaultClass: true
ports:
  web:
    hostPort: 80
  websecure:
    hostPort: 443
resources:
  limits:
    memory: 256Mi
  requests:
    cpu: 100m
    memory: 64Mi
service:
  type: ClusterIP
updateStrategy:
  rollingUpdate:
    maxSurge: 0
    maxUnavailable: 1
  type: RollingUpdate
EOF
helm upgrade --install traefik traefik/traefik --version 42.0.0 -n traefik --create-namespace -f /root/traefik-values.yaml

# ── uv（images.sh app 按 uv.lock 导出依赖：docker/app/requirements.sh）
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# ── 小机器
if [ "${SMALL_NODE:-}" = 1 ]; then
  kubectl apply -f deploy/server/small-node-cpu.yaml
  kubectl -n kube-system scale deploy coredns --replicas=1
fi

kubectl get pods -A
