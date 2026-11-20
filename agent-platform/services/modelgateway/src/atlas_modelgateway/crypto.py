"""账号密钥的信封加密：每个密钥一把随机的数据密钥，数据密钥再用主密钥加密。

    存库的样子   v1:<加密后的数据密钥>:<加密后的密钥>
    主密钥       ATLAS_MODELGW_SECRET_KEYS（K8s Secret），逗号分隔，第一把用来加密，其余只用来解密
                 —— 换主密钥时把新的放在最前面，旧的留着，等全部重新加密后再去掉

★ 管理平台加密、网关解密，同一个实现。明文只在网关内存里，不写日志、不进调用记录。
"""

from __future__ import annotations

from collections.abc import Sequence

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

__all__ = ["CipherError", "SecretCipher"]

_VERSION = "v1"


class CipherError(RuntimeError):
    pass


class SecretCipher:
    def __init__(self, master_keys: Sequence[str]) -> None:
        keys = [k.strip() for k in master_keys if k and k.strip()]
        self._master = MultiFernet([Fernet(k.encode()) for k in keys]) if keys else None

    @property
    def configured(self) -> bool:
        return self._master is not None

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, secret: str) -> str:
        if self._master is None:
            raise CipherError("没有配置主密钥（ATLAS_MODELGW_SECRET_KEYS），不能保存账号密钥")
        dek = Fernet.generate_key()
        wrapped = self._master.encrypt(dek).decode()
        body = Fernet(dek).encrypt(secret.encode()).decode()
        return f"{_VERSION}:{wrapped}:{body}"

    def decrypt(self, token: str) -> str:
        if self._master is None:
            raise CipherError("没有配置主密钥（ATLAS_MODELGW_SECRET_KEYS），解不开账号密钥")
        version, _, rest = token.partition(":")
        wrapped, _, body = rest.partition(":")
        if version != _VERSION or not wrapped or not body:
            raise CipherError("账号密钥的格式不对")
        try:
            dek = self._master.decrypt(wrapped.encode())
            return Fernet(dek).decrypt(body.encode()).decode()
        except InvalidToken as exc:
            raise CipherError("解不开账号密钥（主密钥不对）") from exc

    def rotate(self, token: str) -> str:
        """用当前的主密钥重新加密（换主密钥时用）。"""
        return self.encrypt(self.decrypt(token))
