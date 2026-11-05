"""Strict, side-effect-free parsing of the two-node deployment description."""

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


MAX_CONFIG_BYTES = 65536


class NodeConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class NodeConfiguration:
    source_cidrs: tuple[str, ...]
    analyzer_url: str


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise NodeConfigurationError("NODE_CONFIG: повторяющиеся поля JSON")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise NodeConfigurationError("NODE_CONFIG: недопустимое число JSON")


def _fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise NodeConfigurationError("NODE_CONFIG: неверный состав полей")


def _sources(values):
    if not isinstance(values, list) or not values:
        raise NodeConfigurationError("NODE_CONFIG: требуется primary.source_cidrs")
    result = []
    for value in values:
        try:
            if (not isinstance(value, str) or "/" not in value or "%" in value
                    or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
                raise ValueError
            network = ipaddress.ip_network(value, strict=True)
            if network.prefixlen == 0 or str(network) in result:
                raise ValueError
        except ValueError:
            raise NodeConfigurationError("NODE_CONFIG: некорректный primary.source_cidrs") from None
        result.append(str(network))
    return tuple(result)


def _endpoint(value):
    error = "NODE_CONFIG: некорректный compute.analyzer_url"
    if (not isinstance(value, str) or not value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            or "\\" in value or "%" in value or "?" in value or "#" in value):
        raise NodeConfigurationError(error)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else 80
        if (parsed.scheme != "http" or not host or not 1 <= port <= 65535
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError
        authority = r"\[[^\]]+\](?::[0-9]+)?" if ":" in host else r"[^:]+(?::[0-9]+)?"
        if re.fullmatch(authority, parsed.netloc) is None:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            hostname = host.removesuffix(".")
            labels = hostname.split(".")
            if (len(hostname) > 253 or all(label.isdigit() for label in labels)
                    or any(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None
                           for label in labels)):
                raise ValueError
            host = hostname.lower()
        else:
            host = f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        raise NodeConfigurationError(error) from None
    return f"http://{host}:{port}"


def load_node_configuration(path: Path) -> NodeConfiguration:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
    except (OSError, ValueError):
        raise NodeConfigurationError("NODE_CONFIG: не удалось прочитать файл") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise NodeConfigurationError("NODE_CONFIG: файл превышает 64 КиБ")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields,
                          parse_constant=_invalid_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise NodeConfigurationError("NODE_CONFIG: некорректный UTF-8 JSON") from None
    _fields(data, {"primary", "compute"})
    _fields(data["primary"], {"source_cidrs"})
    _fields(data["compute"], {"analyzer_url"})
    return NodeConfiguration(_sources(data["primary"]["source_cidrs"]),
                             _endpoint(data["compute"]["analyzer_url"]))
