"""适配器：Trae 云端会话接口。

鉴权：离线解密本地登录态（trae_auth），无需用户手动登录。
限制：服务端只返回当前端的会话，历史会话 404 —— 因此这是「补充源」，
      主数据源仍是本地快照 git。云端不可达时自动标记 unavailable，不影响其他源。
"""
import json
import os
import sys

# 引导 collector/ 到 sys.path：base 框架与 trae_auth / trae_api 都在这层
# collector/ 是 base 框架所在层；上溯三级，直接运行本文件也能调试
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
from base import SourceAdapter, put_raw, register  # noqa: E402


@register
class CloudAdapter(SourceAdapter):
    name = "cloud_sessions"
    kind = "cloud_messages"
    DEFAULT_INTERVAL_S = 3600
    priority = 5          # 先跑：鉴权失败时尽早暴露
    description = "Trae 云端会话消息（需本地登录态，历史会话服务端限制）"
    label = "Trae 云端会话"
    fix_hint = "重新登录 Trae 桌面端后重试；或在设置页关闭此源"

    def _auth_infos(self):
        """全部可用登录态（国际版 + CN 版各自独立采集）。"""
        import trae_auth  # noqa: PLC0415
        infos = []
        for (_sp, _k), v in trae_auth.load_auth_info().items():
            if "token" in v and v.get("userId") and v.get("account"):
                infos.append(v)
        return infos

    def probe(self):
        try:
            infos = self._auth_infos()
        except Exception as e:  # noqa: BLE001 — 解密失败/未登录都归为不可用
            return False, f"登录态不可用：{type(e).__name__}: {e}"
        if not infos:
            return False, "未取到有效登录态"
        return True, f"{len(infos)} 个登录态（user_id={str(infos[0]['userId'])[:8]}…）"

    def collect(self):
        import trae_api  # noqa: PLC0415
        total_sessions = total_msgs = 0
        errors = []
        for info in self._auth_infos():
            tok, uid = info["token"], str(info["userId"])
            base = trae_api.base_for(info)
            region = "cn" if "trae-api-cn" in base or "trae.cn" in (info.get("host") or "") else "intl"
            try:
                st, body = trae_api.call(base, "/chat_sessions", tok, uid)
                if st != 200:
                    errors.append(f"{region}: HTTP {st}")
                    continue
                sessions = json.loads(body).get("data", {}).get("items", [])
                got = 0
                for s in sessions:
                    sid = s["chat_session_id"]
                    st2, msgs = trae_api.call(base, f"/chat_sessions/{sid}/messages", tok, uid)
                    if st2 == 200:
                        put_raw(self.conn, self.kind, f"{region}:{sid}", msgs.encode())
                        got += 1
                    put_raw(self.conn, "cloud_session", f"{region}:{sid}",
                            json.dumps(s, ensure_ascii=False).encode())
                total_sessions += len(sessions)
                total_msgs += got
            except Exception as e:  # noqa: BLE001
                errors.append(f"{region}: {type(e).__name__}: {e}")
        if errors and not total_sessions:
            raise RuntimeError("; ".join(errors))
        result = {"sessions": total_sessions, "messages_fetched": total_msgs}
        if errors:
            result["errors"] = errors
        return result
