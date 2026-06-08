"""请求ID中间件"""

import time
from collections.abc import Callable

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

# RequestIDMiddleware 已移除 - 如需request_id功能可重新添加


class RequestTimingMiddleware(BaseHTTPMiddleware):
    """记录请求处理时间的中间件"""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.time()

        # 延迟导入
        from src.common.logging import (
            SERVER_INTERFACE_LOG,
            build_server_access_log,
            build_server_error_access_log,
            generate_request_id,
            get_request_id_header_name,
            write_interface_log,
        )

        # 生成请求ID并添加到请求状态中（默认启用）
        request_id = await generate_request_id()
        request.state.request_id = request_id
        request.state.interface_log_start_time = start_time

        try:
            response = await call_next(request)

            response_time = time.time() - start_time

            response.headers["X-Process-Time"] = f"{response_time:.3f}s"
            header_name = await get_request_id_header_name()
            response.headers[header_name] = request_id
            if not getattr(request.state, "defer_server_interface_log", False):
                write_interface_log(
                    SERVER_INTERFACE_LOG,
                    build_server_access_log(
                        request,
                        response,
                        response_time,
                        request_id,
                    ),
                    request_id,
                )

            return response

        except Exception as exc:
            response_time = time.time() - start_time
            error_content = (
                f'{{"error":"Internal Server Error","request_id":"{request_id}"}}'
            )

            response = Response(
                content=error_content,
                status_code=500,
                media_type="application/json",
            )
            header_name = await get_request_id_header_name()
            response.headers[header_name] = request_id
            response.headers["X-Process-Time"] = f"{response_time:.3f}s"

            write_interface_log(
                SERVER_INTERFACE_LOG,
                build_server_error_access_log(
                    request,
                    response,
                    exc,
                    response_time,
                    request_id,
                ),
                request_id,
            )

            return response


def setup_middlewares(app: FastAPI) -> None:
    """设置所有中间件"""
    # 只保留请求计时中间件
    app.add_middleware(RequestTimingMiddleware)
