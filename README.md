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
