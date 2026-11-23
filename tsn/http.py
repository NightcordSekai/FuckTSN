"""SDK 共用的 HTTP 核心。

职责边界：

* **tsn_http**（本模块）：URL 拼接、公共头部、发请求、调用 `tsn_protocol`
  分类报文、按分类做「协议级重试」（仅 `SERVER_DEGRADED`）。
* **resilient_client**：只负责「传输级重试」（连接重置/超时），不再解析响应体。
* **SDK 类**：只负责各自的签名算法与业务字段。

这样划分后，"这个报文到底代表什么、要不要重试" 只有一个答案，
不会再出现 base/public/private 三套 HTTP 封装给出三种结论的情况。
"""
from __future__ import annotations

import asyncio
import os

import httpx
from loguru import logger

from tsn import protocol
from tsn import routes

DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=20.0)
_IDEMPOTENT_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


class TsnHttpCore:
    """一个 SDK 实例对应一个 core；不持有业务状态。"""

    def __init__(
        self,
        *,
        base_url: str = '',
        headers: dict | None = None,
        client: httpx.AsyncClient | None = None,
        token: str = '',
        protocol_retries: int | None = None,
    ):
        self.base_url = base_url or ''
        self.headers = dict(headers or {})
        self.token = token
        self.client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self.protocol_retries = _env_int('TSN_PROTOCOL_RETRIES', 5) if protocol_retries is None else protocol_retries

    # ---- 配置 -------------------------------------------------------
    def set_base_url(self, base_url: str) -> None:
        self.base_url = routes.normalize_base_url(base_url)

    def set_token(self, token: str) -> None:
        self.token = token

    def build_url(self, path: str) -> str:
        return routes.join_url(self.base_url, path)

    # ---- 请求 -------------------------------------------------------
    async def send(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        headers: dict | None = None,
        timeout: float | httpx.Timeout | None = None,
        context: str = '',
        allow_protocol_retry: bool | None = None,
    ) -> protocol.TsnResult:
        method = method.upper()
        full_url = self.build_url(url)
        merged = {**self.headers, **(headers or {})}
        if files:
            # multipart 请求必须让 httpx 自己写 Content-Type: multipart/form-data; boundary=...
            # 客户端默认头里的 application/x-www-form-urlencoded 会把整段 body 当表单解析，
            # 文件字节被 URLDecoder 处理 → 500 Illegal hex characters in escape (%) pattern。
            merged = {k: v for k, v in merged.items() if k.lower() != 'content-type'}
        if timeout is None:
            timeout = DEFAULT_TIMEOUT
        if allow_protocol_retry is None:
            allow_protocol_retry = method in _IDEMPOTENT_METHODS or method == 'POST'
        attempts = 1 + (self.protocol_retries if allow_protocol_retry else 0)

        result: protocol.TsnResult | None = None
        for attempt in range(attempts):
            try:
                resp = await self.client.request(
                    method,
                    full_url,
                    params=params,
                    data=data,
                    files=files,
                    headers=merged,
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                result = protocol.classify_exception(exc, context=context)
                if result.retryable and attempt + 1 < attempts:
                    await self._sleep(attempt)
                    continue
                return result
            except Exception as exc:  # 非 httpx 异常（编码/参数错误）不重试
                return protocol.classify_exception(exc, context=context)

            result = protocol.classify_response(resp, context=context)
            if not result.retryable or attempt + 1 >= attempts:
                break
            logger.warning(
                '服务端降级，重试 {}/{}：{}（{}）',
                attempt + 1,
                attempts,
                result.describe(),
                context or full_url,
            )
            await self._sleep(attempt)
        assert result is not None
        return result

    @staticmethod
    async def _sleep(attempt: int) -> None:
        await asyncio.sleep(min(0.5 * (2 ** attempt), 8.0))

    async def aclose(self) -> None:
        try:
            await self.client.aclose()
        except Exception:
            pass
