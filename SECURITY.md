# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. If that option is unavailable, open a public issue containing only a request for a private contact channel.

Never include any of the following in an issue, discussion, pull request, test fixture, screenshot, or log:

- Oura client IDs or client secrets
- access tokens, refresh tokens, authorization codes, or callback URLs containing codes
- raw Oura API responses or health records
- populated `app.env` or profile `.env` files

Revoke exposed Oura credentials immediately before reporting the incident.

## Supported versions

Security fixes are made on the latest `main` branch. This project does not currently publish versioned releases.
