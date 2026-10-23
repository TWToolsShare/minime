"""Trae 本地凭证解密 + 云端会话接口客户端。

算法来源：Trae.app/Contents/Resources/app/out/main.js 中 byteCrypto.js 模块（实际形态）：
  enc 布局: [6字节头 0x74 0x63 0x05 0x10 0x00 0x00][32字节随机 key][AES-128-CBC(数据)]
  msgKey  = SHA512(key) || (Gle ^ zle)
  aesKey  = SHA512(msgKey)[:16]   iv = SHA512(msgKey)[16:32]
  明文    = JSON / 字符串（不额外包裹 magic）
解密后即得 iCubeAuthInfo 中的 userInfo：token(RS256 JWT) / refreshToken / userId / account。
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HDR_MAGIC = bytes([0x74, 0x63, 0x05, 0x10, 0x00, 0x00])
KEY_LEN = 32
HDR_LEN = 6

_GLE = bytes([82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
              124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
              84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
              8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37])
_ZLE = bytes([31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
              96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
              160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
              23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125])
MASK = bytes(a ^ b for a, b in zip(_GLE, _ZLE))

def _trae_dir():
    env = os.environ.get("MINIME_TRAE_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    home = os.path.expanduser("~")
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA") or home, "Trae")
    if sys.platform == "darwin":
        return os.path.join(home, "Library/Application Support/Trae")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME")
                        or os.path.join(home, ".config"), "Trae")


TRAE_STORAGE = os.path.join(_trae_dir(), "User/globalStorage/storage.json")
JWT_FILE = os.path.expanduser("~/.trae/trae-jwt-token")


def _derive(key: bytes):
    msg_key = hashlib.sha512(key).digest() + MASK
    h = hashlib.sha512(msg_key).digest()
    return h[:16], h[16:32]


def decrypt(blob: bytes) -> str:
    if blob[:6] != HDR_MAGIC:
        raise ValueError(f"unexpected header {blob[:6].hex()}")
    key = blob[HDR_LEN:HDR_LEN + KEY_LEN]
    body = blob[HDR_LEN + KEY_LEN:]
    body = body[:len(body) // 16 * 16]
    aes_key, iv = _derive(key)
    dec = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    plain = dec.update(body) + dec.finalize()
    # 明文前有少量二进制前缀，JSON 体之后是 PKCS7 填充
    return plain.decode("utf-8", "ignore").lstrip("\x00").split("\x00")[0]


def decrypt_json(blob: bytes):
    text = decrypt(blob)
    start = text.find("{")
    if start < 0:
        raise ValueError("no json payload")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def load_auth_info():
    """返回 {key: userInfo}，离线解出 Trae 登录态（含 access token / refresh token）。"""
    data = json.load(open(TRAE_STORAGE, encoding="utf-8"))
    out = {}
    for k, v in data.items():
        if k.startswith("iCubeAuthInfo://") and isinstance(v, str) and len(v) > 100:
            try:
                out[k] = decrypt_json(base64.b64decode(v))
            except Exception as e:  # noqa: BLE001
                out[k] = {"_error": str(e)}
    return out


def main_user_info():
    info = load_auth_info()
    for k, v in info.items():
        if "token" in v and v.get("account"):
            return v
    raise RuntimeError("no usable auth info found")


def env_from_process():
    pid = subprocess.run(["pgrep", "-f", "Trae.app/Contents/MacOS/Electron"],
                         capture_output=True, text=True).stdout.split()
    env = {}
    for p in pid:
        line = subprocess.run(["ps", "eww", "-p", p], capture_output=True, text=True).stdout
        for tok in line.split():
            k, _, v = tok.partition("=")
            if k in ("CLOUDIDE_TENANT_NAME", "TRAE_PRODUCT_REGION",
                     "ICUBE_USER_DATA_DIR", "TRAE_BRAND_NAME"):
                env.setdefault(k, v)
    return env


def jwt_token():
    return open(JWT_FILE).read().strip() if os.path.exists(JWT_FILE) else None


def jwt_claims(tok):
    payload = tok.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


if __name__ == "__main__":
    info = load_auth_info()
    for k, v in info.items():
        if "_error" in v:
            print(k, "ERR", v["_error"])
            continue
        print(k, "token_len=", len(v.get("token", "")),
              "has_refresh=", bool(v.get("refreshToken")),
              "userId=", v.get("userId"),
              "account=", json.dumps(v.get("account"), ensure_ascii=False)[:300])
    t = jwt_token()
    if t:
        c = jwt_claims(t)
        print("jwt claims:", json.dumps(c, ensure_ascii=False)[:400],
              "exp_in=", c.get("exp", 0) - int(time.time()))
    print("ENV", env_from_process())
