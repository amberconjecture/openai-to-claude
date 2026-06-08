"""OpenAI API client for making asynchronous requests to OpenAI endpoints."""

import codecs
import json
import os
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from loguru import logger

from src.common.logging import (
    CLIENT_INTERFACE_LOG,
    build_client_access_log,
    sanitize_log_mapping,
    sanitize_url,
    summarize_openai_request_payload,
    summarize_openai_response_payload,
    summarize_timeout,
    summarize_token_usage,
    write_interface_log,
)
from src.common.token_cache import get_cached_tokens
from src.models.errors import StandardErrorResponse, get_error_response
from src.models.openai import OpenAIRequest, OpenAIStreamResponse


class OpenAIClientError(Exception):
    """Base exception for OpenAI client errors."""

    def __init__(self, error_response: StandardErrorResponse):
        self.error_response = error_response
        error = error_response.error
        super().__init__(
            f"OpenAIClientError(code={error.code}, type={error.type}, param={error.param})"
        )


class OpenAIServiceClient:
    """Async OpenAI API client with connection pooling and retry logic."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float | None = None,
        stream_read_timeout: float | None = None,
        max_connections: int | None = None,
        max_keepalive_connections: int | None = None,
    ):
        """Initialize OpenAI client with connection pool.

        Args:
            api_key: OpenAI API密钥
            base_url: OpenAI API基础URL
            timeout: 请求超时时间(秒)
            stream_read_timeout: 流式响应两次读取之间的超时时间(秒)，None 表示不限制
            max_connections: 连接池最大连接数
            max_keepalive_connections: 连接池最大空闲连接数
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout if timeout is not None else self._get_timeout_from_env()
        self.stream_read_timeout = stream_read_timeout
        if stream_read_timeout is None and os.getenv("STREAM_READ_TIMEOUT"):
            self.stream_read_timeout = self._get_timeout_from_env(
                "STREAM_READ_TIMEOUT", default=None
            )
        self.max_connections = max_connections or self._get_int_from_env(
            "OPENAI_MAX_CONNECTIONS", 200
        )
        self.max_keepalive_connections = (
            max_keepalive_connections
            or self._get_int_from_env("OPENAI_MAX_KEEPALIVE_CONNECTIONS", 50)
        )

        self.client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Connection": "keep-alive",
            },
            # 确保自动解压缩响应
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=self.max_connections,
                max_keepalive_connections=self.max_keepalive_connections,
                keepalive_expiry=30.0,
            ),
            timeout=self.timeout,
        )

    @staticmethod
    def _get_timeout_from_env(
        env_name: str = "REQUEST_TIMEOUT", default: float | None = 60.0
    ) -> float | None:
        """Read a positive timeout value from the environment."""
        raw_value = os.getenv(env_name)
        if raw_value is None or raw_value == "":
            return default
        try:
            timeout = float(raw_value)
        except ValueError:
            logger.warning(f"Invalid {env_name} value: {raw_value!r}, using {default}")
            return default
        return timeout if timeout > 0 else default

    @staticmethod
    def _get_int_from_env(env_name: str, default: int) -> int:
        """Read a positive integer value from the environment."""
        raw_value = os.getenv(env_name)
        if raw_value is None or raw_value == "":
            return default
        try:
            value = int(raw_value)
        except ValueError:
            logger.warning(f"Invalid {env_name} value: {raw_value!r}, using {default}")
            return default
        return value if value > 0 else default

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.aclose()

    async def aclose(self):
        """Close the HTTP client."""
        await self.client.aclose()

    async def send_request(
        self,
        request: OpenAIRequest,
        endpoint: str = "/chat/completions",
        request_id: str = None,
    ) -> dict[str, Any]:
        """Send synchronous request to OpenAI API.

        Args:
            request: OpenAI request object
            endpoint: API endpoint path
            request_id: 请求ID用于日志追踪

        Returns:
            OpenAI API响应

        Raises:
            OpenAIClientError: 当API返回错误时
        """
        # 获取绑定了请求ID的logger
        from src.common.logging import get_logger_with_request_id

        bound_logger = get_logger_with_request_id(request_id)

        url = f"{self.base_url}{endpoint}"
        request_data = request.model_dump(exclude_none=True)
        request_log = self._build_request_log(
            "POST", url, endpoint, request_data, request_id=request_id
        )

        start_time = time.perf_counter()
        response_log: dict[str, Any] | None = None
        access_error: Exception | dict[str, Any] | None = None
        try:
            response = await self.client.post(
                url,
                json=request_data,
            )
            response_time = time.perf_counter() - start_time
            response_log = self._build_response_log(
                response, response_time, len(response.content)
            )
            response.raise_for_status()

            # 使用 response.text 让 httpx 自动处理编码和解压缩
            try:
                text = response.text
                result = json.loads(text)
                token_usage = summarize_token_usage(result.get("usage"), "openai_usage")
                if token_usage and response_log is not None:
                    response_log["tokens"] = token_usage

                bound_logger.debug(
                    "OpenAI响应概要 - "
                    f"{json.dumps(summarize_openai_response_payload(result), ensure_ascii=False, default=str, separators=(',', ':'))}"
                )

            except json.JSONDecodeError as e:
                content_type = response.headers.get("content-type", "unknown")
                access_error = {
                    "type": "json_decode_error",
                    "message": str(e),
                    "content_type": content_type,
                }
                bound_logger.exception(
                    f"OpenAI JSON解析失败 - Status: {response.status_code}, Content-Type: {content_type}, "
                    f"Content-Length: {response.headers.get('content-length')}, Size: {len(response.content)} bytes, Error: {str(e)}"
                )
                # 抛出包含更多上下文信息的异常
                raise json.JSONDecodeError(
                    f"Failed to parse OpenAI response (Status: {response.status_code}): {str(e)}",
                    response.text,
                    e.pos,
                ) from e
            return result
        except httpx.HTTPStatusError as e:
            # 安全读取响应内容（非流式模式）
            response_body = ""
            try:
                response_body = e.response.text
            except httpx.ResponseNotRead:
                # 如果响应未被读取，直接获取错误信息
                response_body = str(e)

            access_error = {
                "type": "http_status_error",
                "status_code": e.response.status_code,
            }

            raise OpenAIClientError(
                get_error_response(
                    status_code=e.response.status_code,
                    message=response_body,
                    details={"type": "http_error"},
                )
            ) from e

        except httpx.PoolTimeout as e:
            access_error = e
            bound_logger.error(
                f"OpenAI API connection pool timeout - Endpoint: {endpoint}, "
                f"MaxConnections: {self.max_connections}, MaxKeepalive: {self.max_keepalive_connections}"
            )
            raise OpenAIClientError(
                get_error_response(
                    status_code=503,
                    message=str(e),
                    details={
                        "type": "pool_timeout",
                        "max_connections": self.max_connections,
                        "max_keepalive_connections": self.max_keepalive_connections,
                    },
                )
            ) from e

        except httpx.TimeoutException as e:
            access_error = e
            bound_logger.error(
                f"OpenAI API request timeout - Endpoint: {endpoint}, Timeout: {self.timeout}s"
            )
            raise OpenAIClientError(
                get_error_response(
                    status_code=504,
                    message=str(e),
                    details={"type": "timeout_error", "original_error": str(e)},
                )
            ) from e

        except httpx.ConnectError as e:
            access_error = e
            bound_logger.error(
                f"OpenAI API connection error - Endpoint: {endpoint}, Error: {str(e)}"
            )
            raise OpenAIClientError(
                get_error_response(
                    status_code=502,
                    message=str(e),
                    details={"type": "connection_error", "original_error": str(e)},
                )
            ) from e
        finally:
            write_interface_log(
                CLIENT_INTERFACE_LOG,
                build_client_access_log(
                    request_log,
                    response_log,
                    time.perf_counter() - start_time,
                    request_id,
                    access_error,
                ),
                request_id,
            )

    async def send_streaming_request(
        self,
        request: OpenAIRequest,
        endpoint: str = "/chat/completions",
        request_id: str = None,
    ) -> AsyncGenerator[str, None]:
        """Send streaming request to OpenAI API.

        Args:
            request: OpenAI request object
            endpoint: API endpoint path
            request_id: 请求ID用于日志追踪

        Yields:
            原始的Server-Sent Events数据行

        Raises:
            OpenAIClientError: 当API返回错误时
        """
        # 获取绑定了请求ID的logger
        from src.common.logging import get_logger_with_request_id

        bound_logger = get_logger_with_request_id(request_id)

        url = f"{self.base_url}{endpoint}"

        # Ensure streaming is enabled
        request_dict = request.model_dump(exclude_none=True)
        request_dict["stream"] = True

        request_log = self._build_request_log(
            "POST", url, endpoint, request_dict, stream=True, request_id=request_id
        )

        start_time = time.perf_counter()
        stream_done = False
        bytes_received = 0
        raw_chunk_count = 0
        sse_line_count = 0
        response: httpx.Response | None = None
        response_log: dict[str, Any] | None = None
        access_error: Exception | dict[str, Any] | None = None
        token_usage: dict[str, Any] | None = None

        try:
            stream_timeout = httpx.Timeout(
                connect=self.timeout,
                read=self.stream_read_timeout,
                write=self.timeout,
                pool=self.timeout,
            )
            async with self.client.stream(
                "POST",
                url,
                json=request_dict,
                timeout=stream_timeout,
            ) as response:
                response_time = time.perf_counter() - start_time
                response_log = self._build_response_log(
                    response, response_time, None, stream=True
                )
                response.raise_for_status()

                decoder = codecs.getincrementaldecoder("utf-8")()
                buffer = ""

                async for chunk_bytes in response.aiter_bytes():
                    raw_chunk_count += 1
                    bytes_received += len(chunk_bytes)
                    buffer += decoder.decode(chunk_bytes)

                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue

                        sse_line_count += 1
                        stream_token_usage = self._extract_stream_token_usage(line)
                        if stream_token_usage:
                            token_usage = stream_token_usage
                        yield line
                        if line == "data: [DONE]":
                            stream_done = True
                            return

                buffer += decoder.decode(b"", final=True)
                if buffer.strip():
                    sse_line_count += 1
                    yield buffer.strip()
                stream_done = True

        except httpx.HTTPStatusError as e:
            error_body = ""
            try:
                # 尝试读取完整的错误响应体
                error_body = await e.response.aread()
                error_body = error_body.decode("utf-8", errors="ignore")
            except Exception as read_error:
                error_body = f"无法读取错误响应: {str(read_error)}"

            if response_log is None:
                response_log = self._build_response_log(
                    e.response,
                    time.perf_counter() - start_time,
                    None,
                    stream=True,
                )
            access_error = {
                "type": "http_status_error",
                "status_code": e.response.status_code,
            }
            raise OpenAIClientError(
                get_error_response(
                    status_code=e.response.status_code,
                    message=error_body or f"HTTP {e.response.status_code} error",
                    details={"type": "http_error"},
                )
            ) from e

        except httpx.PoolTimeout as e:
            access_error = e
            bound_logger.error(
                f"OpenAI API 连接池超时 - MaxConnections: {self.max_connections}, MaxKeepalive: {self.max_keepalive_connections}"
            )
            raise OpenAIClientError(
                get_error_response(
                    status_code=503,
                    message="Connection pool timeout",
                    details={
                        "type": "pool_timeout",
                        "max_connections": self.max_connections,
                        "max_keepalive_connections": self.max_keepalive_connections,
                    },
                )
            ) from e

        except httpx.TimeoutException as e:
            access_error = e
            bound_logger.error(f"OpenAI API 超时 - Error: {str(e)}")
            raise OpenAIClientError(
                get_error_response(
                    status_code=504,
                    message="Request timeout",
                    details={"type": "timeout_error"},
                )
            ) from e

        except httpx.ConnectError as e:
            access_error = e
            bound_logger.error(f"OpenAI API 连接错误 - Error: {str(e)}")
            raise OpenAIClientError(
                get_error_response(
                    status_code=502,
                    message="Connection error",
                    details={"type": "connection_error"},
                )
            ) from e

        except (httpx.ReadError, httpx.RemoteProtocolError) as e:
            access_error = e
            bound_logger.error(
                f"OpenAI API 流式连接中断 - Type: {type(e).__name__}, Error: {str(e)}"
            )
            raise OpenAIClientError(
                get_error_response(
                    status_code=502,
                    message="Stream interrupted by upstream OpenAI-compatible service",
                    details={
                        "type": "stream_interrupted",
                        "original_error": str(e),
                    },
                )
            ) from e
        finally:
            if response_log is not None:
                if token_usage:
                    response_log["tokens"] = token_usage
                response_log.update(
                    {
                        "bytes_received": bytes_received,
                        "raw_chunk_count": raw_chunk_count,
                        "sse_line_count": sse_line_count,
                        "done": stream_done,
                    }
                )
            write_interface_log(
                CLIENT_INTERFACE_LOG,
                build_client_access_log(
                    request_log,
                    response_log,
                    time.perf_counter() - start_time,
                    request_id,
                    access_error,
                ),
                request_id,
            )

    async def _parse_streaming_chunk(
        self, chunk_data: str, tool_calls_state: dict
    ) -> OpenAIStreamResponse | None:
        """解析流式响应chunk，优雅处理不完整的JSON数据。

        Args:
            chunk_data: JSON字符串的响应块
            tool_calls_state: 预留参数（未使用）

        Returns:
            解析后的响应对象，如果数据不完整则返回None
        """
        import json

        try:
            # 尝试解析JSON数据
            raw_data = json.loads(chunk_data)

            result = OpenAIStreamResponse.model_validate(raw_data)
            return result

        except json.JSONDecodeError as e:
            # JSON解析失败，通常是因为数据被分割，静默跳过
            logger.debug(
                f"Skipping incomplete JSON chunk - Error: {str(e)}, Data: {chunk_data[:100]}"
            )
            return None
        except Exception as e:
            # Pydantic验证失败，可能是tool_calls的增量数据不完整
            logger.debug(
                f"Skipping chunk due to validation error - Error: {str(e)}, Data: {chunk_data[:100]}"
            )
            return None

    async def health_check(self) -> dict[str, bool]:
        """Check OpenAI API availability.

        Returns:
            健康检查结果
        """
        try:
            url = f"{self.base_url}/models"
            response = await self.client.get(url)

            return {
                "openai_service": response.status_code == 200,
                "api_accessible": True,
                "last_check": True,
            }

        except Exception as e:
            logger.exception(f"OpenAI health check failed - Error: {str(e)}")
            return {
                "openai_service": False,
                "api_accessible": False,
                "last_check": True,
            }

    def _build_request_log(
        self,
        method: str,
        url: str,
        endpoint: str,
        request_data: dict[str, Any],
        stream: bool = False,
        request_id: str = None,
    ) -> dict[str, Any]:
        request_log = {
            "direction": "outbound",
            "method": method,
            "url": sanitize_url(url),
            "endpoint": endpoint,
            "stream": stream,
            "headers": sanitize_log_mapping(self.client.headers),
            "timeout": summarize_timeout(self.timeout),
            "stream_read_timeout": self.stream_read_timeout,
            "connection_pool": {
                "max_connections": self.max_connections,
                "max_keepalive_connections": self.max_keepalive_connections,
            },
            "payload": summarize_openai_request_payload(request_data),
        }
        cached_tokens = get_cached_tokens(request_id) if request_id else None
        if cached_tokens:
            request_log["tokens"] = {
                "source": "estimated_request",
                "input_tokens": cached_tokens,
            }
        return request_log

    def _build_response_log(
        self,
        response: httpx.Response,
        response_time_seconds: float,
        body_size_bytes: int | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        return {
            "direction": "outbound",
            "method": response.request.method,
            "url": sanitize_url(response.url),
            "stream": stream,
            "status_code": response.status_code,
            "reason_phrase": response.reason_phrase,
            "http_version": response.http_version,
            "elapsed_ms": round(response_time_seconds * 1000, 2),
            "headers": sanitize_log_mapping(response.headers),
            "content_type": response.headers.get("content-type"),
            "content_length": response.headers.get("content-length"),
            "body_size_bytes": body_size_bytes,
            "redirect_count": len(response.history),
            "redirect_history": [
                {
                    "status_code": item.status_code,
                    "url": sanitize_url(item.url),
                    "headers": sanitize_log_mapping(item.headers),
                }
                for item in response.history
            ],
        }

    @staticmethod
    def _extract_stream_token_usage(line: str) -> dict[str, Any] | None:
        data = line.removeprefix("data:").strip() if line.startswith("data:") else line
        if not data or data == "[DONE]":
            return None

        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return None

        if not isinstance(payload, dict):
            return None

        usage = payload.get("usage")
        if usage is None:
            choices = payload.get("choices")
            if isinstance(choices, list) and choices:
                first_choice = choices[0]
                if isinstance(first_choice, dict):
                    usage = first_choice.get("usage")

        return summarize_token_usage(usage, "openai_usage")
