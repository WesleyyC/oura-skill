# Oura skill

A privacy-conscious Oura Cloud API V2 skill for Codex and Claude Code. It includes a standard-library Python CLI for OAuth, profile-isolated tokens, pagination, retries, and allowlisted health-data queries.

## Requirements

- macOS or Linux
- Python 3.10 or newer
- Git
- An Oura account and an OAuth application in the [Oura developer portal](https://cloud.ouraring.com/oauth/applications)

No Python package installation is required. The runtime uses only the standard library.

## Install for Codex and Claude Code

```bash
git clone https://github.com/WesleyyC/oura-skill.git ~/.local/share/oura-skill
~/.local/share/oura-skill/install.sh --all
```

The installer creates these links without copying credentials or configuration:

- Codex: `~/.agents/skills/oura`
- Claude Code: `~/.claude/skills/oura`

Use `--codex` or `--claude` to install for only one client. Re-running the same command is safe. The installer refuses to replace a file, directory, dangling link, or link to another checkout.

Restart Codex or Claude Code after installation so it discovers the skill.

## Invoke the skill

- In Codex CLI or the IDE extension, mention `$oura` in your prompt. Codex can also select it automatically for Oura requests.
- In Claude Code, enter `/oura`. Claude can also select it automatically when the request matches the skill description.

See the official [Codex skills guide](https://developers.openai.com/codex/skills) and [Claude Code skills guide](https://code.claude.com/docs/en/skills) for client-specific behavior.

## Configure Oura OAuth

1. Create an OAuth application in the [Oura developer portal](https://cloud.ouraring.com/oauth/applications).
2. Add this exact redirect URI to the application: `http://localhost:8910/callback`.
3. Run configuration in your terminal:

   ```bash
   python3 ~/.local/share/oura-skill/scripts/oura.py configure
   ```

   Enter the Oura client ID and client secret when prompted. The secret prompt is hidden. Keep the default scopes unless you intentionally want to limit access.

4. Authorize the default profile:

   ```bash
   python3 ~/.local/share/oura-skill/scripts/oura.py authorize --profile default --open-browser
   ```

5. Confirm nonsecret status:

   ```bash
   python3 ~/.local/share/oura-skill/scripts/oura.py status --profile default
   ```

Oura requires the authorization callback to match a whitelisted redirect URI exactly. See Oura's official [authentication guide](https://cloud.ouraring.com/docs/authentication) and [API V2 documentation](https://cloud.ouraring.com/v2/docs) for current requirements.

## Use the CLI

```bash
# List supported resources without contacting Oura
python3 ~/.local/share/oura-skill/scripts/oura.py resources

# Inclusive daily overview
python3 ~/.local/share/oura-skill/scripts/oura.py daily \
  --profile default \
  --start-date 2026-07-01 \
  --end-date 2026-07-07

# Retrieve all pages for one resource
python3 ~/.local/share/oura-skill/scripts/oura.py get daily_stress \
  --profile default \
  --start-date 2026-07-01 \
  --end-date 2026-07-07 \
  --all-pages
```

Run `python3 ~/.local/share/oura-skill/scripts/oura.py --help` for all commands. See [references/endpoints.md](references/endpoints.md) for supported resources and query types.

### Multiple profiles

Each profile has a separate token file. Authorize another account under a generic label, then always state the intended profile in requests:

```bash
python3 ~/.local/share/oura-skill/scripts/oura.py authorize --profile secondary --open-browser
python3 ~/.local/share/oura-skill/scripts/oura.py profiles
python3 ~/.local/share/oura-skill/scripts/oura.py default-profile secondary
```

Do not merge profiles unless a comparison is explicitly requested, and label comparison results by profile.

## Local data and privacy

Configuration is stored outside this repository under `~/.config/oura` by default:

- `app.env` contains the OAuth application credentials.
- `profiles/PROFILE.env` contains that profile's rotating OAuth tokens.

The CLI creates these files with owner-only permissions. Never commit, paste, log, or attach them to an issue. Oura responses are sensitive health data; request and retain only what you need.

The software is not affiliated with or endorsed by Oura Health Oy. Use of Oura data remains subject to Oura's [API Agreement](https://cloud.ouraring.com/legal/api-agreement).

## Update or uninstall

Update the shared checkout:

```bash
git -C ~/.local/share/oura-skill pull --ff-only
```

To uninstall, remove only the symlinks that point to this checkout, then remove the checkout if you no longer need it. Configuration under `~/.config/oura` is intentionally separate and is not deleted automatically.

## Troubleshooting

- Skill not listed: confirm the relevant link points to the checkout, then restart the client.
- Installer refuses a path: move or remove the conflicting file, directory, or symlink yourself, then rerun exactly one of `--codex`, `--claude`, or `--all`.
- OAuth callback rejected: confirm the application uses the exact redirect URI `http://localhost:8910/callback`.
- Network or API error: run `status` first; reauthorize only when the selected profile is reported as unauthorized.

## Development

```bash
python3 -W error::ResourceWarning -m unittest -v scripts/test_oura.py
python3 -m py_compile scripts/oura.py scripts/test_oura.py
sh -n install.sh
bash -n scripts/test_install.sh
scripts/test_install.sh
```

The skill-structure validator needs PyYAML, so contributors can run it ephemerally with `uv`; it is not a runtime dependency:

```bash
uv run --with pyyaml python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
```

## Security

See [SECURITY.md](SECURITY.md) before reporting a vulnerability. Do not include credentials, authorization callbacks, raw Oura responses, or health records in a public issue.

## License

[MIT](LICENSE)
