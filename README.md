# LionWheel MCP Server

Gives Claude read/write access to your [LionWheel](https://www.lionwheel.com)
delivery operations by wrapping the LionWheel REST API
(https://github.com/lionwheel/api) as MCP tools.

## Tools exposed (53)

**Deliveries (tasks)**
| Tool | What it does |
|------|--------------|
| `create_delivery` | Create a delivery with any documented field (COD, time window, line items, documents, coordinates, gift fields…) |
| `create_simple_delivery` | Create a delivery from just the essentials (cash amount in normal units) |
| `get_delivery` / `get_delivery_status` | Full record / short human-readable status |
| `update_delivery` | Change driver, date and/or status (status by name or number) |
| `set_delivery_status` | Set status by name: `completed`, `failed`, `canceled`… |
| `assign_driver` / `unassign_driver` | Assign or return to the unassigned pool |
| `cancel_delivery`, `mark_delivery_completed`, `mark_delivery_failed`, `mark_delivery_active`, `mark_roundtrip_delivered` | One-step status changes |
| `reschedule_delivery` | Move to another date |
| `get_tasks_by_order_id` / `track_order` | Look up by external order id (raw / with status summary) |
| `get_tasks_by_phone` / `get_deliveries_by_phone_summary` | Look up by recipient phone |
| `add_document` / `add_document_from_url` | Attach a PDF/PNG/JPEG (base64 or download from a URL) |

**Bulk** (up to 200 items per call)
`bulk_create_deliveries`, `bulk_get_deliveries`, `bulk_update_deliveries`, `bulk_assign_driver`,
`bulk_reschedule_deliveries`, `bulk_cancel_deliveries`, `bulk_track_orders`, `bulk_get_visits`,
`bulk_update_visits`, `bulk_get_companies`

**Drivers & routes**
`get_daily_route`, `get_driver_route_summary` (completed vs remaining), `get_daily_routes_for_drivers`,
`optimize_daily_route`, `optimize_routes_for_drivers`, `list_routes`, `get_routes_summary`
(whole-day status breakdown), `get_routes_for_date_range`

**Visits**
`get_visit`, `update_visit`, `reassign_visit`, `reschedule_visit`

**Companies (customers)**
`get_company`, `create_company`, `update_company`, `activate_company`, `deactivate_company`,
`update_company_default_location`, `update_company_time_window`

**Utility**
`get_reference_codes`, `get_server_info`, `check_connection`, `lionwheel_api_request`
(raw call to any `/api/v1` endpoint)

**Prompts:** `daily_operations_report`, `customer_order_lookup`, `driver_day_review`

Dates can be given as `dd/mm/yyyy`, `yyyy-mm-dd`, `today`, `tomorrow`, `yesterday` or `+N`/`-N`.
Statuses can be given by name or number; responses get an extra `status_name` field.

The LionWheel API key lives **only** on the server (env var). Claude never sees it.

---

## 1. Get your LionWheel key

In LionWheel: **Customers** tab → **Edit** to find the API Key, or for a
shipping-company token, **Settings → API definition → KEY API key** (in that case
you also need a `company_id` from the top of the customer settings page).
Customer tokens start with `c_key`.

To test safely first, request sandbox access from support@lionwheel.com and point
`LIONWHEEL_BASE_URL` at `https://test.lionwheel.com/api/v1`.

---

## 2. Run it

### Option A — Local, for Claude Desktop (stdio)

```bash
pip install -r requirements.txt   # pins mcp<2 (FastMCP API)
export LIONWHEEL_API_KEY="your_token"
# export LIONWHEEL_COMPANY_ID="12345"   # only for shipping-company tokens
python lionwheel_mcp.py
```

Then add to `claude_desktop_config.json`
(Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "lionwheel": {
      "command": "python",
      "args": ["/full/path/to/lionwheel_mcp.py"],
      "env": {
        "LIONWHEEL_API_KEY": "your_token",
        "LIONWHEEL_COMPANY_ID": "12345"
      }
    }
  }
}
```

> Local stdio servers work **only in Claude Desktop** — not in claude.ai, the
> mobile apps, or Cowork. For those, use Option B.

### Option B — Remote, for claude.ai / mobile / Cowork (HTTP)

claude.ai connects to your server **from Anthropic's cloud**, so it must be
reachable on the public internet (not localhost, not behind a VPN/firewall).

```bash
docker build -t lionwheel-mcp .
docker run -p 8000:8000 \
  -e LIONWHEEL_API_KEY="your_token" \
  -e LIONWHEEL_COMPANY_ID="12345" \
  -e MCP_BEARER_TOKEN="a-long-random-secret" \
  lionwheel-mcp
```

Deploy that container to any public host (Render, Railway, Fly.io, Google Cloud
Run, a VPS, etc.). Your MCP endpoint will be:

```
https://your-host.example.com/mcp
```

Then in Claude: **Settings → Connectors → `+` (Add custom connector)** → paste the
URL → **Add**, then enable it per-chat via the **`+`** menu → **Connectors**.
(On Team/Enterprise, an Owner adds it under Organization settings → Connectors.)

---

## 3. Security — read this before deploying publicly

This server can **create and cancel real deliveries** and move money fields. A
public, unauthenticated MCP URL means anyone who finds it can do the same.

- Keep `LIONWHEEL_API_KEY` in env/secrets, never in the URL or code.
- Set `MCP_BEARER_TOKEN` and/or put the server behind an auth proxy. For the most
  robust setup, claude.ai supports OAuth (Advanced settings → Client ID/Secret);
  consider an OAuth layer (e.g. Auth0) for production.
- Restrict inbound traffic to Anthropic's published IP ranges where possible.
- Test against the sandbox before pointing at production.
- Custom connectors link Claude to a service Anthropic hasn't verified — you are
  trusting this code and your own deployment.

`MCP_BEARER_TOKEN` is enforced in HTTP mode. Send it as `Authorization: Bearer <token>`,
or (for claude.ai custom connectors, which can't add headers) put it in the URL:
`https://your-app.up.railway.app/mcp?token=<token>`. `/health` is always open for health checks.
Set `LIONWHEEL_READ_ONLY=true` to block every write call.

## Deploying on Railway

The repo includes `railway.json` (Dockerfile build + `/health` check). Create a service from
this GitHub repo, set the variables `LIONWHEEL_API_KEY`, `MCP_BEARER_TOKEN` (and optionally
`LIONWHEEL_COMPANY_ID`, `LIONWHEEL_BASE_URL`, `LIONWHEEL_TIMEZONE`), generate a domain, and add
`https://<domain>/mcp?token=<MCP_BEARER_TOKEN>` as a custom connector in Claude.

---

## Field cheat-sheet

- Dates: `dd/mm/yyyy`. Times: `HH:MM`.
- `money_collect`: cents (300 = 3.00). `cod_type`: Cash=0, Cheque=1.
- `urgency`: REGULAR=0, URGENT=1, SUPER_URGENT=2.
- Task status: UNASSIGNED=0, ASSIGNED=1, ACTIVE=2, COMPLETED=3, CANCELED=4,
  ROUNDTRIP_DELIVERED=5, IN_INVENTORY=6, OUT_INVENTORY=7, FAILED=8,
  FINAL_FAILED=9, IN_TRANSFER=10.
