"""Optional current-user Windows DPAPI storage, bound to an exact model endpoint.

Launch profiles contain no secret. Other platforms keep using environment credentials.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path

from traceh.cli.tui_config import LaunchConfigurationError, atomic_json


def available() -> bool:
    return os.name == "nt"


def _identity(args) -> str:
    from traceh.cli.tui_config import form_values, validate_values

    validate_values(form_values(args))
    if args.provider != "openai-compatible" or not args.base_url:
        raise LaunchConfigurationError("请先选择模型连接，再保存密钥。")
    value = f"{args.provider}\n{args.base_url}\n{args.api_key_env or 'OPENAI_API_KEY'}"
    return hashlib.sha256(value.encode()).hexdigest()


def credential_root() -> Path:
    return Path.home() / ".traceh" / "credentials"


def _crypt(data: bytes, identity: str, *, encrypt: bool) -> bytes:
    if not available():
        raise LaunchConfigurationError("当前平台请使用已有密钥环境变量或环境文件。")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    def blob(value):
        buffer = ctypes.create_string_buffer(value)
        return Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    source, source_buffer = blob(data)
    entropy, entropy_buffer = blob(identity.encode())
    output = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    fn = library.CryptProtectData if encrypt else library.CryptUnprotectData
    fn.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    fn.restype = wintypes.BOOL
    free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    if not fn(
        ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)
    ):
        raise LaunchConfigurationError("系统密钥存储不可用；请重新填写或选择环境变量。")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        free(output.data)


def save_key(args, key: str) -> None:
    if not key:
        raise LaunchConfigurationError("密钥为空，未保存。")
    identity = _identity(args)
    encrypted = _crypt(key.encode("utf-8"), identity, encrypt=True)
    root = credential_root()
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(root / f"{identity}.json", {"format": 1, "encrypted": encrypted.hex()})


def load_key(args) -> str | None:
    import json

    if not available() or args.provider != "openai-compatible" or not args.base_url:
        return None
    identity = _identity(args)
    path = credential_root() / f"{identity}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if (
            set(raw) != {"format", "encrypted"}
            or type(raw["format"]) is not int
            or raw["format"] != 1
        ):
            raise ValueError
        return _crypt(bytes.fromhex(raw["encrypted"]), identity, encrypt=False).decode("utf-8")
    except (OSError, ValueError, TypeError):
        raise LaunchConfigurationError("已保存密钥无法读取；请在模型配置中重新填写。") from None
