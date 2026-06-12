#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从本机已登录的 Chromium 系浏览器（Edge / Chrome / Arc）读取并解密
飞书妙记所需的 Cookie，拼成 Cookie 头返回。

这样程序每次运行都用浏览器里的实时登录态，只要你保持浏览器登录，
就不用手动复制 Cookie，也不会每隔几周失效。

仅访问本机当前用户自己的浏览器数据，用于本人的自动化。
"""

import os
import shutil
import sqlite3
import subprocess
import tempfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.hashes import SHA1
from cryptography.hazmat.backends import default_backend

HOME = os.path.expanduser("~")

# 各浏览器：Cookies 数据库路径 + Keychain 中的 "Safe Storage" 服务名
BROWSERS = [
    ("Edge", f"{HOME}/Library/Application Support/Microsoft Edge/Default/Cookies",
     "Microsoft Edge Safe Storage"),
    ("Chrome", f"{HOME}/Library/Application Support/Google/Chrome/Default/Cookies",
     "Chrome Safe Storage"),
    ("Arc", f"{HOME}/Library/Application Support/Arc/User Data/Default/Cookies",
     "Arc Safe Storage"),
]

# 妙记网页接口需要的 Cookie 所在的域
HOSTS = ("meetings.feishu.cn", ".feishu.cn", ".feishu.net")


def _keychain_password(service):
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-ws", service],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _derive_key(password):
    kdf = PBKDF2HMAC(algorithm=SHA1(), length=16, salt=b"saltysalt",
                     iterations=1003, backend=default_backend())
    return kdf.derive(password.encode("utf-8"))


def _decrypt(encrypted, key):
    if not encrypted or encrypted[:3] not in (b"v10", b"v11"):
        return None
    iv = b" " * 16
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    dec = cipher.decryptor()
    data = dec.update(encrypted[3:]) + dec.finalize()
    if not data:
        return None
    pad = data[-1]
    if 1 <= pad <= 16:
        data = data[:-pad]
    # 新版 Chromium 在明文前加了 32 字节 SHA256 域名哈希
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data[32:].decode("utf-8", "replace")


def _read_browser(name, db_path, service):
    if not os.path.exists(db_path):
        return None
    password = _keychain_password(service)
    if not password:
        return None
    key = _derive_key(password)
    tmp = tempfile.mktemp(suffix=".db")
    try:
        shutil.copy2(db_path, tmp)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                shutil.copy2(db_path + suffix, tmp + suffix)
        conn = sqlite3.connect(tmp)
        q = ("select host_key, name, value, encrypted_value from cookies "
             "where " + " or ".join("host_key=?" for _ in HOSTS))
        rows = conn.execute(q, HOSTS).fetchall()
        conn.close()
    except Exception:
        return None
    finally:
        for p in (tmp, tmp + "-wal", tmp + "-shm"):
            if os.path.exists(p):
                os.remove(p)

    jar = {}
    for host, cname, value, enc in rows:
        val = value if value else _decrypt(enc, key)
        if val:
            jar[cname] = val  # 同名时后写入的覆盖（域更具体的稍后处理影响不大）
    if "bv_csrf_token" not in jar:
        return None
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def get_cookie():
    """返回拼好的 Cookie 字符串；任一浏览器成功即可。失败返回 None。"""
    for name, db_path, service in BROWSERS:
        cookie = _read_browser(name, db_path, service)
        if cookie:
            return cookie, name
    return None, None


if __name__ == "__main__":
    cookie, src = get_cookie()
    if cookie:
        print(f"[OK] 从 {src} 提取到 Cookie，长度 {len(cookie)}，"
              f"含 bv_csrf_token={'bv_csrf_token=' in cookie}")
    else:
        print("[FAIL] 未能从任何浏览器提取到含 bv_csrf_token 的飞书 Cookie。"
              "请确认已在 Edge/Chrome/Arc 登录 meetings.feishu.cn。")
