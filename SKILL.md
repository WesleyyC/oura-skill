---
name: oura
description: Use when a user asks to retrieve, inspect, compare, export, summarize, or analyze Oura Ring or Oura Cloud API V2 data, including sleep, readiness, activity, heart rate, workouts, stress, SpO2, resilience, tags, sessions, or ring information.
---

# Oura

Use the bundled dependency-free Python CLI for Oura Cloud API V2 access. It handles OAuth, refresh-token rotation, pagination, bounded retries, and isolated profiles. Prefer it to direct `curl` calls.

## Safety contract

- Never ask the user to paste a client secret, access token, refresh token, authorization code, or callback URL containing a code into chat.
- Have the user run `configure` and `authorize` in their own terminal. `configure` obtains the client secret with a hidden prompt and saves private files locally.
- Never pass secrets as command-line arguments, print configuration files, or expose raw Oura responses unless the user explicitly asks for the raw data.
- Treat Oura output as sensitive health data. Return only what the request needs and do not persist or publish it unless explicitly asked.
- Use `default` when no profile is specified. Before accessing another profile, name it explicitly and confirm the intended profile.
- Keep OAuth tokens and records isolated by profile. Only combine profiles for an explicitly requested comparison, and label every result.
- Treat absent records as missing data, not zero. State the profile and requested date or datetime range in summaries.

## Locate the CLI

Resolve the CLI from the skill that was actually loaded: take the absolute directory containing this `SKILL.md`, append `scripts/oura.py`, and use that absolute path for every command. Do not assume a particular installation directory or invoke a different checkout.

```bash
python3 /absolute/path/to/the/loaded/oura-skill/scripts/oura.py COMMAND
```

The CLI uses only the Python standard library; do not install packages for normal use.

## Workflow

1. Run `status`. This is safe before configuration and never prints credentials.
2. If app credentials are missing, ask the user to run `configure` in their own terminal. The default profile is `default`, and the default redirect URI is `http://localhost:8910/callback`.
3. If the selected profile is not authorized, ask the user to run `authorize --profile default --open-browser`. The loopback listener validates OAuth state before saving tokens.
4. For a daily overview, run `daily --start-date YYYY-MM-DD --end-date YYYY-MM-DD --profile default`. Both endpoints are inclusive, and the result contains activity, readiness, and sleep for one profile.
5. For other data, run `resources`, then `get RESOURCE` with the supported date, datetime, `--latest`, `--fields`, or `--all-pages` options. Pass the user's actual end date; the CLI normalizes endpoint-specific Oura behavior internally.
6. Read [references/endpoints.md](references/endpoints.md) for the allowlisted resource map and expected OAuth scopes.

To add another person or ring account, authorize a separate profile such as `secondary`. The Oura application credentials are shared locally, while OAuth tokens remain profile-specific. Change the default only when explicitly requested with `default-profile PROFILE`.

## Error handling

- `401`: the CLI refreshes once. If authorization still fails, ask the user to reauthorize that profile.
- `403`: explain that OAuth consent may lack the resource's scope; do not claim the data is absent.
- `429` or `5xx`: the CLI retries within a fixed bound. Report failure without an unbounded retry loop.
- Empty results: mention the requested range, possible Oura sync delay, and that missing data is not a zero value.
- Network failure: report it as a connectivity problem before suggesting reauthorization.

Use the [official Oura API V2 documentation](https://cloud.ouraring.com/v2/docs) when current API behavior must be verified.
