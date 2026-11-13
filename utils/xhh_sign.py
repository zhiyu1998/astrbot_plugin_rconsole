"""
小黑盒（xiaoheihe）hkey 签名算法，逐行移植自 rconsole-plugin/utils/xiaoheihe.js

原始实现依赖 JS 的 32 位按位运算，这里逐步对齐：
    DA(e) = (e & 0x80) ? (((e << 1) ^ 27) & 0xFF) : (e << 1)
所有中间值都落在 0~255，因此用普通整数运算即可复现。
"""

import hashlib
import random
import time

SALT = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"


def _md5(data: str) -> str:
    return hashlib.md5(data.encode("utf-8")).hexdigest()


def _da(e: int) -> int:
    if e & 0x80:
        return ((e << 1) ^ 27) & 0xFF
    return e << 1


def _ba(e: int) -> int:
    return _da(e) ^ e


def _na(e: int) -> int:
    return _ba(_da(e))


def _fa(e: int) -> int:
    return _na(_ba(_da(e)))


def _ua(e: int) -> int:
    return _fa(e) ^ _na(e) ^ _ba(e)


def _za(data: str, table: str = SALT, cut: int = 0) -> str:
    """JS 版 zA：table 先 slice(0, cut)（cut 为负数时表示去掉尾部若干位）"""
    alphabet = table[:cut] if cut else table
    if not alphabet:
        return ""
    return "".join(alphabet[ord(ch) % len(alphabet)] for ch in data)


def _wa(data: str, table: str = SALT) -> str:
    if not table:
        return ""
    return "".join(table[ord(ch) % len(table)] for ch in data)


def _interleave(chunks) -> str:
    """JS 版：按列交错拼接（第 0 位取各段第 0 个字符，第 1 位取各段第 1 个……）"""
    if not chunks:
        return ""
    longest = max(len(c) for c in chunks)
    out = []
    for idx in range(longest):
        for chunk in chunks:
            if idx < len(chunk):
                out.append(chunk[idx])
    return "".join(out)


def _mix(values) -> int:
    """JS 里那段被压缩过的四字节混淆函数，返回混淆后数组所有元素之和"""
    e = list(values)
    t = [0, 0, 0, 0]
    t[0] = _ua(e[0]) ^ _fa(e[1]) ^ _na(e[2]) ^ _ba(e[3])
    t[1] = _ba(e[0]) ^ _ua(e[1]) ^ _fa(e[2]) ^ _na(e[3])
    t[2] = _na(e[0]) ^ _ba(e[1]) ^ _ua(e[2]) ^ _fa(e[3])
    t[3] = _fa(e[0]) ^ _na(e[1]) ^ _ba(e[2]) ^ _ua(e[3])
    for i in range(4):
        e[i] = t[i]
    return sum(e)


def gen_hkey(path: str, timestamp: int, nonce: str) -> str:
    """
    生成小黑盒接口所需的 hkey

    :param path: 接口路径，例如 bbs/app/link/tree
    :param timestamp: 秒级时间戳
    :param nonce: 大写 MD5 随机串
    """
    norm_path = "/" + "/".join([seg for seg in path.split("/") if seg]) + "/"
    merged = _interleave([
        _za(str(timestamp), SALT, -2),
        _wa(norm_path, SALT),
        _wa(nonce, SALT),
    ])[:20]
    digest = _md5(merged)

    tail = [ord(ch) for ch in digest[-6:]]
    num = _mix(tail) % 100
    num_str = f"{num:02d}"
    return _za(digest[:5], SALT, -4) + num_str


def gen_nonce() -> str:
    return _md5(str(time.time()) + str(random.random())).upper()


def get_api_params(kind: str, ident: str) -> dict:
    """
    获取小黑盒完整请求参数

    :param kind: bbs / pc / console / mobile
    :param ident: 帖子 link_id 或游戏 appid / steam_appid
    """
    path_map = {
        "bbs": "bbs/app/link/tree",
        "pc": "game/get_game_detail",
        "console": "game/console/get_game_detail",
        "mobile": "game/mobile/get_game_detail",
    }
    path = path_map.get(kind, path_map["bbs"])
    timestamp = int(time.time())
    nonce = gen_nonce()
    # 原实现里时间戳传的是 timestamp + 1
    hkey = gen_hkey(path, timestamp + 1, nonce)

    params = {
        "os_type": "web",
        "version": "999.0.4",
        "hkey": hkey,
        "_time": timestamp,
        "nonce": nonce,
    }

    if kind == "bbs":
        params.update({
            "link_id": ident,
            "limit": 20,
            "web_version": "2.5",
            "x_client_type": "web",
            "x_app": "heybox_website",
            "x_os_type": "Android",
        })
    elif kind == "pc":
        params["steam_appid"] = ident
    else:
        params["appid"] = ident
    return params


def optimize_image_url(url: str) -> str:
    """小黑盒 CDN 取原图的小技巧：带查询参数的地址末尾补一个反斜杠"""
    if not url:
        return url
    return url + "\\" if "?" in url else url


__all__ = [
    "SALT",
    "gen_hkey",
    "gen_nonce",
    "get_api_params",
    "optimize_image_url",
]