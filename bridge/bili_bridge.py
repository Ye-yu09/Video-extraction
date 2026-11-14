# -*- coding: utf-8 -*-
"""bili_bridge.py —— 把 dsh-bili-scroll 挑中的视频，自动喂给「视频提取器」提取精华。

链路：
  bili-scroll 每轮把结果写进 ~/.dsh/bili-duty/out/<日期>.md（每条 `### BVxxxx` + `- **档位**：…`）。
  本脚本扫描日报，按档位提交到本地提取器 POST /api/extract，完成后写 essence/<BV>_<标题>.md。

本版特性（对应优化项）：
  #1 单实例治理：ensure_service 只在端口空闲时才拉起，且用提取器 venv python；bat 也钉死 venv python。
  #2 失败自动重试：可重试错误（风控/403/服务重启中断）不拉黑，下轮重提；永久错误（视频不存在）才拉黑。
  #3 共享 B 站 cookie：每轮把 bili-scroll 的 bili_credential.json 导出成 Netscape cookies.txt，
     喂给提取器 YTDLP_COOKIES → 会员/登录可见视频也能提。
  #4 解析加固：标题解析兜底读 seen.md；日报当天有更新却扫到 0 条时报警（防 bili-scroll 改格式后静默失效）。
  #5 按档位分模型/深度：TIER_POLICY 决定每档用哪个 depth + asr_model。

用法：
  python bili_bridge.py --dry-run            # 只看会转发哪些 + 用什么档位策略
  python bili_bridge.py                       # 一次心跳（提交新片 + 回收完成 + 同步 cookie）
  python bili_bridge.py --wait                # 提交后就地等结果
  python bili_bridge.py --loop --interval 300 --autostart
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ---------- 路径 ----------
BILI_HOME = Path(os.environ.get("BILI_HOME", r"C:\Users\lenovo\.dsh\bili-duty"))
CRED_FILE = BILI_HOME / "bili_credential.json"
EXTRACTOR_DIR = Path(__file__).resolve().parent.parent
ESSENCE_DIR = EXTRACTOR_DIR / "essence"
BRIDGE_DIR = Path(__file__).resolve().parent
STATE_FILE = BRIDGE_DIR / "forwarded.json"
BRIDGE_LOG = BRIDGE_DIR / "bridge.log"
COOKIE_FILE = EXTRACTOR_DIR / "data" / "bili_cookies.txt"   # 喂给提取器 YTDLP_COOKIES
EXTRACTOR_URL = os.environ.get("EXTRACTOR_URL", "http://127.0.0.1:8000")

# ---------- #5 档位策略：每档用哪个深度 + 哪个 ASR 模型 ----------
# 本机 CPU-only、large-v3 又慢又易被打断，故默认全用稳的 small，仅按“深度”分层。
# 想要更高音质/精度（且有 GPU 或能忍慢）：把 值得看 的 asr_model 改成 "large-v3"。
TIER_POLICY = {
    "值得看": {"depth": "detailed", "asr_model": ""},
    "可以看": {"depth": "essence",  "asr_model": ""},
}
FORWARD_TIERS = {"值得看"}          # 实际转发的档位（保持你选的“只值得看”；想加“可以看”就并进来）
WAIT_POLL_TIMEOUT = 1800
WAIT_POLL_INTERVAL = 10

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
TIER_LINE_RE = re.compile(r"\*\*档位\*\*\s*[：:]\s*(.+?)\s*$", re.MULTILINE)
TITLE_LINE_RE = re.compile(r"^\s*-\s*标题\s*[：:]\s*(.+?)\s*$", re.MULTILINE)
LINK_LINE_RE = re.compile(r"^\s*-\s*链接\s*[：:]\s*(https?://\S+)", re.MULTILINE)
RETRYABLE_CODES = {"ERR_ASR_FAILED", "ERR_DOWNLOAD_FAILED", "ERR_INTERNAL"}  # 多为可重试（含“服务重启中断”）


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with BRIDGE_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def http(method: str, url: str, payload: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return -1, {"error": str(e)}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def load_seen_titles() -> dict:
    """#4 从 seen.md 解析 BV→标题（日报缺标题行时兜底）。seen.md 表：| 日期 | BV | 标题 | UP | 状态 |"""
    titles: dict[str, str] = {}
    seen = BILI_HOME / "seen.md"
    if not seen.exists():
        return titles
    for line in seen.read_text(encoding="utf-8", errors="replace").splitlines():
        if "|" not in line:
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) >= 3:
            mbv = BV_RE.search(cols[1]) if len(cols) > 1 else None
            bv = cols[1].strip() if BV_RE.fullmatch(cols[1].strip()) else (mbv.group(1) if mbv else "")
            if bv and cols[2] and not titles.get(bv):
                titles[bv] = cols[2]
    return titles


def scan_picks() -> list[dict]:
    """扫描日报 out/*.md，返回要转发的档位条目。#4 加固：标题兜底 seen.md。"""
    out_dir = BILI_HOME / "out"
    if not out_dir.exists():
        log(f"日报目录不存在：{out_dir}")
        return []
    seen_titles = load_seen_titles()
    picks, seen_bv = [], set()
    for md in sorted(out_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        for blk in re.split(r"(?m)^###\s+", text)[1:]:
            first = blk.splitlines()[0].strip() if blk.splitlines() else ""
            mbv = BV_RE.search(first)
            if not mbv:
                continue
            bv = mbv.group(1)
            mtier = TIER_LINE_RE.search(blk)
            tier = re.sub(r"[。.\s]+$", "", mtier.group(1).strip()) if mtier else ""
            if tier not in FORWARD_TIERS or bv in seen_bv:
                continue
            seen_bv.add(bv)
            mtitle = TITLE_LINE_RE.search(blk)
            title = (mtitle.group(1) if mtitle else seen_titles.get(bv, bv))
            mlink = LINK_LINE_RE.search(blk)
            picks.append({
                "bv": bv, "title": title, "tier": tier,
                "url": mlink.group(1) if mlink else f"https://www.bilibili.com/video/{bv}",
            })
    # #4 报警：今天有日报更新却扫到 0 条，多半是 bili-scroll 改了报告格式
    if not picks:
        today = datetime.now().strftime("%Y-%m-%d")
        fresh = any((out_dir / f"{today}.md").exists() for _ in [0])
        newest = max((m.stat().st_mtime for m in out_dir.glob("*.md")), default=0)
        if fresh or (newest and time.time() - newest < 6 * 3600):
            log("⚠️ 近期日报有更新，但按 FORWARD_TIERS 扫到 0 条 —— 可能 bili-scroll 改了报告格式，检查档位标记。")
    return picks


def sync_cookies() -> None:
    """#3 把 bili-scroll 的登录 cookie 导出成 Netscape cookies.txt 给提取器（会员片可下）。"""
    if not CRED_FILE.exists():
        return
    try:
        cred = json.loads(CRED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    mapping = {"SESSDATA": "sessdata", "bili_jct": "bili_jct", "buvid3": "buvid3", "DedeUserID": "dedeuserid"}
    lines = ["# Netscape HTTP Cookie File", "# bili-scroll cookie 同步给视频提取器（会员/登录视频下载用）"]
    exp = int(time.time()) + 30 * 86400
    for cname, key in mapping.items():
        val = str(cred.get(key, "")).strip()
        if val:
            lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t{exp}\t{cname}\t{val}")
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text("\n".join(lines) + "\n", encoding="ascii", errors="replace")


def _port_open(host: str = "127.0.0.1", port: int = 8000) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _health_ok() -> bool:
    return http("GET", f"{EXTRACTOR_URL}/api/health", timeout=5)[0] == 200


def ensure_service(autostart: bool) -> bool:
    """#1 单实例（防启动竞态）：
    health 通→复用；端口已被占但还没健康→等它就绪（绝不重复拉）；端口空闲且允许→拉一个 venv 实例。"""
    if _health_ok():
        return True
    if _port_open():
        log("端口 8000 已被占用（服务可能正在启动），等待其就绪，不重复拉起…")
        for _ in range(60):
            time.sleep(2)
            if _health_ok():
                return True
        log("等待 120s 仍不健康，判定为卡死实例，清理后重拉。")
        _kill_port(8000)
        time.sleep(2)
    elif not autostart:
        log("提取器服务未运行。双击「启动服务.bat」或加 --autostart。")
        return False
    log("拉起 uvicorn（venv python）…")
    py = EXTRACTOR_DIR / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = Path(sys.executable)
    logf = (BRIDGE_DIR / "service.log").open("ab")
    # CREATE_NO_WINDOW(0x08000000)：彻底不分配控制台窗口。
    # 之前写的 0x00000008 是 DETACHED_PROCESS —— 它只让子进程脱离父控制台，
    # 而 python.exe 是 console 程序，脱离后 Windows 反而会为它新建一个黑框，
    # 标题就是 python.exe 全路径。stdout/stderr 已重定向到 service.log，不受影响。
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(
        [str(py), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(EXTRACTOR_DIR), stdout=logf, stderr=logf, creationflags=flags,
    )
    for _ in range(60):
        time.sleep(2)
        if _health_ok():
            log("服务已就绪。")
            return True
    log("服务 120 秒内没起来，检查 bridge/service.log。")
    return False


def _kill_port(port: int = 8000) -> None:
    try:
        import psutil
        for c in psutil.net_connections("tcp"):
            if c.laddr and c.laddr.port == port and c.status == "LISTEN" and c.pid:
                psutil.Process(c.pid).kill()
    except Exception:  # noqa: BLE001
        os.system(f'for /f "tokens=5" %a in (\'netstat -ano ^| findstr :{port} ^| findstr LISTENING\') do taskkill /F /PID %a >nul 2>nul')


def submit(pick: dict) -> str | None:
    pol = TIER_POLICY.get(pick["tier"], {"depth": "essence", "asr_model": ""})
    body = {"url": pick["url"], "depth": pol["depth"]}
    if pol.get("asr_model"):
        body["asr_model"] = pol["asr_model"]
    code, resp = http("POST", f"{EXTRACTOR_URL}/api/extract", body, timeout=30)
    if code in (200, 201) and resp.get("task_id"):
        return resp["task_id"]
    log(f"  提交失败 {pick['bv']} -> HTTP {code} {resp}")
    return None


def classify(task_id: str) -> dict:
    """查一次状态：返回 {kind: done|running|retry|permanent, ...}。"""
    code, rec = http("GET", f"{EXTRACTOR_URL}/api/tasks/{task_id}", timeout=15)
    status = rec.get("status", "")
    if status == "done":
        _, result = http("GET", f"{EXTRACTOR_URL}/api/tasks/{task_id}/result", timeout=30)
        return {"kind": "done", "result": result}
    if status in ("failed", "cancelled"):
        err = rec.get("error") or {}
        msg = str(err.get("message", ""))
        code_ = str(err.get("code", ""))
        # “服务重启中断”/风控/下载失败/ASR失败 视为可重试；视频不存在/删除 视为永久
        permanent = code_ == "ERR_VIDEO_UNAVAILABLE" or "不存在" in msg or "已被删除" in msg
        return {"kind": "permanent" if permanent else "retry", "error": err}
    return {"kind": "running"}


def _wait_done(task_id: str) -> dict:
    deadline = time.time() + WAIT_POLL_TIMEOUT
    while time.time() < deadline:
        r = classify(task_id)
        if r["kind"] != "running":
            return r
        time.sleep(WAIT_POLL_INTERVAL)
    return {"kind": "running", "timeout": True}


def safe_name(title: str, bv: str) -> str:
    clean = re.sub(r'[\\/:*?"<>|\r\n]', "_", title).strip()[:60]
    return f"{bv}_{clean or bv}.md"


def write_essence(entry: dict, result: dict) -> Path:
    ESSENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = ESSENCE_DIR / safe_name(entry["title"], entry["bv"])
    md = (result.get("markdown") or "") if result else ""
    pol = TIER_POLICY.get(entry["tier"], {})
    header = (
        f"# {entry['title']}\n\n"
        f"- BV：{entry['bv']}\n- 链接：{entry['url']}\n- 档位：{entry['tier']}\n"
        f"- 深度：{pol.get('depth','essence')} ｜ ASR：{pol.get('asr_model','服务端默认')}\n"
        f"- 生成时间：{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')}\n\n---\n\n"
    )
    path.write_text(header + md + "\n", encoding="utf-8")
    return path


def tick(autostart: bool, dry: bool, wait: bool) -> int:
    # 并发锁：60 秒内已有另一个桥在动就跳过，避免两条并发同时抢加载模型导致超时/打架
    lock = BRIDGE_DIR / ".lock"
    try:
        if lock.exists() and (time.time() - lock.stat().st_mtime) < 60 and not wait:
            log("检测到 60 秒内已有另一桥心跳，跳过本轮（防并发抢模型）。")
            return 0
        lock.write_text(str(os.getpid()))
    except OSError:
        pass
    try:
        return _tick_inner(autostart, dry, wait)
    finally:
        try:
            lock.touch()
        except OSError:
            pass


def _tick_inner(autostart: bool, dry: bool, wait: bool) -> int:
    sync_cookies()
    picks = scan_picks()
    log(f"扫描到 {len(picks)} 条要转发（档位∈{sorted(FORWARD_TIERS)}）。")
    for p in picks:
        log(f"  · {p['bv']} [{p['tier']}→{TIER_POLICY.get(p['tier'],{}).get('depth')}/{TIER_POLICY.get(p['tier'],{}).get('asr_model')}] {p['title'][:36]}")
    if dry:
        return 0
    state = load_state()
    new = [p for p in picks if p["bv"] not in state]
    if not new and not any(not e.get("written") for e in state.values()):
        return 0
    if not ensure_service(autostart):
        return 1

    # 1) 提交新片
    for p in new:
        tid = submit(p)
        if not tid:
            continue
        state[p["bv"]] = {
            "bv": p["bv"], "task_id": tid, "title": p["title"], "url": p["url"], "tier": p["tier"],
            "submitted_at": datetime.now(timezone.utc).isoformat(), "written": False,
        }
        save_state(state)
        log(f"  已提交 {p['bv']} task_id={tid}")

    # 2) 回收 + #2 失败重试
    changed = False
    for bv, e in list(state.items()):
        if e.get("written"):
            continue
        r = _wait_done(e["task_id"]) if wait else classify(e["task_id"])
        kind = r["kind"]
        if kind == "running":
            continue
        if kind == "done":
            path = write_essence(e, r["result"])
            e["written"] = True
            changed = True
            log(f"  ✅ 精华已写入：{path.name}")
        elif kind == "permanent":
            e["written"] = True
            e["failed"] = "permanent"
            changed = True
            log(f"  ⛔ 永久失败（视频不存在/删除），拉黑：{bv}")
        elif kind == "retry":
            state.pop(bv, None)   # 从记录里删掉 → 下轮重新提交
            changed = True
            log(f"  ♻️ 可重试失败（{r.get('error',{}).get('code','')}），本轮不拉黑，下轮重提：{bv}")
    if changed:
        save_state(state)
    return 0


def main() -> None:
    global FORWARD_TIERS
    ap = argparse.ArgumentParser(description="bili-scroll → 视频提取器 桥接器")
    ap.add_argument("--loop", action="store_true", help="常驻，按 --interval 心跳")
    ap.add_argument("--interval", type=int, default=300, help="loop 每轮间隔秒（默认 300）")
    ap.add_argument("--autostart", action="store_true", help="服务没起就清残留并拉起")
    ap.add_argument("--wait", action="store_true", help="提交后就地等结果")
    ap.add_argument("--dry-run", action="store_true", help="只扫描打印，不提交")
    ap.add_argument("--tiers", default=",".join(FORWARD_TIERS), help="逗号分隔的转发档位")
    args = ap.parse_args()

    FORWARD_TIERS = {t.strip() for t in args.tiers.split(",") if t.strip()}

    if args.loop:
        log(f"常驻心跳：每 {args.interval}s，转发档位={sorted(FORWARD_TIERS)}，策略={TIER_POLICY}")
        while True:
            try:
                tick(args.autostart, args.dry_run, args.wait)
            except Exception as e:  # noqa: BLE001
                log(f"本轮异常：{e!r}")
            time.sleep(args.interval)
    else:
        sys.exit(tick(args.autostart, args.dry_run, args.wait))


if __name__ == "__main__":
    main()
