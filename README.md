# 🎬 视频内容提取与智能整理

![CI](https://github.com/Ye-yu09/Video-extraction/actions/workflows/ci.yml/badge.svg)
![license](https://img.shields.io/badge/license-MIT-green)

输入一个 B 站视频链接，自动完成 **下载 → 音频提取 → 语音转文字 → 大模型整理**，输出结构化精华内容（摘要、要点、分主题详解、原文引用、行动项），并附完整转写与 SRT 字幕。

> 开发规范见同目录《开发提示词-视频内容提取与智能整理.md》，本文档为实现产物说明。
>
> 📦 部署与运行见 [DEPLOY.md](DEPLOY.md) ｜ 🤝 欢迎提 [Issue](https://github.com/Ye-yu09/Video-extraction/issues) / [Pull Request](https://github.com/Ye-yu09/Video-extraction/pulls)

---

## 功能特性

- ✅ 链接校验 + 视频元信息获取（标题/UP主/时长）
- ✅ 音频下载（bestaudio 直链，B 站 m4a 无需 FFmpeg 即可转写）
- ✅ 离线语音转文字（faster-whisper，VAD 过滤静音/纯音乐段可配置，输出带时间戳分段 + SRT，支持中英等自动语言检测）
- ✅ **LLM 转写纠错**：转写后自动二次校对 ASR 错别字/同音误识别（如"美贼"→"美债"），只修错字不改语义，失败自动降级（保留原始转写 `transcript.raw.json`）
- ✅ 智能分块（token 预算切分 + 块间重叠，防止切断语义）
- ✅ 大模型 Map-Reduce 整理（逐块提炼 → 全局归纳，JSON 结构化输出）
- ✅ **视频概要**：一句话摘要之外，额外生成 150-300 字整体概述，快速了解全片讲了什么（三档均输出）
- ✅ **提取深度三档落地**：`essence` 精华摘要 / `detailed` 详细要点（+分主题）/ `full` 全文整理（+逐段细览）。API 边界校验非法值（422），深度随任务落库并在历史页/结果页展示
- ✅ **结果缓存去重**：按「规范化链接 + 提取深度 + 自定义模板哈希」复合键复用进行中/已完成任务——重复提交同一链接+深度直接返回已有结果，省去重复下载/转写/LLM 调用；深度或自定义模板不同则不命中，避免结果错配；提交加锁防并发重复建任务（含 queued 阶段）；降级完成的任务不复用（配好 LLM 后重跑拿真结果）
- ✅ **转写级缓存（省钱 2.0）**：同一视频换深度/用途时复用已转写文本，跳过下载+转写+纠错全流程；缓存键含 BV+ASR 模型+语言+VAD+纠错开关，改配置自动失效
- ✅ **LLM 用量统计**：每次调用累计 token 数（prompt/completion），任务结果页展示「LLM 消耗 X tokens」，省钱效果可见
- ✅ **全局 LLM 并发限流**：`MAX_CONCURRENT_LLM_TASKS` 限制同时整理的任务数，防多任务并发打爆 API 与费用
- ✅ **运行时 ASR 模型覆盖**：前端「快速预览档」可选 tiny/base/small/medium，无需改 .env 即可切换转写速度/精度
- ✅ **输出语言参数**：支持中文/English/日本語输出，透传给 LLM 提示词
- ✅ **B 站 Cookie 支持**：`YTDLP_COOKIES` 配置 cookies.txt 路径，解锁大会员/需登录视频
- ✅ **视频分 P 支持**：请求参数 `page` 指定分 P（`?p=N`），不同分 P 独立任务与转写缓存
- ✅ **摘要自检**：`SUMMARY_SELF_CHECK`（off/full/always）Reduce 后 LLM 再评审一遍，漏关键数据自动补充 key_points
- ✅ **纠错断句+标点档**：`CORRECT_PUNCTUATION` 或请求参数开启后，纠错同时优化断句/标点（opt-in，默认只修错字保引用对齐）
- ✅ **API 鉴权**：`API_TOKEN` 非空时 `/api/extract` 等写操作需带 `X-API-Key` 头，防公网部署时他人白嫖 LLM 余额
- ✅ **模型下载进度可视化**：新增「⬇️ 加载/下载模型」阶段（20-30%），首次用 large-v3 不再"卡住假死"；hf_hub 进度实时回传前端
- ✅ **ASR 启动预热**：`ASR_PREWARM=true` 服务启动即后台检查/下载模型权重，首个任务不再干等几分钟
- ✅ **转写超时保护**：`ASR_TIMEOUT=1800`（秒）防 CPU 长视频无限等待，超时提示换 tiny/base/small 档
- ✅ **CPU 转写提速**：`ASR_CONTEXT_TEXT=false`（默认）实测同音频转写快约 2 倍（341s→172s），要点/时间戳校准不受影响；`ASR_CPU_THREADS=0` 自动吃满全部核；`ASR_VAD_FALLBACK` 自动回退修复 large-v3+VAD 空结果 bug
- ✅ **HF 镜像自动兜底**：未显式配置 `HF_ENDPOINT` 时自动走 hf-mirror.com，国内环境开箱即用不再 WinError 10060
- ✅ **分 P 一键批量**：粘贴多P链接自动探测选集 → 勾选集数 → 批量提交；或直接填范围语法 `1-3` / `2,5,8` / `1-4,7`（前端解析、后端校验，非法写法 400）
- ✅ **导出 Word / PDF**：结果页一键导出 `.docx`（python-docx，缺依赖自动降级 .md）/ `.pdf`（LibreOffice headless）
- ✅ **重试保留原参数**：历史页「重试」按原任务的深度/分P/模型/输出语言重跑，不再固定 essence
- ✅ **LLM 消耗可见化**：结果页显示「💰 LLM 消耗 X tokens（输入+输出）」，缓存复用显示 0
- ✅ **数据保留策略**：`DATA_RETENTION_DAYS` 自动清理过期任务目录与 DB 记录
- ✅ **Markdown 渲染视图**：结果区支持渲染视图/源码切换，标题/列表/引用/粗体可视化
- ✅ **视频元信息缓存**：`META_CACHE_TTL` 缓存标题/时长，换深度重提时省一次网络请求
- ✅ **真实时间戳校准**：Map 输入带 `[mm:ss]` 逐句时间；要点/引用由后端对转写段做确定性模糊匹配回填出处（不依赖 LLM 编造时间）
- ✅ **降级保护**：无 LLM Key / LLM 失败 / 无模型环境，均保证有输出
- ✅ **任务持久化**：SQLite 落库，服务重启后任务状态与结果完整恢复，中断任务自动标记 failed
- ✅ **任务列表分页 + 状态筛选**：`GET /api/tasks?page=&page_size=&status=` 返回 total/items 结构
- ✅ **前端任务历史页**：状态筛选、分页、运行中 5 秒自动刷新、回看结果/下载 .md/字幕/重试/取消，缓存命中有提示
- ✅ **自定义提示词模板**：逐块提炼/全局归纳模板可经 .env 或 API 参数定制（占位符替换，不与 JSON 冲突）
- ✅ 双入口：Web 界面（FastAPI + 进度轮询）与命令行（CLI）
- ✅ 统一错误码 + 重试机制 + 任务取消 + 并发限流
- ✅ Docker 一键部署（CPU 版，内置 FFmpeg 与 HF 镜像）

## 目录结构

```
视频提取器/
├── app/
│   ├── main.py          # FastAPI 入口与 REST API
│   ├── pipeline.py      # 管道编排 + 任务状态管理
│   ├── downloader.py    # yt-dlp 封装（元信息/音频下载）
│   ├── transcriber.py   # faster-whisper 封装（含 mock 模式）
│   ├── chunker.py       # 文本分块（token 预算 + 重叠）
│   ├── summarizer.py    # LLM Map-Reduce 整理 + Markdown 渲染
│   ├── models.py        # Pydantic 数据模型
│   ├── config.py        # .env 配置
│   ├── errors.py        # 统一错误码
│   └── cli.py           # 命令行入口
├── static/index.html    # Web 前端
├── tests/               # pytest 单元测试
├── data/                # 运行产物（按 task_id 隔离）
├── requirements.txt
└── .env.example
```

## 快速开始

### 1. 安装依赖

```bash
# 建议 Python 3.11+
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

首次运行 ASR 会自动下载 Whisper 模型（`large-v3` 约 3GB；可用 `ASR_MODEL=small` 换轻量模型，约 460MB）。GPU 环境自动启用 CUDA。

> 🇨🇳 国内网络无法访问 HuggingFace 时，先设置镜像再运行：
> ```bash
> # Windows: setx HF_ENDPOINT https://hf-mirror.com   （新开终端生效）
> # Linux/macOS: export HF_ENDPOINT=https://hf-mirror.com
> ```

**预下载模型到本地（推荐，避免运行时下载）：**
```bash
# Windows PowerShell
$env:HF_ENDPOINT = "https://hf-mirror.com"
.\.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='Systran/faster-whisper-small', local_dir=r'data\models\whisper-small')"
```
然后在 `.env` 中设置 `ASR_MODEL=data/models/whisper-small`（应用会自动识别本地目录）。

### 2. 配置

```bash
copy .env.example .env    # Windows
# cp .env.example .env    # Linux/macOS
```

编辑 `.env`，**至少配置 `LLM_API_KEY`**（DeepSeek / 通义千问 / Kimi 等任一 OpenAI 兼容服务）。

> 不配置 LLM Key 时应用仍可运行，但「智能整理」降级为输出转写原文要点。

### 3a. 命令行使用

```bash
python -m app.cli "https://www.bilibili.com/video/BVxxxxxx" --out 精华.md
python -m app.cli "https://www.bilibili.com/video/BVxxxxxx" --depth detailed
```

### 3b. Web 界面

```bash
uvicorn app.main:app --reload --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

### 3c. Docker 部署（可选）

```bash
# 前提：已安装 Docker；本机已有 .env（含 LLM_API_KEY）
docker compose up -d --build
# 打开 http://127.0.0.1:8000
```

镜像已内置 FFmpeg，默认走 HF 镜像（`HF_ENDPOINT=https://hf-mirror.com`），数据目录挂载到宿主 `./data`（任务与模型均持久化）。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 服务状态、LLM/ASR 配置是否就绪 |
| POST | `/api/extract` | 提交链接，返回 `task_id`；`depth` 非法返回 422；命中缓存返回 `cached: true` 复用已有任务 |
| GET | `/api/tasks/{id}` | 轮询任务状态（status/stage/progress/error，含 depth/url 落库参数） |
| GET | `/api/tasks/{id}/result` | 完成后的 Markdown 结果（未完成返回 409） |
| GET | `/api/tasks/{id}/transcript?as_srt=true` | 完整转写 JSON / SRT 字幕 |
| POST | `/api/tasks/{id}/cancel` | 取消任务 |
| GET | `/api/tasks?page=&page_size=&status=` | 任务列表分页+状态筛选（`{total, page, page_size, status, items}`；status 可选 queued/running/done/failed/cancelled，空=全部） |
| GET | `/api/pages?url=` | 探测多 P 视频选集列表（分 P 批量用；单 P 返回空） |
| GET | `/api/tasks/{id}/export?fmt=docx\|pdf` | 导出 Word / PDF（未完成 409；无 LibreOffice 时 PDF 报 500、DOCX 降级 .md） |

## 错误码

| 错误码 | 含义 | 处理建议 |
|---|---|---|
| `ERR_URL_INVALID` | 链接格式错误 | 检查链接是否为 bilibili.com/video/BV… |
| `ERR_VIDEO_UNAVAILABLE` | 私享/删除/地区限制/需登录 | 更换公开视频或稍后重试；配置 `YTDLP_COOKIES` 可解需登录视频 |
| `ERR_DOWNLOAD_FAILED` | 下载失败（已自动重试） | 稍后重试；安装 FFmpeg 可提高成功率 |
| `ERR_ASR_FAILED` | 无人声或转写失败 | 确认视频含语音；安装 faster-whisper |
| `ERR_ASR_TIMEOUT` | 转写超时（默认 30 分钟） | 前端换 tiny/base/small 档重试；或调大 `ASR_TIMEOUT` |
| `ERR_LLM_FAILED` | LLM 调用失败（已自动降级） | 检查 `LLM_API_KEY` 余额与限流 |
| `ERR_TOO_LONG` | 视频/文本超限 | 调大 `MAX_VIDEO_DURATION`/`MAX_CHUNKS` 或人工截取 |
| `ERR_CANCELLED` | 用户取消 | — |

## 配置项（.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | DeepSeek | OpenAI 兼容协议三件套 |
| `ASR_MODEL` | `large-v3` | tiny/small/medium/large-v3，或本地模型目录（如 `data/models/whisper-small`） |
| `ASR_DEVICE` | `auto` | auto/cuda/cpu |
| `ASR_LANGUAGE` | `zh` | 指定语言（zh/en/ja…）；`none`/`auto` 表示自动检测 |
| `ASR_VAD_FILTER` | `true` | 过滤静音/纯音乐段；歌曲类视频识别不出内容时设 `false` |
| `ASR_ENGINE` | `auto` | auto/whisper/mock |
| `ASR_TIMEOUT` | `1800` | 单任务转写超时（秒），0=不限；CPU 长视频防无限等待 |
| `ASR_PREWARM` | `true` | 启动时后台预热 ASR 模型（Hub 名自动走镜像下载权重） |
| `ASR_VAD_FALLBACK` | `true` | VAD 返回空结果时自动无 VAD 重跑（修 large-v3+vad 空转写 bug） |
| `ASR_CPU_THREADS` | `0` | CPU 推理线程数，0=自动吃满全部核（提速关键项） |
| `ASR_CONTEXT_TEXT` | `false` | false=快约 2 倍（实测 341s→172s）；true=逐字精度略高但慢 |
| `LLM_CORRECT` | `true` | 转写纠错开关（LLM 二次校对错字） |
| `CORRECT_BATCH_SIZE` / `CORRECT_MAX_SEGMENTS` | `20` / `500` | 纠错批大小与段数上限 |
| `CORRECT_PUNCTUATION` | `false` | 纠错增强档：修错字基础上优化断句/标点（opt-in，保引用对齐） |
| `SUMMARY_SELF_CHECK` | `off` | 摘要自检：off=关闭 / full=仅 full 档 / always=所有档（+1 次 LLM 调用） |
| `API_TOKEN` | （空） | 非空时 /api/* 写操作需带 X-API-Key 头（防公网白嫖 LLM 余额） |
| `MAX_VIDEO_DURATION` | `10800` | 秒，0=不限 |
| `MAX_CHUNKS` | `50` | 分块上限 |
| `CHUNK_TOKENS` / `OVERLAP_TOKENS` | `3000` / `200` | 分块与重叠预算 |
| `MAX_CONCURRENT_ASR` | `2` | 转写并发上限 |
| `MAX_CONCURRENT_LLM_TASKS` | `2` | 全局 LLM 并发上限（同时整理/纠错的任务数） |
| `TRANSCRIPT_CACHE` | `true` | 转写级缓存（同视频换深度时复用，跳过下载+转写+纠错） |
| `META_CACHE_TTL` | `86400` | 视频元信息缓存 TTL（秒，0=关闭） |
| `DATA_RETENTION_DAYS` | `0` | 数据保留天数（0=不清理；>0 自动清理过期任务目录与 DB 记录） |
| `LLM_MAX_TOKENS` | `0` | 单次 LLM 响应 max_tokens 上限（0=不限） |
| `YTDLP_COOKIES` | （空） | yt-dlp cookies 文件路径（大会员/需登录视频） |
| `KEEP_MEDIA` | `false` | 保留中间音频/视频 |

## 自定义提示词模板

整理阶段默认可直接使用；需要定制时有两种方式（请求级优先于 .env）：

**1. API 提交时传入**（`POST /api/extract`）：
```json
{
  "url": "https://www.bilibili.com/video/BVxxxxxx",
  "map_prompt": "请用 Markdown 列表提炼第 {Index} 段（{StartTime}-{EndTime}s）……\n{ChunkText}",
  "reduce_prompt": "请归纳《{Title}》……\n{ChunkResults}"
}
```

**2. .env 全局配置**：`LLM_MAP_PROMPT` / `LLM_REDUCE_PROMPT`（见 `.env.example`）。

占位符（`{Var}` 形式精确替换，不与模板内 JSON 花括号冲突）：

| 阶段 | 可用占位符 |
|---|---|
| Map（逐块提炼） | `{Index}` `{StartTime}` `{EndTime}` `{Audience}` `{Purpose}` `{ChunkText}` |
| Reduce（全局归纳） | `{Title}` `{Uploader}` `{Duration}` `{Audience}` `{Purpose}` `{ChunkResults}` |

> ⚠️ 自定义模板仍应要求模型「只输出 JSON」并按默认结构返回字段（summary/key_points/topics/quotes/actions），否则字段缺失会静默为空。

## LLM 提示词

整理阶段的完整提示词（Map/Reduce 两阶段）见《开发提示词-视频内容提取与智能整理.md》第 8 节，代码实现位于 `app/summarizer.py`。

## 验证

```bash
python -m pytest tests -q                  # 单元测试（103/103 通过）
python tools/verify_api.py                 # API 冒烟：启动/校验/缓存去重/鉴权/导出/分P探测（临时 DATA_DIR）
python -m app.cli "<真实B站链接>"           # 端到端
```

**已实测结论**（2026-11 开发环境）：
- 真实 B 站视频下载 ✅（bestaudio 直链，m4a 无需 FFmpeg）
- 真实 Whisper 转写 ✅（本地 small 模型 + CPU int8，自动检测 `en`，212s 音频 → 28 段/1700+ 字，歌词识别正确，SRT 正常生成）
- 智能整理（DeepSeek Map-Reduce）✅（真实 LLM 调用：摘要/要点/分主题/引用/行动项结构完整，无降级）
- 时间戳校准 ✅（引用出处已对齐真实转写时间：如副歌 → 00:43、01:09 等；重复歌词句子取首现位置）
- 中文视频全链路 ✅（BV1C34q6yEv5 财经科普：自动检测 zh、562s→126 段、要点 8 条全含数据——25万亿市值/5.22万亿英伟达/H100 折旧等，出处 01:03/02:13/03:23/05:29 等全部命中真实时间；产物 `data/e2e_zh.md`）
- 转写纠错 ✅（同视频 126 段中 34 段被修正，全部合理无误改："美贼→美债"×5、"振卫→真为"、"油产阶级→无产阶级"、"星际之门/压舱石/AI基建"等；不确定处保守保留原文；对照见 `data/correct_report.txt`）
- 全流程端到端 ✅（真实下载 → 真实转写 → 真实 LLM 整理 → Markdown 落盘，产物见 `data/e2e_full_llm.md`）
- Web API 全链路 ✅（提交→轮询→结果，无效链接 400，任务取消）
- 任务持久化 ✅（服务重启后任务状态与结果完整恢复）
- 浏览器 UI 全流程 ✅（输入→进度→结果渲染→复制/下载）

## Roadmap

- [x] 任务状态持久化（SQLite，重启不丢失）
- [x] 自定义提示词模板（.env / API 双通道）
- [x] 转写纠错（LLM 二次校对 ASR 文本）
- [x] 提取深度三档校验落地（essence/detailed/full）
- [x] 结果缓存去重（链接+深度+模板复合键复用）
- [x] 转写级缓存（同视频换深度时复用转写，跳过下载+转写+纠错）
- [x] LLM 用量统计 + 全局并发限流
- [x] 运行时 ASR 模型覆盖（快速预览档）
- [x] 输出语言参数（zh/en/ja）
- [x] B 站 Cookie 支持（大会员视频）
- [x] 数据保留策略（自动清理过期任务）
- [x] 前端任务历史页（状态筛选 + 分页 + 回看结果）
- [x] Markdown 渲染视图（渲染/源码切换）
- [x] 视频元信息缓存
- [x] API 鉴权（X-API-Key，防公网白嫖）
- [x] 摘要自检（Reduce 后评审补齐关键数据）
- [x] 纠错断句+标点增强档（opt-in）
- [x] 视频分 P 指定处理
- [x] 模型下载进度可视化 + ASR 启动预热 + 转写超时保护
- [x] HF 镜像自动兜底（国内开箱即用）
- [x] 分 P 一键批量（选集探测 + 勾选提交 + `1-3` 范围语法）
- [x] 导出 Word / PDF
- [x] 重试保留原任务参数
- [x] Docker Compose 一键部署（CPU 版）
- [ ] Docker GPU 版（CUDA 加速转写）