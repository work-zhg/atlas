"""gRPC 转换：一个方法 = 一个工具。

    发现   服务端反射（grpc.reflection.v1alpha）或上传描述文件（FileDescriptorSet，
           protoc --include_imports --descriptor_set_out）→ 存库（mcp_service.descriptor）
    参数   请求消息的字段 → JSON Schema（按 proto3 JSON 映射），参数名用 proto 的字段名
    调用   动态构造请求消息；一元直接返回，服务端流把消息合并成数组（最多 100 条）

映射（mcp_tool.mapping）：{method: "包.服务/方法", stream, request, response}

★ 只支持一元和服务端流：客户端流、双向流标成「不支持」，不能选。
★ 调用时只用库里的描述，不再反射：服务端改了接口，要管理员「重新发现」。
★ 连接按（地址, 传输安全, 证书）在进程内复用；描述按服务缓存。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import grpc
from google.protobuf import (  # noqa: F401  加载常用类型，描述文件没带它们时从默认池取
    any_pb2,
    descriptor_pb2,
    descriptor_pool,
    duration_pb2,
    empty_pb2,
    field_mask_pb2,
    json_format,
    message_factory,
    struct_pb2,
    timestamp_pb2,
    wrappers_pb2,
)
from google.protobuf.descriptor import Descriptor, FieldDescriptor
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

from . import DiscoveryFailed, UpstreamUnavailable, describe_error, tool_result, trace_headers

__all__ = [
    "TLS_MODES",
    "Channels",
    "InvalidArguments",
    "Method",
    "Pools",
    "call",
    "effect_of_method",
    "load_pool",
    "methods",
    "reflect",
    "services_in",
]

TLS_MODES = ("plaintext", "tls", "mtls")
_SYSTEM = ("grpc.reflection.", "grpc.health.")
_STREAM_LIMIT = 100
_MAX_DEPTH = 4

_SCALARS = {
    FieldDescriptor.TYPE_DOUBLE: "number",
    FieldDescriptor.TYPE_FLOAT: "number",
    FieldDescriptor.TYPE_INT32: "integer",
    FieldDescriptor.TYPE_SINT32: "integer",
    FieldDescriptor.TYPE_SFIXED32: "integer",
    FieldDescriptor.TYPE_UINT32: "integer",
    FieldDescriptor.TYPE_FIXED32: "integer",
    FieldDescriptor.TYPE_INT64: "integer",
    FieldDescriptor.TYPE_SINT64: "integer",
    FieldDescriptor.TYPE_SFIXED64: "integer",
    FieldDescriptor.TYPE_UINT64: "integer",
    FieldDescriptor.TYPE_FIXED64: "integer",
    FieldDescriptor.TYPE_BOOL: "boolean",
    FieldDescriptor.TYPE_STRING: "string",
    FieldDescriptor.TYPE_BYTES: "string",
}
#: 常用类型（Well-Known Types）的 JSON 形状
_WELL_KNOWN: dict[str, dict[str, Any]] = {
    "google.protobuf.Timestamp": {"type": "string", "format": "date-time"},
    "google.protobuf.Duration": {"type": "string", "description": "如 1.5s"},
    "google.protobuf.FieldMask": {"type": "string"},
    "google.protobuf.Struct": {"type": "object"},
    "google.protobuf.Value": {},
    "google.protobuf.ListValue": {"type": "array"},
    "google.protobuf.Empty": {"type": "object"},
    "google.protobuf.Any": {"type": "object"},
    "google.protobuf.StringValue": {"type": "string"},
    "google.protobuf.BytesValue": {"type": "string"},
    "google.protobuf.BoolValue": {"type": "boolean"},
    "google.protobuf.DoubleValue": {"type": "number"},
    "google.protobuf.FloatValue": {"type": "number"},
    "google.protobuf.Int32Value": {"type": "integer"},
    "google.protobuf.UInt32Value": {"type": "integer"},
    "google.protobuf.Int64Value": {"type": "integer"},
    "google.protobuf.UInt64Value": {"type": "integer"},
}


class InvalidArguments(Exception):
    """参数转不成请求消息（字段不存在、类型不对）。"""


def effect_of_method(method: str) -> str:
    if re.match(r"^(Get|List|Search|Query|Find|Export|Watch|Describe|Check)", method):
        return "read"
    if re.match(r"^(Delete|Remove|Purge|Drop)", method):
        return "destructive"
    return "write"


def _snake(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return (text if text and text[0].isalpha() else f"rpc_{text}")[:64]


@dataclass
class Method:
    """描述里的一个方法（发现结果，登记前给管理员挑）。"""

    service: str
    method: str
    stream: str
    request: str
    response: str
    name: str
    input_schema: dict[str, Any]
    unsupported: str | None = None

    @property
    def key(self) -> str:
        return f"{self.service}/{self.method}"

    @property
    def effect(self) -> str:
        return effect_of_method(self.method)

    def mapping(self) -> dict[str, Any]:
        return {
            "method": self.key,
            "stream": self.stream,
            "request": self.request,
            "response": self.response,
        }


# ───────────────────────────── 连接


class Channels:
    """进程内复用 gRPC 连接。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], grpc.aio.Channel] = {}

    def get(self, address: str, tls: str, *, cert: str | None, key: str | None) -> grpc.aio.Channel:
        ident = hashlib.sha256(f"{cert or ''}\n{key or ''}".encode()).hexdigest()
        row = self._rows.get((address, tls, ident))
        if row is not None:
            return row
        if tls == "plaintext":
            channel = grpc.aio.insecure_channel(address)
        elif tls == "mtls":
            creds = grpc.ssl_channel_credentials(
                private_key=(key or "").encode(), certificate_chain=(cert or "").encode()
            )
            channel = grpc.aio.secure_channel(address, creds)
        else:
            channel = grpc.aio.secure_channel(address, grpc.ssl_channel_credentials())
        self._rows[(address, tls, ident)] = channel
        return channel

    async def aclose(self) -> None:
        for channel in self._rows.values():
            await channel.close()
        self._rows.clear()


# ───────────────────────────── 发现


async def reflect(
    channel: grpc.aio.Channel, *, metadata: Sequence[tuple[str, str]], timeout_s: float
) -> bytes:
    """服务端反射 → 序列化的 FileDescriptorSet（含依赖）。"""
    stub = reflection_pb2_grpc.ServerReflectionStub(channel)
    files: dict[str, descriptor_pb2.FileDescriptorProto] = {}
    try:
        call = stub.ServerReflectionInfo(metadata=list(metadata), timeout=timeout_s)

        async def ask(**kw: Any) -> reflection_pb2.ServerReflectionResponse:
            await call.write(reflection_pb2.ServerReflectionRequest(**kw))
            resp = await call.read()
            if resp is grpc.aio.EOF:
                raise DiscoveryFailed("服务端反射提前结束")
            if resp.HasField("error_response"):
                raise DiscoveryFailed(f"服务端反射出错：{resp.error_response.error_message}")
            return resp

        def keep(resp: reflection_pb2.ServerReflectionResponse) -> None:
            for raw in resp.file_descriptor_response.file_descriptor_proto:
                proto = descriptor_pb2.FileDescriptorProto.FromString(raw)
                files.setdefault(proto.name, proto)

        listed = await ask(list_services="")
        services = [s.name for s in listed.list_services_response.service]
        for name in services:
            if not name.startswith(_SYSTEM):
                keep(await ask(file_containing_symbol=name))
        pending = [d for f in list(files.values()) for d in f.dependency if d not in files]
        while pending:
            dep = pending.pop()
            if dep in files or _well_known(dep):
                continue
            keep(await ask(file_by_filename=dep))
            pending.extend(d for f in list(files.values()) for d in f.dependency if d not in files)
        await call.done_writing()
    except grpc.aio.AioRpcError as exc:
        if exc.code() == grpc.StatusCode.UNIMPLEMENTED:
            raise DiscoveryFailed("服务端没有开启反射：请上传描述文件") from exc
        raise DiscoveryFailed(f"服务端反射失败：{exc.code().name} {exc.details()}") from exc
    if not files:
        raise DiscoveryFailed("服务端反射没有返回任何服务")
    return descriptor_pb2.FileDescriptorSet(file=list(files.values())).SerializeToString()


def _well_known(name: str) -> bool:
    try:
        descriptor_pool.Default().FindFileByName(name)
    except KeyError:
        return False
    return True


def load_pool(data: bytes) -> descriptor_pool.DescriptorPool:
    try:
        fds = descriptor_pb2.FileDescriptorSet.FromString(data)
    except Exception as exc:
        raise DiscoveryFailed("描述文件不是 FileDescriptorSet") from exc
    files = {f.name: f for f in fds.file}
    pool = descriptor_pool.DescriptorPool()
    added: set[str] = set()

    def add(name: str, depth: int = 0) -> None:
        if name in added:
            return
        if depth > 50:
            raise DiscoveryFailed("描述文件的依赖有循环")
        proto = files.get(name)
        if proto is None:
            try:
                found = descriptor_pool.Default().FindFileByName(name)
            except KeyError as exc:
                raise DiscoveryFailed(
                    f"描述文件缺少依赖 {name}（生成时加 --include_imports）"
                ) from exc
            proto = descriptor_pb2.FileDescriptorProto()
            found.CopyToProto(proto)
        for dep in proto.dependency:
            add(dep, depth + 1)
        pool.Add(proto)
        added.add(name)

    for name in files:
        add(name)
    return pool


def methods(data: bytes) -> list[Method]:
    """描述里的全部方法（跳过反射、健康检查这类系统服务）。"""
    pool = load_pool(data)
    fds = descriptor_pb2.FileDescriptorSet.FromString(data)
    out: list[Method] = []
    taken: set[str] = set()
    for f in fds.file:
        for svc in f.service:
            full = f"{f.package}.{svc.name}" if f.package else svc.name
            if full.startswith(_SYSTEM):
                continue
            desc = pool.FindServiceByName(full)
            for m in desc.methods:
                stream = (
                    "bidi"
                    if m.client_streaming and m.server_streaming
                    else "client"
                    if m.client_streaming
                    else "server"
                    if m.server_streaming
                    else "unary"
                )
                name = _snake(m.name)
                base, n = name, 2
                while name in taken:
                    name, n = f"{base[:60]}_{n}", n + 1
                taken.add(name)
                out.append(
                    Method(
                        service=full,
                        method=m.name,
                        stream=stream,
                        request=m.input_type.full_name,
                        response=m.output_type.full_name,
                        name=name,
                        input_schema=message_schema(m.input_type),
                        unsupported={"client": "客户端流", "bidi": "双向流"}.get(stream),
                    )
                )
    return out


def services_in(data: bytes) -> list[str]:
    fds = descriptor_pb2.FileDescriptorSet.FromString(data)
    return [
        name
        for f in fds.file
        for s in f.service
        if not (name := f"{f.package}.{s.name}" if f.package else s.name).startswith(_SYSTEM)
    ]


def message_schema(desc: Descriptor, depth: int = 0) -> dict[str, Any]:
    if desc.full_name in _WELL_KNOWN:
        return dict(_WELL_KNOWN[desc.full_name])
    if depth >= _MAX_DEPTH:
        return {"type": "object"}
    props = {f.name: _field_schema(f, depth) for f in desc.fields}
    return {"type": "object", "properties": props}


def _field_schema(f: FieldDescriptor, depth: int) -> dict[str, Any]:
    if f.message_type is not None and f.message_type.GetOptions().map_entry:
        value = f.message_type.fields_by_name["value"]
        return {"type": "object", "additionalProperties": _single(value, depth + 1)}
    single = _single(f, depth + 1)
    if f.is_repeated:
        return {"type": "array", "items": single}
    return single


def _single(f: FieldDescriptor, depth: int) -> dict[str, Any]:
    if f.type == FieldDescriptor.TYPE_ENUM:
        return {"type": "string", "enum": [v.name for v in f.enum_type.values]}
    if f.type in (FieldDescriptor.TYPE_MESSAGE, FieldDescriptor.TYPE_GROUP):
        return message_schema(f.message_type, depth)
    schema: dict[str, Any] = {"type": _SCALARS.get(f.type, "string")}
    if f.type == FieldDescriptor.TYPE_BYTES:
        schema["description"] = "base64"
    return schema


# ───────────────────────────── 调用


class Pools:
    """服务的描述 → DescriptorPool（按描述的哈希缓存）。"""

    def __init__(self) -> None:
        self._rows: dict[str, descriptor_pool.DescriptorPool] = {}

    def get(self, data: bytes) -> descriptor_pool.DescriptorPool:
        digest = hashlib.sha256(data).hexdigest()
        pool = self._rows.get(digest)
        if pool is None:
            if len(self._rows) > 200:
                self._rows.clear()
            pool = self._rows[digest] = load_pool(data)
        return pool


async def call(
    channel: grpc.aio.Channel,
    pool: descriptor_pool.DescriptorPool,
    mapping: Mapping[str, Any],
    arguments: Mapping[str, Any],
    *,
    metadata: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    """调一次方法 → MCP 线上格式的 CallToolResult。连不上抛 UpstreamUnavailable，
    参数不对抛 InvalidArguments；其余 gRPC 错误作为工具错误返回。"""
    request_cls = message_factory.GetMessageClass(pool.FindMessageTypeByName(mapping["request"]))
    response_cls = message_factory.GetMessageClass(pool.FindMessageTypeByName(mapping["response"]))
    try:
        request = json_format.ParseDict(dict(arguments), request_cls())
    except json_format.ParseError as exc:
        raise InvalidArguments(str(exc)) from exc
    md = [*metadata, *((k.lower(), v) for k, v in trace_headers().items())]
    path = f"/{mapping['method']}"
    try:
        if mapping.get("stream") == "server":
            stream = channel.unary_stream(
                path,
                request_serializer=request_cls.SerializeToString,
                response_deserializer=response_cls.FromString,
            )(request, metadata=md)
            messages: list[dict[str, Any]] = []
            truncated = False
            async for msg in stream:
                if len(messages) >= _STREAM_LIMIT:
                    truncated = True
                    stream.cancel()
                    break
                messages.append(json_format.MessageToDict(msg, preserving_proto_field_name=True))
            data: dict[str, Any] = {"messages": messages}
            text = json.dumps(messages, ensure_ascii=False)
            if truncated:
                text += f"\n…（流里超过 {_STREAM_LIMIT} 条消息，只取了前 {_STREAM_LIMIT} 条）"
            return tool_result(text, structured=data)
        response = await channel.unary_unary(
            path,
            request_serializer=request_cls.SerializeToString,
            response_deserializer=response_cls.FromString,
        )(request, metadata=md)
    except grpc.aio.AioRpcError as exc:
        if exc.code() == grpc.StatusCode.UNAVAILABLE:
            raise UpstreamUnavailable(f"gRPC UNAVAILABLE: {exc.details()}") from exc
        return tool_result(f"gRPC {exc.code().name}: {exc.details() or ''}".strip(), is_error=True)
    except grpc.RpcError as exc:
        raise UpstreamUnavailable(describe_error(exc)) from exc
    data = json_format.MessageToDict(response, preserving_proto_field_name=True)
    return tool_result(json.dumps(data, ensure_ascii=False), structured=data)
