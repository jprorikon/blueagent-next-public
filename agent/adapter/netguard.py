"""Firecrawl 起始 URL 的公網閘：模型給的網址不能直接碰家裡的內網。

規則（全部都是「擋」，沒有例外清單）：
  · 只收 `http`／`https`；
  · 主機名**解析出來的每一個位址**都得是公網（`ipaddress.is_global`）：
    回環、私網、鏈路本地、CGNAT（tailnet 的 100.64/10）、保留段、多播一律擋；
  · 解析不到就擋（fail-closed；`.local` 解析得到也會落在私網段）。

這裡在請求交給自架 Firecrawl 前檢查起始 URL；重定向、子資源與 DNS rebinding
仍需 Firecrawl 服務端的網路政策防護，不能假裝這一道檢查已覆蓋整次抓取。
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse


def _resolve(host: str) -> list[str]:
    """主機名 → 位址（測試會換掉這支，不碰真的 DNS）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return []
    return sorted({str(info[4][0]) for info in infos})


def _public_ip(text: str) -> bool:
    try:
        address = ipaddress.ip_address(str(text).split("%", 1)[0])
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(address.is_global) and not address.is_multicast


def host_reason(host: str) -> str:
    """這個主機能不能連？回空字＝可以；否則回原因。"""
    name = str(host or "").strip().rstrip(".").lower()
    if not name:
        return "no_host"
    try:
        literal = ipaddress.ip_address(name)
    except ValueError:
        literal = None
    addresses = [str(literal)] if literal is not None else _resolve(name)
    if not addresses:
        return "unresolvable"
    if not all(_public_ip(address) for address in addresses):
        return "private_address"
    return ""


def blocked_reason(url: str, *, schemes=("http", "https")) -> str:
    """這個網址能不能抓？回空字＝可以；否則回原因（`scheme`／`private_address`…）。"""
    parts = urllib.parse.urlsplit(str(url or "").strip())
    if parts.scheme.lower() not in schemes:
        return "scheme"
    try:
        parts.port                                       # 壞的 port 會丟 ValueError
    except ValueError:
        return "bad_port"
    return host_reason(parts.hostname or "")


__all__ = ["blocked_reason", "host_reason"]
