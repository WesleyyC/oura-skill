# Oura V2 resources

Source snapshot: [Oura OpenAPI 1.35](https://cloud.ouraring.com/v2/static/json/openapi-1.35.json), checked 2026-07-18.

The scope column is a troubleshooting hint based on Oura's consent categories; the API remains authoritative. The CLI does not reject a request locally based on this column.

The CLI contract for every `start_date` / `end_date` pair is inclusive. Oura currently treats `end_date` as exclusive for `daily_activity`, `sleep`, and `workout`, so the CLI advances only those upstream requests by one day and filters returned `day` values back to the requested interval. Callers must pass the actual requested end date without compensating for endpoint behavior.

| Resource | Path suffix | Time parameters | Expected scope |
|---|---|---|---|
| `daily_activity` | `daily_activity` | `start_date`, `end_date` | `daily` |
| `daily_cardiovascular_age` | `daily_cardiovascular_age` | `start_date`, `end_date` | `daily` |
| `daily_readiness` | `daily_readiness` | `start_date`, `end_date` | `daily` |
| `daily_resilience` | `daily_resilience` | `start_date`, `end_date` | `daily` |
| `daily_sleep` | `daily_sleep` | `start_date`, `end_date` | `daily` |
| `daily_spo2` | `daily_spo2` | `start_date`, `end_date` | `spo2Daily` |
| `daily_stress` | `daily_stress` | `start_date`, `end_date` | `daily` |
| `enhanced_tag` | `enhanced_tag` | `start_date`, `end_date` | `tag` |
| `heartrate` | `heartrate` | `start_datetime`, `end_datetime`, `latest` | `heartrate` |
| `personal_info` | `personal_info` | none | `personal` |
| `rest_mode_period` | `rest_mode_period` | `start_date`, `end_date` | `daily` |
| `ring_battery_level` | `ring_battery_level` | `start_datetime`, `end_datetime`, `latest` | `personal` |
| `ring_configuration` | `ring_configuration` | none | `personal` |
| `session` | `session` | `start_date`, `end_date` | `session` |
| `sleep` | `sleep` | `start_date`, `end_date` | `daily` |
| `sleep_time` | `sleep_time` | `start_date`, `end_date` | `daily` |
| `tag` | `tag` | `start_date`, `end_date` | `tag` |
| `vo2_max` | `vO2_max` | `start_date`, `end_date` | `daily` |
| `workout` | `workout` | `start_date`, `end_date` | `workout` |

All collection routes except `personal_info` accept `fields`; all paginated routes accept `next_token`. `ring_configuration` accepts `fields` and `next_token` but no date range.
