"""Local TLS/proxy/keepalive checks using the pinned platform's dependencies."""

import asyncio
import datetime
import ipaddress
import os
import ssl
import tempfile
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from litellm_guardrails.upstream_stream import StreamManager


EVENT = b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'


def certificate(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic upstream test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    ca, private = Path(directory) / "ca.pem", Path(directory) / "key.pem"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca, private)
    return ca, context


async def run():
    connections, proxied, tasks = set(), [], set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        connections.add(writer.get_extra_info("peername"))
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: " +
                             str(len(EVENT)).encode() + b"\r\n\r\n" + EVENT)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    async def proxy(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            proxied.append(head.split(b"\r\n", 1)[0])
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: " +
                         str(len(EVENT)).encode() + b"\r\n\r\n" + EVENT)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    variables = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE")
    saved = {name: os.environ.pop(name, None) for name in variables}
    manager = StreamManager()
    servers = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            ca, context = certificate(directory)
            os.environ["SSL_CERT_FILE"] = str(ca)
            servers.append(await asyncio.start_server(serve, "127.0.0.1", 0, ssl=context))
            servers.append(await asyncio.start_server(serve, "127.0.0.1", 0))
            servers.append(await asyncio.start_server(proxy, "127.0.0.1", 0))
            tls_port, http_port, proxy_port = [server.sockets[0].getsockname()[1] for server in servers]

            async def request(key, url):
                client = await manager.client({"timeout": 3}, key, "responses")
                response = await client.send(client.build_request("GET", url), stream=True)
                assert await response.aread() == EVENT
                await manager.close_request(key)

            tls_url = f"https://127.0.0.1:{tls_port}/v1/responses"
            await request("tls-a", tls_url)
            await request("tls-b", tls_url)
            assert len(connections) == 1, "Completed requests must reuse the TLS connection"
            os.environ.pop("SSL_CERT_FILE")
            try:
                await request("untrusted", tls_url)
            except httpx.ConnectError:
                pass
            else:
                raise AssertionError("Untrusted TLS certificate was accepted")
            await manager.close_request("untrusted")

            os.environ["HTTP_PROXY"] = f"http://127.0.0.1:{proxy_port}"
            await request("proxy", "http://synthetic.invalid/v1/responses")
            assert proxied == [b"GET http://synthetic.invalid/v1/responses HTTP/1.1"]
            os.environ["NO_PROXY"] = "127.0.0.1"
            await request("bypass", f"http://127.0.0.1:{http_port}/v1/responses")
            assert len(proxied) == 1, "Internal upstream must bypass the proxy"
            await manager.aclose()
            assert not manager.pools and not manager.requests and not manager.closing
    finally:
        await manager.aclose()
        for server in servers:
            server.close()
            await server.wait_closed()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        for name in variables:
            os.environ.pop(name, None)
            if saved[name] is not None:
                os.environ[name] = saved[name]
    print("PASS upstream transport: trusted/rejected TLS, keepalive, HTTP proxy, NO_PROXY, pool cleanup")


if __name__ == "__main__":
    asyncio.run(run())
