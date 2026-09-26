# DATA_PROVIDER

`RacingDataProvider` exposes:

- `get_race(race_date, racecourse, race_number)`
- `get_race_entries(race_id)`
- `get_horse_history(horse)`
- `get_race_results(race_id)`
- `get_race_updates(race_id)`

`JraOfficialProvider` is intentionally conservative:

- HTTPS only.
- Host allowlist: `www.jra.go.jp`, `jra.jp`.
- On-demand fetch only.
- Race-card URL resolution follows the public race-card selection pages by POSTing the same `cname` value used by official `doAction` links.
- Race-result URL resolution follows the public result selection pages the same way.
- Recent-run facts embedded in public race-card rows are parsed and exposed through `get_horse_history`.
- Single-win odds and popularity shown in public race-card rows are parsed when present.
- Current weather and turf/dirt condition are read from the public baba page when the requested racecourse is listed there.
- Cache per URL by SHA-256.
- Fetch log with HTTP status, source, timestamp, and content hash.
- Timeout and minimum interval between network fetches.
- No CAPTCHA bypass, login automation, private API discovery, or bulk crawling.

Live race-card mode was smoke-tested against `2026-08-16 札幌 11R`. Live result mode was smoke-tested against `2026-08-08 札幌 11R`. If the requested race is not listed, not yet published, already removed, not finalized, or the official HTML structure changes, the provider reports unavailable rather than trying bypasses.

For emergency manual operation, `JRA_RACE_URL_INDEX` can point to a JSON file keyed as `YYYY-MM-DD|競馬場|R番号` with a full public race-card URL.
