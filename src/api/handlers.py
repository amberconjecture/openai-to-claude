"""
Anthropic /v1/messages 端点处理程序

实现Anthropic native messages API与OpenAI API的转换和代理
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from types import SimpleNamespace

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from src.common.logging import (
    SERVER_INTERFACE_LOG,
    build_server_access_log,
    format_log_fields,
    sanitize_url,
    summarize_anthropic_request_payload,
    summarize_anthropic_response_payload,
    summarize_openai_response_payload,
    summarize_sse_event,
    summarize_token_usage,
    write_interface_log,
)
from src.core.clients.openai_client import OpenAIServiceClient
from src.core.converters.request_converter import (
    AnthropicToOpenAIConverter,
)
from src.core.converters.response_converter import OpenAIToAnthropicConverter
from src.models.anthropic import (
    AnthropicMessageResponse,
    AnthropicRequest,
)
from src.models.errors import get_error_response

router = APIRouter(prefix="/v1", tags=["messages"])

STREAM_HEARTBEAT_INTERVAL_SECONDS = 5.0
STREAM_HEARTBEAT_COMMENT = ": keep-alive\n\n"
STREAM_CHUNK_IDLE_TIMEOUT_SECONDS = 300.0


def _record_stream_token_usage(request: Request, event_text: str) -> None:
    """从 Anthropic SSE 事件里提取 usage，写入最终接口日志上下文。"""
    data_parts = []
    for line in event_text.splitlines():
        if line.startswith("data:"):
            data_parts.append(line.removeprefix("data:").strip())

    if not data_parts:
        return

    try:
        event_data = json.loads("\n".join(data_parts))
    except json.JSONDecodeError:
        return

    if not isinstance(event_data, dict):
        return

    usage = event_data.get("usage")
    if usage is None:
        message = event_data.get("message")
        if isinstance(message, dict):
            usage = message.get("usage")

    token_usage = summarize_token_usage(usage, "anthropic_usage")
    if token_usage:
        request.state.interface_log_tokens = token_usage


def _write_deferred_server_interface_log(
    request: Request,
    request_id: str | None,
    response: StreamingResponse,
) -> None:
    """流式响应结束后写一行服务端接口日志。"""
    if getattr(request.state, "server_interface_log_written", False):
        return

    request.state.server_interface_log_written = True
    start_time = getattr(request.state, "interface_log_start_time", time.time())
    response_time = time.time() - start_time
    response_like = SimpleNamespace(
        status_code=getattr(response, "status_code", 200),
        headers=getattr(response, "headers", {}),
        media_type=getattr(response, "media_type", "text/event-stream"),
    )
    write_interface_log(
        SERVER_INTERFACE_LOG,
        build_server_access_log(request, response_like, response_time, request_id),
        request_id,
    )


class MessagesHandler:
    """处理Anthropic /v1/messages 端点请求"""

    def __init__(self, config):
        self.request_converter = AnthropicToOpenAIConverter()
        self.response_converter = OpenAIToAnthropicConverter()
        self.config = config
        self._config = None
        self.client = OpenAIServiceClient(
            api_key=config.openai.api_key,
            base_url=config.openai.base_url,
        )

    @classmethod
    async def create(cls, config=None):
        """异步工厂方法创建 MessagesHandler 实例"""
        if config is None:
            from src.config.settings import get_config

            config = await get_config()

        instance = cls.__new__(cls)
        instance.request_converter = AnthropicToOpenAIConverter()
        instance.response_converter = OpenAIToAnthropicConverter()
        instance.config = config
        instance._config = config
        instance.client = OpenAIServiceClient(
            api_key=config.openai.api_key,
            base_url=config.openai.base_url,
        )
        return instance

    async def aclose(self):
        """关闭处理器持有的上游客户端连接池。"""
        await self.client.aclose()

    async def process_message(
        self, request: AnthropicRequest, request_id: str = None
    ) -> AnthropicMessageResponse:
        """处理非流式消息请求"""
        # 获取绑定了请求ID的logger
        from src.common.logging import get_logger_with_request_id

        bound_logger = get_logger_with_request_id(request_id)

        try:
            bound_logger.debug("处理非流式请求")
            # 验证请求
            # await validate_anthropic_request(request, request_id)
            # 将 Anthropic 请求转换为 OpenAI 格式（异步）
            openai_request = await self.request_converter.convert_anthropic_to_openai(
                request, request_id
            )

            # 发送到 OpenAI
            openai_response = await self.client.send_request(
                openai_request, request_id=request_id
            )
            bound_logger.debug(
                "OpenAI响应概要 - "
                f"{format_log_fields(summarize_openai_response_payload(openai_response))}"
            )

            # 将 OpenAI 响应转回 Anthropic 格式
            anthropic_response = await self.response_converter.convert_response(
                openai_response, request.model, request_id
            )
            bound_logger.info(
                "Anthropic响应生成完成 - "
                f"{format_log_fields(summarize_anthropic_response_payload(anthropic_response))}"
            )

            return anthropic_response

        except ValidationError as e:
            bound_logger.warning(f"Validation error - Errors: {e.errors()}")
            error_response = get_error_response(
                422, details={"validation_errors": e.errors(), "request_id": request_id}
            )
            raise HTTPException(
                status_code=422, detail=error_response.model_dump()
            ) from e

        except json.JSONDecodeError as e:
            # 专门处理JSON解析错误，这通常发生在OpenAI响应解析时
            bound_logger.exception(
                f"JSON解析错误 - Error: {str(e)}, Position: {e.pos if hasattr(e, 'pos') else 'unknown'}"
            )
            error_response = get_error_response(
                502,
                message="上游服务返回无效JSON格式",
                details={"json_error": str(e), "request_id": request_id},
            )
            raise HTTPException(
                status_code=502, detail=error_response.model_dump()
            ) from e
        except HTTPException as e:
            bound_logger.exception(
                f"处理非流式消息请求错误 - Type: {type(e).__name__}, Error: {str(e)}"
            )
            error_response = get_error_response(
                e.status_code, message=str(e.detail), details={"request_id": request_id}
            )
            raise HTTPException(
                status_code=e.status_code,
                detail=error_response.model_dump(exclude_none=True),
            ) from e

        except Exception as e:
            bound_logger.exception(
                f"处理非流式消息请求错误 - Type: {type(e).__name__}, Error: {str(e)}"
            )
            error_response = get_error_response(
                500, message=str(e), details={"request_id": request_id}
            )
            raise HTTPException(
                status_code=500, detail=error_response.model_dump(exclude_none=True)
            ) from e

    async def process_stream_message(
        self, request: AnthropicRequest, request_id: str = None
    ) -> AsyncGenerator[str, None]:
        """处理流式消息请求，使用新的流式转换器"""
        if not request.stream:
            raise ValueError("流式响应参数必须为true")

        # 获取绑定了请求ID的logger
        from src.common.logging import get_logger_with_request_id

        bound_logger = get_logger_with_request_id(request_id)

        try:
            # await validate_anthropic_request(request, request_id)
            openai_request = await self.request_converter.convert_anthropic_to_openai(
                request, request_id
            )

            # 创建 OpenAI 流式数据源
            async def openai_stream_generator():
                bound_logger.info("开始OpenAI流式生成")
                chunk_count = 0
                upstream_stream = self.client.send_streaming_request(
                    openai_request, request_id=request_id
                )
                try:
                    async for chunk in upstream_stream:
                        # 跳过被解析器过滤掉的不完整chunk（通常是tool_calls片段）
                        if chunk is not None:
                            chunk_count += 1
                            # 将 OpenAI 响应对象转换为字符串格式
                            bound_logger.debug(
                                "OpenAI流式事件概要 - "
                                f"{format_log_fields(summarize_sse_event(chunk))}"
                            )
                            yield f"{chunk}\n\n"
                    bound_logger.debug(f"OpenAI流式生成完成，总共{chunk_count}个chunk")
                finally:
                    await upstream_stream.aclose()

            # 使用新的流式转换器
            bound_logger.info("开始流式转换")
            async for (
                anthropic_event
            ) in self.response_converter.convert_openai_stream_to_anthropic_stream(
                openai_stream_generator(), model=request.model, request_id=request_id
            ):
                bound_logger.debug(
                    "Anthropic流式事件概要 - "
                    f"{format_log_fields(summarize_sse_event(anthropic_event))}"
                )
                yield anthropic_event
            bound_logger.info("流式转换完成")

        except (ValidationError, ValueError) as e:
            error_detail = e.errors() if hasattr(e, "errors") else str(e)
            bound_logger.warning(f"流式请求验证失败 - Errors: {error_detail}")
            error_response = get_error_response(422, message=str(error_detail))
            # 在错误响应中添加请求ID
            error_data = error_response.model_dump()
            if request_id:
                error_data["request_id"] = request_id
            yield f"event: error\ndata: {json.dumps(error_data, ensure_ascii=False)}\n\n"

        except json.JSONDecodeError as e:
            # 专门处理流式模式下的JSON解析错误
            bound_logger.exception(
                f"流式模式JSON解析错误 - Error: {str(e)}, Position: {e.pos if hasattr(e, 'pos') else 'unknown'}"
            )
            error_response = get_error_response(
                502,
                message="流式响应中发现无效JSON格式",
                details={"json_error": str(e), "request_id": request_id},
            )
            error_data = error_response.model_dump()
            if request_id:
                error_data["request_id"] = request_id
            yield f"event: error\ndata: {json.dumps(error_data, ensure_ascii=False)}\n\n"

        except Exception as e:
            bound_logger.exception(
                f"流式请求处理错误 - Type: {type(e).__name__}, Error: {str(e)}"
            )
            error_response = get_error_response(500, message=str(e))
            # 在错误响应中添加请求ID
            error_data = error_response.model_dump()
            if request_id:
                error_data["request_id"] = request_id
            yield f"event: error\ndata: {json.dumps(error_data, ensure_ascii=False)}\n\n"


@router.post("/messages")
async def messages_endpoint(request: Request, background_tasks: BackgroundTasks):
    """
    Anthropic /v1/messages 端点

    这个端点实现了Anthropic原生messages API的主要功能：
    - 接受Anthropic格式的请求
    - 转换为OpenAI格式发送到后端
    - 返回Anthropic格式的响应
    """
    # 从应用状态获取消息处理器（已由main.py在启动时初始化）
    handler: MessagesHandler = request.app.state.messages_handler

    # 获取请求ID（由中间件生成，如果启用的话）
    from src.common.logging import (
        get_logger_with_request_id,
        get_request_id_from_request,
    )

    request_id = get_request_id_from_request(request)
    bound_logger = get_logger_with_request_id(request_id)

    # 记录请求
    client_ip = request.client.host if request.client else "unknown"
    bound_logger.info(
        f"收到Anthropic请求 - Method: {request.method}, URL: {sanitize_url(request.url)}, IP: {client_ip}"
    )

    try:
        # 解析请求体
        body = await request.json()
        bound_logger.debug(
            "Anthropic请求概要 - "
            f"{format_log_fields(summarize_anthropic_request_payload(body))}"
        )

        anthropic_request = AnthropicRequest(**body)

        # 记录清理后的请求信息（移除敏感信息）
        # safe_body = sanitize_for_logging(body)
        # logger.debug("请求已清理", request_body=safe_body)

        # 根据请求类型处理响应
        if anthropic_request.stream:
            request.state.defer_server_interface_log = True

            async def stream_response():
                """透传 SSE chunk，并用注释 heartbeat 避免空闲连接被中断。"""
                stream = handler.process_stream_message(
                    anthropic_request, request_id=request_id
                )
                next_chunk_task = asyncio.create_task(stream.__anext__())
                loop = asyncio.get_running_loop()
                last_chunk_at = loop.time()
                try:
                    while True:
                        idle_remaining = STREAM_CHUNK_IDLE_TIMEOUT_SECONDS - (
                            loop.time() - last_chunk_at
                        )
                        if idle_remaining <= 0:
                            error_data = get_error_response(
                                504,
                                message=(
                                    "Streaming response timed out waiting for the next "
                                    "chunk"
                                ),
                                details={
                                    "type": "stream_chunk_timeout",
                                    "timeout_seconds": STREAM_CHUNK_IDLE_TIMEOUT_SECONDS,
                                },
                            ).model_dump()
                            if request_id:
                                error_data["request_id"] = request_id
                            yield (
                                "event: error\n"
                                f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
                            )
                            break

                        done, _ = await asyncio.wait(
                            {next_chunk_task},
                            timeout=min(
                                STREAM_HEARTBEAT_INTERVAL_SECONDS,
                                idle_remaining,
                            ),
                        )
                        if not done:
                            if (
                                loop.time() - last_chunk_at
                                >= STREAM_CHUNK_IDLE_TIMEOUT_SECONDS
                            ):
                                continue
                            yield STREAM_HEARTBEAT_COMMENT
                            continue

                        try:
                            chunk = next_chunk_task.result()
                        except StopAsyncIteration:
                            break

                        last_chunk_at = loop.time()
                        _record_stream_token_usage(request, chunk)
                        yield chunk
                        next_chunk_task = asyncio.create_task(stream.__anext__())
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    bound_logger.exception(f"流式处理出错 - Error: {str(e)}")
                    error_data = {"error": str(e)}
                    if request_id:
                        error_data["request_id"] = request_id
                    error_event = f"event: error\ndata: {json.dumps(error_data)}\n\n"
                    yield error_event
                finally:
                    if not next_chunk_task.done():
                        next_chunk_task.cancel()
                        try:
                            await next_chunk_task
                        except (asyncio.CancelledError, StopAsyncIteration):
                            pass
                    await stream.aclose()
                    _write_deferred_server_interface_log(
                        request,
                        request_id,
                        streaming_response,
                    )

            streaming_response = StreamingResponse(
                stream_response(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "X-Content-Type-Options": "nosniff",
                },
            )
            return streaming_response
        else:
            # 非流式响应
            response = await handler.process_message(
                anthropic_request, request_id=request_id
            )
            request.state.interface_log_tokens = summarize_token_usage(
                response.usage,
                "anthropic_usage",
            )
            json_response = JSONResponse(content=response.model_dump(exclude_none=True))
            if request_id:
                json_response.headers["X-Request-ID"] = request_id
            return json_response

    except ValidationError as e:
        bound_logger.warning(f"请求验证失败 - Errors: {e.errors()}")
        error_response = get_error_response(
            422, details={"validation_errors": e.errors()}
        )
        error_detail = error_response.model_dump()
        error_detail["request_id"] = request_id
        raise HTTPException(status_code=422, detail=error_detail) from e

    except json.JSONDecodeError as e:
        bound_logger.warning(f"请求中的JSON格式错误 - Error: {str(e)}")
        error_response = get_error_response(400, message="无效的JSON格式")
        error_detail = error_response.model_dump()
        error_detail["request_id"] = request_id
        raise HTTPException(status_code=400, detail=error_detail) from e

    except Exception as e:
        # 检查是否为HTTPException，避免重复记录已处理的错误
        if isinstance(e, HTTPException):
            # HTTPException已经在内层处理过，直接重新抛出
            raise e

        bound_logger.exception(
            f"在messages端点发生意外错误 - Type: {type(e).__name__}, Error: {str(e)}"
        )
        error_response = get_error_response(500, message=str(e))
        error_detail = error_response.model_dump()
        error_detail["request_id"] = request_id
        raise HTTPException(status_code=500, detail=error_detail) from e
