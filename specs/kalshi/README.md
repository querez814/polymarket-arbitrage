# Kalshi Predictions API specifications

Pinned from Kalshi's official API documentation on 2026-07-21.

| File | Official source | SHA-256 |
| --- | --- | --- |
| `predictions-openapi.yaml` | `https://docs.kalshi.com/openapi.yaml` | `47d87f6b14947712dc44706260b59cdefb0b8b828d6395e3dac691458e3f5fa4` |
| `predictions-asyncapi.yaml` | `https://docs.kalshi.com/asyncapi.yaml` | `e86105374ec9eadb2a0d96278280a0319f1499385599af0f6dcd8c71a9b1372f` |

Browserbase resolved the download cards on `https://docs.kalshi.com/welcome` to
Kalshi's predictions REST and WebSocket specification assets. The
`docs.kalshi.com` mirror was used for the local download because the equivalent
`kalshi.com/docs/api/predictions/*` URLs returned HTTP 429 to the terminal.

## Contract used by this repository

- V2 create: `POST /portfolio/events/orders`
- Read one: `GET /portfolio/orders/{order_id}`
- Read many/reconcile: `GET /portfolio/orders`
- V2 cancel: `DELETE /portfolio/events/orders/{order_id}`
- Authenticated lifecycle channels: `fill` and `user_orders`

The REST specification is the source of truth for request/response shapes. The
WebSocket specification defines the recovery and real-time lifecycle channels;
it does not replace authoritative REST reconciliation after reconnects.

Do not put API keys or private-key material in this directory.
