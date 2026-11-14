# 部署与运行

两种部署方式：本地 venv，或 Docker。

## 一、本地运行（Windows / Linux / macOS）

1. 建虚拟环境并装依赖：
   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # Linux / macOS
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. 配置环境变量：复制 `.env.example` 为 `.env`，至少填 `LLM_API_KEY`：
   ```bash
   cp .env.example .env
   ```
3. 启动服务：
   - **Windows**：双击 `启动服务.bat`（或 `启动器.vbs`，无黑框）
   - **跨平台**：`uvicorn app.main:app --host 127.0.0.1 --port 8000`
4. 打开 http://127.0.0.1:8000 ，粘贴 B 站链接即可。

> 首次用 `large-v3` 会联网下载模型（默认走 hf-mirror）；想快速验证把 `.env` 的 `ASR_MODEL` 临时设为 `small`。

## 二、Docker 运行

```bash
cp .env.example .env      # 填 LLM_API_KEY
docker compose up -d --build
```
访问 http://localhost:8000 。数据持久化在 `./data`。

## 三、关键环境变量（完整见 `.env.example`）

| 变量 | 作用 | 默认 |
|---|---|---|
| `LLM_API_KEY` | 大模型密钥（OpenAI 兼容协议） | 必填 |
| `LLM_BASE_URL` / `LLM_MODEL` | 模型端点 / 名称 | DeepSeek |
| `ASR_MODEL` | 转写模型 tiny/small/medium/large-v3 | large-v3 |
| `ASR_ENGINE` | auto / whisper / mock | auto |
| `YTDLP_COOKIES` | B 站 cookies.txt 路径（会员/登录片） | 空 |
| `API_TOKEN` | 写操作鉴权（公网部署建议设） | 空（不鉴权） |

## 四、内置桥（可选，仅 Windows + 装了刷B站插件）

`.env` 设 `BRIDGE_ENABLED=true` 后，uvicorn 进程会自动定时扫描 `~/.dsh/bili-duty/out/*.md`，
把「值得看」的 B 站视频转发给本服务提取，结果写入 `essence/`。
不装插件 / Linux 环境下可设 `BRIDGE_ENABLED=false` 关闭。详见 `bridge/SPEC.md`。

## 五、测试与 CI

```bash
python -m pytest tests -q
```
纯单元测试，不联网、不调真实模型/LLM。GitHub Actions（`.github/workflows/ci.yml`）
会在每次 push / PR 自动运行。

## 六、用现成镜像快速体验（无需自己 build）

本仓库每次合并到 `main` 会自动构建镜像并推送到 GitHub Container Registry：

```bash
docker run -p 8000:8000 --env-file .env ghcr.io/ye-yu09/video-extraction:latest
```

打开 http://localhost:8000 。需自备一份 `.env`（至少填 `LLM_API_KEY`）。

> 首次镜像需在仓库 → **Packages** 页把它设为 **Public**，别人才能 `docker pull`（GHCR 包默认私有）。

## 七、部署到公网（让别人直接访问）

1. 一台带 Docker 的服务器（VPS）。
2. `git clone` 本仓库 → 填 `.env`（**务必设 `API_TOKEN`**）→ `docker compose up -d --build`。
3. 用 Caddy / Nginx 反代加 HTTPS，或防火墙放行 8000 端口。

> ⚠️ 公网部署一定要设 `API_TOKEN`，否则任何人都能调用并消耗你的 LLM 额度
> （服务绑定 `0.0.0.0` 且无 token 时，启动会打醒目告警）。
