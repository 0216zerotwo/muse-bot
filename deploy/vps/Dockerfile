# 伙伴服务器镜像：Hermes（源码，按 uv.lock 安装，和电脑上的环境一致）+ 伙伴需要的系统组件。
# install.sh 在 /opt/companion 下构建它；伙伴插件、网页和数据都在挂载的 /opt/data 里，不在镜像里。
FROM debian:13-slim

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 TZ=Asia/Shanghai
# chromium：伙伴自己的浏览器（无界面）；nodejs/npm：Playwright 浏览器连接（npx @playwright/mcp）；
# ffmpeg：语音；ripgrep：查文件；fonts-noto-cjk/emoji：网页截图里的中文和表情；git/curl：伙伴干活常用
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git tzdata procps \
      chromium fonts-noto-cjk fonts-noto-color-emoji \
      nodejs npm ffmpeg ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

WORKDIR /opt/hermes
# 先只装依赖（改了代码不用重新下载依赖）
COPY hermes/pyproject.toml hermes/uv.lock ./
RUN uv python install 3.14 && \
    uv sync --frozen --no-dev --no-install-project --python 3.14 \
      --extra mcp --extra web --extra ddgs --extra doc-extract --extra computer-use && \
    uv pip install --python /opt/venv/bin/python http-ece==1.2.1 py-vapid==1.9.6
COPY hermes/ ./
RUN uv sync --frozen --no-dev --python 3.14 \
      --extra mcp --extra web --extra ddgs --extra doc-extract --extra computer-use

# 不用 root 运行；数据目录 /opt/data 归这个用户
RUN useradd --uid 10000 --create-home --home-dir /home/companion companion && \
    mkdir -p /opt/data && chown companion:companion /opt/data
USER companion
ENV HERMES_HOME=/opt/data PATH=/opt/venv/bin:$PATH PYTHONIOENCODING=utf-8
CMD ["python", "hermes", "gateway", "run"]
