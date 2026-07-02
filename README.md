# LionWheel MCP Server

Gives Claude read/write access to your [LionWheel](https://www.lionwheel.com)
delivery operations by wrapping the LionWheel REST API
(https://github.com/lionwheel/api) as MCP tools.

## Tools exposed

| Tool | What it does |
|------|--------------|
| `create_delivery` | Create a delivery (task) |
| `get_delivery` | Fetch a task by id (status, driver, etc.) |
| `update_delivery` | Reassign driver, change date, or set status |
| `get_tasks_by_order_id` | Look up tasks by your external order id |
| `get_tasks_by_phone` | Look up tasks by recipient phone |
| `add_document` | Attach a base64 PDF/PNG/JPEG to a task |
| `get_daily_route` | A driver's route for a date |
| `optimize_daily_route` | Optimize a driver's route for a date |
| `list_routes` | List routes for a date |
| `get_visit` / `update_visit` | Read / update a visit |
| `get_company` / `create_company` / `update_company` | Manage companies |

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
pip install -r requirements.txt
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

> Note: `MCP_BEARER_TOKEN` is read by the entrypoint as a flag; enforcing it
> requires wrapping the ASGI app with an auth check or fronting it with a proxy.
> The simplest production-grade path is an auth proxy / API gateway in front of
> the container. See the security section above.

---

## Field cheat-sheet

- Dates: `dd/mm/yyyy`. Times: `HH:MM`.
- `money_collect`: cents (300 = 3.00). `cod_type`: Cash=0, Cheque=1.
- `urgency`: REGULAR=0, URGENT=1, SUPER_URGENT=2.
- Task status: UNASSIGNED=0, ASSIGNED=1, ACTIVE=2, COMPLETED=3, CANCELED=4,
  ROUNDTRIP_DELIVERED=5, IN_INVENTORY=6, OUT_INVENTORY=7, FAILED=8,
  FINAL_FAILED=9, IN_TRANSFER=10.
