"""Request-owned responses over shared HTTP clients, using public SDK APIs."""

import asyncio
import copy
import hashlib
import json
import logging
import os
import ssl
import time
from http.cookiejar import CookieJar, DefaultCookiePolicy
from weakref import WeakKeyDictionary

import anyio
import httpx
import litellm
from openai import AsyncOpenAI
from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, get_default_headers, get_ssl_configuration

from litellm_guardrails.metrics import UPSTREAM_STREAMS, UPSTREAM_STREAM_CLEANUP_ERRORS
from litellm_guardrails.anthropic_restoration import is_messages_route, messages_chat_response
from litellm_guardrails.sse_integrity import IncompleteUpstreamStream, verified_response
from litellm_guardrails.stream_cleanup import finish_stream_cleanup

TRANSPORT_ID = "ru_upstream_request_id"
MAX_HTTP_POOLS = 8
MAX_HTTP_CONNECTIONS = 512
MAX_KEEPALIVE_CONNECTIONS = 64
IDLE_POOL_SECONDS = 60
_MANAGERS = WeakKeyDictionary()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


def transport_request_id(data):
    metadata = data.get("litellm_metadata")
    value = metadata.get(TRANSPORT_ID) if isinstance(metadata, dict) else None
    return value if isinstance(value, str) else None


def _log(event, *, level=logging.INFO, **fields):
    logger.log(level, json.dumps({"event": event, **fields}, sort_keys=True))


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


def _ssl_settings(kwargs):
    verify = get_ssl_configuration(kwargs.get("ssl_verify"))
    cert = os.getenv("SSL_CERTIFICATE") or getattr(litellm, "ssl_certificate", None)
    environment = {name: os.getenv(name) for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "SSL_SECURITY_LEVEL", "SSL_ECDH_CURVE")}
    key = hashlib.sha256(json.dumps([id(verify) if isinstance(verify, ssl.SSLContext) else verify,
                                    cert, environment], sort_keys=True).encode()).hexdigest()
    return key, verify, cert


def _unused_transport(request):
    raise RuntimeError("Request facade must use its shared upstream pool")


class _Pool:
    def __init__(self, verify, cert, transport=None):
        self.verify = verify
        self.client = httpx.AsyncClient(verify=verify, cert=cert, transport=transport,
            limits=httpx.Limits(max_connections=MAX_HTTP_CONNECTIONS, max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS),
            cookies=CookieJar(policy=_NoCookies()), follow_redirects=True)
        self.borrowers = 0
        self.idle_since = time.monotonic()


class RequestHTTPClient(httpx.AsyncClient):
    def __init__(self, pool, api, choices, key, timeout, messages_route=False):
        super().__init__(verify=pool.verify, trust_env=False, timeout=timeout,
                         transport=httpx.MockTransport(_unused_transport),
                         cookies=CookieJar(policy=_NoCookies()), follow_redirects=True)
        self.headers["Accept-Encoding"] = "gzip, deflate"
        self.pool, self.api, self.choices, self.key = pool, api, choices, key
        self.responses = []
        self.messages_route = messages_route
        self.released = False
        self.has_stream = False
        self.stream_failure = None
        self.close_lock = asyncio.Lock()
        pool.borrowers += 1

    def observe(self, outcome, reason=None):
        if outcome == "incomplete":
            self.stream_failure = reason
        UPSTREAM_STREAMS.labels(api=self.api, outcome=outcome).inc()
        _log("upstream_stream_result", api=self.api, outcome=outcome, reason=reason, request_id=self.key)

    async def send(self, request, **kwargs):
        if self.is_closed:
            raise RuntimeError("Upstream request client is closed")
        if kwargs.get("auth", httpx.USE_CLIENT_DEFAULT) is httpx.USE_CLIENT_DEFAULT:
            kwargs["auth"] = self.auth
        if kwargs.get("follow_redirects", httpx.USE_CLIENT_DEFAULT) is httpx.USE_CLIENT_DEFAULT:
            kwargs["follow_redirects"] = self.follow_redirects
        for hook in self.event_hooks["request"]:
            await hook(request)
        response = await self.pool.client.send(request, **kwargs)
        self.responses.append(response)
        for hook in self.event_hooks["response"]:
            await hook(response)
        if kwargs.get("stream") and 200 <= response.status_code < 300:
            self.has_stream = True
            api = "responses" if request.url.path.rstrip("/").endswith("/responses") else "chat"
            self.api = api
            response = verified_response(response, api, self.choices, self.observe)
            if api == "chat" and self.messages_route:
                response = messages_chat_response(response, self._messages_stream_failure)
            self.responses[-1] = response
        return response

    def _messages_stream_failure(self, reason):
        # Some SDK adapters synthesize a finish after swallowing iterator errors.
        # Retain evidence before control returns to that adapter.
        if self.stream_failure != reason:
            self.stream_failure = reason
            _log("upstream_messages_tool_stream_failed", api=self.api,
                 request_id=self.key, reason=reason)

    async def aclose(self):
        async with self.close_lock:
            if self.released:
                return
            async def close_response(response):
                try:
                    if not response.is_closed:
                        await response.aclose()
                except Exception as error:
                    UPSTREAM_STREAM_CLEANUP_ERRORS.labels(api=self.api).inc()
                    _log("upstream_stream_close_failed", level=logging.WARNING, api=self.api, error_type=type(error).__name__, request_id=self.key)
            try:
                with anyio.fail_after(2, shield=True):
                    await asyncio.gather(*(close_response(response) for response in self.responses))
            except Exception as error:
                UPSTREAM_STREAM_CLEANUP_ERRORS.labels(api=self.api).inc()
                _log("upstream_stream_close_failed", level=logging.WARNING, api=self.api, error_type=type(error).__name__, request_id=self.key)
            finally:
                self.responses.clear()
                await super().aclose()
                self.released = True
                self.pool.borrowers -= 1
                self.pool.idle_since = time.monotonic()


class StreamManager:
    def __init__(self, transport=None):
        self.transport = transport
        self.pools = {}
        self.pool_lock = asyncio.Lock()
        self.requests = {}
        self.closing = {}
        self.watcher = None
        self.closed = False

    async def _watch(self):
        try:
            while True:
                await asyncio.sleep(IDLE_POOL_SECONDS)
                async with self.pool_lock:
                    expired = [key for key, pool in self.pools.items()
                               if not pool.borrowers and time.monotonic() - pool.idle_since >= IDLE_POOL_SECONDS]
                    pools = [self.pools.pop(key) for key in expired]
                for pool in pools:
                    await self._close_pool(pool)
        finally:
            if not self.closed:
                await finish_stream_cleanup(self.aclose())

    async def client(self, kwargs, key, api):
        choices = kwargs.get("n") if kwargs.get("n") is not None else 1
        if not isinstance(choices, int) or isinstance(choices, bool) or not 1 <= choices <= 128:
            raise ValueError("Unsupported upstream choice count")
        async with self.pool_lock:
            if self.closed:
                raise RuntimeError("Upstream stream manager is closed")
            pool_key, verify, cert = _ssl_settings(kwargs)
            if pool_key not in self.pools:
                if len(self.pools) >= MAX_HTTP_POOLS:
                    idle = next((k for k, p in self.pools.items() if not p.borrowers), None)
                    if idle is None:
                        raise httpx.PoolTimeout("Upstream HTTP pool configuration capacity exceeded")
                    pool = self.pools.pop(idle)
                    await self._close_pool(pool)
                self.pools[pool_key] = _Pool(verify, cert, self.transport)
            client = RequestHTTPClient(self.pools[pool_key], api, choices, key, kwargs.get("timeout", 600),
                                       messages_route=is_messages_route(kwargs))
            self.requests.setdefault(key, []).append(client)
            if self.watcher is None:
                self.watcher = asyncio.create_task(self._watch(), name="ru-upstream-http-pool")
            return client

    async def close_request(self, key):
        clients = self.requests.pop(key, [])
        if clients:
            async def close_clients():
                try:
                    await asyncio.gather(*(client.aclose() for client in clients))
                finally:
                    self.closing.pop(key, None)
            self.closing[key] = asyncio.create_task(close_clients(), name="ru-upstream-request-close")
        if key in self.closing:
            await asyncio.shield(self.closing[key])

    async def _close_pool(self, pool):
        try:
            with anyio.fail_after(2, shield=True):
                await pool.client.aclose()
        except Exception as error:
            _log("upstream_http_pool_close_failed", level=logging.WARNING, error_type=type(error).__name__)

    def assert_stream_valid(self, key):
        # A platform wrapper can swallow a transport error after a real finish
        # of only one choice. Consult our own state before forwarding its output.
        for client in reversed(self.requests.get(key, [])):
            if client.has_stream:
                if client.stream_failure:
                    raise IncompleteUpstreamStream(client.stream_failure)
                break

    async def aclose(self):
        async with self.pool_lock:
            if self.closed:
                return
            self.closed = True
        join_watcher = self.watcher and self.watcher is not asyncio.current_task() and not self.watcher.cancelling()
        if join_watcher:
            self.watcher.cancel()
        await asyncio.gather(*(self.close_request(key) for key in set(self.requests) | set(self.closing)))
        await asyncio.gather(*(self._close_pool(pool) for pool in self.pools.values()))
        self.pools.clear()
        if join_watcher:
            await asyncio.gather(self.watcher, return_exceptions=True)
        for loop, manager in list(_MANAGERS.items()):
            if manager is self:
                _MANAGERS.pop(loop, None)
        _log("upstream_http_pool_closed")


def _manager(create=True):
    loop = asyncio.get_running_loop()
    manager = _MANAGERS.get(loop)
    if manager is None and create:
        manager = _MANAGERS[loop] = StreamManager()
    return manager


async def close_request_streams(data):
    manager = _manager(create=False)
    if manager is not None:
        await manager.close_request(transport_request_id(data))


def assert_request_stream_valid(data):
    manager = _manager(create=False)
    if manager is not None:
        manager.assert_stream_valid(transport_request_id(data))


async def close_upstream_stream_clients():
    manager = _MANAGERS.pop(asyncio.get_running_loop(), None)
    if manager is not None:
        await finish_stream_cleanup(manager.aclose())


class UpstreamStreamAdapter(CustomLogger):
    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        kind = getattr(call_type, "value", call_type)
        openai_route = kwargs.get("custom_llm_provider") == "openai" or str(kwargs.get("model", "")).startswith("openai/")
        if kind in {"acompletion", "aresponses"} and openai_route and is_messages_route(kwargs):
            # Both stock Messages adapters omit this native option. Read only
            # the bool from the server's original request; never forward its body.
            original_request = kwargs.get("proxy_server_request")
            body = original_request.get("body") if isinstance(original_request, dict) else None
            choice = body.get("tool_choice") if isinstance(body, dict) else None
            disable_parallel = choice.get("disable_parallel_tool_use") if isinstance(choice, dict) else None
            if type(disable_parallel) is bool:
                kwargs["parallel_tool_calls"] = not disable_parallel
        bridge = kind == "aresponses" and openai_route and (
            bool(kwargs.get("use_chat_completions_api"))
            or str(kwargs.get("model", "")).startswith("openai/chat_completions/")
        )
        if bridge:
            code = param = message = None
            if kwargs.get("previous_response_id") is not None:
                code = "responses_chat_bridge_history_unsupported"
                param = "previous_response_id"
                message = "The Responses-to-Chat bridge requires full visible history without previous_response_id."
            elif kwargs.get("stream") and isinstance(kwargs.get("tool_choice"), dict) and kwargs["tool_choice"].get("type") == "function":
                if is_messages_route(kwargs):
                    # The SDK builds its Responses stream snapshot with a Chat
                    # named-choice object, which the Responses schema rejects.
                    # Required + exactly the selected function preserves the
                    # named-call constraint while using its supported path.
                    name = kwargs["tool_choice"].get("name")
                    tools = kwargs.get("tools")
                    selected = [tool for tool in tools if isinstance(tool, dict)
                                and tool.get("type") == "function" and tool.get("name") == name] if isinstance(tools, list) else []
                    if isinstance(name, str) and name and len(selected) == 1:
                        kwargs["tools"] = [copy.deepcopy(selected[0])]
                        kwargs["tool_choice"] = "required"
                    else:
                        code = "messages_named_tool_invalid"
                        param = "tool_choice"
                        message = "Named tool selection requires exactly one matching function definition."
                else:
                    code = "responses_chat_bridge_named_tool_unsupported"
                    param = "tool_choice"
                    message = "Named tool selection is unsupported by the streaming Responses-to-Chat bridge. Use Chat Completions, non-streaming Responses, or required with a single tool."
            if code:
                _log("upstream_responses_bridge_rejected", reason=code, request_id=transport_request_id(kwargs))
                raise litellm.BadRequestError(
                    message=f"{code}: {message}", model=kwargs.get("model", ""), llm_provider="openai",
                    body={"message": message, "type": "invalid_request_error", "param": param, "code": code},
                )
            # The stock bridge invokes acompletion, where the real Chat client is injected.
            return kwargs
        if not kwargs.get("stream") or kind not in {"acompletion", "aresponses"} or not openai_route:
            return kwargs
        key = transport_request_id(kwargs)
        if not key:
            raise RuntimeError("Upstream SSE requires server-owned request correlation")
        client = await _manager().client(kwargs, key, "chat" if kind == "acompletion" else "responses")
        try:
            previous = kwargs.get("client")
            if kind == "acompletion":
                if previous is not None:
                    if not isinstance(previous, AsyncOpenAI):
                        raise ValueError("Unsupported preconfigured upstream client")
                    sdk = previous.with_options(http_client=client)
                else:
                    sdk = AsyncOpenAI(api_key=kwargs.get("api_key") or "",
                        base_url=kwargs.get("api_base"), organization=kwargs.get("organization"),
                        http_client=client, timeout=kwargs.get("timeout", 600), max_retries=kwargs.get("max_retries", 2))
            else:
                if previous is not None:
                    if not isinstance(previous, AsyncHTTPHandler):
                        raise ValueError("Unsupported preconfigured Responses HTTP client")
                    sdk = copy.copy(previous)
                    client.headers.update(previous.client.headers)
                    client.params = previous.client.params
                    client.auth = previous.client.auth
                    client.event_hooks = previous.client.event_hooks
                    client.follow_redirects = previous.client.follow_redirects
                    if kwargs.get("timeout") is None:
                        client.timeout = previous.client.timeout
                else:
                    sdk = AsyncHTTPHandler(timeout=kwargs.get("timeout"))
                    client.headers.update(get_default_headers())
                    client.timeout = sdk.client.timeout
                    await sdk.client.aclose()
                client.headers["Accept-Encoding"] = "gzip, deflate"
                sdk.client = client
            kwargs["client"] = sdk
            return kwargs
        except BaseException:
            await finish_stream_cleanup(_manager().close_request(key))
            raise

    async def async_post_call_failure_hook(self, request_data, original_exception, user_api_key_dict, traceback_str=None):
        await finish_stream_cleanup(close_request_streams(request_data))


adapter = UpstreamStreamAdapter()
