#!/usr/bin/env python3
"""
Anthropic-OpenAI Proxy 启动脚本

使用 JSON 配置文件中的 host 和 port 启动服务器，并支持环境变量覆盖。
配置优先级：
1. 命令行指定的 config 参数
2. 环境变量 CONFIG_PATH 指定的路径
3. ./config/settings.json (默认)
4. ./config/example.json (模板)
"""

import argparse
import os
import sys
from pathlib import Path

import uvicorn

# 添加 src 目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.config.settings import DEFAULT_CONFIG_PATH, Config


def main():
    """主启动函数"""
    try:
        parser = argparse.ArgumentParser(description="启动 Anthropic-OpenAI Proxy")
        parser.add_argument(
            "--config", type=str, help="JSON 配置文件路径 (默认为 config/settings.json)"
        )
        parser.add_argument(
            "--config-path",
            type=str,
            default=DEFAULT_CONFIG_PATH,
            help="配置文件路径，可通过 CONFIG_PATH 环境变量指定",
        )

        args = parser.parse_args()

        # 确保从项目根目录启动
        project_root = Path(__file__).parent
        os.chdir(project_root)

        # 确定配置文件路径
        config_path = args.config or os.getenv("CONFIG_PATH", args.config_path)
        os.environ["CONFIG_PATH"] = config_path

        # 同步加载配置
        config = Config.from_file_sync(config_path)

        # 获取服务器配置
        host, port = config.get_server_config()

        print("🚀 启动 OpenAI To Claude Server...")
        print(f"   配置文件: {config_path}")
        print(f"   监听地址: {host}:{port}")
        print()
        print("📋 重要端点:")
        print(f"   健康检查: http://{host}:{port}/health")
        print(f"   API文档: http://{host}:{port}/docs")
        print(f"   OpenAPI: http://{host}:{port}/openapi.json")
        print()

        # 启动 Uvicorn 服务器
        uvicorn.run(
            "src.main:app",
            host=host,
            port=port,
            # reload=True,
            timeout_keep_alive=60,
            log_level=config.logging.level.lower(),
        )
    except Exception as e:
        print(f"❌ 启动失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
