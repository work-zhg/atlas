"""cluster 服务的入口。

★ 部署时必须显式给 backend=kubernetes。默认的内存实现只用于本机开发 ——
  一个「起来了但什么都没真的创建」的 cluster 会让 acp 的每一轮都超时，
  而日志上看不出原因。
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from atlas_cluster.api import create_app
from atlas_cluster.backend import InMemoryBackend
from atlas_cluster.config import ClusterSettings


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = ClusterSettings()
    mode = os.environ.get("ATLAS_CLUSTER_BACKEND", "").strip().lower()

    if mode == "kubernetes":
        from atlas_cluster.k8s import KubernetesBackend

        backend = KubernetesBackend(settings.namespace)
    elif mode == "memory":
        logging.getLogger(__name__).warning(
            "cluster 以内存后端启动 —— 不会真的创建任何 Pod，仅供本机开发"
        )
        backend = InMemoryBackend()
    else:
        sys.stderr.write(
            "必须显式设置 ATLAS_CLUSTER_BACKEND=kubernetes|memory —— "
            "默认值会让「没配集群」这件事静默发生\n"
        )
        raise SystemExit(2)

    uvicorn.run(
        create_app(backend=backend, settings=settings),
        host=os.environ.get("ATLAS_CLUSTER_HOST", "0.0.0.0"),  # noqa: S104
        port=_port(),
    )


def _port() -> int:
    """监听端口。

    ★ 首选 ATLAS_CLUSTER_HTTP_PORT，而不是更顺眼的 ATLAS_CLUSTER_PORT ——
      后者会被 K8s 占用：只要同 namespace 里有一个叫 atlas-cluster 的
      Service（而 server 的默认 cluster_base_url 就是 http://atlas-cluster:8010，
      也就是说它多半就叫这个名），kubelet 就会注入一组遗留变量，其中
      ATLAS_CLUSTER_PORT=tcp://<clusterIP>:8010。直接 int() 它的结果是
      **进程起不来**，而报错指向端口配置，离真因（Service 名与配置前缀
      撞车）很远。

    ★ 旧名仍然认，但解析不出整数就当没设过：那种值一定是 K8s 注入的，
      不是人写的。人写错了会在这里拿到默认端口 —— 比崩溃好，因为崩溃
      在 K8s 里表现为 CrashLoopBackOff，而日志要翻好几层才看得到。
    """
    for name in ("ATLAS_CLUSTER_HTTP_PORT", "ATLAS_CLUSTER_PORT"):
        raw = os.environ.get(name, "").strip()
        if raw.isdigit():
            return int(raw)
        if raw:
            logging.getLogger(__name__).warning(
                "%s=%r 不是端口号，已忽略（K8s 的 service link 会注入这种值）", name, raw
            )
    return 8010


if __name__ == "__main__":
    main()
