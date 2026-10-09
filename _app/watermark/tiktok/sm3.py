"""
纯 Python 实现的国密 SM3 哈希算法 (无第三方依赖)

用于抖音 a_bogus 签名计算。gmssl 兼容接口:
    sm3_hash(int_list) -> str  输入 0-255 整数列表, 返回 64 位小写十六进制
    bytes_to_list(bytes) -> List[int]
"""
import struct
from typing import List

_IV = [0x7380166f, 0x4914b2b9, 0x172442d7, 0xda8a0600,
       0xa96f30bc, 0x163138aa, 0xe38dee4d, 0xb0fb0e4e]


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _p0(x: int) -> int:
    return x ^ _rotl(x, 9) ^ _rotl(x, 17)


def _p1(x: int) -> int:
    return x ^ _rotl(x, 15) ^ _rotl(x, 23)


def _ff(x: int, y: int, z: int, j: int) -> int:
    if j < 16:
        return x ^ y ^ z
    return (x & y) | (x & z) | (y & z)


def _gg(x: int, y: int, z: int, j: int) -> int:
    if j < 16:
        return x ^ y ^ z
    return (x & y) | (~x & z)


_T = [0x79cc4519] * 16 + [0x7a879d8a] * 48


def sm3_digest(data: bytes) -> bytes:
    """SM3 哈希，返回 32 字节 digest"""
    msg = bytearray(data)
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += struct.pack(">Q", len(data) * 8)
    v = list(_IV)
    for off in range(0, len(msg), 64):
        w = list(struct.unpack(">16I", bytes(msg[off:off + 64])))
        for j in range(16, 68):
            x = w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15)
            w.append(_p1(x) ^ _rotl(w[j - 13], 7) ^ w[j - 6])
        w1 = [w[j] ^ w[j + 4] for j in range(64)]
        a, b, c, d, e, f, g, h = v
        for j in range(64):
            ss1 = _rotl((_rotl(a, 12) + e + _rotl(_T[j], j % 32)) & 0xFFFFFFFF, 7)
            ss2 = ss1 ^ _rotl(a, 12)
            tt1 = (_ff(a, b, c, j) + d + ss2 + w1[j]) & 0xFFFFFFFF
            tt2 = (_gg(e, f, g, j) + h + ss1 + w[j]) & 0xFFFFFFFF
            d, c, b, a = c, _rotl(b, 9), a, tt1
            h, g, f, e = g, _rotl(f, 19), e, _p0(tt2)
        v = [(x ^ y) & 0xFFFFFFFF for x, y in
             zip(v, [a, b, c, d, e, f, g, h])]
    return struct.pack(">8I", *v)


def sm3_hex(data: bytes) -> str:
    return sm3_digest(data).hex()


def bytes_to_list(data: bytes) -> List[int]:
    return list(data)


def sm3_hash(int_list: List[int]) -> str:
    """gmssl.sm3.sm3_hash 兼容: 输入整数列表 -> 十六进制字符串"""
    return sm3_hex(bytes(int_list))


if __name__ == "__main__":
    # 国密 SM3 标准测试向量
    assert sm3_hex(b"abc") == \
        "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0", "SM3(abc) 失败"
    assert sm3_hex(b"abcd" * 16) == \
        "debe9ff92275b8a138604889c18e5a4d6fdb70e5387e5765293dcba39c0c5732", "SM3(abcd*16) 失败"
    print("SM3 自检通过 ✓")
