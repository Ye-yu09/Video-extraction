"""API 冒烟验证：服务启动 + depth 校验 + 链接校验 + 缓存去重回路。

用法：python tools/verify_api.py（Windows：.venv/Scripts/python.exe tools/verify_api.py）
注意：使用临时 DATA_DIR，不触碰真实 data/tasks.db；run_task 被替换为空实现，不发真实下载/转写。
"""
from __future__ import annotations

import os
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# 临时 DATA_DIR：mkdtemp 子目录在部分沙箱下不可写，改为直接在候选根目录下建专用目录
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cands = [os.path.join(_BASE, "data"), tempfile.gettempdir()]
TMP = None
for _c in _cands:
    cand = os.path.join(_c, "_smoke_tmp")
    try:
        os.makedirs(cand, exist_ok=True)
        probe = os.path.join(cand, "probe.txt")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        TMP = cand
        break
    except OSError:
        continue
if TMP is None:
    print("无法创建临时目录"); sys.exit(2)
os.environ["DATA_DIR"] = TMP
os.environ["ASR_ENGINE"] = "mock"
os.environ["LLM_API_KEY"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

# 冒烟用固定空库：每次重跑前清掉旧 tasks.db，避免历史残留任务干扰断言
_db = os.path.join(TMP, "tasks.db")
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_db + _ext)
    except OSError:
        pass

import app.main as main  # noqa: E402

# 不触发真实下载/转写/LLM：替换后台运行函数为空实现
async def _fake_run(*args, **kwargs):
    await asyncio.sleep(0)


main.run_task = _fake_run

client = TestClient(main.app)
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" {extra}" if extra else ""))
    if not cond:
        fails.append(name)


# 1. 服务可启动
r = client.get("/api/health")
check("health 200", r.status_code == 200 and r.json().get("ok") is True)
r = client.get("/")
check("index 200", r.status_code == 200 and "历史任务" in r.text)

# 2. 任务列表接口（含 status 参数）
r = client.get("/api/tasks?page=1&page_size=5&status=done")
check("tasks 列表 200", r.status_code == 200 and "items" in r.json() and "total" in r.json())

# 3. 非法链接 → 400 ERR_URL_INVALID
r = client.post("/api/extract", json={"url": "https://example.com/1"})
check("非法链接 400", r.status_code == 400 and r.json().get("detail", {}).get("code") == "ERR_URL_INVALID",
      str(r.json()))

# 4. 非法 depth → 422（Pydantic 校验）
r = client.post("/api/extract", json={"url": "https://www.bilibili.com/video/BV1GJ411x7h7", "depth": "超详细"})
check("非法 depth 422", r.status_code == 422, str(r.json()))

# 5. 首次提交 → 新建任务 cached=false
payload = {"url": "https://www.bilibili.com/video/BV1GJ411x7h7", "depth": "detailed"}
r1 = client.post("/api/extract", json=payload)
check("首次提交 cached=false", r1.status_code == 200 and r1.json().get("cached") is False, str(r1.json()))
tid1 = r1.json().get("task_id")

# 6. 相同链接+深度重复提交 → 复用同一任务（queued/running 时也算命中，task_id 一致）
r2 = client.post("/api/extract", json={"url": "www.bilibili.com/video/BV1GJ411x7h7", "depth": "detailed"})
check("重复提交复用 task_id", r2.status_code == 200 and r2.json().get("task_id") == tid1, str(r2.json()))

# 7. 不同深度 → 不命中缓存，新建任务
r3 = client.post("/api/extract", json={"url": "https://www.bilibili.com/video/BV1GJ411x7h7", "depth": "full"})
check("不同深度新建任务", r3.status_code == 200 and r3.json().get("task_id") != tid1, str(r3.json()))

# 8. 任务详情可见落库的 depth/url
r = client.get(f"/api/tasks/{tid1}")
check("任务详情含 depth/url", r.status_code == 200 and
      r.json().get("depth") == "detailed" and r.json().get("url") == "https://www.bilibili.com/video/BV1GJ411x7h7",
      str(r.json()))

# 9. 新字段：asr_model / output_lang 透传与回显
r4 = client.post("/api/extract", json={
    "url": "https://www.bilibili.com/video/BV1C34q6yEv5", "depth": "essence", "asr_model": "tiny", "output_lang": "en",
})
check("asr_model/output_lang 回显", r4.status_code == 200 and
      r4.json().get("asr_model") == "tiny" and r4.json().get("output_lang") == "en", str(r4.json()))
r = client.get(f"/api/tasks/{r4.json().get('task_id')}")
check("任务详情含 asr_model/output_lang", r.status_code == 200 and
      r.json().get("asr_model") == "tiny" and r.json().get("output_lang") == "en", str(r.json()))

# 10. health 含新配置项
r = client.get("/api/health")
check("health 含 transcript_cache", r.status_code == 200 and "transcript_cache" in r.json() and "data_retention_days" in r.json())

# 10b. 导出端点：未完成任务 409；非法 fmt 400
r = client.post("/api/extract", json={"url": "https://www.bilibili.com/video/BV1GJ411x7h7"})
pending_id = r.json().get("task_id")
r = client.get(f"/api/tasks/{pending_id}/export?fmt=docx")
check("未完成导出 409", r.status_code == 409, str(r.status_code))
r = client.get(f"/api/tasks/{pending_id}/export?fmt=exe")
check("非法导出格式 400", r.status_code == 400, str(r.status_code))

# 10c. 多 P 探测端点：非法链接 400（合法链接需真实网络，冒烟不触网）
r = client.get("/api/pages?url=https://example.com/x")
check("pages 非法链接 400", r.status_code == 400, str(r.status_code))

# 11. 新请求字段：page / correct_punctuation / summary_self_check 透传
r5 = client.post("/api/extract", json={
    "url": "https://www.bilibili.com/video/BV1GJ411x7h7", "page": 2,
    "correct_punctuation": True, "summary_self_check": "full",
})
check("page/纠错/自检参数透传", r5.status_code == 200, str(r5.json()))
r = client.get(f"/api/tasks/{r5.json().get('task_id')}")
check("任务详情含 page", r.status_code == 200 and r.json().get("page") == 2, str(r.json()))

# 12. 鉴权：API_TOKEN 开启后写操作需 X-API-Key
import importlib  # noqa: E402
import app.config as _cfg  # noqa: E402
_saved_token = main.settings.api_token
main.settings.api_token = "smoke_secret"
auth_client = TestClient(main.app)
r = auth_client.post("/api/extract", json={"url": "https://www.bilibili.com/video/BV1GJ411x7h7"})
check("鉴权-无Token写操作401", r.status_code == 401, str(r.status_code))
r = auth_client.post("/api/extract", json={"url": "https://www.bilibili.com/video/BV1GJ411x7h7"},
                     headers={"X-API-Key": "smoke_secret"})
check("鉴权-带Token写操作放行", r.status_code == 200, str(r.status_code))
r = auth_client.get("/api/health")
check("鉴权-读操作放行", r.status_code == 200, str(r.status_code))
main.settings.api_token = _saved_token
del importlib

print()
if fails:
    print(f"共 {len(fails)} 项失败：{fails}")
    sys.exit(1)
print("全部通过 ✅")