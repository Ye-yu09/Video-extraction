# 开发状态快照（2026-11-08 更新）

> ✅ **全部核心目标已完成**。最近一轮：视频概要（overview，150-300 字整体概述）（v9）。

## 最新改进（2026-11-08 v9）✅
- **视频概要（overview）**：在"一句话摘要"之外，Reduce 阶段额外生成 150-300 字的整体概述，讲清全片主题、核心论点、关键数据与结论，让人快速知道视频讲了什么。`SummaryResult.overview` 字段；四个 Reduce 模板（essence/detailed/full + 默认）均要求输出 overview；`render_markdown` 在"一句话摘要"后渲染"## 视频概要"段
- **向后兼容**：旧任务无 overview 字段时默认空串，render 自动跳过该段，不报错
- 实测：单元测试 103/103（新增 4 例：reduce 解析 overview / render 渲染概要段 / 空 overview 跳过兼容 / 三档模板均含 overview 要求）；API 冒烟 21/21；服务已重启加载

## 上一轮（2026-11-08 v8）✅
- **VAD 空结果自动回退**：实测发现 `ASR_VAD_FILTER=true + large-v3` 在该版本返回 0 段空转写（隐藏 bug），新增 `ASR_VAD_FALLBACK=true`——空结果时自动无 VAD 重跑一次，保证永远出真实文本；`_transcribe_no_vad` 兜底路径
- **上下文条件转写提速 2 倍**：新增 `ASR_CONTEXT_TEXT`（默认 false）。实测 212s 音频 `condition_on_previous_text` True=341s / False=172s，**快约 2 倍**；False 时长句偶发少字但要点/时间戳校准（去空白子串匹配）不受影响，beam_size 保持 5 不牺牲精度
- **CPU 线程吃满核**：新增 `ASR_CPU_THREADS`（默认 0=自动全部核，本机 14 核）；实测本 faster-whisper 版本**并行 transcribe 会互抢算力反而更慢**（2 路并行 780s > 串行 657s），故放弃分段多线程方案，改由单段吃满 CPU 提速
- **语言固定天然提速**：`.env ASR_LANGUAGE=zh` 已跳过全片语言检测 pass（实测省约 6%）
- 实测：单元测试 99/99（修复沙箱下 `tmp_path` 夹具系统 TMP 被拒：覆盖为工作区内临时目录 fixture）；API 冒烟 21/21；基准复测 condition=False 172s vs True 341s
- 沙箱适配：verify_api 临时 DATA_DIR 改在 `data/_smoke_tmp`（系统 TMP 子目录 sqlite 被 workspace-write 策略拒绝）

## 上一轮（2026-11-07 v7）✅
- **分 P 范围语法批量**：请求新增 `pages` 字段，支持 `1-3`（闭区间）、`2,5,8`（枚举）、`1-4,7`（混合），自动去重升序、上限 200 集；非法写法（`abc`/`3-1`/`0`）返回 400 中文提示。前端「分 P」框直接填 `1-3` 即可一次提交多集——后端逐页走 submit_or_reuse（各集独立任务、缓存互不干扰），返回 `{batch:true, items:[{page,task_id,cached}]}`，历史页跟踪进度
- **分 P 越界智能退化**：批量前先探测实际选集数（fetch_pages），超出集数的页码自动裁剪（如 2 集视频填 `1-5` → 只跑 1,2）；单P视频误填 `pages=9-10` 退化为主视频 page=0，不再产生 `?p=N` 下载报错；仅剩 1 页时退化为单任务路径
- **快捷方式启动脚本修复**：定位并解决"双击一闪而过"与 TESTBE3 错误弹窗问题——bat 改 GBK 编码无 BOM、去除 chcp/timeout 兼容陷阱、start 命令去掉嵌套 cmd 直接拉起 python；冷启动实测通过（服务起 + 自动开浏览器）
- 实测：单元测试 99/99（新增 11 例：parse_pages 范围/枚举/混合去重/全角逗号/非法/超限 + TestClient 批量分支/越界裁剪/单P退化/非法400/单页回归）；API 冒烟 21/21（修复脚本 GBK 控制台 UnicodeEncodeError）；live 验证 pages=5/9-10 于单P视频均正确退化为 page=0

## 上一轮（2026-11-07 v6）✅
- **模型下载进度可视化**：新增 `Stage.LOADING_MODEL`（「⬇️ 加载/下载模型」，进度 20-30%）；hf_hub 进度钩子 `_progress_tqdm` 把权重下载百分比实时回传——首次用 large-v3 不再"卡住假死"
- **ASR 启动预热**：`ASR_PREWARM=true` 服务 lifespan 后台线程 `ensure_model()` 检查/下载权重，首个任务免等几分钟
- **转写超时保护**：`ASR_TIMEOUT=1800`（秒，0=不限）；转写改专用线程 + `asyncio.wait_for` future 模式（真取消不占资源），超时抛 `ERR_ASR_TIMEOUT` 并提示换 tiny/base/small 档
- **HF 镜像自动兜底**：config 加载时未显式配 `HF_ENDPOINT` 则默认 `https://hf-mirror.com`（你这次踩的 WinError 10060 从根上解决，Docker/他人机器同样受益）
- **分 P 一键批量**：`GET /api/pages?url=` yt-dlp `extract_flat` 探测选集列表；前端链接失焦自动弹「🎞️ 检测到多 P 视频」面板 → 勾选集数 → 批量提交（各集独立任务，转写缓存命中时后续集秒出）
- **导出 Word / PDF**：新模块 `app/exporter.py`；`GET /api/tasks/{id}/export?fmt=docx|pdf`；python-docx 渲染标题/列表/引用/粗体（缺依赖自动降级 .md 纯文本）；PDF 走 LibreOffice headless（未装时报友好 500）；结果页工具栏加「导出 Word」「导出 PDF」按钮
- **重试保留原参数**：历史页「重试」读取原任务 depth/page/asr_model/output_lang/purpose 重跑（此前固定 essence）
- **LLM 消耗可见化**：结果页 meta 显示「💰 LLM 消耗 X tokens（输入+输出）」；缓存复用零调用显示「0（缓存复用，零调用）」；pipeline 在 cached_tx+零调用时标记 `llm_tokens.cached_reuse`
- 实测：单元测试 88/88（新增 8 例：exporter 降级/分发/soffice 缺失、loading_model 阶段、asr_timeout 配置、_safe_cb 新旧签名兼容、ensure_model 本地目录、fetch_pages 非法链接）；API 冒烟 21/21（新增未完成导出 409 / 非法 fmt 400 / pages 非法链接 400）

## 上一轮（2026-11-07 v5.1）✅
- **修复：降级任务不再参与缓存复用**——`find_cached` 对 DONE 任务增加 `summary.degraded` 判断：若当时 LLM 未配置/整理失败产出降级稿，之后配好 LLM 再提交会重跑拿真结果，而不是永久复用降级稿（浏览器实操发现：BV1GJ411x7h7 essence 命中了早前无 Key 时的降级缓存）；RUNNING/QUEUED 接入不受影响；转写级缓存仍正常生效（实测新任务下载+转写秒完成、整理耗时 5.5s vs 首次全量）
- **前端 errMsg 解析增强**：鉴权 401 返回顶层 `{code,message}`（非 FastAPI detail 包装）此前显示笼统"请求失败"，现在正确显示「❌ API Token 无效或缺失（需在 X-API-Key 头传入）」
- 实测：单元测试 80/80（新增 3 例：degraded-done 不复用 / normal-done 复用 / running-degraded 可接入）；浏览器全流程操作验证：鉴权拦截提示 → curl 带 Token 建任务 done → 降级缓存修复后重跑产出完整精华稿（摘要/要点/分主题/引用/行动项齐全）→ 二次提交 cached:true → 历史页自动刷新与分页筛选均正常

## 上一轮（2026-11-07 v5）✅
- **API 鉴权（防公网白嫖 LLM）**：`API_TOKEN` 非空时，`/api/*` 写操作（POST）需带 `X-API-Key` 头，否则 401；读操作（health/tasks 列表/详情）放行；前端表单新增 API Token 输入框，自动附带 header
- **摘要自检（质量提升）**：`SUMMARY_SELF_CHECK`（off/full/always）Reduce 后 LLM 再评审一遍，发现遗漏关键数据/事实则补充 key_points（`_self_check` 函数，最多补 3 条）；`full` 档自动、`always` 全档；前端「摘要自检」勾选 = `full`
- **纠错断句+标点档**：`CORRECT_PUNCTUATION` 或请求参数 `correct_punctuation` 开启时，纠错用增强模板（修错字 + 优化断句/标点，不增删词语不改变语义，保引用对齐）；默认只修错字
- **视频分 P 指定处理**：请求参数 `page`（0=默认/无分P，>0=`?p=N`）；`downloader._page_url` 拼接、`fetch_meta`/`download_audio` 透传；`VideoMeta`/`TaskRecord` 落库 `page`；转写缓存键与结果缓存去重均含 `page`（不同分 P 隔离）；前端分 P 输入框
- 实测：单元测试 77/77（新增 13 例：鉴权 4、分P 参数/缓存键/page_url 5、corrector punctuation override 2、新字段默认值 1、非法 page 1）；API 冒烟 `tools/verify_api.py` 18/18（新增 page/纠错/自检透传 + 鉴权无Token 401 + 带Token放行 + 读操作放行）

## 上一轮（2026-11-07 v4）✅
- **转写级缓存（省钱 2.0）**：同一视频换深度/用途时复用已转写文本，跳过下载+转写+纠错全流程；缓存键 = BV + ASR 模型 + 语言 + VAD + 纠错开关 + 引擎，改任一配置自动失效；缓存存纠错后最终文本，命中时 stats 标注 `cached_transcript: true`；首次跑完后写入 `data/cache/transcripts/{key}.json`
- **LLM 用量统计**：`_call_llm` 每次成功响应累计 `resp.usage` 的 prompt/completion tokens（进程级，线程安全）；任务完成时 stats 写入 `llm_tokens: {prompt, completion}`，前端结果页展示「LLM 消耗 X tokens」
- **全局 LLM 并发限流**：`MAX_CONCURRENT_LLM_TASKS=2` 信号量包裹纠错+整理阶段，防多任务并发打爆 API 与费用
- **运行时 ASR 模型覆盖**：前端「快速预览档」可选 tiny/base/small/medium，请求参数 `asr_model` 覆盖服务端 `ASR_MODEL`；转写缓存键含模型名，不同模型自动隔离
- **输出语言参数**：`output_lang`（zh/en/ja）透传 Map/Reduce 提示词，要求 LLM 用指定语言撰写全部输出
- **B 站 Cookie 支持**：`YTDLP_COOKIES` 配置 cookies.txt 路径，解锁大会员/需登录视频
- **数据保留策略**：`DATA_RETENTION_DAYS` 启动+每日定时清理过期任务目录与 DB 记录（含转写缓存文件）；`store.delete()` 新增
- **Markdown 渲染视图**：结果区支持渲染视图/源码切换，轻量 MD→HTML（标题/列表/引用/粗体/代码/分隔线/details），不引第三方库
- **视频元信息缓存**：`META_CACHE_TTL=86400` 内存缓存标题/时长，换深度重提时省一次 yt-dlp 网络请求
- **LLM_MAX_TOKENS**：单次响应上限配置（0=不限），防 full 档长输出截断
- 实测：单元测试 64/64（新增 13 例：转写缓存键 4、LLM 用量统计 3、create_task 新字段 2、清理策略 2、store.delete 2）；API 冒烟 `tools/verify_api.py` 12/12（含 asr_model/output_lang 回显、health 新配置项）

## 上一轮（2026-11-07 v3）✅
- **提取深度全链路落地**：边界校验（models 层 Literal 校验，非法 depth → 422，不再静默回退 essence）；TaskRecord 落库 `url/depth/audience/purpose/prompt_hash`；前端结果区/历史页展示深度标签；CLI `--depth` 三档保持
- **结果缓存去重（省钱核心）**：`find_cached` 改为「规范化链接 + depth + 自定义模板哈希」复合键——essence 缓存不会被 detailed 请求误命中；自定义模板不同强制重跑；**修复 queued 竞态**（video 未获取时凭创建时落库的 `rec.url` 命中，防并发重复建任务）；`submit_or_reuse` 提交锁保证 find+create 原子；DONE 结果经 SQLite 持久化，服务重启后依然命中
- **前端任务历史页（可回看历史）**：状态筛选（后端 `GET /api/tasks?status=` 新参数）、分页（10 条/页 + 上下页控件）、运行中/排队任务 5 秒自动刷新、操作列（查看结果/下载 .md/字幕 SRT/重试/取消）；提交命中缓存时绿色提示「⚡ 命中缓存」，422 参数错误转为友好中文提示
- 实测：单元测试 51/51（新增 21 例：depth 校验 2、缓存深度区分 2、queued 落库命中 1、模板哈希 2、submit_or_reuse 2、create_task 落库 1、状态筛选 1 等）；API 冒烟 `tools/verify_api.py` 8/8（启动/非法链接 400/非法 depth 422/首提 cached=false/重复提交复用 task_id/不同深度新建/详情含 depth+url）
- 产物：`static/index.html`（双卡页：提取 + 历史任务）、`tools/verify_api.py`（可复用冒烟脚本，临时 DATA_DIR 不碰真实库）

## 上一轮（2026-11-07 v2）✅
- **LLM 转写纠错**（新增 Stage.CORRECTING）：转写后逐批（20 段）交给 LLM 校对错字（"美贼→美债""星际之门""压舱石"等），只修错字不改语义、失败自动降级保留原文；原始转写留档 `transcript.raw.json`
- 实测：126 段中 34 段被修正，全部合理无误改，不确定处保守保留；对照 `data/correct_report.txt`；单元测试 30/30（6 例 corrector：替换/降级/上限/无 Key 跳过）
- 配置：`LLM_CORRECT=true`（默认开）、`CORRECT_BATCH_SIZE=20`、`CORRECT_MAX_SEGMENTS=500`
- **时间戳确定性校准**：Map 输入改为带 `[mm:ss]` 逐句时间（Chunk.timed_text）；KeyPoint 增加 evidence 字段；后端 `_align_quote_time` 用转写段做 ①整句包含 ②前缀 ③difflib 相似度 三级模糊匹配回填出处，覆盖 LLM 猜测值
- **要点具体化**：Map 要求 core_ideas 结合原文具体表述；Reduce 要求 point 含细节/数据/关键词、evidence 逐字原句
- 实测（DeepSeek 真实调用）：引用出处命中真实时间（副歌 00:43、01:09、00:00）；要点含"副歌在 00:43/01:25/01:42/02:41/02:58/03:15 重复"等具体内容，产物 `data/llm_verify_v2.md`
- **中文视频全链路实测**（BV1C34q6yEv5 财经科普）：自动检测 zh、562s→126 段；8 条要点全含具体数据（25万亿/5.22万亿/H100 折旧/ChatGPT 亏 140 亿等），出处 01:03/02:13/03:23/05:29/04:51 全部命中真实时间；6 个分主题按时间段组织；产物 `data/e2e_zh.md`。小瑕疵：small 模型个别词识别误差（"资产阶级"→"油产阶级"等），数据类要点全部正确
- 已知边界：重复歌词句对齐取首现位置（歧义固有）；LLM 偶发不逐字抄录 evidence 时对齐退化
- 单元测试 24/24（新增 8 例：对齐 6 例 + 校准 2 例）

## 已完成 ✅
- 第一版完整实现：下载 → 转写 → 分块 → LLM 整理 → 输出（Web + CLI 双入口）
- **LLM 智能整理实测通过（DeepSeek）**：Map-Reduce 真实调用，产物 `data/llm_verify.md`、`data/e2e_full_llm.md`（无降级，摘要/要点/分主题/引用/行动项结构完整）
- **全流程端到端**：真实下载 → 真实 Whisper 转写（en/212s/28段）→ 真实 LLM 整理 → Markdown 落盘
- 单元测试 16/16；任务持久化 + 重启恢复 + 中断任务自动标记 failed
- ASR 配置增强：本地模型目录 / ASR_LANGUAGE=none / ASR_VAD_FILTER
- 任务分页 + 自定义提示词模板（.env 与 API 双通道）
- Docker 部署文件；PyAV==13.1.0 锁定

## 环境备忘
- venv：`.venv`（Python 3.12）；服务：`$env:ASR_ENGINE="mock"; .\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000`
- 本地模型：`data/models/whisper-small`（461MB 已就位）；转写验证脚本：`tools/verify_transcribe.py <音频>`
- 国内网络：HuggingFace 直连超时，必须 `HF_ENDPOINT=https://hf-mirror.com`
- PowerShell 会把 Python stderr 日志显示为红色错误并报 exit 1，属显示假象，以 `$LASTEXITCODE` 为准

## 待办（可选项）
- Docker GPU 版（需 NVIDIA 机器实测）、SSE 实时进度（性价比低暂缓）
- 安全提示：`.env` 含真实 API Key，注意不要提交到公开仓库（.dockerignore 已排除）；若开启 `API_TOKEN`，白嫖风险已缓解