"""Register our public ASGI middleware, then run the unmodified LiteLLM CLI."""

if __name__ in {"__main__", "__mp_main__"}:
    from litellm.proxy.proxy_server import app

    from litellm_guardrails.request_cancellation import RequestCancellationMiddleware

    # Spawned Uvicorn workers also import the main module before loading app.
    app.add_middleware(RequestCancellationMiddleware)

if __name__ == "__main__":
    from litellm import run_server

    run_server()
