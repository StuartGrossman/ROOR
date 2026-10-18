# TeamROR Fly paper service

Base URL: https://wispy-song-231.fly.dev
All endpoints require `Authorization: Bearer <token>`; the token is stored locally in `work/fly-login/api-token.txt` in the project workspace. Never add it to a URL or commit it.

- `GET /api/v1/callouts?date=YYYY-MM-DD&after=0&limit=100`: raw message revisions for a New York trading date. Omit date to page the archive. `nextCursor`/`hasMore` support paging.
- `GET /api/v1/messages/{message_id}`: full captured revisions for a message.
- `GET /api/v1/signals?date=YYYY-MM-DD`: text-level entry/add/exit and guidance candidates. `review` and possible trims require human review; these labels do not establish position ownership or order fills.
- `GET /api/v1/execution`: paper worker state, order counts, Telegram health and exit review flags.
- `GET /api/v1/status`: capture health and cursor.

The worker submits automatically to the hard-coded Alpaca paper API. It uses $200 starter and one $400 add, sizes from a fresh option ask, submits market orders, applies no automatic stop losses or fixed profit targets, and follows matched source exit guidance. Market-order fills can exceed the budget. The strategy currently accepts clear SPY same-day entries only and uses the existing 15:15 ET entry cutoff. Ambiguous exits and trim wording are flagged for review, not automatically sold. A broker fill is required to confirm an exit.

The Mac worker is paused after reconciliation. Do not start a second worker against the same account. The Fly service uses one always-on machine and an encrypted volume for archive, Telegram session, and the order journal. Monitor `/api/v1/execution`, particularly connection health and review flags; the single machine is not highly available. Historical archive messages are not automatically traded.

## Parser 2.0.0

The API, worker and historical replay use `worker/interpretation.py`. `/callouts`,
`/signals`, `/messages/{id}` and `/trades` return `parserVersion`; `/execution`
returns `parser_version`. Interpretation is separate from freshness, source-position
matching, order acceptance and confirmed fills.

A first actionable edited SPY entry may open at its observation time only while
both the original post and edit remain within 60 seconds, no prior order exists,
and no intervening trade instruction supersedes it. Unknown originals are never
backdated. Newer message revisions cannot be undone by delayed old revisions.
Price-only entry edits preserve owned-position identity; actual contract changes
require review. Edited pending buys are cancelled without discarding filled lots.

The worker understands immediate sell phrasing, same-trade warning/“now” context,
source sell/hold choices, conditional profitable exits, and explicit whole-contract
trims. Half trims round down; ambiguous quantities require review. Profit-protection
guidance requests an immediate exit only when a fresh full-position bid is profitable.
No automatic loss stop or fixed profit target is enabled.

Telegram push updates are saved into a durable SQLite inbox. One consumer processes
the revisions and confirms orders; polling/catch-up provides recovery. All observed
versions are retained, including intermediate edits. Versions never observed cannot
be reconstructed or guaranteed by the API.

The unchanged profile is $200 starter, one $400 add, market orders, SPY 0DTE only,
$5,000 strategy capital and a 15:15 New York entry/add cutoff. Broker expiry events
are reconciled separately; the worker never invents an expiry fill. Historical broker
liquidation and settlement models are assumptions, explicitly labelled in reports.
