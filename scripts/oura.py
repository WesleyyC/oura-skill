#!/usr/bin/env python3
"""Secure command-line access to Oura Cloud API V2."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
import getpass
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import random as random_module
import re
import secrets
import shlex
import sys
import tempfile
import time
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
import webbrowser


DEFAULT_REDIRECT_URI = "http://localhost:8910/callback"
DEFAULT_SCOPES = "daily heartrate workout tag session spo2Daily personal"
DEFAULT_PROFILE = "default"
PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
ENV_KEY_RE = re.compile(r"OURA_[A-Z0-9_]+\Z")
APP_KEYS = (
    "OURA_CLIENT_ID",
    "OURA_CLIENT_SECRET",
    "OURA_REDIRECT_URI",
    "OURA_SCOPES",
    "OURA_DEFAULT_PROFILE",
)
TOKEN_KEYS = (
    "OURA_ACCESS_TOKEN",
    "OURA_REFRESH_TOKEN",
    "OURA_TOKEN_EXPIRES_AT",
    "OURA_GRANTED_SCOPES",
)
AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_BASE_URL = "https://api.ouraring.com/v2/usercollection"
FIELDS_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(,[A-Za-z_][A-Za-z0-9_]*)*\Z")


class ConfigError(RuntimeError):
    """Raised when local Oura configuration is missing or unsafe."""


class OAuthError(RuntimeError):
    """Raised when Oura authorization cannot complete safely."""


class QueryError(RuntimeError):
    """Raised when an Oura resource or query is not allowlisted."""


class ApiError(RuntimeError):
    """Raised when an authenticated Oura API request fails."""


@dataclass(frozen=True)
class AppConfig:
    client_id: str
    client_secret: str
    redirect_uri: str = DEFAULT_REDIRECT_URI
    scopes: str = DEFAULT_SCOPES
    default_profile: str = DEFAULT_PROFILE


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float
    granted_scopes: str
    persistable: bool = True


@dataclass(frozen=True)
class LoopbackBinding:
    host: str
    port: int
    path: str


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[
    [str, str, Mapping[str, str], bytes | None, float], HttpResponse
]


@dataclass(frozen=True)
class ResourceSpec:
    path: str
    temporal: str
    scope: str
    supports_latest: bool = False
    supports_fields: bool = True
    upstream_end_date_exclusive: bool = False


RESOURCE_SPECS = {
    "daily_activity": ResourceSpec(
        "daily_activity", "date", "daily", upstream_end_date_exclusive=True
    ),
    "daily_cardiovascular_age": ResourceSpec(
        "daily_cardiovascular_age", "date", "daily"
    ),
    "daily_readiness": ResourceSpec("daily_readiness", "date", "daily"),
    "daily_resilience": ResourceSpec("daily_resilience", "date", "daily"),
    "daily_sleep": ResourceSpec("daily_sleep", "date", "daily"),
    "daily_spo2": ResourceSpec("daily_spo2", "date", "spo2Daily"),
    "daily_stress": ResourceSpec("daily_stress", "date", "daily"),
    "enhanced_tag": ResourceSpec("enhanced_tag", "date", "tag"),
    "heartrate": ResourceSpec("heartrate", "datetime", "heartrate", True),
    "personal_info": ResourceSpec(
        "personal_info", "none", "personal", False, False
    ),
    "rest_mode_period": ResourceSpec("rest_mode_period", "date", "daily"),
    "ring_battery_level": ResourceSpec(
        "ring_battery_level", "datetime", "personal", True
    ),
    "ring_configuration": ResourceSpec(
        "ring_configuration", "none", "personal"
    ),
    "session": ResourceSpec("session", "date", "session"),
    "sleep": ResourceSpec(
        "sleep", "date", "daily", upstream_end_date_exclusive=True
    ),
    "sleep_time": ResourceSpec("sleep_time", "date", "daily"),
    "tag": ResourceSpec("tag", "date", "tag"),
    "vo2_max": ResourceSpec("vO2_max", "date", "daily"),
    "workout": ResourceSpec(
        "workout", "date", "workout", upstream_end_date_exclusive=True
    ),
}


def default_config_dir() -> Path:
    return Path.home() / ".config" / "oura"


def example_app_values() -> dict[str, str]:
    return {
        "OURA_CLIENT_ID": "example-id",
        "OURA_CLIENT_SECRET": "example-secret",
        "OURA_REDIRECT_URI": DEFAULT_REDIRECT_URI,
        "OURA_SCOPES": DEFAULT_SCOPES,
        "OURA_DEFAULT_PROFILE": DEFAULT_PROFILE,
    }


def validate_profile(label: str) -> str:
    if not PROFILE_RE.fullmatch(label):
        raise ConfigError(
            "Profile must use 1-32 lowercase letters, digits, hyphens, or underscores."
        )
    return label


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise ConfigError(f"Could not secure configuration directory: {path}") from exc


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigError(f"Could not read configuration file: {path}") from exc

    values: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped, comments=True, posix=True)
        except ValueError as exc:
            raise ConfigError(
                f"Invalid quoting in configuration file {path} at line {line_number}."
            ) from exc
        if len(tokens) != 1 or "=" not in tokens[0]:
            raise ConfigError(
                f"Expected one KEY=VALUE entry in {path} at line {line_number}."
            )
        key, value = tokens[0].split("=", 1)
        if not ENV_KEY_RE.fullmatch(key):
            raise ConfigError(
                f"Invalid configuration key in {path} at line {line_number}."
            )
        if key in values:
            raise ConfigError(f"Duplicate configuration key {key} in {path}.")
        values[key] = value
    return values


def atomic_write_env(path: Path, values: Mapping[str, str]) -> None:
    for key, value in values.items():
        if not ENV_KEY_RE.fullmatch(key):
            raise ConfigError(f"Invalid configuration key: {key}")
        if "\n" in value or "\r" in value:
            raise ConfigError(f"Configuration value for {key} contains a newline.")

    _ensure_private_directory(path.parent)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            os.fchmod(handle.fileno(), 0o600)
            for key, value in values.items():
                handle.write(f"{key}={shlex.quote(value)}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as exc:
        raise ConfigError(f"Could not write configuration file: {path}") from exc
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def write_app_config(config_dir: Path, values: Mapping[str, str]) -> None:
    normalized = {key: str(values.get(key, "")) for key in APP_KEYS}
    atomic_write_env(config_dir / "app.env", normalized)


def load_app_config(config_dir: Path, env: Mapping[str, str]) -> AppConfig:
    configured_path = env.get("OURA_APP_ENV_FILE", "").strip()
    path = Path(configured_path).expanduser() if configured_path else config_dir / "app.env"
    values = parse_env_file(path)
    for key in APP_KEYS:
        process_value = env.get(key, "")
        if process_value:
            values[key] = process_value

    values.setdefault("OURA_REDIRECT_URI", DEFAULT_REDIRECT_URI)
    values.setdefault("OURA_SCOPES", DEFAULT_SCOPES)
    values.setdefault("OURA_DEFAULT_PROFILE", DEFAULT_PROFILE)
    missing = [key for key in ("OURA_CLIENT_ID", "OURA_CLIENT_SECRET") if not values.get(key)]
    if missing:
        raise ConfigError("Missing required configuration: " + ", ".join(missing))

    default_profile = validate_profile(values["OURA_DEFAULT_PROFILE"])
    redirect_uri = values["OURA_REDIRECT_URI"].strip()
    scopes = values["OURA_SCOPES"].strip()
    if not redirect_uri:
        raise ConfigError("OURA_REDIRECT_URI cannot be empty.")
    if not scopes:
        raise ConfigError("OURA_SCOPES cannot be empty.")
    return AppConfig(
        values["OURA_CLIENT_ID"],
        values["OURA_CLIENT_SECRET"],
        redirect_uri,
        scopes,
        default_profile,
    )


def profile_path(config_dir: Path, profile: str) -> Path:
    return config_dir / "profiles" / f"{validate_profile(profile)}.env"


def write_profile_tokens(config_dir: Path, profile: str, tokens: TokenSet) -> None:
    label = validate_profile(profile)
    values = {
        "OURA_PROFILE": label,
        "OURA_ACCESS_TOKEN": tokens.access_token,
        "OURA_REFRESH_TOKEN": tokens.refresh_token,
        "OURA_TOKEN_EXPIRES_AT": str(tokens.expires_at),
        "OURA_GRANTED_SCOPES": tokens.granted_scopes,
    }
    atomic_write_env(profile_path(config_dir, label), values)


def load_profile_tokens(
    config_dir: Path, profile: str, env: Mapping[str, str]
) -> TokenSet:
    label = validate_profile(profile)
    values = parse_env_file(profile_path(config_dir, label))
    stored_label = values.get("OURA_PROFILE")
    if stored_label and stored_label != label:
        raise ConfigError(f"Profile file label does not match selected profile: {label}")

    process_override = False
    for key in TOKEN_KEYS:
        process_value = env.get(key, "")
        if process_value:
            values[key] = process_value
            process_override = True

    required = (
        "OURA_ACCESS_TOKEN",
        "OURA_REFRESH_TOKEN",
        "OURA_TOKEN_EXPIRES_AT",
    )
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ConfigError(
            f"Profile {label} is not authorized; missing " + ", ".join(missing)
        )
    try:
        expires_at = float(values["OURA_TOKEN_EXPIRES_AT"])
    except ValueError as exc:
        raise ConfigError(f"Profile {label} has an invalid token expiry.") from exc
    return TokenSet(
        values["OURA_ACCESS_TOKEN"],
        values["OURA_REFRESH_TOKEN"],
        expires_at,
        values.get("OURA_GRANTED_SCOPES", ""),
        persistable=not process_override,
    )


def resolve_profile(
    explicit: str | None, config: AppConfig, env: Mapping[str, str]
) -> str:
    if explicit:
        return validate_profile(explicit)
    environment_profile = env.get("OURA_PROFILE", "")
    if environment_profile:
        return validate_profile(environment_profile)
    return validate_profile(config.default_profile)


def build_authorization_url(config: AppConfig, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "scope": config.scopes,
            "state": state,
        }
    )


def validate_loopback_redirect(uri: str) -> LoopbackBinding:
    try:
        parsed = urlparse(uri)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("OURA_REDIRECT_URI has an invalid port.") from exc
    if parsed.scheme != "http":
        raise ConfigError("OURA_REDIRECT_URI must use HTTP on a loopback host.")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigError("OURA_REDIRECT_URI must use localhost or a loopback address.")
    if port is None:
        raise ConfigError("OURA_REDIRECT_URI must include an explicit port.")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("OURA_REDIRECT_URI cannot include user information.")
    if not parsed.path or not parsed.path.startswith("/"):
        raise ConfigError("OURA_REDIRECT_URI must include a callback path.")
    if parsed.params or parsed.query or parsed.fragment:
        raise ConfigError("OURA_REDIRECT_URI cannot include params, query, or fragment.")
    return LoopbackBinding(parsed.hostname, port, parsed.path)


def validate_callback(callback: Mapping[str, str], expected_state: str) -> str:
    if callback.get("error"):
        raise OAuthError("Oura authorization was denied or could not complete.")
    code = callback.get("code", "")
    returned_state = callback.get("state", "")
    if not code:
        raise OAuthError("Oura callback did not include an authorization code.")
    if not returned_state or not secrets.compare_digest(returned_state, expected_state):
        raise OAuthError("Oura callback state did not match; authorization was stopped.")
    return code


def sanitize_error(message: str, secret_values: list[str]) -> str:
    sanitized = message
    for value in sorted((value for value in secret_values if value), key=len, reverse=True):
        sanitized = sanitized.replace(value, "[REDACTED]")
    return sanitized


def _structured_error(payload: Mapping[str, object]) -> str:
    parts: list[str] = []
    for key in ("title", "detail", "error", "error_description"):
        value = payload.get(key)
        if isinstance(value, str) and value and value not in parts:
            parts.append(value)
    return ": ".join(parts)


def _json_object(body: bytes, context: str) -> dict[str, object]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthError(f"{context} returned malformed JSON.") from exc
    if not isinstance(payload, dict):
        raise OAuthError(f"{context} returned an unexpected JSON value.")
    return payload


def urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> HttpResponse:
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                response.status,
                dict(response.headers.items()),
                response.read(),
            )
    except HTTPError as exc:
        return HttpResponse(exc.code, dict(exc.headers.items()), exc.read())
    except (URLError, OSError) as exc:
        raise OSError("Network request failed.") from exc


def token_from_response(payload: Mapping[str, object], now: float) -> TokenSet:
    try:
        access_token = str(payload["access_token"])
        refresh_value = str(payload["refresh_token"])
        expires_in = float(payload["expires_in"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OAuthError("Oura returned an incomplete token response.") from exc
    if not access_token or not refresh_value or expires_in <= 0:
        raise OAuthError("Oura returned an invalid token response.")
    return TokenSet(
        access_token,
        refresh_value,
        now + expires_in - 60.0,
        str(payload.get("scope", "")),
    )


def _post_token_form(
    config: AppConfig,
    form: Mapping[str, str],
    transport: Transport,
    now: float,
) -> TokenSet:
    body = urlencode(form).encode("utf-8")
    try:
        response = transport(
            "POST",
            TOKEN_URL,
            {"Content-Type": "application/x-www-form-urlencoded"},
            body,
            30.0,
        )
    except OSError as exc:
        raise OAuthError("Could not reach the Oura token endpoint.") from exc

    if not 200 <= response.status < 300:
        try:
            payload = _json_object(response.body, "Oura token endpoint")
            detail = _structured_error(payload)
        except OAuthError:
            detail = ""
        message = detail or f"Oura token endpoint returned HTTP {response.status}."
        raise OAuthError(
            sanitize_error(
                message,
                [config.client_secret, form.get("code", ""), form.get("refresh_token", "")],
            )
        )
    payload = _json_object(response.body, "Oura token endpoint")
    return token_from_response(payload, now)


def exchange_code(
    config: AppConfig,
    code: str,
    transport: Transport = urllib_transport,
    now: float | None = None,
) -> TokenSet:
    return _post_token_form(
        config,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "redirect_uri": config.redirect_uri,
        },
        transport,
        time.time() if now is None else now,
    )


def refresh_token(
    config: AppConfig,
    current: TokenSet,
    transport: Transport = urllib_transport,
    now: float | None = None,
) -> TokenSet:
    return _post_token_form(
        config,
        {
            "grant_type": "refresh_token",
            "refresh_token": current.refresh_token,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
        },
        transport,
        time.time() if now is None else now,
    )


def receive_loopback_callback(
    binding: LoopbackBinding,
    expected_state: str,
    timeout: float,
    on_ready: Callable[[], None] | None = None,
) -> dict[str, str]:
    captured: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - HTTP handler API
            parsed = urlparse(self.path)
            if parsed.path != binding.path:
                captured["error"] = "invalid_callback_path"
                status = 404
                body = b"Oura callback path did not match. Return to the terminal."
            else:
                query = parse_qs(parsed.query)
                for key in ("code", "state", "error", "error_description"):
                    if query.get(key):
                        captured[key] = query[key][0]
                status = 200 if captured.get("code") else 400
                body = b"Oura authorization received. You may return to the terminal."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        server = HTTPServer((binding.host, binding.port), CallbackHandler)
    except OSError as exc:
        raise OAuthError(
            f"Could not listen for the Oura callback on {binding.host}:{binding.port}."
        ) from exc
    try:
        server.timeout = timeout
        if on_ready is not None:
            on_ready()
        server.handle_request()
    finally:
        server.server_close()
    if not captured:
        raise OAuthError("Timed out waiting for the Oura authorization callback.")
    validate_callback(captured, expected_state)
    return captured


def authorize_profile(
    config_dir: Path,
    config: AppConfig,
    profile: str,
    *,
    transport: Transport = urllib_transport,
    callback_receiver: Callable[
        [LoopbackBinding, str, float], Mapping[str, str]
    ]
    | None = None,
    browser_opener: Callable[[str], bool] = webbrowser.open,
    url_presenter: Callable[[str], None] = print,
    open_browser: bool = False,
    timeout: float = 180.0,
    now: Callable[[], float] = time.time,
) -> dict[str, object]:
    label = validate_profile(profile)
    binding = validate_loopback_redirect(config.redirect_uri)
    state = label + "." + secrets.token_urlsafe(32)
    authorization_url = build_authorization_url(config, state)

    def present() -> None:
        url_presenter(authorization_url)
        if open_browser:
            browser_opener(authorization_url)

    if callback_receiver is None:
        callback = receive_loopback_callback(
            binding, state, timeout, on_ready=present
        )
    else:
        present()
        callback = callback_receiver(binding, state, timeout)
    code = validate_callback(callback, state)
    tokens = exchange_code(config, code, transport, now=now())
    write_profile_tokens(config_dir, label, tokens)
    return {
        "profile": label,
        "authorized": True,
        "granted_scopes": tokens.granted_scopes,
        "expires_at": tokens.expires_at,
    }


def _parse_iso_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise QueryError(f"Invalid ISO 8601 datetime: {value}") from exc


def validate_query(
    resource: str, query: Mapping[str, object]
) -> dict[str, object]:
    spec = RESOURCE_SPECS.get(resource)
    if spec is None:
        raise QueryError(f"Unknown Oura resource: {resource}")

    allowed: set[str] = set()
    if spec.temporal == "date":
        allowed.update(("start_date", "end_date"))
    elif spec.temporal == "datetime":
        allowed.update(("start_datetime", "end_datetime"))
    if spec.supports_latest:
        allowed.add("latest")
    if spec.supports_fields:
        allowed.add("fields")

    supplied = {key for key, value in query.items() if value is not None}
    unknown = sorted(supplied - allowed)
    if unknown:
        raise QueryError(
            f"Unsupported query parameter for {resource}: " + ", ".join(unknown)
        )

    normalized: dict[str, object] = {}
    for key in sorted(supplied):
        value = query[key]
        if key in {"start_date", "end_date"}:
            if not isinstance(value, str):
                raise QueryError(f"{key} must be an ISO date string.")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise QueryError(f"Invalid ISO date for {key}: {value}") from exc
            normalized[key] = value
        elif key in {"start_datetime", "end_datetime"}:
            if not isinstance(value, str):
                raise QueryError(f"{key} must be an ISO 8601 string.")
            _parse_iso_datetime(value)
            normalized[key] = value
        elif key == "latest":
            if not isinstance(value, bool):
                raise QueryError("latest must be a boolean.")
            normalized[key] = value
        elif key == "fields":
            if not isinstance(value, str) or not FIELDS_RE.fullmatch(value):
                raise QueryError("fields must be a comma-separated list of field names.")
            normalized[key] = value

    if "start_date" in normalized and "end_date" in normalized:
        if date.fromisoformat(str(normalized["start_date"])) > date.fromisoformat(
            str(normalized["end_date"])
        ):
            raise QueryError("start_date must not be later than end_date.")
    if "start_datetime" in normalized and "end_datetime" in normalized:
        start = _parse_iso_datetime(str(normalized["start_datetime"]))
        end = _parse_iso_datetime(str(normalized["end_datetime"]))
        try:
            out_of_order = start > end
        except TypeError as exc:
            raise QueryError(
                "start_datetime and end_datetime must use compatible time zones."
            ) from exc
        if out_of_order:
            raise QueryError("start_datetime must not be later than end_datetime.")
    return normalized


def _upstream_query(
    spec: ResourceSpec, normalized: Mapping[str, object]
) -> dict[str, object]:
    query = dict(normalized)
    if spec.upstream_end_date_exclusive and "end_date" in query:
        requested_end = date.fromisoformat(str(query["end_date"]))
        query["end_date"] = (requested_end + timedelta(days=1)).isoformat()
    return query


def _filter_requested_days(
    data: list[object], normalized: Mapping[str, object]
) -> list[object]:
    start = (
        date.fromisoformat(str(normalized["start_date"]))
        if "start_date" in normalized
        else None
    )
    end = (
        date.fromisoformat(str(normalized["end_date"]))
        if "end_date" in normalized
        else None
    )
    if start is None and end is None:
        return data

    filtered: list[object] = []
    for item in data:
        if not isinstance(item, Mapping) or not isinstance(item.get("day"), str):
            filtered.append(item)
            continue
        try:
            item_day = date.fromisoformat(item["day"])
        except ValueError:
            filtered.append(item)
            continue
        if start is not None and item_day < start:
            continue
        if end is not None and item_day > end:
            continue
        filtered.append(item)
    return filtered


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _api_json(response: HttpResponse) -> dict[str, object]:
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("Oura API returned malformed JSON.") from exc
    if not isinstance(payload, dict):
        raise ApiError("Oura API returned an unexpected JSON value.")
    return payload


class OuraClient:
    def __init__(
        self,
        config: AppConfig,
        profile: str,
        tokens: TokenSet,
        config_dir: Path,
        env: Mapping[str, str],
        *,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        random: Callable[[], float] = random_module.random,
        now: Callable[[], float] = time.time,
        warning: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.profile = validate_profile(profile)
        self.tokens = tokens
        self.config_dir = config_dir
        self.env = env
        self.transport = transport
        self.sleep = sleep
        self.random = random
        self.now = now
        self.warning = warning or (lambda _message: None)

    def _secret_values(self) -> list[str]:
        return [
            self.config.client_secret,
            self.tokens.access_token,
            self.tokens.refresh_token,
        ]

    def _rotate_tokens(self) -> None:
        was_persistable = self.tokens.persistable
        try:
            rotated = refresh_token(
                self.config,
                self.tokens,
                self.transport,
                now=self.now(),
            )
        except OAuthError as exc:
            raise ApiError(
                f"Could not refresh OAuth for profile {self.profile}; reauthorization is required."
            ) from exc
        rotated = replace(rotated, persistable=was_persistable)
        if was_persistable:
            try:
                write_profile_tokens(self.config_dir, self.profile, rotated)
            except ConfigError as exc:
                raise ApiError(
                    "Oura rotated the refresh token, but it could not be persisted safely; request stopped."
                ) from exc
        else:
            self.warning(
                "Refreshed process-supplied tokens are valid only for this command."
            )
        self.tokens = rotated

    def _request_with_retries(self, url: str) -> HttpResponse:
        attempt = 0
        while True:
            try:
                response = self.transport(
                    "GET",
                    url,
                    {"Authorization": f"Bearer {self.tokens.access_token}"},
                    None,
                    30.0,
                )
            except OSError as exc:
                raise ApiError("Oura API network request failed.") from exc

            retry_delay: float | None = None
            if response.status == 429 and attempt < 2:
                retry_after = _header(response.headers, "Retry-After")
                try:
                    retry_delay = min(60.0, max(0.0, float(retry_after or "")))
                except ValueError:
                    retry_delay = float(2**attempt)
            elif response.status in {500, 502, 503, 504} and attempt < 2:
                retry_delay = min(8.0, float(2**attempt) + self.random())
            if retry_delay is None:
                return response
            self.sleep(retry_delay)
            attempt += 1

    def _authorized_get(self, path: str, query: Mapping[str, object]) -> dict[str, object]:
        if self.tokens.expires_at <= self.now():
            self._rotate_tokens()

        encoded_values = {
            key: ("true" if value is True else "false" if value is False else str(value))
            for key, value in query.items()
        }
        url = f"{API_BASE_URL}/{path}"
        if encoded_values:
            url += "?" + urlencode(encoded_values)

        response = self._request_with_retries(url)
        if response.status == 401:
            self._rotate_tokens()
            response = self._request_with_retries(url)
            if response.status == 401:
                raise ApiError(
                    f"Oura authorization failed for profile {self.profile}; reauthorization is required."
                )
        if not 200 <= response.status < 300:
            try:
                detail = _structured_error(_api_json(response))
            except ApiError:
                detail = ""
            message = detail or f"Oura API returned HTTP {response.status}."
            raise ApiError(sanitize_error(message, self._secret_values()))
        return _api_json(response)

    def get(
        self,
        resource: str,
        query: Mapping[str, object],
        *,
        all_pages: bool,
    ) -> dict[str, object]:
        spec = RESOURCE_SPECS.get(resource)
        normalized = validate_query(resource, query)
        assert spec is not None
        page_query = _upstream_query(spec, normalized)
        merged: list[object] = []
        seen_tokens: set[str] = set()
        page_count = 0
        final_token: str | None = None

        while True:
            payload = self._authorized_get(spec.path, page_query)
            page_count += 1
            if resource == "personal_info":
                return {
                    "profile": self.profile,
                    "resource": resource,
                    "query": normalized,
                    "data": payload,
                    "next_token": None,
                    "page_count": 1,
                }
            data = payload.get("data")
            if not isinstance(data, list):
                raise ApiError(f"Oura resource {resource} did not return a data list.")
            merged.extend(data)
            raw_token = payload.get("next_token")
            if raw_token is not None and not isinstance(raw_token, str):
                raise ApiError(f"Oura resource {resource} returned an invalid next token.")
            final_token = raw_token
            if not all_pages or not final_token:
                break
            if final_token in seen_tokens:
                raise ApiError(f"Oura resource {resource} repeated a pagination token.")
            if page_count >= 100:
                raise ApiError(f"Oura resource {resource} exceeded 100 pages.")
            seen_tokens.add(final_token)
            page_query["next_token"] = final_token

        if spec.temporal == "date":
            merged = _filter_requested_days(merged, normalized)

        return {
            "profile": self.profile,
            "resource": resource,
            "query": normalized,
            "data": merged,
            "next_token": final_token,
            "page_count": page_count,
        }

    def daily(self, start_date: str, end_date: str) -> dict[str, object]:
        query = {"start_date": start_date, "end_date": end_date}
        results = {
            resource: self.get(resource, query, all_pages=True)["data"]
            for resource in ("daily_activity", "daily_readiness", "daily_sleep")
        }
        return {
            "profile": self.profile,
            "start_date": start_date,
            "end_date": end_date,
            **results,
        }


def _app_config_values(config: AppConfig) -> dict[str, str]:
    return {
        "OURA_CLIENT_ID": config.client_id,
        "OURA_CLIENT_SECRET": config.client_secret,
        "OURA_REDIRECT_URI": config.redirect_uri,
        "OURA_SCOPES": config.scopes,
        "OURA_DEFAULT_PROFILE": config.default_profile,
    }


def configure_app_interactive(
    config_dir: Path,
    *,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] = getpass.getpass,
) -> dict[str, object]:
    existing = parse_env_file(config_dir / "app.env")
    existing_id = existing.get("OURA_CLIENT_ID", "")
    existing_secret = existing.get("OURA_CLIENT_SECRET", "")
    existing_redirect = existing.get("OURA_REDIRECT_URI", DEFAULT_REDIRECT_URI)
    existing_scopes = existing.get("OURA_SCOPES", DEFAULT_SCOPES)
    existing_profile = existing.get("OURA_DEFAULT_PROFILE", DEFAULT_PROFILE)

    client_id_prompt = "Oura Client ID"
    if existing_id:
        client_id_prompt += " [press Enter to keep configured value]"
    client_id = input_fn(client_id_prompt + ": ").strip() or existing_id

    secret_prompt = "Oura Client Secret"
    if existing_secret:
        secret_prompt += " [press Enter to keep configured value]"
    client_secret = secret_fn(secret_prompt + ": ").strip() or existing_secret

    redirect_uri = (
        input_fn(f"Redirect URI [{existing_redirect}]: ").strip()
        or existing_redirect
    )
    scopes = input_fn(f"OAuth scopes [{existing_scopes}]: ").strip() or existing_scopes
    default_profile = (
        input_fn(f"Default profile [{existing_profile}]: ").strip()
        or existing_profile
    )

    if not client_id:
        raise ConfigError("Oura Client ID cannot be empty.")
    if not client_secret:
        raise ConfigError("Oura Client Secret cannot be empty.")
    validate_loopback_redirect(redirect_uri)
    validate_profile(default_profile)
    if not scopes.strip():
        raise ConfigError("OAuth scopes cannot be empty.")

    config = AppConfig(
        client_id,
        client_secret,
        redirect_uri,
        " ".join(scopes.split()),
        default_profile,
    )
    write_app_config(config_dir, _app_config_values(config))
    return {
        "configured": True,
        "redirect_uri": config.redirect_uri,
        "scopes": config.scopes,
        "default_profile": config.default_profile,
    }


def status_payload(
    config_dir: Path,
    env: Mapping[str, str],
    explicit_profile: str | None = None,
    *,
    now: float | None = None,
) -> tuple[dict[str, object], int]:
    try:
        config = load_app_config(config_dir, env)
    except ConfigError:
        return {
            "configured": False,
            "profile": explicit_profile or env.get("OURA_PROFILE") or DEFAULT_PROFILE,
            "profile_authorized": False,
        }, 2
    profile = resolve_profile(explicit_profile, config, env)
    try:
        tokens = load_profile_tokens(config_dir, profile, env)
    except ConfigError:
        return {
            "configured": True,
            "profile": profile,
            "profile_authorized": False,
            "redirect_uri": config.redirect_uri,
        }, 2
    current_time = time.time() if now is None else now
    return {
        "configured": True,
        "profile": profile,
        "profile_authorized": True,
        "token_expired": tokens.expires_at <= current_time,
        "granted_scopes": tokens.granted_scopes,
        "redirect_uri": config.redirect_uri,
    }, 0


def profiles_payload(
    config_dir: Path,
    env: Mapping[str, str],
    *,
    now: float | None = None,
) -> dict[str, object]:
    config = load_app_config(config_dir, env)
    current_time = time.time() if now is None else now
    profiles: list[dict[str, object]] = []
    directory = config_dir / "profiles"
    if directory.exists():
        for path in sorted(directory.glob("*.env")):
            label = path.stem
            try:
                validate_profile(label)
                tokens = load_profile_tokens(config_dir, label, {})
            except ConfigError:
                profiles.append(
                    {
                        "label": label,
                        "default": label == config.default_profile,
                        "authorized": False,
                    }
                )
                continue
            profiles.append(
                {
                    "label": label,
                    "default": label == config.default_profile,
                    "authorized": True,
                    "token_expired": tokens.expires_at <= current_time,
                    "granted_scopes": tokens.granted_scopes,
                }
            )
    return {"default_profile": config.default_profile, "profiles": profiles}


def resources_payload() -> dict[str, object]:
    return {
        "resources": {
            name: asdict(spec) for name, spec in sorted(RESOURCE_SPECS.items())
        }
    }


def set_default_profile(
    config_dir: Path, env: Mapping[str, str], profile: str
) -> dict[str, object]:
    config = load_app_config(config_dir, env)
    label = validate_profile(profile)
    updated = replace(config, default_profile=label)
    write_app_config(config_dir, _app_config_values(updated))
    return {"default_profile": label}


def _client_for_profile(
    config_dir: Path,
    env: Mapping[str, str],
    explicit_profile: str | None,
) -> OuraClient:
    config = load_app_config(config_dir, env)
    profile = resolve_profile(explicit_profile, config, env)
    tokens = load_profile_tokens(config_dir, profile, env)
    return OuraClient(
        config,
        profile,
        tokens,
        config_dir,
        env,
        warning=lambda message: print(message, file=sys.stderr),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oura.py", description="Secure Oura Cloud API V2 access"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("configure", help="Configure the Oura OAuth application")

    authorize_parser = subparsers.add_parser(
        "authorize", help="Authorize one named Oura profile"
    )
    authorize_parser.add_argument("--profile")
    authorize_parser.add_argument("--open-browser", action="store_true")
    authorize_parser.add_argument("--timeout", type=float, default=180.0)

    status_parser = subparsers.add_parser(
        "status", help="Show nonsecret configuration and authorization status"
    )
    status_parser.add_argument("--profile")

    subparsers.add_parser("profiles", help="List configured profile labels")

    default_parser = subparsers.add_parser(
        "default-profile", help="Change the default profile label"
    )
    default_parser.add_argument("profile")

    subparsers.add_parser("resources", help="List allowlisted Oura resources")

    get_parser = subparsers.add_parser("get", help="Retrieve one Oura resource")
    get_parser.add_argument("resource")
    get_parser.add_argument("--profile")
    get_parser.add_argument("--start-date")
    get_parser.add_argument("--end-date")
    get_parser.add_argument("--start-datetime")
    get_parser.add_argument("--end-datetime")
    get_parser.add_argument("--latest", action="store_true")
    get_parser.add_argument("--fields")
    get_parser.add_argument("--all-pages", action="store_true")

    daily_parser = subparsers.add_parser(
        "daily", help="Retrieve daily activity, readiness, and sleep"
    )
    daily_parser.add_argument("--profile")
    daily_parser.add_argument("--start-date", required=True)
    daily_parser.add_argument("--end-date", required=True)
    return parser


def _query_from_args(args: argparse.Namespace) -> dict[str, object]:
    query: dict[str, object] = {}
    for name in (
        "start_date",
        "end_date",
        "start_datetime",
        "end_datetime",
        "fields",
    ):
        value = getattr(args, name, None)
        if value is not None:
            query[name] = value
    if getattr(args, "latest", False):
        query["latest"] = True
    return query


def _write_json(payload: Mapping[str, object]) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_dir = default_config_dir()
    env = os.environ
    try:
        if args.command == "configure":
            payload = configure_app_interactive(config_dir)
            exit_code = 0
        elif args.command == "authorize":
            if args.timeout <= 0:
                raise ConfigError("Authorization timeout must be positive.")
            config = load_app_config(config_dir, env)
            profile = resolve_profile(args.profile, config, env)
            payload = authorize_profile(
                config_dir,
                config,
                profile,
                url_presenter=lambda url: print(
                    "Open this Oura authorization URL:\n" + url,
                    file=sys.stderr,
                ),
                open_browser=args.open_browser,
                timeout=args.timeout,
            )
            exit_code = 0
        elif args.command == "status":
            payload, exit_code = status_payload(config_dir, env, args.profile)
        elif args.command == "profiles":
            payload = profiles_payload(config_dir, env)
            exit_code = 0
        elif args.command == "default-profile":
            payload = set_default_profile(config_dir, env, args.profile)
            exit_code = 0
        elif args.command == "resources":
            payload = resources_payload()
            exit_code = 0
        elif args.command == "get":
            query = _query_from_args(args)
            validate_query(args.resource, query)
            client = _client_for_profile(config_dir, env, args.profile)
            payload = client.get(args.resource, query, all_pages=args.all_pages)
            exit_code = 0
        elif args.command == "daily":
            validate_query(
                "daily_sleep",
                {"start_date": args.start_date, "end_date": args.end_date},
            )
            client = _client_for_profile(config_dir, env, args.profile)
            payload = client.daily(args.start_date, args.end_date)
            exit_code = 0
        else:
            parser.error("unknown command")
            return 2
        _write_json(payload)
        return exit_code
    except (ConfigError, OAuthError, QueryError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ApiError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
