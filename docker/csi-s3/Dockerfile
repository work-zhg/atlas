# 本机的 S3 CSI 驱动镜像：k8s-csi-s3 + s3fs（deploy/local/05-csi-s3.yaml，doc/sandbox-opensandbox-design.html §6）。
#
#   docker build -f docker/csi-s3/Dockerfile -t atlas-csi-s3:0.43.7 \
#     --build-arg HTTP_PROXY=http://host.docker.internal:58591 \
#     --build-arg HTTPS_PROXY=http://host.docker.internal:58591 docker/csi-s3
#
# ★ 为什么不用上游镜像：
#   · cr.yandex 上的镜像只有 amd64，本机是 arm64；
#   · 上游装的是 Alpine edge 的 s3fs 1.97。s3fs 1.96 起的目录列表缓存会让「只含子目录的目录」
#     rename 做一半就报 ENOTEMPTY，新旧两棵树各留一半（P0 实测；上游 d400a5c 已修，未发布）。
#     这里用 Alpine 3.23 稳定源里的 s3fs 1.95 —— 没有那层缓存。s3fs 发布含修复的版本后再升。
# ★ 只用 s3fs mounter：不装 geesefs / rclone。
FROM golang:1.25-alpine AS build
ARG CSI_S3_VERSION=v0.43.7
RUN apk add --no-cache git
RUN git clone --depth 1 --branch "${CSI_S3_VERSION}" https://github.com/yandex-cloud/k8s-csi-s3.git /src
WORKDIR /src
RUN go mod download
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags '-extldflags "-static"' -o /s3driver ./cmd/s3driver

FROM alpine:3.23
RUN apk add --no-cache fuse mailcap s3fs-fuse
COPY --from=build /s3driver /s3driver
ENTRYPOINT ["/s3driver"]
