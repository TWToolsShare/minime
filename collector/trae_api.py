"""调用 Trae 云端 chat_sessions 接口，验证鉴权并落盘原始响应。"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(__file__))
import trae_auth  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "run")
BASE = "https://coresg-normal.trae.ai/api/remote/v1"


def call(path, token, user_id, method="GET", body=None, extra=None, retries=2):
    url = f"{BASE}{path}"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}user_id={user_id}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Cloud-IDE-JWT {token}",
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                       " (KHTML, like Gecko) Trae/1.107.1 Chrome/142.0.7444.235"
                       " Electron/39.2.7 Safari/537.36"),
    }
    if extra:
        headers.update(extra)
    data = json.dumps(body).encode() if body is not None else None
    last = None
    # 网络抖动（SSL EOF/超时）重试，避免一次抖动就把整个源标成失败
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise last


def main():
    info = trae_auth.main_user_info()
    token, uid = info["token"], str(info["userId"])
    print("user:", info["account"].get("username"), "scope:", info["account"].get("scope"))

    probes = [
        "/chat_sessions",
        "/chat_sessions?page_size=20",
        "/chat_sessions?limit=20&offset=0",
    ]
    for p in probes:
        st, body = call(p, token, uid)
        print(f"--- {p} -> {st} {len(body)}B")
        print(body[:400])
        if st == 200:
            os.makedirs(OUT, exist_ok=True)
            fn = os.path.join(OUT, "probe_sessions.json")
            open(fn, "w").write(body)
            print("saved", fn)
            break


if __name__ == "__main__":
    main()
