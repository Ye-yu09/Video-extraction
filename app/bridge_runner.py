"""内置桥心跳：把 bili_bridge 跑在 uvicorn 进程里，取代外部 Windows 计划任务。

档位②整合的设计要点：
- 桥脚本本体仍是 ../bridge/bili_bridge.py，保持可独立手动运行；本模块只做「进程内调度」，
  不改桥的任何业务逻辑（扫描日报 / 提交 / 回收 / 写 essence / 失败重试 / cookie 同步）。
- tick 内部是同步 urllib + 文件 IO，用 asyncio.to_thread 丢到线程池，绝不卡 FastAPI 事件循环。
- 用非阻塞心跳（wait=False）：每轮只做「提交新片 + 回收已完成 + 写 essence」，单轮毫秒级；
  真正的下载/转写/摘要仍由服务自身的任务队列异步跑，桥只负责喂片和收片。
- 服务就在本进程，ensure_service(autostart=False) 会先 _health_ok() 秒过，不会重复拉起新实例。
- 内置桥要求服务监听在 8000（与 启动服务.bat / bili_bridge 默认 EXTRACTOR_URL 一致）。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

logger = logging.getLogger("app.bridge")

# bridge/ 目录（bili_bridge.py 所在处）；用 __file__ 定位，与 cwd 无关
_BRIDGE_DIR = Path(__file__).resolve().parent.parent / "bridge"


def _load_bridge():
    """把 bridge/ 加入 sys.path 后导入 bili_bridge。

    保持其 __file__ 仍指向 bridge/，于是 forwarded.json / bridge.log / service.log / .lock
    等状态文件位置与外部计划任务时期完全一致，去重记录无缝衔接。
    """
    if str(_BRIDGE_DIR) not in sys.path:
        sys.path.insert(0, str(_BRIDGE_DIR))
    import bili_bridge  # noqa: PLC0415  动态路径导入，非包内符号

    return bili_bridge


async def bridge_loop(interval: int, tiers: set[str], startup_delay: int = 5) -> None:
    """常驻后台心跳：每 interval 秒跑一次 bili_bridge.tick（线程池内，不阻塞事件循环）。"""
    try:
        bili = _load_bridge()
    except Exception:  # noqa: BLE001
        logger.exception("加载 bili_bridge 失败，内置桥未启动（可临时改用外部 run_bridge.bat）")
        return

    bili.FORWARD_TIERS = tiers
    logger.info(
        "内置桥心跳已启动：每 %ss ｜ 转发档位=%s ｜ 服务=%s",
        interval, sorted(tiers), getattr(bili, "EXTRACTOR_URL", "http://127.0.0.1:8000"),
    )

    # 等 uvicorn 真正开始监听，避免首轮 ensure_service 在端口未就绪时空跑一轮
    await asyncio.sleep(startup_delay)

    while True:
        try:
            # tick(autostart=False, dry=False, wait=False)：服务在本进程，无需 autostart；
            # 非 wait 模式不就地长等转写，提交后下轮回收，单轮快且不阻塞
            await asyncio.to_thread(bili.tick, False, False, False)
        except Exception:  # noqa: BLE001
            logger.exception("内置桥心跳异常（下一轮重试）")
        await asyncio.sleep(interval)
