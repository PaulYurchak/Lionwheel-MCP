"""
LionWheel MCP Server
====================

Wraps the LionWheel delivery-management REST API (https://github.com/lionwheel/api)
as Model Context Protocol tools, so Claude can read and manage your deliveries.

The LionWheel API key is read from the environment (LIONWHEEL_API_KEY) and injected
server-side. Claude never sees or handles the key.

Run modes
---------
  stdio (local, for Claude Desktop):
      python lionwheel_mcp.py

  HTTP (remote, for claude.ai / mobile / Cowork custom connectors):
      TRANSPORT=http PORT=8000 python lionwheel_mcp.py

Environment variables
---------------------
  LIONWHEEL_API_KEY     (required)  Your LionWheel token. A shipping-company token,
                                    or a customer token that starts with "c_key".
  LIONWHEEL_BASE_URL    (optional)  Defaults to https://members.lionwheel.com/api/v1
                                    Use https://test.lionwheel.com/api/v1 for the sandbox.
  LIONWHEEL_COMPANY_ID  (optional)  Default company_id to attach to created deliveries
                                    when using a shipping-company token.
  TRANSPORT             (optional)  "stdio" (default) or "http".
  PORT                  (optional)  HTTP port, default 8000.
  MCP_BEARER_TOKEN      (optional)  If set (HTTP mode), require this bearer token on
                                    every request. Strongly recommended for public
                                    deployments — see README.
"""

import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

API_KEY = os.environ.get("LIONWHEEL_API_KEY", "")
BASE_URL = os.environ.get(
    "LIONWHEEL_BASE_URL", "https://members.lionwheel.com/api/v1"
).rstrip("/")
DEFAULT_COMPANY_ID = os.environ.get("LIONWHEEL_COMPANY_ID")

mcp = FastMCP("lionwheel")

_client = httpx.AsyncClient(timeout=30.0)


# --------------------------------------------------------------------------- #
# Low-level request helper
# --------------------------------------------------------------------------- #

async def _request(
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> dict[str, Any]:
    """Make an authenticated request to LionWheel and return a normalized result."""
    if not API_KEY:
        return {
            "ok": False,
            "error": "LIONWHEEL_API_KEY is not set on the server.",
        }

    query = {"key": API_KEY}
    if params:
        query.update({k: v for k, v in params.items() if v is not None})

    url = f"{BASE_URL}/{path.lstrip('/')}"

    try:
        resp = await _client.request(method, url, params=query, json=json_body)
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"Network error contacting LionWheel: {exc}"}

    # Friendly messages for the documented failure codes.
    if resp.status_code == 401:
        return {"ok": False, "status": 401, "error": "Authentication error — check the API key / token type."}
    if resp.status_code == 403:
        return {"ok": False, "status": 403, "error": "Data mismatch error (403). Check company_id / required fields."}

    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}

    return {"ok": resp.is_success, "status": resp.status_code, "data": data}


# --------------------------------------------------------------------------- #
# Deliveries / tasks
# --------------------------------------------------------------------------- #

@mcp.tool()
async def create_delivery(
    pickup_at: str,
    original_order_id: str,
    destination_city: str,
    destination_street: str,
    destination_number: str,
    destination_recipient_name: str,
    destination_phone: str,
    company_id: Optional[int] = None,
    notes: Optional[str] = None,
    source_city: Optional[str] = None,
    source_street: Optional[str] = None,
    source_number: Optional[str] = None,
    source_recipient_name: Optional[str] = None,
    source_phone: Optional[str] = None,
    destination_zip_code: Optional[str] = None,
    destination_floor: Optional[str] = None,
    destination_apartment: Optional[str] = None,
    destination_notes: Optional[str] = None,
    destination_email: Optional[str] = None,
    money_collect: Optional[int] = None,
    cod_type: Optional[int] = None,
    urgency: Optional[int] = None,
    driver_id: Optional[int] = None,
    earliest: Optional[str] = None,
    latest: Optional[str] = None,
    packages_quantity: Optional[int] = None,
    line_items: Optional[list] = None,
    extra_fields: Optional[dict] = None,
) -> dict:
    """Create a new delivery (task) in LionWheel.

    Dates use dd/mm/yyyy format. `money_collect` is in cents (e.g. 300 = 3.00).
    `cod_type`: Cash=0, Cheque=1. `urgency`: REGULAR=0, URGENT=1, SUPER_URGENT=2.
    `line_items` is a list like [{"name":"orange","quantity":"6","price":"11.99"}].
    Pass any additional documented fields via `extra_fields`.

    Returns LionWheel's task_id, public_id, tracking_link, label and barcode on success.
    """
    body: dict[str, Any] = {
        "pickup_at": pickup_at,
        "original_order_id": original_order_id,
        "destination_city": destination_city,
        "destination_street": destination_street,
        "destination_number": destination_number,
        "destination_recipient_name": destination_recipient_name,
        "destination_phone": destination_phone,
    }
    optional = {
        "company_id": company_id if company_id is not None else (
            int(DEFAULT_COMPANY_ID) if DEFAULT_COMPANY_ID else None
        ),
        "notes": notes,
        "source_city": source_city,
        "source_street": source_street,
        "source_number": source_number,
        "source_recipient_name": source_recipient_name,
        "source_phone": source_phone,
        "destination_zip_code": destination_zip_code,
        "destination_floor": destination_floor,
        "destination_apartment": destination_apartment,
        "destination_notes": destination_notes,
        "destination_email": destination_email,
        "money_collect": money_collect,
        "cod_type": cod_type,
        "urgency": urgency,
        "driver_id": driver_id,
        "earliest": earliest,
        "latest": latest,
        "packages_quantity": packages_quantity,
        "line_items": line_items,
    }
    body.update({k: v for k, v in optional.items() if v is not None})
    if extra_fields:
        body.update(extra_fields)

    return await _request("POST", "tasks/create", json_body=body)


@mcp.tool()
async def get_delivery(task_id: int) -> dict:
    """Fetch a delivery (task) by its LionWheel task_id.

    Status codes: UNASSIGNED=0, ASSIGNED=1, ACTIVE=2, COMPLETED=3, CANCELED=4,
    ROUNDTRIP_DELIVERED=5, IN_INVENTORY=6, OUT_INVENTORY=7, FAILED=8,
    FINAL_FAILED=9, IN_TRANSFER=10.
    """
    return await _request("GET", f"tasks/show/{task_id}")


@mcp.tool()
async def update_delivery(
    task_id: int,
    driver_id: Optional[int] = None,
    pickup_at: Optional[str] = None,
    status: Optional[int] = None,
) -> dict:
    """Update a delivery: reassign driver, change date (dd/mm/yyyy), or set status.

    Settable status values: UNASSIGNED=0, ASSIGNED=1, ACTIVE=2, COMPLETED=3,
    CANCELED=4, ROUNDTRIP_DELIVERED=5, FAILED=8.
    """
    body = {
        k: v
        for k, v in {"driver_id": driver_id, "pickup_at": pickup_at, "status": status}.items()
        if v is not None
    }
    return await _request("PUT", f"tasks/{task_id}/update", json_body=body)


@mcp.tool()
async def get_tasks_by_order_id(order_id: str) -> dict:
    """Find deliveries by the external order ID that was supplied at creation."""
    return await _request("GET", f"tasks/by_order_id/{order_id}")


@mcp.tool()
async def get_tasks_by_phone(phone: str) -> dict:
    """Find deliveries by the recipient's phone number."""
    return await _request("GET", f"tasks/by_phone/{phone}")


@mcp.tool()
async def add_document(task_id: int, file_base64: str) -> dict:
    """Attach a document (base64-encoded) to a delivery. Supported: PDF, PNG, JPEG."""
    return await _request(
        "POST", f"tasks/{task_id}/add_document", json_body={"file": file_base64}
    )


# --------------------------------------------------------------------------- #
# Drivers / routes / visits
# --------------------------------------------------------------------------- #

@mcp.tool()
async def get_daily_route(driver_id: int, date: Optional[str] = None) -> dict:
    """Get a driver's route for a date (defaults to today). Date is dd/mm/yyyy."""
    return await _request(
        "GET", f"drivers/{driver_id}/daily_route", params={"date": date}
    )


@mcp.tool()
async def optimize_daily_route(driver_id: int, date: Optional[str] = None) -> dict:
    """Optimize a driver's route for a date (defaults to today). Date is dd/mm/yyyy."""
    return await _request(
        "POST",
        f"drivers/{driver_id}/optimize_daily_route",
        json_body={"date": date} if date else None,
    )


@mcp.tool()
async def list_routes(date: Optional[str] = None, response_format: str = "json") -> dict:
    """List routes for a date (defaults to today). `response_format`: json or xml."""
    return await _request(
        "GET", "routes", params={"date": date, "format": response_format}
    )


@mcp.tool()
async def get_visit(visit_id: int) -> dict:
    """Fetch a single visit by its visit_id."""
    return await _request("GET", f"visits/{visit_id}")


@mcp.tool()
async def update_visit(
    visit_id: int, driver_id: Optional[int] = None, visit_at: Optional[str] = None
) -> dict:
    """Update a visit's assigned driver or date (dd/mm/yyyy)."""
    body = {
        k: v
        for k, v in {"driver_id": driver_id, "visit_at": visit_at}.items()
        if v is not None
    }
    return await _request("PUT", f"visits/{visit_id}", json_body=body)


# --------------------------------------------------------------------------- #
# Companies
# --------------------------------------------------------------------------- #

@mcp.tool()
async def get_company(company_id: int) -> dict:
    """Fetch company details (name, contacts, default location) by company_id."""
    return await _request("GET", f"companies/{company_id}")


@mcp.tool()
async def create_company(
    name: str,
    primary_email: Optional[str] = None,
    legal_id: Optional[str] = None,
    office_phone: Optional[str] = None,
    cell_phone: Optional[str] = None,
    phone2: Optional[str] = None,
    fax: Optional[str] = None,
    default_earliest_at: Optional[str] = None,
    default_latest_at: Optional[str] = None,
    is_active: Optional[bool] = None,
    default_location_attributes: Optional[dict] = None,
) -> dict:
    """Create a company. `default_location_attributes` accepts name/city/street/
    number/floor/apartment/zip_code/notes/default_recipient_name/default_phone/
    default_email. Times are like "10:00".
    """
    body: dict[str, Any] = {"name": name}
    optional = {
        "primary_email": primary_email,
        "legal_id": legal_id,
        "office_phone": office_phone,
        "cell_phone": cell_phone,
        "phone2": phone2,
        "fax": fax,
        "default_earliest_at": default_earliest_at,
        "default_latest_at": default_latest_at,
        "is_active": is_active,
        "default_location_attributes": default_location_attributes,
    }
    body.update({k: v for k, v in optional.items() if v is not None})
    return await _request("POST", "companies", json_body=body)


@mcp.tool()
async def update_company(
    company_id: int,
    name: Optional[str] = None,
    primary_email: Optional[str] = None,
    legal_id: Optional[str] = None,
    office_phone: Optional[str] = None,
    cell_phone: Optional[str] = None,
    phone2: Optional[str] = None,
    fax: Optional[str] = None,
    default_earliest_at: Optional[str] = None,
    default_latest_at: Optional[str] = None,
    is_active: Optional[bool] = None,
    default_location_attributes: Optional[dict] = None,
) -> dict:
    """Update a company. For `default_location_attributes`, include an `id` to edit
    the existing default location, or omit it to create a new one.
    """
    body = {
        k: v
        for k, v in {
            "name": name,
            "primary_email": primary_email,
            "legal_id": legal_id,
            "office_phone": office_phone,
            "cell_phone": cell_phone,
            "phone2": phone2,
            "fax": fax,
            "default_earliest_at": default_earliest_at,
            "default_latest_at": default_latest_at,
            "is_active": is_active,
            "default_location_attributes": default_location_attributes,
        }.items()
        if v is not None
    }
    return await _request("PATCH", f"companies/{company_id}", json_body=body)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    transport = os.environ.get("TRANSPORT", "stdio").lower()
    if transport == "http":
        # Streamable HTTP transport for remote / claude.ai custom connectors.
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = int(os.environ.get("PORT", "8000"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
