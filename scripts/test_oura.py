from __future__ import annotations

from dataclasses import dataclass, replace
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

SCRIPT_PATH = Path(__file__).with_name("oura.py")
SPEC = importlib.util.spec_from_file_location("oura_cli", SCRIPT_PATH)
assert SPEC and SPEC.loader
oura = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = oura
SPEC.loader.exec_module(oura)


class ScaffoldTests(unittest.TestCase):
    def test_main_is_callable(self) -> None:
        self.assertTrue(callable(oura.main))


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tempdir.name) / "oura"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_environment_overrides_app_file_and_default_is_default(self) -> None:
        oura.write_app_config(
            self.config_dir,
            {
                "OURA_CLIENT_ID": "file-id",
                "OURA_CLIENT_SECRET": "file-secret",
                "OURA_REDIRECT_URI": "http://localhost:8910/callback",
                "OURA_SCOPES": oura.DEFAULT_SCOPES,
                "OURA_DEFAULT_PROFILE": "default",
            },
        )
        config = oura.load_app_config(
            self.config_dir,
            {"OURA_CLIENT_ID": "env-id"},
        )
        self.assertEqual(config.client_id, "env-id")
        self.assertEqual(config.client_secret, "file-secret")
        self.assertEqual(config.default_profile, "default")

    def test_private_files_and_directories_have_exact_modes(self) -> None:
        oura.write_app_config(self.config_dir, oura.example_app_values())
        oura.write_profile_tokens(
            self.config_dir,
            "default",
            oura.TokenSet("access", "refresh", 1234.0, "daily personal"),
        )
        app_mode = stat.S_IMODE((self.config_dir / "app.env").stat().st_mode)
        token_mode = stat.S_IMODE(
            (self.config_dir / "profiles" / "default.env").stat().st_mode
        )
        self.assertEqual(app_mode, 0o600)
        self.assertEqual(token_mode, 0o600)
        self.assertEqual(stat.S_IMODE(self.config_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.config_dir / "profiles").stat().st_mode), 0o700
        )

    def test_profile_paths_cannot_escape_profile_directory(self) -> None:
        for label in ("../secondary", "Default", "secondary/name", "", "."):
            with self.subTest(label=label), self.assertRaises(oura.ConfigError):
                oura.validate_profile(label)

    def test_profile_token_files_are_isolated(self) -> None:
        oura.write_profile_tokens(
            self.config_dir,
            "default",
            oura.TokenSet("w-access", "w-refresh", 1234.0, "daily"),
        )
        oura.write_profile_tokens(
            self.config_dir,
            "secondary",
            oura.TokenSet("x-access", "x-refresh", 5678.0, "daily"),
        )
        self.assertEqual(
            oura.load_profile_tokens(self.config_dir, "default", {}).access_token,
            "w-access",
        )
        self.assertEqual(
            oura.load_profile_tokens(self.config_dir, "secondary", {}).access_token,
            "x-access",
        )

    def test_app_env_file_override_is_loaded_before_process_values(self) -> None:
        alternate = Path(self.tempdir.name) / "alternate.env"
        oura.atomic_write_env(alternate, oura.example_app_values())
        config = oura.load_app_config(
            self.config_dir,
            {"OURA_APP_ENV_FILE": str(alternate), "OURA_CLIENT_ID": "process-id"},
        )
        self.assertEqual(config.client_id, "process-id")
        self.assertEqual(config.client_secret, "example-secret")

    def test_process_token_override_is_never_marked_persistable(self) -> None:
        oura.write_profile_tokens(
            self.config_dir,
            "default",
            oura.TokenSet("file-access", "file-refresh", 1234.0, "daily"),
        )
        tokens = oura.load_profile_tokens(
            self.config_dir,
            "default",
            {
                "OURA_ACCESS_TOKEN": "process-access",
                "OURA_REFRESH_TOKEN": "process-refresh",
                "OURA_TOKEN_EXPIRES_AT": "5678",
                "OURA_GRANTED_SCOPES": "daily",
            },
        )
        self.assertEqual(tokens.access_token, "process-access")
        self.assertFalse(tokens.persistable)

    def test_profile_file_label_must_match_selected_profile(self) -> None:
        path = self.config_dir / "profiles" / "default.env"
        oura.atomic_write_env(
            path,
            {
                "OURA_PROFILE": "secondary",
                "OURA_ACCESS_TOKEN": "access",
                "OURA_REFRESH_TOKEN": "refresh",
                "OURA_TOKEN_EXPIRES_AT": "1234",
                "OURA_GRANTED_SCOPES": "daily",
            },
        )
        with self.assertRaises(oura.ConfigError):
            oura.load_profile_tokens(self.config_dir, "default", {})

    def test_env_parser_does_not_execute_shell_syntax(self) -> None:
        path = Path(self.tempdir.name) / "safe.env"
        path.write_text("OURA_CLIENT_ID='$(touch should-not-exist)'\n", encoding="utf-8")
        values = oura.parse_env_file(path)
        self.assertEqual(values["OURA_CLIENT_ID"], "$(touch should-not-exist)")
        self.assertFalse((Path.cwd() / "should-not-exist").exists())


class OAuthTests(unittest.TestCase):
    def test_authorization_url_uses_exact_redirect_scopes_and_state(self) -> None:
        config = oura.AppConfig("id", "secret")
        url = oura.build_authorization_url(config, "default.random-state")
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.geturl().split("?", 1)[0], oura.AUTHORIZE_URL)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["redirect_uri"], ["http://localhost:8910/callback"])
        self.assertEqual(query["scope"], [oura.DEFAULT_SCOPES])
        self.assertEqual(query["state"], ["default.random-state"])
        self.assertNotIn("secret", url)

    def test_redirect_validation_accepts_only_loopback_http(self) -> None:
        binding = oura.validate_loopback_redirect("http://localhost:8910/callback")
        self.assertEqual(
            (binding.host, binding.port, binding.path),
            ("localhost", 8910, "/callback"),
        )
        for uri in (
            "https://example.com/callback",
            "http://localhost/callback",
            "file:///callback",
            "http://user@localhost:8910/callback",
            "http://localhost:8910/callback?secret=value",
        ):
            with self.subTest(uri=uri), self.assertRaises(oura.ConfigError):
                oura.validate_loopback_redirect(uri)

    def test_token_response_requires_rotating_pair_and_computes_expiry(self) -> None:
        token = oura.token_from_response(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "scope": "daily personal",
            },
            now=1000.0,
        )
        self.assertEqual(token.expires_at, 4540.0)
        self.assertEqual(token.refresh_token, "refresh")
        with self.assertRaises(oura.OAuthError):
            oura.token_from_response(
                {"access_token": "access", "expires_in": 3600}, now=1000.0
            )

    def test_state_mismatch_never_exchanges_or_writes_tokens(self) -> None:
        callback = {"code": "code", "state": "secondary.other"}
        with self.assertRaises(oura.OAuthError):
            oura.validate_callback(callback, "default.expected")

    def test_exchange_code_posts_form_without_leaking_secret_in_error(self) -> None:
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append((method, url, dict(headers), body, timeout))
            return oura.HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "access_token": "access",
                        "refresh_token": "refresh",
                        "expires_in": 3600,
                        "scope": "daily",
                    }
                ).encode("utf-8"),
            )

        token = oura.exchange_code(
            oura.AppConfig("client-id", "client-secret"),
            "authorization-code",
            transport,
            now=1000.0,
        )
        form = parse_qs(calls[0][3].decode("utf-8"))
        self.assertEqual(calls[0][0:2], ("POST", oura.TOKEN_URL))
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual(form["redirect_uri"], [oura.DEFAULT_REDIRECT_URI])
        self.assertEqual(token.access_token, "access")

    def test_two_authorizations_write_only_the_selected_profile(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config_dir = Path(temporary.name) / "oura"
        config = oura.AppConfig("id", "secret")
        token_responses = iter(
            [
                {
                    "access_token": "default-a",
                    "refresh_token": "default-r",
                    "expires_in": 3600,
                    "scope": "daily",
                },
                {
                    "access_token": "secondary-a",
                    "refresh_token": "secondary-r",
                    "expires_in": 3600,
                    "scope": "daily",
                },
            ]
        )

        def transport(method, url, headers, body, timeout):
            return oura.HttpResponse(
                200, {}, json.dumps(next(token_responses)).encode("utf-8")
            )

        def callback_receiver(binding, state, timeout):
            profile = state.split(".", 1)[0]
            return {"code": profile + "-code", "state": state}

        displayed_urls: list[str] = []
        for profile in ("default", "secondary"):
            oura.authorize_profile(
                config_dir,
                config,
                profile,
                transport=transport,
                callback_receiver=callback_receiver,
                browser_opener=lambda _: True,
                url_presenter=displayed_urls.append,
                open_browser=False,
                timeout=30.0,
                now=lambda: 1000.0,
            )
        self.assertEqual(
            oura.load_profile_tokens(config_dir, "default", {}).access_token,
            "default-a",
        )
        self.assertEqual(
            oura.load_profile_tokens(config_dir, "secondary", {}).access_token,
            "secondary-a",
        )
        self.assertEqual(len(displayed_urls), 2)
        self.assertTrue(all("secret" not in url for url in displayed_urls))


@dataclass(frozen=True)
class RecordedCall:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout: float


class SequenceTransport:
    def __init__(self, responses: list[oura.HttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[RecordedCall] = []

    def __call__(self, method, url, headers, body, timeout) -> oura.HttpResponse:
        self.calls.append(RecordedCall(method, url, dict(headers), body, timeout))
        if not self.responses:
            raise AssertionError("Transport received more calls than expected")
        return self.responses.pop(0)


def response(
    status: int, payload: dict[str, object], **headers: str
) -> oura.HttpResponse:
    return oura.HttpResponse(status, headers, json.dumps(payload).encode("utf-8"))


CLIENT_TEMP_DIRS: list[tempfile.TemporaryDirectory[str]] = []


def make_client(transport: oura.Transport) -> oura.OuraClient:
    temporary = tempfile.TemporaryDirectory()
    CLIENT_TEMP_DIRS.append(temporary)
    config_dir = Path(temporary.name) / "oura"
    config = oura.AppConfig("client-id", "client-secret")
    tokens = oura.TokenSet(
        "old-access", "old-refresh", 4_000_000_000.0, "daily"
    )
    oura.write_app_config(
        config_dir,
        {
            "OURA_CLIENT_ID": config.client_id,
            "OURA_CLIENT_SECRET": config.client_secret,
            "OURA_REDIRECT_URI": config.redirect_uri,
            "OURA_SCOPES": config.scopes,
            "OURA_DEFAULT_PROFILE": config.default_profile,
        },
    )
    oura.write_profile_tokens(config_dir, "default", tokens)
    client = oura.OuraClient(
        config,
        "default",
        tokens,
        config_dir,
        {},
        transport=transport,
        sleep=lambda _: None,
        random=lambda: 0.0,
        now=lambda: 1_000.0,
    )
    return client


def tearDownModule() -> None:
    while CLIENT_TEMP_DIRS:
        CLIENT_TEMP_DIRS.pop().cleanup()


class ApiClientTests(unittest.TestCase):
    def test_unknown_resource_cannot_select_an_arbitrary_url(self) -> None:
        with self.assertRaises(oura.QueryError):
            oura.validate_query("https://example.com/steal", {})

    def test_date_and_datetime_parameters_are_not_interchangeable(self) -> None:
        oura.validate_query(
            "daily_sleep",
            {"start_date": "2026-07-01", "end_date": "2026-07-02"},
        )
        with self.assertRaises(oura.QueryError):
            oura.validate_query(
                "daily_sleep", {"start_datetime": "2026-07-01T00:00:00Z"}
            )
        with self.assertRaises(oura.QueryError):
            oura.validate_query("heartrate", {"start_date": "2026-07-01"})

    def test_half_open_resources_advance_the_upstream_end_date(self) -> None:
        requested = {"start_date": "2026-07-12", "end_date": "2026-07-12"}

        for resource in ("daily_activity", "sleep", "workout"):
            with self.subTest(resource=resource):
                transport = SequenceTransport([response(200, {"data": []})])
                result = make_client(transport).get(
                    resource,
                    requested,
                    all_pages=False,
                )
                query = parse_qs(urlparse(transport.calls[0].url).query)

                self.assertEqual(query["start_date"], ["2026-07-12"])
                self.assertEqual(query["end_date"], ["2026-07-13"])
                self.assertEqual(result["query"], requested)

    def test_inclusive_resources_preserve_the_upstream_end_date(self) -> None:
        requested = {"start_date": "2026-07-12", "end_date": "2026-07-12"}

        for resource in ("daily_readiness", "daily_sleep", "daily_stress"):
            with self.subTest(resource=resource):
                transport = SequenceTransport([response(200, {"data": []})])
                make_client(transport).get(resource, requested, all_pages=False)
                query = parse_qs(urlparse(transport.calls[0].url).query)

                self.assertEqual(query["end_date"], ["2026-07-12"])

    def test_half_open_resources_filter_spillover_days(self) -> None:
        requested = {"start_date": "2026-07-12", "end_date": "2026-07-12"}

        for resource in ("daily_activity", "sleep", "workout"):
            with self.subTest(resource=resource):
                transport = SequenceTransport(
                    [
                        response(
                            200,
                            {
                                "data": [
                                    {"day": "2026-07-12", "id": "inside"},
                                    {"day": "2026-07-13", "id": "spillover"},
                                ]
                            },
                        )
                    ]
                )
                result = make_client(transport).get(
                    resource,
                    requested,
                    all_pages=False,
                )

                self.assertEqual(
                    [row["id"] for row in result["data"]],
                    ["inside"],
                )

    def test_pagination_merges_data_and_stops_at_last_page(self) -> None:
        transport = SequenceTransport(
            [
                response(
                    200,
                    {"data": [{"day": "2026-07-01"}], "next_token": "next"},
                ),
                response(
                    200,
                    {"data": [{"day": "2026-07-02"}], "next_token": None},
                ),
            ]
        )
        client = make_client(transport)
        result = client.get("daily_sleep", {}, all_pages=True)
        self.assertEqual(
            [row["day"] for row in result["data"]],
            ["2026-07-01", "2026-07-02"],
        )
        self.assertEqual(result["page_count"], 2)
        self.assertIn("next_token=next", transport.calls[1].url)

    def test_401_refreshes_once_persists_rotation_then_retries(self) -> None:
        transport = SequenceTransport(
            [
                response(401, {"title": "expired"}),
                response(
                    200,
                    {
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "expires_in": 3600,
                        "scope": "daily",
                    },
                ),
                response(200, {"data": []}),
            ]
        )
        client = make_client(transport)
        result = client.get("daily_sleep", {}, all_pages=False)
        self.assertEqual(result["data"], [])
        self.assertEqual(client.tokens.refresh_token, "new-refresh")
        self.assertEqual(
            transport.calls[2].headers["Authorization"], "Bearer new-access"
        )
        persisted = oura.load_profile_tokens(client.config_dir, "default", {})
        self.assertEqual(persisted.refresh_token, "new-refresh")

    def test_expired_token_refreshes_before_first_api_request(self) -> None:
        transport = SequenceTransport(
            [
                response(
                    200,
                    {
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "expires_in": 3600,
                        "scope": "daily",
                    },
                ),
                response(200, {"data": []}),
            ]
        )
        client = make_client(transport)
        client.tokens = replace(client.tokens, expires_at=999.0)
        client.get("daily_sleep", {}, all_pages=False)
        self.assertEqual(transport.calls[0].url, oura.TOKEN_URL)
        self.assertEqual(
            transport.calls[1].headers["Authorization"], "Bearer new-access"
        )

    def test_rotated_token_write_failure_stops_before_api_retry(self) -> None:
        transport = SequenceTransport(
            [
                response(401, {"title": "expired"}),
                response(
                    200,
                    {
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "expires_in": 3600,
                        "scope": "daily",
                    },
                ),
                response(200, {"data": []}),
            ]
        )
        client = make_client(transport)
        with patch.object(
            oura,
            "write_profile_tokens",
            side_effect=oura.ConfigError("fixture write failure"),
        ):
            with self.assertRaisesRegex(oura.ApiError, "could not be persisted"):
                client.get("daily_sleep", {}, all_pages=False)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(client.tokens.refresh_token, "old-refresh")

    def test_error_text_redacts_every_known_secret(self) -> None:
        text = oura.sanitize_error(
            "client-secret access-token refresh-token",
            ["client-secret", "access-token", "refresh-token"],
        )
        self.assertNotIn("secret", text)
        self.assertNotIn("access-token", text)
        self.assertNotIn("refresh-token", text)

    def test_429_honors_retry_after_with_bounded_retry(self) -> None:
        transport = SequenceTransport(
            [
                response(429, {"title": "limited"}, **{"Retry-After": "2"}),
                response(200, {"data": []}),
            ]
        )
        sleeps: list[float] = []
        client = make_client(transport)
        client.sleep = sleeps.append
        client.get("daily_sleep", {}, all_pages=False)
        self.assertEqual(sleeps, [2.0])

    def test_403_preserves_structured_detail_without_raw_body(self) -> None:
        transport = SequenceTransport(
            [response(403, {"title": "Forbidden", "detail": "Missing daily scope"})]
        )
        with self.assertRaisesRegex(oura.ApiError, "Missing daily scope"):
            make_client(transport).get("daily_sleep", {}, all_pages=False)

    def test_5xx_retries_twice_then_stops(self) -> None:
        transport = SequenceTransport(
            [
                response(503, {"title": "unavailable"}),
                response(503, {"title": "unavailable"}),
                response(503, {"title": "unavailable"}),
            ]
        )
        with self.assertRaises(oura.ApiError):
            make_client(transport).get("daily_sleep", {}, all_pages=False)
        self.assertEqual(len(transport.calls), 3)

    def test_malformed_json_and_network_errors_become_api_errors(self) -> None:
        malformed = SequenceTransport([oura.HttpResponse(200, {}, b"not-json")])
        with self.assertRaises(oura.ApiError):
            make_client(malformed).get("daily_sleep", {}, all_pages=False)

        def offline(method, url, headers, body, timeout):
            raise OSError("fixture offline")

        with self.assertRaisesRegex(oura.ApiError, "network"):
            make_client(offline).get("daily_sleep", {}, all_pages=False)

    def test_daily_combines_three_resources_for_one_profile(self) -> None:
        transport = SequenceTransport(
            [
                response(200, {"data": [{"day": "2026-07-01", "score": 80}]}),
                response(200, {"data": [{"day": "2026-07-01", "score": 81}]}),
                response(200, {"data": [{"day": "2026-07-01", "score": 82}]}),
            ]
        )
        result = make_client(transport).daily("2026-07-01", "2026-07-01")
        self.assertEqual(result["profile"], "default")
        self.assertEqual(result["daily_activity"][0]["score"], 80)
        self.assertEqual(result["daily_readiness"][0]["score"], 81)
        self.assertEqual(result["daily_sleep"][0]["score"], 82)
        queries = [parse_qs(urlparse(call.url).query) for call in transport.calls]
        self.assertEqual(queries[0]["end_date"], ["2026-07-02"])
        self.assertEqual(queries[1]["end_date"], ["2026-07-01"])
        self.assertEqual(queries[2]["end_date"], ["2026-07-01"])


def run_cli(*args: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OURA_")
    }
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def prepare_cli_config(home: Path, default_profile: str = "default") -> Path:
    config_dir = home / ".config" / "oura"
    values = oura.example_app_values()
    values["OURA_DEFAULT_PROFILE"] = default_profile
    oura.write_app_config(config_dir, values)
    return config_dir


def write_cli_profile(config_dir: Path, profile: str, access: str) -> None:
    oura.write_profile_tokens(
        config_dir,
        profile,
        oura.TokenSet(
            access, "fixture-refresh", 4_000_000_000.0, "daily personal"
        ),
    )


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_resources_prints_json_without_configuration(self) -> None:
        result = run_cli("resources", home=self.home)
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertIn("daily_sleep", payload["resources"])

    def test_status_without_configuration_is_nonsecret_and_nonzero(self) -> None:
        result = run_cli("status", home=self.home)
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["configured"], False)
        self.assertNotIn("token", result.stderr.lower())

    def test_profiles_lists_default_as_default_without_token_values(self) -> None:
        config_dir = prepare_cli_config(self.home, default_profile="default")
        write_cli_profile(config_dir, "default", "visible-only-in-fixture")
        result = run_cli("profiles", home=self.home)
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["profiles"][0]["label"], "default")
        self.assertTrue(payload["profiles"][0]["default"])
        self.assertNotIn(
            "visible-only-in-fixture", result.stdout + result.stderr
        )

    def test_get_rejects_unknown_resource_before_network_access(self) -> None:
        result = run_cli("get", "../personal_info", home=self.home)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown Oura resource", result.stderr)

    def test_configure_uses_hidden_secret_prompt_and_writes_default_default(self) -> None:
        visible_answers = iter(
            [
                "client-id",
                "",
                "",
                "",
            ]
        )
        visible_prompts: list[str] = []
        secret_prompts: list[str] = []

        def input_fn(prompt: str) -> str:
            visible_prompts.append(prompt)
            return next(visible_answers)

        def secret_fn(prompt: str) -> str:
            secret_prompts.append(prompt)
            return "client-secret-value"

        payload = oura.configure_app_interactive(
            self.home / ".config" / "oura",
            input_fn=input_fn,
            secret_fn=secret_fn,
        )
        config = oura.load_app_config(self.home / ".config" / "oura", {})
        self.assertEqual(config.default_profile, "default")
        self.assertEqual(config.redirect_uri, "http://localhost:8910/callback")
        self.assertEqual(payload["default_profile"], "default")
        self.assertEqual(len(secret_prompts), 1)
        self.assertNotIn(
            "client-secret-value", "".join(visible_prompts + secret_prompts)
        )

    def test_default_profile_command_preserves_shared_credentials(self) -> None:
        prepare_cli_config(self.home, default_profile="default")
        result = run_cli("default-profile", "secondary", home=self.home)
        self.assertEqual(result.returncode, 0)
        config = oura.load_app_config(self.home / ".config" / "oura", {})
        self.assertEqual(config.client_secret, "example-secret")
        self.assertEqual(config.default_profile, "secondary")


if __name__ == "__main__":
    unittest.main()
