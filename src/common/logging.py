"""Loguru日志配置"""

import json
import sys
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from loguru import logger

MAX_LOG_VALUE_LENGTH = 1000
MAX_LOG_LIST_ITEMS = 50
SERVER_INTERFACE_LOG = "server"
CLIENT_INTERFACE_LOG = "client"

SENSITIVE_FIELD_NAMES = {
    "authorization",
    "proxy-authorization",
    "api-key",
    "apikey",
    "x-api-key",
    "key",
    "access-key",
    "secret-key",
    "openai-api-key",
    "cookie",
    "set-cookie",
    "password",
}


def configure_logging(log_config) -> None:
    """配置Loguru日志系统

    Args:
        log_config: 日志配置对象
    """
    # 移除默认的handler
    logger.remove()

    # 使用相对路径而不是绝对路径
    log_path = _prepare_log_file(Path("logs/app.log"))
    server_interface_log_path = _prepare_log_file(Path("logs/server_interface.jsonl"))
    client_interface_log_path = _prepare_log_file(Path("logs/client_interface.jsonl"))

    # 控制台日志格式（包含请求ID）
    console_format = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{extra[request_id]}</cyan> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"

    # 配置控制台日志
    logger.add(
        sys.stdout,
        format=console_format,
        level=log_config.level,
        colorize=True,
        filter=_default_log_filter,
    )

    # 配置文件日志（包含截取的异常堆栈）
    logger.add(
        str(log_path),
        level=log_config.level,
        rotation="10 MB",
        retention="1 day",
        encoding="utf-8",
        enqueue=True,  # 异步写入
        filter=_default_log_filter,
    )

    # 服务端接口日志：一行一个JSON对象，只写请求/响应元数据
    logger.add(
        str(server_interface_log_path),
        format="{message}",
        level="INFO",
        rotation="100 MB",
        retention="7 days",
        encoding="utf-8",
        enqueue=True,
        filter=_interface_log_filter(SERVER_INTERFACE_LOG),
    )

    # 客户端接口日志：一行一个JSON对象，只写请求/响应元数据
    logger.add(
        str(client_interface_log_path),
        format="{message}",
        level="INFO",
        rotation="100 MB",
        retention="7 days",
        encoding="utf-8",
        enqueue=True,
        filter=_interface_log_filter(CLIENT_INTERFACE_LOG),
    )

    # 配置全局异常处理
    def exception_handler(exc_type, exc_value, exc_traceback):
        """全局异常处理器"""
        if issubclass(exc_type, KeyboardInterrupt):
            # 允许KeyboardInterrupt正常退出
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        logger.opt(exception=(exc_type, exc_value, exc_traceback)).critical(
            "未捕获的异常"
        )

    # 设置全局异常处理器
    sys.excepthook = exception_handler


class RequestLogger:
    """请求日志处理器"""

    async def log_response(
        self, status_code: int, response_time: float, request_id: str = None
    ):
        """记录响应结束"""
        bound_logger = get_logger_with_request_id(request_id)

        response_time_ms = round(response_time * 1000, 2)
        bound_logger.info(
            f"请求完成 - Status: {status_code}, Time: {response_time_ms}ms"
        )

    async def log_error(
        self, error: Exception, context: dict = None, request_id: str = None
    ):
        """记录错误情况"""
        bound_logger = get_logger_with_request_id(request_id)

        error_type = type(error).__name__
        error_message = str(error)
        context_str = f", Context: {context}" if context else ""

        # 使用loguru的exception方法记录完整的堆栈跟踪
        bound_logger.exception(
            f"请求处理错误 - Type: {error_type}, Message: {error_message}{context_str}"
        )


# 全局logger实例
request_logger = RequestLogger()


async def generate_request_id() -> str:
    """生成唯一的请求ID

    Returns:
        str: 格式为 req_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx 的请求ID
    """
    return f"req_{uuid.uuid4().hex}"


async def should_enable_request_id() -> bool:
    """检查是否应该启用请求ID（始终启用）

    Returns:
        bool: 始终返回True，请求ID功能默认启用
    """
    return True


async def get_request_id_header_name() -> str:
    """获取请求ID响应头名称

    Returns:
        str: 固定返回 "X-Request-ID"
    """
    return "X-Request-ID"


def get_request_id_from_request(request) -> str | None:
    """从请求对象中安全地获取请求ID

    Args:
        request: FastAPI Request对象

    Returns:
        str | None: 请求ID，如果不存在则返回None
    """
    try:
        return getattr(request.state, "request_id", None)
    except AttributeError:
        return None


async def log_exception(message: str = "发生异常", **kwargs):
    """记录异常的便捷函数

    使用示例:
        try:
            # 一些可能出错的代码
            pass
        except Exception as e:
            log_exception("处理请求时发生错误", request_id="123", user_id="456")

    Args:
        message: 异常描述信息
        **kwargs: 额外的上下文信息
    """
    kwargs_str = ", ".join([f"{k}: {v}" for k, v in kwargs.items()]) if kwargs else ""
    full_message = f"{message} - {kwargs_str}" if kwargs_str else message
    logger.exception(full_message)


def get_logger_with_request_id(request_id: str = None):
    """获取绑定了请求ID的日志器实例

    Args:
        request_id: 请求ID，如果为None则使用默认值

    Returns:
        绑定了请求ID的logger实例
    """
    if request_id:
        return logger.bind(request_id=request_id)
    else:
        return logger.bind(request_id="---")


def format_log_fields(fields: Mapping[str, Any]) -> str:
    """将接口日志字段格式化为单行JSON，便于检索和解析。"""
    return json.dumps(
        fields,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )


def write_interface_log(
    interface_log: str,
    fields: Mapping[str, Any],
    request_id: str | None = None,
) -> None:
    """写入接口JSONL日志，不进入普通应用日志。"""
    logger.bind(
        interface_log=interface_log,
        request_id=request_id or fields.get("request_id") or "---",
    ).info(format_log_fields(fields))


def sanitize_log_mapping(mapping: Any) -> dict[str, Any]:
    """脱敏并标准化 headers/query params 等键值结构。"""
    sanitized: dict[str, Any] = {}
    for key, value in _iter_mapping_items(mapping):
        key_text = str(key)
        safe_value = (
            _mask_sensitive_value(value)
            if _is_sensitive_key(key_text)
            else _truncate_for_log(value)
        )
        if key_text in sanitized:
            existing = sanitized[key_text]
            if not isinstance(existing, list):
                sanitized[key_text] = [existing]
            sanitized[key_text].append(safe_value)
        else:
            sanitized[key_text] = safe_value
    return sanitized


def sanitize_url(url: Any) -> str:
    """脱敏URL中的敏感query参数。"""
    url_text = str(url)
    try:
        parsed = urlsplit(url_text)
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        sanitized_query = urlencode(
            [
                (
                    key,
                    _mask_sensitive_value(value)
                    if _is_sensitive_key(key)
                    else _truncate_for_log(value),
                )
                for key, value in query_items
            ],
            doseq=True,
        )
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                sanitized_query,
                parsed.fragment,
            )
        )
    except Exception:
        return _truncate_for_log(url_text)


def summarize_timeout(timeout: Any) -> Any:
    """汇总 httpx Timeout 或普通 timeout 值。"""
    if timeout is None or isinstance(timeout, int | float | str):
        return timeout

    timeout_fields = {}
    for attr in ("connect", "read", "write", "pool"):
        value = getattr(timeout, attr, None)
        if value is not None:
            timeout_fields[attr] = value
    return timeout_fields or str(timeout)


def build_server_request_log(request: Any) -> dict[str, Any]:
    """构造服务端入站请求接口日志，不读取请求体。"""
    client = getattr(request, "client", None)
    scope = getattr(request, "scope", {}) or {}
    server = scope.get("server")
    headers = getattr(request, "headers", {})
    url = getattr(request, "url", "")

    return {
        "direction": "inbound",
        "method": getattr(request, "method", "unknown"),
        "url": sanitize_url(url),
        "path": getattr(url, "path", scope.get("path")),
        "query_params": sanitize_log_mapping(getattr(request, "query_params", {})),
        "client": {
            "host": getattr(client, "host", None),
            "port": getattr(client, "port", None),
        },
        "server": _format_address(server),
        "scheme": getattr(url, "scheme", scope.get("scheme")),
        "http_version": scope.get("http_version"),
        "headers": sanitize_log_mapping(headers),
        "content_type": _get_mapping_value(headers, "content-type"),
        "content_length": _get_mapping_value(headers, "content-length"),
    }


def build_server_access_log(
    request: Any,
    response: Any,
    response_time_seconds: float,
    request_id: str | None = None,
) -> dict[str, Any]:
    """构造单行服务端接口日志，不读取请求/响应体。"""
    request_info = build_server_request_log(request)
    response_info = build_server_response_log(request, response, response_time_seconds)

    request_info.pop("direction", None)
    response_info.pop("direction", None)
    response_info.pop("method", None)
    response_info.pop("url", None)
    response_info.pop("path", None)

    access_log = {
        "timestamp": _utc_now_iso(),
        "request_id": request_id,
        "direction": "inbound",
        "request": request_info,
        "response": response_info,
    }
    tokens = getattr(getattr(request, "state", None), "interface_log_tokens", None)
    if tokens:
        access_log["tokens"] = tokens
    return access_log


def build_server_response_log(
    request: Any,
    response: Any,
    response_time_seconds: float,
) -> dict[str, Any]:
    """构造服务端出站响应接口日志，不读取响应体。"""
    headers = getattr(response, "headers", {})
    url = getattr(request, "url", "")
    media_type = getattr(response, "media_type", None)

    return {
        "direction": "inbound",
        "method": getattr(request, "method", "unknown"),
        "url": sanitize_url(url),
        "path": getattr(url, "path", None),
        "status_code": getattr(response, "status_code", None),
        "elapsed_ms": round(response_time_seconds * 1000, 2),
        "headers": sanitize_log_mapping(headers),
        "content_type": _get_mapping_value(headers, "content-type") or media_type,
        "content_length": _get_mapping_value(headers, "content-length"),
        "streaming": _is_streaming_response(response),
    }


def build_server_error_access_log(
    request: Any,
    response: Any,
    exc: Exception,
    response_time_seconds: float,
    request_id: str | None = None,
) -> dict[str, Any]:
    """构造单行服务端异常接口日志，不读取请求/响应体。"""
    access_log = build_server_access_log(
        request,
        response,
        response_time_seconds,
        request_id,
    )
    access_log["error"] = {
        "type": type(exc).__name__,
        "message": str(exc),
    }
    return access_log


def build_server_error_log(
    request: Any,
    exc: Exception,
    response_time_seconds: float,
) -> dict[str, Any]:
    """构造服务端异常接口日志，不读取请求/响应体。"""
    url = getattr(request, "url", "")
    return {
        "direction": "inbound",
        "method": getattr(request, "method", "unknown"),
        "url": sanitize_url(url),
        "path": getattr(url, "path", None),
        "status_code": 500,
        "elapsed_ms": round(response_time_seconds * 1000, 2),
        "error_type": type(exc).__name__,
        "error_message": str(exc),
    }


def build_client_access_log(
    request_info: Mapping[str, Any],
    response_info: Mapping[str, Any] | None,
    response_time_seconds: float,
    request_id: str | None = None,
    error: Exception | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造单行客户端接口日志，不读取请求/响应体。"""
    request_data = dict(request_info)
    response_data = dict(response_info or {})
    request_data.pop("direction", None)
    response_data.pop("direction", None)
    tokens = _combine_token_usage(
        request_data.pop("tokens", None),
        response_data.pop("tokens", None),
    )
    response_data["elapsed_ms"] = round(response_time_seconds * 1000, 2)

    access_log = {
        "timestamp": _utc_now_iso(),
        "request_id": request_id,
        "direction": "outbound",
        "request": request_data,
        "response": response_data,
    }
    if tokens:
        access_log["tokens"] = tokens

    if error is not None:
        if isinstance(error, Mapping):
            access_log["error"] = dict(error)
        else:
            access_log["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }

    return access_log


def summarize_token_usage(
    usage: Any,
    source: str,
) -> dict[str, Any] | None:
    """标准化 token 使用量字段。"""
    if usage is None:
        return None

    if hasattr(usage, "model_dump"):
        usage_data = usage.model_dump(exclude_none=True)
    elif isinstance(usage, Mapping):
        usage_data = dict(usage)
    else:
        return None

    input_tokens = usage_data.get("input_tokens", usage_data.get("prompt_tokens"))
    output_tokens = usage_data.get("output_tokens", usage_data.get("completion_tokens"))
    total_tokens = usage_data.get("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    token_usage = {
        "source": source,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }

    for key in (
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "service_tier",
    ):
        if key in usage_data:
            token_usage[key] = usage_data[key]

    return {key: value for key, value in token_usage.items() if value is not None}


def summarize_anthropic_request_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """汇总 Anthropic 请求体元数据，不包含消息正文或工具schema。"""
    messages = _as_list(payload.get("messages"))
    tools = _as_list(payload.get("tools"))
    system = payload.get("system")
    metadata = payload.get("metadata")

    role_counts, content_type_counts = _summarize_message_metadata(messages)
    return {
        "model": payload.get("model"),
        "stream": payload.get("stream", False),
        "max_tokens": payload.get("max_tokens"),
        "temperature": payload.get("temperature"),
        "top_p": payload.get("top_p"),
        "top_k": payload.get("top_k"),
        "message_count": len(messages),
        "message_role_counts": role_counts,
        "message_content_type_counts": content_type_counts,
        "system": _summarize_system_metadata(system),
        "tool_count": len(tools),
        "tool_names": _limited_list(_extract_tool_names(tools)),
        "tool_choice": _summarize_value_shape(payload.get("tool_choice")),
        "metadata_keys": sorted(metadata.keys()) if isinstance(metadata, dict) else [],
        "stop_sequences_count": len(_as_list(payload.get("stop_sequences"))),
        "thinking": _summarize_value_shape(payload.get("thinking")),
    }


def summarize_openai_request_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """汇总 OpenAI 请求体元数据，不包含消息正文或工具schema。"""
    messages = _as_list(payload.get("messages"))
    tools = _as_list(payload.get("tools"))
    role_counts, content_type_counts = _summarize_message_metadata(messages)

    return {
        "model": payload.get("model"),
        "stream": payload.get("stream", False),
        "max_tokens": payload.get("max_tokens"),
        "max_completion_tokens": payload.get("max_completion_tokens"),
        "temperature": payload.get("temperature"),
        "top_p": payload.get("top_p"),
        "top_k": payload.get("top_k"),
        "message_count": len(messages),
        "message_role_counts": role_counts,
        "message_content_type_counts": content_type_counts,
        "tool_count": len(tools),
        "tool_names": _limited_list(_extract_tool_names(tools)),
        "tool_choice": _summarize_value_shape(payload.get("tool_choice")),
        "parallel_tool_calls": payload.get("parallel_tool_calls"),
        "stop_count": len(_as_list(payload.get("stop"))),
        "response_format": _summarize_value_shape(payload.get("response_format")),
        "stream_options_keys": sorted(payload.get("stream_options", {}).keys())
        if isinstance(payload.get("stream_options"), dict)
        else [],
        "n": payload.get("n"),
        "seed": payload.get("seed"),
        "think": payload.get("think"),
        "has_user": payload.get("user") is not None,
    }


def summarize_openai_response_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """汇总 OpenAI 响应体元数据，不包含响应正文。"""
    choices = _as_list(payload.get("choices"))
    finish_reasons = []
    choice_indices = []
    message_roles = []
    delta_field_counts: dict[str, int] = {}

    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        if "finish_reason" in choice:
            finish_reasons.append(choice.get("finish_reason"))
        if "index" in choice:
            choice_indices.append(choice.get("index"))

        message = choice.get("message")
        if isinstance(message, Mapping) and message.get("role"):
            message_roles.append(message.get("role"))

        delta = choice.get("delta")
        if isinstance(delta, Mapping):
            for key in delta:
                if key in {"content", "reasoning_content"}:
                    delta_field_counts[f"has_{key}"] = (
                        delta_field_counts.get(f"has_{key}", 0) + 1
                    )
                else:
                    delta_field_counts[key] = delta_field_counts.get(key, 0) + 1

    return {
        "id": payload.get("id"),
        "object": payload.get("object"),
        "created": payload.get("created"),
        "model": payload.get("model"),
        "choice_count": len(choices),
        "choice_indices": _limited_list(choice_indices),
        "finish_reasons": _limited_list(finish_reasons),
        "message_roles": _limited_list(message_roles),
        "delta_field_counts": delta_field_counts,
        "usage": payload.get("usage"),
        "system_fingerprint_present": payload.get("system_fingerprint") is not None,
    }


def summarize_anthropic_response_payload(response: Any) -> dict[str, Any]:
    """汇总 Anthropic 响应对象元数据，不包含响应正文。"""
    payload = (
        response.model_dump(exclude_none=True)
        if hasattr(response, "model_dump")
        else response
    )
    if not isinstance(payload, Mapping):
        return {"response_type": type(response).__name__}

    content_blocks = _as_list(payload.get("content"))
    content_type_counts: dict[str, int] = {}
    for block in content_blocks:
        block_type = (
            block.get("type") if isinstance(block, Mapping) else type(block).__name__
        )
        content_type_counts[str(block_type)] = (
            content_type_counts.get(str(block_type), 0) + 1
        )

    return {
        "id": payload.get("id"),
        "type": payload.get("type"),
        "role": payload.get("role"),
        "model": payload.get("model"),
        "stop_reason": payload.get("stop_reason"),
        "stop_sequence_present": payload.get("stop_sequence") is not None,
        "content_block_count": len(content_blocks),
        "content_type_counts": content_type_counts,
        "usage": payload.get("usage"),
    }


def summarize_sse_event(event_text: str) -> dict[str, Any]:
    """汇总 SSE 事件元数据，不输出 data 正文。"""
    event_type = None
    data_parts = []
    line_count = 0

    for line in event_text.splitlines():
        line_count += 1
        if line.startswith("event:"):
            event_type = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_parts.append(line.removeprefix("data:").strip())

    stripped_event = event_text.strip()
    if not data_parts and (
        stripped_event.startswith("{") or stripped_event == "[DONE]"
    ):
        data_parts.append(stripped_event)

    data_text = "\n".join(data_parts)
    summary = {
        "event": event_type or "message",
        "line_count": line_count,
        "data_size_chars": len(data_text),
    }

    if data_text == "[DONE]":
        summary["done"] = True
        return summary

    if not data_text:
        return summary

    try:
        payload = json.loads(data_text)
    except json.JSONDecodeError:
        summary["data_json"] = False
        return summary

    summary["data_json"] = True
    if isinstance(payload, Mapping):
        summary.update(_summarize_sse_payload(payload))
    else:
        summary["data_type"] = type(payload).__name__
    return summary


def _iter_mapping_items(mapping: Any) -> Iterable[tuple[Any, Any]]:
    if mapping is None:
        return []
    if hasattr(mapping, "multi_items"):
        return mapping.multi_items()
    if hasattr(mapping, "items"):
        return mapping.items()
    return mapping


def _prepare_log_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    if path.exists():
        path.chmod(0o644)
    return path


def _prepare_log_record(record: dict[str, Any]) -> None:
    record["extra"].setdefault("request_id", "---")


def _default_log_filter(record: dict[str, Any]) -> bool:
    _prepare_log_record(record)
    return record["extra"].get("interface_log") is None


def _interface_log_filter(interface_log: str):
    def _filter(record: dict[str, Any]) -> bool:
        _prepare_log_record(record)
        return record["extra"].get("interface_log") == interface_log

    return _filter


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _get_mapping_value(mapping: Any, key: str) -> Any:
    try:
        return mapping.get(key)
    except AttributeError:
        return None


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("_", "-")
    return (
        normalized in SENSITIVE_FIELD_NAMES
        or "token" in normalized
        or "secret" in normalized
        or "password" in normalized
        or "credential" in normalized
        or "authorization" in normalized
        or "cookie" in normalized
        or normalized.endswith("-key")
    )


def _mask_sensitive_value(value: Any) -> str:
    text = str(value)
    if not text:
        return ""

    parts = text.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() in {"bearer", "basic"}:
        return f"{parts[0]} {_mask_secret(parts[1])}"
    return _mask_secret(text)


def _mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}(masked)"


def _truncate_for_log(value: Any, max_length: int = MAX_LOG_VALUE_LENGTH) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): (
                _mask_sensitive_value(item)
                if _is_sensitive_key(str(key))
                else _truncate_for_log(item, max_length)
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set):
        values = list(value)
        truncated_values = [
            _truncate_for_log(item, max_length) for item in values[:MAX_LOG_LIST_ITEMS]
        ]
        if len(values) > MAX_LOG_LIST_ITEMS:
            truncated_values.append(
                f"...<truncated {len(values) - MAX_LOG_LIST_ITEMS} items>"
            )
        return truncated_values

    text = str(value)
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}...<truncated {len(text) - max_length} chars>"


def _format_address(address: Any) -> Any:
    if isinstance(address, list | tuple) and len(address) >= 2:
        return {"host": address[0], "port": address[1]}
    return address


def _combine_token_usage(*usage_items: Any) -> dict[str, Any] | None:
    combined: dict[str, Any] = {}
    sources = []
    for usage in usage_items:
        if not usage:
            continue
        if hasattr(usage, "model_dump"):
            usage_data = usage.model_dump(exclude_none=True)
        elif isinstance(usage, Mapping):
            usage_data = dict(usage)
        else:
            continue

        source = usage_data.pop("source", None)
        if source:
            sources.append(source)
        combined.update(
            {key: value for key, value in usage_data.items() if value is not None}
        )

    if not combined:
        return None
    if sources:
        combined["source"] = sources[-1] if len(set(sources)) == 1 else sources
    return combined


def _is_streaming_response(response: Any) -> bool:
    media_type = getattr(response, "media_type", None)
    if media_type == "text/event-stream":
        return True
    headers = getattr(response, "headers", {})
    content_type = _get_mapping_value(headers, "content-type")
    return isinstance(content_type, str) and "text/event-stream" in content_type


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _limited_list(values: Iterable[Any]) -> list[Any]:
    values_list = list(values)
    result = values_list[:MAX_LOG_LIST_ITEMS]
    if len(values_list) > MAX_LOG_LIST_ITEMS:
        result.append(f"...<truncated {len(values_list) - MAX_LOG_LIST_ITEMS} items>")
    return result


def _summarize_message_metadata(
    messages: list[Any],
) -> tuple[dict[str, int], dict[str, int]]:
    role_counts: dict[str, int] = {}
    content_type_counts: dict[str, int] = {}

    for message in messages:
        if not isinstance(message, Mapping):
            role = type(message).__name__
            content = None
        else:
            role = str(message.get("role", "unknown"))
            content = message.get("content")

        role_counts[role] = role_counts.get(role, 0) + 1
        for content_type in _iter_content_types(content):
            content_type_counts[content_type] = (
                content_type_counts.get(content_type, 0) + 1
            )

    return role_counts, content_type_counts


def _iter_content_types(content: Any) -> Iterable[str]:
    if isinstance(content, str):
        return ["text"]
    if content is None:
        return ["null"]
    if isinstance(content, list):
        content_types = []
        for item in content:
            if isinstance(item, Mapping):
                content_types.append(str(item.get("type", "unknown")))
            else:
                content_types.append(type(item).__name__)
        return content_types
    return [type(content).__name__]


def _extract_tool_names(tools: list[Any]) -> list[str]:
    names = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        if tool.get("name"):
            names.append(str(tool["name"]))
            continue
        function = tool.get("function")
        if isinstance(function, Mapping) and function.get("name"):
            names.append(str(function["name"]))
    return names


def _summarize_system_metadata(system: Any) -> dict[str, Any]:
    if system is None:
        return {"present": False}
    if isinstance(system, str):
        return {"present": True, "type": "text", "length": len(system)}
    if isinstance(system, list):
        item_types = []
        for item in system:
            item_types.append(
                item.get("type", "unknown")
                if isinstance(item, Mapping)
                else type(item).__name__
            )
        return {
            "present": True,
            "type": "list",
            "item_count": len(system),
            "item_types": _limited_list(item_types),
        }
    return {"present": True, "type": type(system).__name__}


def _summarize_value_shape(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {"type": "object", "keys": sorted(value.keys())}
    if isinstance(value, list):
        return {"type": "list", "count": len(value)}
    return value


def _summarize_sse_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "data_keys": sorted(str(key) for key in payload.keys()),
    }

    for key in ("id", "object", "created", "model", "type", "role", "index"):
        if key in payload:
            summary[key] = payload.get(key)

    if "usage" in payload:
        summary["usage"] = payload.get("usage")

    choices = payload.get("choices")
    if isinstance(choices, list):
        finish_reasons = []
        delta_field_counts: dict[str, int] = {}
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            finish_reasons.append(choice.get("finish_reason"))
            delta = choice.get("delta")
            if isinstance(delta, Mapping):
                for key in delta:
                    if key in {"content", "reasoning_content"}:
                        delta_field_counts[f"has_{key}"] = (
                            delta_field_counts.get(f"has_{key}", 0) + 1
                        )
                    else:
                        delta_field_counts[str(key)] = (
                            delta_field_counts.get(str(key), 0) + 1
                        )
        summary["choice_count"] = len(choices)
        summary["finish_reasons"] = _limited_list(finish_reasons)
        summary["delta_field_counts"] = delta_field_counts

    content_block = payload.get("content_block")
    if isinstance(content_block, Mapping):
        summary["content_block_type"] = content_block.get("type")

    delta = payload.get("delta")
    if isinstance(delta, Mapping):
        summary["delta_type"] = delta.get("type")
        summary["delta_keys"] = sorted(str(key) for key in delta.keys())
        summary["has_text_delta"] = delta.get("text") is not None
        summary["has_thinking_delta"] = delta.get("thinking") is not None
        summary["has_partial_json_delta"] = delta.get("partial_json") is not None

    message = payload.get("message")
    if isinstance(message, Mapping):
        summary["message"] = {
            "id": message.get("id"),
            "type": message.get("type"),
            "role": message.get("role"),
            "model": message.get("model"),
            "content_block_count": len(_as_list(message.get("content"))),
            "stop_reason": message.get("stop_reason"),
            "usage": message.get("usage"),
        }

    return summary
