"""Explicit limits and remote-reference policy for untrusted payloads."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import SplitResult, urlsplit, urlunsplit

from payload_palette.errors import PayloadValidationError, problem
from payload_palette.media import normalize_mime_type
from payload_palette.models import PartKind

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LEGACY_IPV4 = re.compile(
    r"^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+)){0,3}$",
    re.IGNORECASE,
)
_PERCENT_ESCAPE = re.compile(r"[0-9a-fA-F]{2}")
_UNRESERVED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
_PART_KINDS = frozenset({"text", "image", "audio", "video"})

MAX_POLICY_PARTS = 1_000_000
MAX_POLICY_TEXT_CHARACTERS = 100_000_000
MAX_POLICY_BYTES = 1024 * 1024 * 1024
MAX_REMOTE_URL_CHARACTERS = 16_384

_BLOCKED_IPV4_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_PUBLIC_IPV4_EXCEPTIONS = frozenset(
    {ipaddress.IPv4Address("192.0.0.9"), ipaddress.IPv4Address("192.0.0.10")}
)
_BLOCKED_IPV6_NETWORKS = tuple(
    ipaddress.IPv6Network(value)
    for value in (
        "::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "2002::/16",
        "3fff::/20",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)
_PUBLIC_IPV6_EXCEPTIONS = tuple(
    ipaddress.IPv6Network(value)
    for value in (
        "2001:1::1/128",
        "2001:1::2/128",
        "2001:1::3/128",
        "2001:3::/32",
        "2001:4:112::/48",
        "2001:20::/28",
        "2001:30::/28",
    )
)
_NAT64_WELL_KNOWN_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
_GLOBAL_UNICAST_IPV6 = ipaddress.IPv6Network("2000::/3")

DEFAULT_MIME_TYPES: Mapping[PartKind, frozenset[str]] = MappingProxyType(
    {
        "text": frozenset({"text/plain"}),
        "image": frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
        "audio": frozenset(
            {
                "audio/wav",
                "audio/mpeg",
                "audio/ogg",
                "audio/flac",
                "audio/mp4",
                "audio/webm",
            }
        ),
        "video": frozenset({"video/mp4", "video/webm", "video/quicktime"}),
    }
)

DEFAULT_MAX_BYTES: Mapping[PartKind, int] = MappingProxyType(
    {
        "text": 1 * 1024 * 1024,
        "image": 10 * 1024 * 1024,
        "audio": 25 * 1024 * 1024,
        "video": 100 * 1024 * 1024,
    }
)


def _contains_forbidden_url_character(value: str) -> bool:
    return any(
        character == "\\"
        or character.isspace()
        or ord(character) < 32
        or 127 <= ord(character) <= 159
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    )


def _strip_one_trailing_dot(host: str, original: str) -> str:
    if host.endswith(".."):
        raise ValueError(f"host must not contain multiple trailing dots: {original!r}")
    return host[:-1] if host.endswith(".") else host


def _parse_ip_or_reject_legacy(
    host: str, original: str
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        if _LEGACY_IPV4.fullmatch(host):
            raise ValueError(f"ambiguous legacy IPv4 host is not allowed: {original!r}") from None
        return None


def _normalize_host(host: str, *, allow_wildcard: bool = True) -> str:
    if not isinstance(host, str):
        raise ValueError("host patterns must be strings")
    if _contains_forbidden_url_character(host):
        raise ValueError(f"invalid host pattern: {host!r}")
    if "%" in host:
        raise ValueError(f"IPv6 zone identifiers and percent escapes are not allowed: {host!r}")
    candidate = _strip_one_trailing_dot(host.lower(), host)
    if not candidate:
        raise ValueError("host cannot be empty")
    if candidate.startswith("*."):
        if not allow_wildcard:
            raise ValueError("wildcards are not valid URL hosts")
        suffix = candidate[2:]
        if _parse_ip_or_reject_legacy(suffix, host) is not None:
            raise ValueError("wildcards cannot target an IP address")
        try:
            suffix = suffix.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError(f"invalid host pattern: {host!r}") from exc
        suffix = _strip_one_trailing_dot(suffix, host)
        if _parse_ip_or_reject_legacy(suffix, host) is not None:
            raise ValueError("wildcards cannot target an IP address")
        _validate_domain(suffix)
        return f"*.{suffix}"
    address = _parse_ip_or_reject_legacy(candidate, host)
    if address is not None:
        return address.compressed.lower()
    try:
        candidate = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError(f"invalid host pattern: {host!r}") from exc
    candidate = _strip_one_trailing_dot(candidate, host)
    address = _parse_ip_or_reject_legacy(candidate, host)
    if address is not None:
        raise ValueError(f"Unicode host must not map to an IP address: {host!r}")
    _validate_domain(candidate)
    return candidate


def _validate_domain(host: str) -> None:
    if len(host) > 253 or any(not _HOST_LABEL.fullmatch(label) for label in host.split(".")):
        raise ValueError(f"invalid host pattern: {host!r}")


def _matches_host(host: str, pattern: str) -> bool:
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix) and host != suffix[1:]
    return host == pattern


def _is_local_address(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return _is_non_global_ipv4(address)
    if address.ipv4_mapped is not None:
        return _is_non_global_ipv4(address.ipv4_mapped)
    if address in _NAT64_WELL_KNOWN_PREFIX:
        embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        return _is_non_global_ipv4(embedded)
    if any(address in network for network in _PUBLIC_IPV6_EXCEPTIONS):
        return False
    return address not in _GLOBAL_UNICAST_IPV6 or any(
        address in network for network in _BLOCKED_IPV6_NETWORKS
    )


def _is_non_global_ipv4(address: ipaddress.IPv4Address) -> bool:
    if address in _PUBLIC_IPV4_EXCEPTIONS:
        return False
    return any(address in network for network in _BLOCKED_IPV4_NETWORKS)


def _normalize_percent_encoding(value: str) -> str:
    normalized: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "%":
            normalized.append(character)
            index += 1
            continue
        escape = value[index + 1 : index + 3]
        if len(escape) != 2 or not _PERCENT_ESCAPE.fullmatch(escape):
            raise ValueError("URL contains a malformed percent escape")
        decoded = chr(int(escape, 16))
        normalized.append(decoded if decoded in _UNRESERVED else f"%{escape.upper()}")
        index += 3
    return "".join(normalized)


def _remove_dot_segments(path: str) -> str:
    """Apply RFC 3986 dot-segment removal without decoding reserved slashes."""

    output: list[str] = []
    index = 0
    length = len(path)
    while index < length:
        remaining = length - index
        if path.startswith("../", index):
            index += 3
        elif path.startswith(("./", "/./"), index):
            index += 2
        elif remaining == 2 and path.startswith("/.", index):
            output.append("/")
            break
        elif path.startswith("/../", index):
            index += 3
            if output:
                output.pop()
        elif remaining == 3 and path.startswith("/..", index):
            if output:
                output.pop()
            output.append("/")
            break
        elif (remaining == 1 and path[index] == ".") or (
            remaining == 2 and path.startswith("..", index)
        ):
            break
        else:
            separator = path.find("/", index + 1 if path[index] == "/" else index)
            if separator < 0:
                output.append(path[index:])
                break
            else:
                output.append(path[index:separator])
                index = separator
    return "".join(output)


def _bounded_int(name: str, value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class RemoteURLPolicy:
    """Rules for accepting URL references without fetching them.

    Remote media is disabled when ``allowed_hosts`` is empty. Wildcards are
    explicit suffix patterns such as ``*.example.org`` and do not match the
    apex domain.
    """

    allowed_hosts: tuple[str, ...] = ()
    require_https: bool = True
    block_local_addresses: bool = True
    redact_query: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.allowed_hosts, (str, bytes)) or not isinstance(
            self.allowed_hosts, Sequence
        ):
            raise ValueError("allowed_hosts must be a sequence of host strings")
        for name in ("require_https", "block_local_addresses", "redact_query"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        normalized = tuple(_normalize_host(host) for host in self.allowed_hosts)
        object.__setattr__(self, "allowed_hosts", normalized)

    @property
    def enabled(self) -> bool:
        """Whether any remote host can pass this policy."""

        return bool(self.allowed_hosts)

    def validate(self, value: str, path: str) -> tuple[str, str]:
        """Validate a remote URL and return display and canonical forms.

        The display form may redact the query. The canonical form retains it
        solely for fingerprinting and is never placed in the manifest.
        """

        if not self.enabled:
            raise problem(
                "remote_url_disabled",
                "remote media references are disabled by policy",
                path,
                "allow an exact host with --allow-host",
            )
        if not isinstance(value, str):
            raise problem(
                "invalid_url",
                "remote URL must be a string",
                path,
            )
        if len(value) > MAX_REMOTE_URL_CHARACTERS:
            raise problem(
                "url_too_long",
                f"remote URL exceeds the {MAX_REMOTE_URL_CHARACTERS}-character limit",
                path,
            )
        if _contains_forbidden_url_character(value):
            raise problem(
                "invalid_url",
                "remote URL contains forbidden whitespace, control characters, or backslashes",
                path,
            )
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise problem("invalid_url", f"invalid remote URL: {exc}", path) from exc
        if parsed.scheme.lower() not in {"http", "https"}:
            raise problem("url_scheme", "remote URLs must use HTTP or HTTPS", path)
        if self.require_https and parsed.scheme.lower() != "https":
            raise problem("https_required", "remote URLs must use HTTPS", path)
        if parsed.username is not None or parsed.password is not None:
            raise problem("url_credentials", "credentials are not allowed in media URLs", path)
        try:
            raw_host = parsed.hostname or ""
            host = _normalize_host(raw_host, allow_wildcard=False) if raw_host else ""
            port = parsed.port
            path_value = _remove_dot_segments(_normalize_percent_encoding(parsed.path or "/"))
            query = _normalize_percent_encoding(parsed.query)
            _normalize_percent_encoding(parsed.fragment)
        except ValueError as exc:
            raise problem("invalid_url", f"invalid remote URL: {exc}", path) from exc
        if not host:
            raise problem("url_host", "remote URL must include a host", path)
        if self.block_local_addresses and _is_local_address(host):
            raise problem("local_address", "local and non-public IP addresses are blocked", path)
        if not any(_matches_host(host, pattern) for pattern in self.allowed_hosts):
            raise problem("host_not_allowed", f"remote host {host!r} is not allowlisted", path)
        scheme = parsed.scheme.lower()
        if (scheme, port) in {("https", 443), ("http", 80)}:
            port = None
        normalized = SplitResult(
            scheme=scheme,
            netloc=_netloc(host, port),
            path=path_value or "/",
            query=query,
            fragment="",
        )
        canonical = urlunsplit(normalized)
        if self.redact_query and query:
            normalized = normalized._replace(query="[redacted]")
        return urlunsplit(normalized), canonical


def _netloc(host: str, port: int | None) -> str:
    rendered = f"[{host}]" if ":" in host else host
    return rendered if port is None else f"{rendered}:{port}"


@dataclass(frozen=True, slots=True)
class NormalizationPolicy:
    """Resource, MIME, and URL constraints for one normalization run."""

    max_parts: int = 128
    max_text_characters: int = 200_000
    max_total_inline_bytes: int = 128 * 1024 * 1024
    max_bytes_by_kind: Mapping[PartKind, int] = field(
        default_factory=lambda: dict(DEFAULT_MAX_BYTES)
    )
    allowed_mime_types: Mapping[PartKind, frozenset[str]] = field(
        default_factory=lambda: dict(DEFAULT_MIME_TYPES)
    )
    remote: RemoteURLPolicy = field(default_factory=RemoteURLPolicy)
    verify_known_signatures: bool = True
    allow_url_safe_base64: bool = False

    def __post_init__(self) -> None:
        _bounded_int("max_parts", self.max_parts, 1, MAX_POLICY_PARTS)
        _bounded_int(
            "max_text_characters",
            self.max_text_characters,
            0,
            MAX_POLICY_TEXT_CHARACTERS,
        )
        _bounded_int(
            "max_total_inline_bytes",
            self.max_total_inline_bytes,
            0,
            MAX_POLICY_BYTES,
        )
        if not isinstance(self.max_bytes_by_kind, Mapping):
            raise ValueError("max_bytes_by_kind must be a mapping")
        if not isinstance(self.allowed_mime_types, Mapping):
            raise ValueError("allowed_mime_types must be a mapping")
        if not isinstance(self.remote, RemoteURLPolicy):
            raise ValueError("remote must be a RemoteURLPolicy")
        for name in ("verify_known_signatures", "allow_url_safe_base64"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        byte_limits = dict(self.max_bytes_by_kind)
        if set(byte_limits) != _PART_KINDS:
            raise ValueError("max_bytes_by_kind must contain exactly text, image, audio, and video")
        byte_limits = {
            kind: _bounded_int(f"max_bytes_by_kind[{kind!r}]", limit, 0, MAX_POLICY_BYTES)
            for kind, limit in byte_limits.items()
        }
        raw_mime_types = dict(self.allowed_mime_types)
        if set(raw_mime_types) != _PART_KINDS:
            raise ValueError(
                "allowed_mime_types must contain exactly text, image, audio, and video"
            )
        mime_types: dict[str, frozenset[str]] = {}
        for kind, values in raw_mime_types.items():
            if isinstance(values, (str, bytes)):
                raise ValueError(f"allowed_mime_types[{kind!r}] must be a collection of strings")
            try:
                materialized = tuple(values)
            except TypeError as exc:
                raise ValueError(
                    f"allowed_mime_types[{kind!r}] must be a collection of strings"
                ) from exc
            normalized_values: set[str] = set()
            for value in materialized:
                if not isinstance(value, str):
                    raise ValueError(f"allowed MIME values for {kind!r} must be strings")
                try:
                    normalized = normalize_mime_type(value, f"policy.{kind}")
                except PayloadValidationError as exc:
                    raise ValueError(f"invalid allowed MIME type {value!r} for {kind}") from exc
                if normalized != value:
                    raise ValueError(f"allowed MIME type {value!r} must already be normalized")
                normalized_values.add(normalized)
            mime_types[kind] = frozenset(normalized_values)
        object.__setattr__(self, "max_bytes_by_kind", MappingProxyType(byte_limits))
        object.__setattr__(self, "allowed_mime_types", MappingProxyType(mime_types))

    def check_mime(self, kind: PartKind, mime_type: str, path: str) -> None:
        """Ensure a normalized MIME type is accepted for its part kind."""

        allowed = self.allowed_mime_types.get(kind, frozenset())
        if mime_type not in allowed:
            raise problem(
                "mime_not_allowed",
                f"MIME type {mime_type!r} is not allowed for {kind} parts",
                path,
                f"allowed values: {', '.join(sorted(allowed)) or 'none'}",
            )

    def check_size(self, kind: PartKind, size: int, path: str) -> None:
        """Ensure decoded content stays below the part-specific byte limit."""

        maximum = self.max_bytes_by_kind[kind]
        if size > maximum:
            raise problem(
                "part_too_large",
                f"decoded {kind} part is {size} bytes; limit is {maximum} bytes",
                path,
            )


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MIME_TYPES",
    "MAX_POLICY_BYTES",
    "MAX_POLICY_PARTS",
    "MAX_POLICY_TEXT_CHARACTERS",
    "MAX_REMOTE_URL_CHARACTERS",
    "NormalizationPolicy",
    "RemoteURLPolicy",
]
