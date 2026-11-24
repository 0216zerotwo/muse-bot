# 伙伴服务器镜像：Muse 原生运行时（按根 pyproject.toml + uv.lock 安装）+ 伙伴需要的系统组件。
# install.sh 在安装目录构建它；网页和数据都在挂载的 /opt/data 里，不在镜像里。
FROM debian:13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# chromium：伙伴自己的浏览器（无界面）；nodejs/npm + @playwright/mcp：Playwright 浏览器连接；
# ffmpeg：语音；ripgrep：查文件；fonts-noto-cjk/emoji：网页截图里的中文和表情；git/curl：伙伴干活常用
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git tzdata procps \
      chromium fonts-noto-cjk fonts-noto-color-emoji \
      nodejs npm ffmpeg ripgrep \
    && npm install -g @playwright/mcp@0.0.83 \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

WORKDIR /opt/muse
# 先只装依赖（改了代码不用重新下载依赖）
COPY pyproject.toml uv.lock ./
RUN touch README.md LICENSE && \
    uv python install 3.14 && \
    uv sync --frozen --no-dev --no-install-project --python 3.14
COPY muse/ ./muse/
COPY companion/ ./companion/
COPY LICENSE NOTICE README.md ./
# 注：debian:13-slim 系统自带 libsqlite3 3.46.1 含 WAL-reset 损坏 bug，而 uv 安装的独立 Python 3.14
# 内置了新版 SQLite（>= 3.54），并与存量用户 /opt/data/companion/stt/pylib SenseVoice 扩展 ABI 兼容；
# 下面构建期自检显式校验 SQLite >= 3.51.3 及 FTS5 trigram 分词器，确保绝不回退。
RUN uv sync --frozen --no-dev --python 3.14 && \
    /opt/venv/bin/python -c 'import py_vapid, http_ece, anthropic, sqlite3, openai, starlette, uvicorn, muse, companion; \
assert sqlite3.sqlite_version_info >= (3, 51, 3), f"SQLite {sqlite3.sqlite_version} has WAL-reset bug"; \
db = sqlite3.connect(":memory:"); \
db.execute("CREATE VIRTUAL TABLE docs USING fts5(content, tokenize=trigram)"); \
db.execute("INSERT INTO docs VALUES (?)", ("musebot",)); \
assert db.execute("SELECT count(*) FROM docs WHERE docs MATCH ?", ("seb",)).fetchone()[0] == 1, "FTS5 trigram self-test failed"; \
db.close()' && \
    printf '#!/bin/sh\nexec /opt/venv/bin/musebot serve "$@"\n' > /usr/local/bin/gateway && \
    chmod 0755 /usr/local/bin/gateway

# 不用 root 运行；数据目录 /opt/data 归这个用户
RUN useradd --uid 10000 --create-home --home-dir /home/companion companion && \
    mkdir -p /opt/data && chmod 0700 /opt/data && chown companion:companion /opt/data
USER companion
ENV MUSE_HOME=/opt/data HERMES_HOME=/opt/data PATH=/opt/venv/bin:$PATH PYTHONIOENCODING=utf-8
CMD ["musebot", "serve"]
