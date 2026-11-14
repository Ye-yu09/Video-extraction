# bili-scroll → 视频提取器 串联 · 需求与待办

> 状态：**串联已建好并在运行**（2026-11-08 开工）。瓶颈在提取器模型档位，非串联。

### 已完成
- 桥脚本 `bili_bridge.py`（心跳/等待/autostart/asr-model/tiers/depth）
- 计划任务 `bili-essence-bridge`：每 10 分钟心跳，已验证触发（Result=0）；完成的任务会自动写 essence/
- 提取器服务：已加 `HF_HUB_OFFLINE=1` 到 .env（避免联网校验挂死），uvicorn 跑在 8000
- 端到端验证：扫描→提交→提取器下载音频(bestaudio m4a，无需 ffmpeg)→ASR 运行 ✓

### 当前瓶颈（提取器侧，非串联）
- 本机 Intel Arc 显卡=无 CUDA → faster-whisper 只能 CPU；large-v3 CPU 加载+转写很慢（两并发更甚）
- small/tiny 模型下不来：huggingface.co 与 hf-mirror TLS 均被拦（curl exit35 / python SSLEOF）
- 只有 large-v3 完整缓存可用

## ✅ 完工（2026-11-08）：端到端跑通，已产出 essence
途中定位并修复的真实问题：
1. **提取器 `.env` 从来没被加载**：`config.py` 里 `load_dotenv()`（无路径）在本机不生效。
   → 已改为 `load_dotenv(Path(__file__).resolve().parent.parent / ".env")`（绝对路径、cwd 无关）。
   副作用：我先前用 PowerShell `Add-Content` 追加含中文的 ASR_MODEL 行，被按 GBK 写入污染了 .env，
   导致 dotenv 解析整个文件 UnicodeDecodeError、连 LLM_API_KEY 都读不到。已用 Python 以 UTF-8 重写 .env 修好。
2. **小模型来源**：HF/hf-mirror TLS 被拦，改从 **ModelScope** 镜像 `Systran/faster-whisper-small` 抓到本地
   `Desktop/视频提取器/models/faster-whisper-small/`（model.bin 483MB 完整），并把 .env 的 ASR_MODEL 指向它 → 加载 1s。
3. **桥脚本 bug**：state 记录漏存 `bv` 字段（KeyError）；`log()` 向 GBK 控制台 print `✅` 崩溃
   → 已补 bv 字段 + 脚本顶部 `sys.stdout.reconfigure(encoding='utf-8')`。

### 运行方式（现状）
- 服务：`127.0.0.1:8000`，默认 ASR_MODEL=本地 small，LLM=DeepSeek（.env）
- 计划任务 `bili-essence-bridge`：每 10 分钟跑 `run_bridge.bat`（= `python bili_bridge.py --autostart`），
  提交 bili-scroll 新挑中的"值得看"、回收完成结果写 `essence/<BV>_<标题>.md`
- 已产出：BV1ei4n6pEZY、BV1zn4E6REy2 两份精华（摘要+带时间戳要点+金句+行动项+完整转写）

### 想切回高质量 large-v3
编辑 `.env` 的 `ASR_MODEL=models/faster-whisper-small`（本地模型目录，相对项目根；也可写绝对路径）
改回 `ASR_MODEL=large-v3`（CPU 慢，适合不急的片子）。

## 优化轮（2026-11-08 二轮）— 已实现并验证
- **#1 单实例治理**：`ensure_service` 改为 race-safe —— 端口已被占（哪怕还没健康）就等待、绝不重复拉起；
  `run_bridge.bat` 钉死用 `.venv\Scripts\python.exe`。之前"跑不通"的深层原因之一是启动竞态拉出多个实例。
- **#2 失败自动重试**：`classify()` 区分可重试(风控/403/服务重启中断/ASR失败) vs 永久(视频不存在/删除)；
  可重试不拉黑、下轮重提；永久才写 failed 拉黑。比提取器自身把"重启中断"标 retryable:false 更合理。
- **#3 共享 B 站 cookie**：每轮 `sync_cookies()` 把 bili-scroll 的 `bili_credential.json`
  (sessdata/bili_jct/buvid3/dedeuserid) 导出 Netscape cookies.txt → `data/bili_cookies.txt`；
  `.env` 加 `YTDLP_COOKIES=…/data/bili_cookies.txt`。已验证服务读到且文件存在 → 会员/登录可见视频也能提。
- **#4 解析加固**：标题解析兜底读 `seen.md`（BV1Wu 之前解析成 BV 号，现已正确出标题）；
  当天日报有更新却扫到 0 条时打 ⚠️ 报警（防 bili-scroll 改报告格式后静默失效）。
- **#5 按档位分层**：`TIER_POLICY` = 值得看→detailed/small，可以看→essence/small。
  （本机 CPU-only、large-v3 又慢又易被打断，故默认全用稳的 small、只按深度分层；
   想要 large-v3 就把 值得看 的 asr_model 改成 "large-v3"。）

### 运维命令
- 立即手动跑一次：`cd 视频提取器\bridge && ..\.venv\Scripts\python.exe bili_bridge.py --autostart`
- 停用自动：`Disable-ScheduledTask -TaskName bili-essence-bridge`；恢复：`Enable-...`
- 看日志：`bridge\bridge.log`（桥）、`bridge\service.log`（提取器服务）

## 目标
dsh-bili-scroll 每轮搜到并挑中的好视频，自动把 BV 链接发给本地「视频提取器」，
由提取器下载→转写→摘要，产出精华存起来。

## 已敲定的选择（全选推荐项）
- **触发方式**：独立桥 + 定时轮询（不改插件源码，升级不被覆盖）
- **转发范围**：只转发档位 = **值得看** 的（不含"可以看/不值当"）
- **精华去向**：存到提取器旁新目录 **`essence/`**（每个 BV 一份 md）
- **提取深度**：**essence**（精华摘要）

## 关键事实（已核实，开工时不用再查）
- bili-scroll 家目录：`~/.dsh/bili-duty`（Windows 即 `C:\Users\<你>\.dsh\bili-duty`）
  - 挑片记录：`out\<日期>.md`，每条 `### BVxxxxxxxxxx` 起头，块内含 `- **档位**：值得看/可以看/不值当`、`- 标题：`、`- 链接：https://www.bilibili.com/video/BVxxxxxxxxxx`
  - 另有 `seen.md`（表：日期|BV|标题|UP|状态=挑了/跳过，**不含档位**）与 `digest\<yyyy-mm>.md`（每行带链接）
  - **档位只在 out\*.md 里**，所以"只值得看"要解析日报块，不能只看 seen.md
- 那一轮跑在沙箱 profile `bili-duty`（无文件系统/无命令/无联网）→ **串联必须在外部/宿主层做**
- 提取器：FastAPI，`http://127.0.0.1:8000`（`启动服务.bat` 起 uvicorn）
  - `POST /api/extract` body `{url, depth}` → `{task_id, status}`（异步）
  - `GET /api/tasks/{id}` → status: queued/running/done/failed
  - `GET /api/tasks/{id}/result` → `{markdown, summary, video, stats}`
  - `validate_url` 只认 `bilibili.com/video/BVxxxxxxxxxx`；`.env` 无 `API_TOKEN`（POST 不鉴权）；`LLM_API_KEY` 已配（DeepSeek）
  - venv 齐（uvicorn/fastapi/yt_dlp/faster_whisper/openai）；**不需要系统 ffmpeg**（bestaudio m4a 直链 + PyAV 解码）
- 桥脚本已起草：`bridge/bili_bridge.py`（提取器项目内）
  - 纯标准库；参数 `--dry-run / --loop / --interval / --autostart / --depth / --tiers`
  - 去重记录：`bridge\forwarded.json`；日志：`bridge\bridge.log`

## 开工 TODO（按序）
1. 起提取器服务（`启动服务.bat`，或跑桥时加 `--autostart`）。
2. 首次转写会下 Whisper `large-v3`（~1.5G，走 hf-mirror）；**想快速验证先把 `.env` 的 `ASR_MODEL` 临时设 `small`**。
3. `python bili_bridge.py --dry-run` —— 验证解析出哪些"值得看"（当前应是 BV1ei4n6pEZY、BV1zn4E6REy2）。
4. `python bili_bridge.py --autostart` —— 提交并等结果，写 essence/。
5. 配定时（二选一）：
   - Windows 计划任务每 5–10 分钟跑一次 one-shot：`schtasks /Create /SC MINUTE /MO 10 ...`
   - 或常驻：`python bili_bridge.py --loop --interval 300 --autostart`
6. 可选增强：把精华回填进 bili-scroll 日报 / 让桥顺带导出 docx（提取器有 `/api/tasks/{id}/export?fmt=docx`）。

## 待确认的小问题（开工时问用户）
- 定时用「计划任务」还是「常驻进程」？
- essence/ 目录位置就放 `视频提取器\essence\` 可以吗？
- 首次验证用 small 模型跑通后再切回 large-v3，还是直接 large-v3？

## 档位②整合（2026-11-13）：桥并入 uvicorn 进程，去掉外部计划任务
把「提取器 + 桥」自洽成**一个项目、一个进程、一个启动入口**；刷B站插件仍保持独立（接口不变）。
- **新增** `app/bridge_runner.py`：`lifespan` 起后台心跳，用 `asyncio.to_thread` 跑 `bili_bridge.tick`，
  不卡 FastAPI 事件循环；`tick(autostart=False)` 时服务在本进程，`ensure_service` 秒过、绝不重复拉起。
- **config.py** 加 `BRIDGE_ENABLED / BRIDGE_INTERVAL / BRIDGE_TIERS`（.env 默认 `true / 600 / 值得看`）。
- **bili_bridge.py 本体不改**，仍可独立手动跑（`python bili_bridge.py --dry-run`）；
  `forwarded.json / bridge.log / .lock` 位置不变，去重记录与外部计划任务时期无缝衔接。
- **health** 端点新增 `bridge_enabled/interval/tiers`，可据此确认新代码已加载。
- **计划任务** `bili-essence-bridge` 已 `Disable`；实测内置桥首轮扫到 9 条，提交/回收/重试均正常。
- **run_bridge.bat 保留**作「手动触发一次」工具，已不再被计划任务调用。
- **生效方式**：重启服务即可（内置桥随 uvicorn 自动起）。内置桥要求服务监听 8000（与启动脚本一致）。
- **彻底清理**（确认内置桥稳定后）：`Unregister-ScheduledTask -TaskName bili-essence-bridge`
