"""对着真网关跑的测试用具：在 Docker 里起 OpenResty（渲染好的配置 + 测试桩），用裸 TLS 发请求。

★ 为什么自己拼 HTTP：安全回归要发畸形路径（%252f、反斜杠、..），httpx 之类的客户端会先把它们规范化。
★ 测试桩（tests/stub）是同一个容器里的另一个 OpenResty，扮演内部 Ingress（127.0.0.1:18080）。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from atlas_gateway.model import load
from atlas_gateway.render import render
from atlas_gateway.rules import check
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

IMAGE = os.environ.get("ATLAS_GATEWAY_TEST_IMAGE", "openresty/openresty:1.27.1.2-bookworm-fat")
ROUTES = Path(__file__).resolve().parents[1] / "routes.yaml"
STUB = Path(__file__).resolve().parent / "stub"
ATLAS = "atlas.test"
LANGFUSE = "langfuse.test"
ISSUER, AUDIENCE = "atlas-usercenter", "atlas-gateway"


def docker_ok() -> str | None:
    """能跑就返回 None，否则返回跳过的原因。"""
    if shutil.which("docker") is None:
        return "没有 docker"
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return "Docker 没在运行"
    has = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0
    if not has and subprocess.run(["docker", "pull", IMAGE], capture_output=True).returncode != 0:
        return f"拉不到镜像 {IMAGE}"
    return None


# ─────────────────────────────────────────────── 令牌与证书


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class SigningKey:
    kid: str
    key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)

    def jwk(self) -> dict:
        raw = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return {
            "kty": "OKP",
            "crv": "Ed25519",
            "kid": self.kid,
            "x": b64url(raw),
            "use": "sig",
            "alg": "EdDSA",
        }

    def token(self, *, header: dict | None = None, **claims) -> str:
        now = int(time.time())
        body = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": str(uuid.uuid4()),
            "tid": str(uuid.uuid4()),
            "mcp": False,
            "iat": now,
            "exp": now + 900,
            **claims,
        }
        h = {"alg": "EdDSA", "typ": "JWT", "kid": self.kid, **(header or {})}
        signing_input = f"{b64url(json.dumps(h).encode())}.{b64url(json.dumps(body).encode())}"
        return f"{signing_input}.{b64url(self.key.sign(signing_input.encode()))}"


def jwks(*keys: SigningKey) -> str:
    return json.dumps({"keys": [k.jwk() for k in keys]})


def self_signed(directory: Path, host: str) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    d = directory / host
    d.mkdir(parents=True, exist_ok=True)
    (d / "fullchain.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "privkey.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


# ─────────────────────────────────────────────── 裸 HTTP


@dataclass
class Resp:
    status: int
    headers: dict[str, list[str]]
    body: bytes

    def header(self, name: str) -> str | None:
        v = self.headers.get(name.lower())
        return v[0] if v else None

    def json(self):
        return json.loads(self.body)


def _dechunk(data: bytes) -> bytes:
    out = b""
    while data:
        line, _, data = data.partition(b"\r\n")
        size = int(line.split(b";")[0] or b"0", 16)
        if size == 0:
            break
        out, data = out + data[:size], data[size + 2 :]
    return out


def parse(data: bytes) -> Resp:
    if not data:
        return Resp(0, {}, b"")
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers.setdefault(k.strip().lower(), []).append(v.strip())
    if "chunked" in ",".join(headers.get("transfer-encoding", [])).lower():
        body = _dechunk(body)
    return Resp(status, headers, body)


class Gateway:
    """一个跑着的网关容器。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.name = f"atlas-gw-test-{uuid.uuid4().hex[:8]}"
        self.state = root / "state"
        self.ports: dict[int, int] = {}

    # ── 生命周期
    def prepare(self, **env: str) -> None:
        certs, acme, out = self.root / "certs", self.root / "acme", self.root / "out"
        for d in (certs, acme / ".well-known" / "acme-challenge", self.state / "cache"):
            d.mkdir(parents=True, exist_ok=True)
        # 容器里的 worker 是 nobody：状态目录要可写（公钥缓存文件）
        for d in (self.state, self.state / "cache"):
            d.chmod(0o777)
        for host in (ATLAS, LANGFUSE):
            self_signed(certs, host)
        (acme / ".well-known" / "acme-challenge" / "tok-123").write_text("acme-proof")
        table = load(
            ROUTES,
            {
                "ATLAS_PUBLIC_HOST": ATLAS,
                "ATLAS_LANGFUSE_HOST": LANGFUSE,
                "GATEWAY_INTERNAL_INGRESS": "127.0.0.1:18080",
                "GATEWAY_CERT_DIR": str(certs),
                "GATEWAY_LOG_DIR": "/usr/local/openresty/nginx/logs",
                "GATEWAY_ADMIN_LISTEN": "0.0.0.0:8081",
                "GATEWAY_JWKS_CACHE": "/state/cache/jwks.json",
                "GATEWAY_JWKS_RETRY": "1",
                "GATEWAY_JWKS_REFRESH": "2",
                "GATEWAY_BAN_THRESHOLD": "3",
                "GATEWAY_BAN_WINDOW": "60",
                "GATEWAY_BAN_DURATION": "120",
                **env,
            },
        )
        problems = check(table, cert_root=certs)
        assert not problems, problems
        render(table, out, install_dir="/etc/openresty/atlas")

    def start(self) -> None:
        certs = self.root / "certs"
        cmd = [
            "docker", "run", "-d", "--name", self.name,
            "-p", "127.0.0.1::443", "-p", "127.0.0.1::80", "-p", "127.0.0.1::8081",
            "-v", f"{self.root / 'out'}:/etc/openresty/atlas:ro",
            "-v", f"{STUB}:/etc/openresty/stub:ro",
            "-v", f"{certs}:{certs}:ro",
            "-v", f"{self.root / 'acme'}:/var/www/acme:ro",
            "-v", f"{self.state}:/state",
            IMAGE, "sh", "-c",
            "openresty -c /etc/openresty/stub/nginx.conf && "
            "exec openresty -c /etc/openresty/atlas/nginx.conf -g 'daemon off;'",
        ]  # fmt: skip
        subprocess.run(cmd, check=True, capture_output=True)
        for port in (443, 80, 8081):
            out = subprocess.run(
                ["docker", "port", self.name, f"{port}/tcp"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.ports[port] = int(out.stdout.strip().splitlines()[0].rsplit(":", 1)[1])
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if self.admin("GET", "/healthz").status == 200:
                    return
            except OSError:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"网关没起来：\n{self.logs()}")

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)

    def logs(self) -> str:
        # 官方镜像里 logs/error.log、access.log 链接到 stderr / stdout：日志都在 docker logs 里
        out = subprocess.run(
            ["docker", "logs", "--tail", "200", self.name], capture_output=True, text=True
        )
        return out.stdout + out.stderr

    def exec(self, script: str) -> str:
        out = subprocess.run(
            ["docker", "exec", self.name, "sh", "-c", script], capture_output=True, text=True
        )
        return out.stdout + out.stderr

    def set_jwks(self, *keys: SigningKey) -> None:
        self.set_jwks_text(jwks(*keys))

    def set_jwks_text(self, text: str) -> None:
        tmp = self.state / "uc-jwks.json.tmp"
        tmp.write_text(text)
        tmp.replace(self.state / "uc-jwks.json")

    def wait_ready(self, timeout: float = 15) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.admin("GET", "/readyz").status == 200:
                return
            time.sleep(0.2)
        raise AssertionError(f"公钥一直没拉到：\n{self.logs()}")

    # ── 请求
    def connect(self, port: int, sni: str | None) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        if sni is None:
            return sock
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx.wrap_socket(sock, server_hostname=sni)

    def raw(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        host: str = ATLAS,
        port: int = 443,
        tls: bool = True,
        content_length: int | None = None,
    ) -> Resp:
        hdrs = {"Host": host, "Connection": "close", **(headers or {})}
        if body or content_length is not None or method in ("POST", "PUT", "PATCH", "DELETE"):
            hdrs.setdefault(
                "Content-Length", str(len(body) if content_length is None else content_length)
            )
        head = (
            f"{method} {path} HTTP/1.1\r\n"
            + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
            + "\r\n"
        )
        sock = self.connect(self.ports[port], host if tls else None)
        try:
            sock.sendall(head.encode("latin-1") + body)
            data = b""
            while True:
                try:
                    chunk = sock.recv(65536)
                except (ConnectionResetError, ssl.SSLError):
                    break
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        return parse(data)

    def get(self, path: str, **kw) -> Resp:
        return self.raw("GET", path, **kw)

    def post(self, path: str, body: bytes = b"{}", **kw) -> Resp:
        return self.raw("POST", path, body=body, **kw)

    def admin(self, method: str, path: str) -> Resp:
        return self.raw(method, path, host="localhost", port=8081, tls=False)

    def stream(self, path: str, headers: dict[str, str], until: bytes) -> list[tuple[float, bytes]]:
        """收流式响应：返回 [(距发出请求的秒数, 这次收到的字节)]，收到 until 为止。"""
        hdrs = {"Host": ATLAS, "Connection": "close", **headers}
        head = (
            f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
        )
        sock = self.connect(self.ports[443], ATLAS)
        t0 = time.time()
        sock.sendall(head.encode())
        got: list[tuple[float, bytes]] = []
        buf = b""
        while until not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            got.append((time.time() - t0, chunk))
        sock.close()
        return got

    def websocket(self, path: str, headers: dict[str, str], text: str) -> tuple[Resp, str | None]:
        """握手 + 发一帧文本 + 收一帧。返回（握手响应，收到的文本）。"""
        key = base64.b64encode(os.urandom(16)).decode()
        hdrs = {
            "Host": ATLAS,
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": key,
            "Sec-WebSocket-Version": "13",
            **headers,
        }
        head = (
            f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
        )
        sock = self.connect(self.ports[443], ATLAS)
        try:
            sock.sendall(head.encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    return parse(buf), None
                buf += chunk
            head_bytes, _, rest = buf.partition(b"\r\n\r\n")
            resp = parse(head_bytes + b"\r\n\r\n")
            if resp.status != 101:
                # 没升级：收完响应体（连接是 keep-alive，不能读到关）
                chunked = "chunked" in (resp.header("transfer-encoding") or "")
                need = int(resp.header("content-length") or 0)
                while (b"0\r\n\r\n" not in rest) if chunked else (len(rest) < need):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    rest += chunk
                return Resp(resp.status, resp.headers, _dechunk(rest) if chunked else rest), None
            payload = text.encode()
            mask = os.urandom(4)
            frame = (
                bytes([0x81, 0x80 | len(payload)])
                + mask
                + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            )
            sock.sendall(frame)
            while len(rest) < 2:
                rest += sock.recv(4096)
            length = rest[1] & 0x7F
            offset = 2
            if length == 126:
                while len(rest) < 4:
                    rest += sock.recv(4096)
                length = struct.unpack(">H", rest[2:4])[0]
                offset = 4
            while len(rest) < offset + length:
                rest += sock.recv(4096)
            return resp, rest[offset : offset + length].decode()
        finally:
            sock.close()
