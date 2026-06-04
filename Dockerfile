# 构建阶段
FROM python:3.11-slim AS builder

# 设置工作目录
WORKDIR /app

# 安装 uv 工具
RUN pip install --no-cache-dir uv

# 先复制依赖清单，最大化 Docker 依赖层缓存命中
COPY pyproject.toml uv.lock ./

# 将依赖安装到项目虚拟环境；不安装本项目，避免复制源码前破坏缓存
RUN uv sync --frozen --no-dev --no-install-project

# 运行阶段
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# 复制构建阶段已安装好的虚拟环境
COPY --from=builder /app/.venv /app/.venv

# 复制项目文件
COPY . .

# 创建日志和配置目录
RUN mkdir -p /app/logs /app/config

# 暴露端口
ARG SERVER_PORT=8000
EXPOSE ${SERVER_PORT}

# 启动应用
CMD ["/app/.venv/bin/python", "main.py"]
