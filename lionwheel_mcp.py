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

  HTTP (remote, for claude.ai / mobile / Cowork custom connectors, Railway, ...):
      TRANSPORT=http PORT=8000 python lionwheel_mcp.py

Environment variables
---------------------
  LIONWHEEL_API_KEY     (required)  Your LionWheel token. A shipping-company token,
                                    or a customer token that starts with "c_key".
  LIONWHEEL_BASE_URL    (optional)  Defaults to https://members.lionwheel.com/api/v1
                                    Use https://test.lionwheel.com/api/v1 for the sandbox.
  LIONWHEEL_COMPANY_ID  (optional)  Default company_id to attach to created deliveries
                                    when using a shipping-company token.
  LIONWHEEL_TIMEZONE    (optional)  Timezone used to resolve "today"/"tomorrow".
                                    Defaults to Asia/Jerusalem.
  LIONWHEEL_READ_ONLY   (optional)  "true" blocks every write call (create/update/
                                    optimize/raw non-GET). Handy for safe exploration.
  TRANSPORT             (optional)  "stdio" (default) or "http".
  PORT                  (optional)  HTTP port, default 8000 (Railway sets this).
  MCP_BEARER_TOKEN      (optional)  If set (HTTP mode), every request to /mcp must carry
                                    the token, either as "Authorization: Bearer <token>"
                                    or as a "?token=<token>" query parameter (claude.ai
                                    custom connectors can't send custom headers, so use
                                    the query-string form there).
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hmac
import logging
import os
import re
from typing import Any, Optional, Union
from urllib.parse import parse_qs, quote
from zoneinfo import ZoneInfo

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
TIMEZONE = os.environ.get("LIONWHEEL_TIMEZONE", "Asia/Jerusalem")
READ_ONLY = os.environ.get("LIONWHEEL_READ_ONLY", "").lower() in ("1", "true", "yes")
BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")

mcp = FastMCP(
    "lionwheel",
    instructions=(
        "Tools for the LionWheel delivery-management system. Deliveries are called "
        "'tasks'. Dates may be given as dd/mm/yyyy, yyyy-mm-dd, 'today', 'tomorrow' "
        "or 'yesterday'. Status values may be given by name (e.g. 'completed') or "
        "number. Use get_reference_codes for all code tables. Write actions affect "
        "real deliveries, so confirm destructive changes (cancel, bulk updates) with "
        "the user first."
    ),
    host="0.0.0.0",
    stateless_http=True,
)

_client = httpx.AsyncClient(timeout=30.0)

# httpx logs full request URLs at INFO, which include ?key=<API key>. Never log them.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# --------------------------------------------------------------------------- #
# Reference tables (from the LionWheel API docs)
# --------------------------------------------------------------------------- #

STATUS_CODES: dict[int, str] = {
    0: "UNASSIGNED",
    1: "ASSIGNED",
    2: "ACTIVE",
    3: "COMPLETED",
    4: "CANCELED",
    5: "ROUNDTRIP_DELIVERED",
    6: "IN_INVENTORY",
    7: "OUT_INVENTORY",
    8: "FAILED",
    9: "FINAL_FAILED",
    10: "IN_TRANSFER",
}
SETTABLE_STATUSES = {0, 1, 2, 3, 4, 5, 8}
STATUS_ALIASES = {
    "UNASSIGNED": 0, "ASSIGNED": 1, "ACTIVE": 2, "IN_PROGRESS": 2, "PICKED_UP": 2,
    "COMPLETED": 3, "COMPLETE": 3, "DELIVERED": 3, "DONE": 3,
    "CANCELED": 4, "CANCELLED": 4, "CANCEL": 4,
    "ROUNDTRIP_DELIVERED": 5, "IN_INVENTORY": 6, "OUT_INVENTORY": 7,
    "FAILED": 8, "FINAL_FAILED": 9, "IN_TRANSFER": 10,
}
COD_TYPES = {0: "Cash", 1: "Cheque", 2: "Card", 3: "Bank Transfer"}
URGENCY = {0: "REGULAR", 1: "URGENT", 2: "SUPER_URGENT"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _today() -> dt.date:
    try:
        return dt.datetime.now(ZoneInfo(TIMEZONE)).date()
    except Exception:  # unknown tz name
        return dt.date.today()


def _date(value: Optional[str]) -> Optional[str]:
    """Normalize a date into LionWheel's dd/mm/yyyy format.

    Accepts dd/mm/yyyy, dd.mm.yyyy, dd-mm-yyyy, yyyy-mm-dd, ISO datetimes,
    'today', 'tomorrow', 'yesterday', and '+N' / '-N' day offsets.
    Unrecognized strings are passed through unchanged.
    """
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    low = v.lower()
    rel = {"today": 0, "tomorrow": 1, "yesterday": -1}
    if low in rel:
        return (_today() + dt.timedelta(days=rel[low])).strftime("%d/%m/%Y")
    if re.fullmatch(r"[+-]\d+", low):
        return (_today() + dt.timedelta(days=int(low))).strftime("%d/%m/%Y")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", v)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return f"{d:02d}/{mo:02d}/{y:04d}"
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", v)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        return f"{d:02d}/{mo:02d}/{y:04d}"
    return v


def _status(value: Union[int, str, None]) -> Optional[int]:
    """Convert a status name or number to LionWheel's integer code."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    key = s.upper().replace(" ", "_").replace("-", "_")
    if key in STATUS_ALIASES:
        return STATUS_ALIASES[key]
    raise ValueError(
        f"Unknown status '{value}'. Use one of: {', '.join(STATUS_CODES.values())}"
    )


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _annotate(obj: Any) -> Any:
    """Add a human readable `status_name` next to every integer `status` field."""
    if isinstance(obj, dict):
        out = {k: _annotate(v) for k, v in obj.items()}
        st = obj.get("status")
        if isinstance(st, int) and st in STATUS_CODES and "status_name" not in obj:
            out["status_name"] = STATUS_CODES[st]
        return out
    if isinstance(obj, list):
        return [_annotate(x) for x in obj]
    return obj


def _collect_statuses(obj: Any, acc: dict[str, int]) -> None:
    if isinstance(obj, dict):
        st = obj.get("status")
        if isinstance(st, int) and st in STATUS_CODES:
            name = STATUS_CODES[st]
            acc[name] = acc.get(name, 0) + 1
        for v in obj.values():
            _collect_statuses(v, acc)
    elif isinstance(obj, list):
        for x in obj:
            _collect_statuses(x, acc)


_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def _request(
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> dict[str, Any]:
    """Make an authenticated request to LionWheel and return a normalized result."""
    method = method.upper()
    if not API_KEY:
        return {"ok": False, "error": "LIONWHEEL_API_KEY is not set on the server."}
    if READ_ONLY and method in _WRITE_METHODS:
        return {
            "ok": False,
            "error": "Server is in read-only mode (LIONWHEEL_READ_ONLY=true); "
                     "write operations are disabled.",
        }

    query = {"key": API_KEY}
    if params:
        query.update({k: v for k, v in params.items() if v is not None})

    url = f"{BASE_URL}/{path.lstrip('/')}"

    try:
        resp = await _client.request(method, url, params=query, json=json_body)
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"Network error contacting LionWheel: {exc}"}

    if resp.status_code == 401:
        return {"ok": False, "status": 401,
                "error": "Authentication error — check the API key / token type."}
    if resp.status_code == 403:
        detail = resp.text[:500]
        return {"ok": False, "status": 403,
                "error": "Data mismatch error (403). Check company_id / required fields.",
                "detail": detail}
    if resp.status_code == 404:
        return {"ok": False, "status": 404, "error": "Not found.", "path": path}

    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}

    return {"ok": resp.is_success, "status": resp.status_code, "data": _annotate(data)}


def _seg(value: Any) -> str:
    """URL-encode a path segment."""
    return quote(str(value), safe="")


# --------------------------------------------------------------------------- #
# Deliveries / tasks — core endpoints
# --------------------------------------------------------------------------- #

def _build_delivery_body(**fields: Any) -> dict[str, Any]:
    extra = fields.pop("extra_fields", None)
    if fields.get("pickup_at") is not None:
        fields["pickup_at"] = _date(fields["pickup_at"])
    if fields.get("company_id") is None and fields.get("company") is None and DEFAULT_COMPANY_ID:
        fields["company_id"] = int(DEFAULT_COMPANY_ID)
    body = _clean(fields)
    if extra:
        body.update(extra)
    return body


@mcp.tool()
async def create_delivery(
    original_order_id: str,
    destination_city: str,
    destination_street: str,
    destination_number: str,
    destination_recipient_name: str,
    destination_phone: str,
    pickup_at: Optional[str] = "today",
    company_id: Optional[int] = None,
    company: Optional[dict] = None,
    notes: Optional[str] = None,
    source_city: Optional[str] = None,
    source_street: Optional[str] = None,
    source_number: Optional[str] = None,
    source_zip_code: Optional[str] = None,
    source_floor: Optional[str] = None,
    source_apartment: Optional[str] = None,
    source_notes: Optional[str] = None,
    source_recipient_name: Optional[str] = None,
    source_phone: Optional[str] = None,
    source_email: Optional[str] = None,
    source_latitude: Optional[float] = None,
    source_longitude: Optional[float] = None,
    destination_zip_code: Optional[str] = None,
    destination_floor: Optional[str] = None,
    destination_apartment: Optional[str] = None,
    destination_notes: Optional[str] = None,
    destination_entrance: Optional[str] = None,
    destination_entrance_code: Optional[str] = None,
    destination_phone2: Optional[str] = None,
    destination_email: Optional[str] = None,
    destination_latitude: Optional[float] = None,
    destination_longitude: Optional[float] = None,
    delivery_method: Optional[str] = None,
    greeting: Optional[str] = None,
    gifter_name: Optional[str] = None,
    gifter_phone: Optional[str] = None,
    is_roundtrip: Optional[bool] = None,
    packages_quantity: Optional[int] = None,
    pallets_quantity: Optional[int] = None,
    money_collect: Optional[int] = None,
    cod_type: Optional[int] = None,
    is_self_pickup: Optional[bool] = None,
    age_verification: Optional[bool] = None,
    leave_next_to_door: Optional[bool] = None,
    earliest: Optional[str] = None,
    latest: Optional[str] = None,
    line_items: Optional[list] = None,
    urgency: Optional[int] = None,
    driver_id: Optional[int] = None,
    wait_time: Optional[int] = None,
    documents: Optional[list] = None,
    extra_fields: Optional[dict] = None,
) -> dict:
    """Create a new delivery (task) in LionWheel. Supports every documented field.

    - pickup_at: delivery date (dd/mm/yyyy, yyyy-mm-dd, 'today', 'tomorrow'). Default today.
    - original_order_id: your external order id — should be unique.
    - company_id: customer company (needed with a shipping-company token; falls back to
      LIONWHEEL_COMPANY_ID). Or pass `company` = {"name": "...", "external_id": "..."}
      to create/assign a company by external id.
    - money_collect: cash-on-delivery amount in CENTS (300 = 3.00).
    - cod_type: Cash=0, Cheque=1, Card=2, Bank Transfer=3.
    - urgency: REGULAR=0, URGENT=1, SUPER_URGENT=2.
    - earliest / latest: time window, e.g. "10:00" / "14:00".
    - wait_time: service time at destination, minutes.
    - line_items: [{"name","quantity","sku","price","weight","variant","notes"}, ...]
      (values are strings).
    - documents: [{"filename": "x.pdf", "file": "<base64>"}] (PDF/PNG/JPEG).
    - latitude/longitude override address geocoding.
    - extra_fields: any other field to merge into the request body.

    Returns task_id, public_id, tracking_link, label, barcode and destination region.
    """
    body = _build_delivery_body(**{k: v for k, v in locals().items()})
    return await _request("POST", "tasks/create", json_body=body)


@mcp.tool()
async def create_simple_delivery(
    order_id: str,
    city: str,
    street: str,
    house_number: str,
    recipient_name: str,
    phone: str,
    date: Optional[str] = "today",
    notes: Optional[str] = None,
    packages: Optional[int] = None,
    cash_to_collect: Optional[float] = None,
    urgent: bool = False,
    company_id: Optional[int] = None,
) -> dict:
    """Quick way to create a delivery with just the essentials.

    `cash_to_collect` is in normal currency units (e.g. 49.90) and is converted to
    cents automatically (cash COD). For all options use create_delivery.
    """
    return await create_delivery(
        original_order_id=order_id,
        destination_city=city,
        destination_street=street,
        destination_number=house_number,
        destination_recipient_name=recipient_name,
        destination_phone=phone,
        pickup_at=date,
        notes=notes,
        packages_quantity=packages,
        money_collect=round(cash_to_collect * 100) if cash_to_collect else None,
        cod_type=0 if cash_to_collect else None,
        urgency=1 if urgent else None,
        company_id=company_id,
    )


@mcp.tool()
async def get_delivery(task_id: int) -> dict:
    """Fetch a delivery (task) by its LionWheel task_id.

    Status codes: UNASSIGNED=0, ASSIGNED=1, ACTIVE=2, COMPLETED=3, CANCELED=4,
    ROUNDTRIP_DELIVERED=5, IN_INVENTORY=6, OUT_INVENTORY=7, FAILED=8,
    FINAL_FAILED=9, IN_TRANSFER=10. A `status_name` field is added automatically.
    """
    return await _request("GET", f"tasks/show/{task_id}")


@mcp.tool()
async def update_delivery(
    task_id: int,
    driver_id: Optional[int] = None,
    pickup_at: Optional[str] = None,
    status: Optional[Union[int, str]] = None,
) -> dict:
    """Update a delivery: reassign driver, change date, and/or set status.

    `status` accepts a name or number. Settable: UNASSIGNED=0, ASSIGNED=1, ACTIVE=2,
    COMPLETED=3, CANCELED=4, ROUNDTRIP_DELIVERED=5, FAILED=8.
    """
    try:
        st = _status(status)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if st is not None and st not in SETTABLE_STATUSES:
        return {"ok": False, "error": f"Status {st} ({STATUS_CODES.get(st)}) cannot be set "
                                      f"via the API. Settable: "
                                      f"{[STATUS_CODES[s] for s in sorted(SETTABLE_STATUSES)]}"}
    body = _clean({"driver_id": driver_id, "pickup_at": _date(pickup_at), "status": st})
    if not body:
        return {"ok": False, "error": "Nothing to update — pass driver_id, pickup_at or status."}
    return await _request("PUT", f"tasks/{task_id}/update", json_body=body)


@mcp.tool()
async def get_tasks_by_order_id(order_id: str) -> dict:
    """Find deliveries by the external order ID that was supplied at creation."""
    return await _request("GET", f"tasks/by_order_id/{_seg(order_id)}")


@mcp.tool()
async def get_tasks_by_phone(phone: str) -> dict:
    """Find deliveries by the recipient's phone number."""
    return await _request("GET", f"tasks/by_phone/{_seg(phone)}")


@mcp.tool()
async def add_document(task_id: int, file_base64: str, filename: Optional[str] = None) -> dict:
    """Attach a document (base64-encoded) to a delivery. Supported: PDF, PNG, JPEG."""
    body = _clean({"file": file_base64, "filename": filename})
    return await _request("POST", f"tasks/{task_id}/add_document", json_body=body)


# --------------------------------------------------------------------------- #
# Deliveries — convenience actions
# --------------------------------------------------------------------------- #

@mcp.tool()
async def get_delivery_status(task_id: int) -> dict:
    """Get a short, human-readable status summary for a delivery
    (status name, driver, date) instead of the full record."""
    res = await _request("GET", f"tasks/show/{task_id}")
    if not res.get("ok"):
        return res
    d = res["data"]
    task = d.get("task", d) if isinstance(d, dict) else d
    if not isinstance(task, dict):
        return res
    st = task.get("status")
    return {
        "ok": True,
        "task_id": task.get("task_id", task.get("id", task_id)),
        "status": st,
        "status_name": STATUS_CODES.get(st, str(st)),
        "driver_id": task.get("driver_id"),
        "pickup_at": task.get("pickup_at"),
        "created_at": task.get("created_at"),
        "company_id": task.get("company_id"),
    }


@mcp.tool()
async def set_delivery_status(task_id: int, status: Union[int, str]) -> dict:
    """Set a delivery's status by name or number (e.g. 'completed', 'failed', 3)."""
    return await update_delivery(task_id=task_id, status=status)


@mcp.tool()
async def assign_driver(task_id: int, driver_id: int) -> dict:
    """Assign (or reassign) a driver to a delivery and mark it ASSIGNED."""
    return await update_delivery(task_id=task_id, driver_id=driver_id, status=1)


@mcp.tool()
async def unassign_driver(task_id: int) -> dict:
    """Return a delivery to the UNASSIGNED pool."""
    return await _request(
        "PUT", f"tasks/{task_id}/update", json_body={"status": 0, "driver_id": None}
    )


@mcp.tool()
async def cancel_delivery(task_id: int) -> dict:
    """Cancel a delivery (status CANCELED). Confirm with the user before calling."""
    return await update_delivery(task_id=task_id, status=4)


@mcp.tool()
async def mark_delivery_completed(task_id: int) -> dict:
    """Mark a delivery as COMPLETED (delivered)."""
    return await update_delivery(task_id=task_id, status=3)


@mcp.tool()
async def mark_delivery_failed(task_id: int) -> dict:
    """Mark a delivery as FAILED."""
    return await update_delivery(task_id=task_id, status=8)


@mcp.tool()
async def mark_delivery_active(task_id: int) -> dict:
    """Mark a delivery as ACTIVE (picked up / out for delivery)."""
    return await update_delivery(task_id=task_id, status=2)


@mcp.tool()
async def mark_roundtrip_delivered(task_id: int) -> dict:
    """Mark a round-trip delivery as ROUNDTRIP_DELIVERED."""
    return await update_delivery(task_id=task_id, status=5)


@mcp.tool()
async def reschedule_delivery(task_id: int, new_date: str) -> dict:
    """Move a delivery to another date (dd/mm/yyyy, yyyy-mm-dd, 'tomorrow', '+2'...)."""
    return await update_delivery(task_id=task_id, pickup_at=new_date)


@mcp.tool()
async def track_order(order_id: str) -> dict:
    """Look up an order by its external order id and return each matching delivery's
    status in plain words (plus the raw records)."""
    res = await _request("GET", f"tasks/by_order_id/{_seg(order_id)}")
    if res.get("ok"):
        counts: dict[str, int] = {}
        _collect_statuses(res.get("data"), counts)
        res["status_summary"] = counts
    return res


@mcp.tool()
async def get_deliveries_by_phone_summary(phone: str) -> dict:
    """Find all deliveries for a recipient phone and summarize them by status."""
    res = await _request("GET", f"tasks/by_phone/{_seg(phone)}")
    if res.get("ok"):
        counts: dict[str, int] = {}
        _collect_statuses(res.get("data"), counts)
        res["status_summary"] = counts
    return res


@mcp.tool()
async def add_document_from_url(
    task_id: int, url: str, filename: Optional[str] = None
) -> dict:
    """Download a PDF/PNG/JPEG from a public URL and attach it to a delivery."""
    try:
        r = await _client.get(url, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Could not download file: {exc}"}
    if len(r.content) > 15 * 1024 * 1024:
        return {"ok": False, "error": "File is larger than 15 MB."}
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    head = r.content[:8]
    if not (head.startswith(b"%PDF") or head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8")):
        return {"ok": False, "error": f"Unsupported file type ({ctype or 'unknown'}). "
                                      "Only PDF, PNG and JPEG are supported."}
    name = filename or url.rstrip("/").split("/")[-1].split("?")[0] or "document"
    return await add_document(
        task_id=task_id,
        file_base64=base64.b64encode(r.content).decode(),
        filename=name,
    )


# --------------------------------------------------------------------------- #
# Deliveries — bulk operations
# --------------------------------------------------------------------------- #

_BULK_LIMIT = 200
_sem = asyncio.Semaphore(5)


async def _limited(coro):
    async with _sem:
        return await coro


async def _bulk(items: list, fn) -> dict:
    if len(items) > _BULK_LIMIT:
        return {"ok": False, "error": f"Too many items ({len(items)}); max {_BULK_LIMIT} per call."}
    results = await asyncio.gather(*(_limited(fn(x)) for x in items))
    ok = sum(1 for r in results if r.get("ok"))
    return {
        "ok": ok == len(results),
        "succeeded": ok,
        "failed": len(results) - ok,
        "results": [{"input": x, **r} for x, r in zip(items, results)],
    }


@mcp.tool()
async def bulk_create_deliveries(deliveries: list[dict]) -> dict:
    """Create many deliveries in one call (max 200).

    Each item uses the same field names as create_delivery, e.g.
    {"original_order_id": "A1", "destination_city": "Tel Aviv",
     "destination_street": "Dizengoff", "destination_number": "50",
     "destination_recipient_name": "Dana", "destination_phone": "0501234567",
     "pickup_at": "tomorrow"}.
    Returns per-item results.
    """
    async def one(item: dict) -> dict:
        body = _build_delivery_body(**dict(item))
        return await _request("POST", "tasks/create", json_body=body)
    return await _bulk(deliveries, one)


@mcp.tool()
async def bulk_get_deliveries(task_ids: list[int]) -> dict:
    """Fetch many deliveries by task_id at once (max 200), with a status breakdown."""
    res = await _bulk(task_ids, lambda t: _request("GET", f"tasks/show/{t}"))
    counts: dict[str, int] = {}
    for r in res.get("results", []):
        _collect_statuses(r.get("data"), counts)
    res["status_summary"] = counts
    return res


@mcp.tool()
async def bulk_update_deliveries(
    task_ids: list[int],
    driver_id: Optional[int] = None,
    pickup_at: Optional[str] = None,
    status: Optional[Union[int, str]] = None,
) -> dict:
    """Apply the same change (driver / date / status) to many deliveries (max 200).
    Confirm with the user before bulk-cancelling."""
    return await _bulk(
        task_ids,
        lambda t: update_delivery(task_id=t, driver_id=driver_id, pickup_at=pickup_at, status=status),
    )


@mcp.tool()
async def bulk_assign_driver(task_ids: list[int], driver_id: int) -> dict:
    """Assign one driver to many deliveries at once (max 200)."""
    return await bulk_update_deliveries(task_ids=task_ids, driver_id=driver_id, status=1)


@mcp.tool()
async def bulk_reschedule_deliveries(task_ids: list[int], new_date: str) -> dict:
    """Move many deliveries to a new date at once (max 200)."""
    return await bulk_update_deliveries(task_ids=task_ids, pickup_at=new_date)


@mcp.tool()
async def bulk_cancel_deliveries(task_ids: list[int]) -> dict:
    """Cancel many deliveries at once (max 200). ALWAYS confirm with the user first."""
    return await bulk_update_deliveries(task_ids=task_ids, status=4)


@mcp.tool()
async def bulk_track_orders(order_ids: list[str]) -> dict:
    """Look up many external order ids at once (max 200)."""
    return await _bulk(order_ids, lambda o: _request("GET", f"tasks/by_order_id/{_seg(o)}"))


# --------------------------------------------------------------------------- #
# Drivers / routes
# --------------------------------------------------------------------------- #

@mcp.tool()
async def get_daily_route(driver_id: int, date: Optional[str] = None) -> dict:
    """Get a driver's route for a date (defaults to today)."""
    return await _request(
        "GET", f"drivers/{driver_id}/daily_route", params={"date": _date(date)}
    )


@mcp.tool()
async def get_driver_route_summary(driver_id: int, date: Optional[str] = None) -> dict:
    """Summarize a driver's day: number of stops and a breakdown by status
    (e.g. how many COMPLETED vs still ASSIGNED). Includes the raw route."""
    res = await get_daily_route(driver_id=driver_id, date=date)
    if res.get("ok"):
        counts: dict[str, int] = {}
        _collect_statuses(res.get("data"), counts)
        res["status_summary"] = counts
        res["total_with_status"] = sum(counts.values())
        done = counts.get("COMPLETED", 0) + counts.get("ROUNDTRIP_DELIVERED", 0)
        res["completed"] = done
        res["remaining"] = sum(
            n for s, n in counts.items() if s in ("UNASSIGNED", "ASSIGNED", "ACTIVE")
        )
    return res


@mcp.tool()
async def get_daily_routes_for_drivers(driver_ids: list[int], date: Optional[str] = None) -> dict:
    """Get the routes of several drivers for one date in a single call."""
    return await _bulk(driver_ids, lambda d: get_driver_route_summary(driver_id=d, date=date))


@mcp.tool()
async def optimize_daily_route(driver_id: int, date: Optional[str] = None) -> dict:
    """Optimize a driver's route for a date (defaults to today)."""
    d = _date(date)
    return await _request(
        "POST",
        f"drivers/{driver_id}/optimize_daily_route",
        json_body={"date": d} if d else None,
    )


@mcp.tool()
async def optimize_routes_for_drivers(driver_ids: list[int], date: Optional[str] = None) -> dict:
    """Optimize the routes of several drivers for one date."""
    return await _bulk(driver_ids, lambda d: optimize_daily_route(driver_id=d, date=date))


@mcp.tool()
async def list_routes(date: Optional[str] = None, response_format: str = "json") -> dict:
    """List all routes for a date (defaults to today). `response_format`: json or xml."""
    return await _request(
        "GET", "routes", params={"date": _date(date), "format": response_format}
    )


@mcp.tool()
async def get_routes_summary(date: Optional[str] = None) -> dict:
    """Overview of all routes for a date with a status breakdown across every stop —
    good for 'how is today going?' questions."""
    res = await list_routes(date=date)
    if res.get("ok"):
        counts: dict[str, int] = {}
        _collect_statuses(res.get("data"), counts)
        data = res.get("data")
        routes = data.get("routes", data) if isinstance(data, dict) else data
        res["route_count"] = len(routes) if isinstance(routes, list) else None
        res["status_summary"] = counts
    return res


@mcp.tool()
async def get_routes_for_date_range(start_date: str, end_date: str) -> dict:
    """List routes for every day between two dates (inclusive, max 31 days)."""
    s = dt.datetime.strptime(_date(start_date), "%d/%m/%Y").date()
    e = dt.datetime.strptime(_date(end_date), "%d/%m/%Y").date()
    if e < s:
        s, e = e, s
    days = [(s + dt.timedelta(days=i)).strftime("%d/%m/%Y") for i in range((e - s).days + 1)]
    if len(days) > 31:
        return {"ok": False, "error": "Range too long (max 31 days)."}
    return await _bulk(days, lambda d: get_routes_summary(date=d))


# --------------------------------------------------------------------------- #
# Visits
# --------------------------------------------------------------------------- #

@mcp.tool()
async def get_visit(visit_id: int) -> dict:
    """Fetch a single visit (a route stop) by its visit_id."""
    return await _request("GET", f"visits/{visit_id}")


@mcp.tool()
async def update_visit(
    visit_id: int, driver_id: Optional[int] = None, visit_at: Optional[str] = None
) -> dict:
    """Update a visit's assigned driver and/or date."""
    body = _clean({"driver_id": driver_id, "visit_at": _date(visit_at)})
    if not body:
        return {"ok": False, "error": "Nothing to update — pass driver_id or visit_at."}
    return await _request("PUT", f"visits/{visit_id}", json_body=body)


@mcp.tool()
async def reassign_visit(visit_id: int, driver_id: int) -> dict:
    """Move a visit to a different driver."""
    return await update_visit(visit_id=visit_id, driver_id=driver_id)


@mcp.tool()
async def reschedule_visit(visit_id: int, new_date: str) -> dict:
    """Move a visit to another date."""
    return await update_visit(visit_id=visit_id, visit_at=new_date)


@mcp.tool()
async def bulk_get_visits(visit_ids: list[int]) -> dict:
    """Fetch many visits at once (max 200)."""
    return await _bulk(visit_ids, lambda v: _request("GET", f"visits/{v}"))


@mcp.tool()
async def bulk_update_visits(
    visit_ids: list[int], driver_id: Optional[int] = None, visit_at: Optional[str] = None
) -> dict:
    """Reassign and/or reschedule many visits at once (max 200)."""
    return await _bulk(
        visit_ids, lambda v: update_visit(visit_id=v, driver_id=driver_id, visit_at=visit_at)
    )


# --------------------------------------------------------------------------- #
# Companies (customers)
# --------------------------------------------------------------------------- #

@mcp.tool()
async def get_company(company_id: int) -> dict:
    """Fetch company details (name, contacts, default location) by company_id."""
    return await _request("GET", f"companies/{company_id}")


@mcp.tool()
async def bulk_get_companies(company_ids: list[int]) -> dict:
    """Fetch several companies at once (max 200)."""
    return await _bulk(company_ids, lambda c: _request("GET", f"companies/{c}"))


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
    """Create a company (customer). `default_location_attributes` accepts name/city/
    street/number/floor/apartment/zip_code/notes/default_recipient_name/default_phone/
    default_email. Times are like "10:00".
    """
    body = _clean({
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
    })
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
    body = _clean({
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
    })
    if not body:
        return {"ok": False, "error": "Nothing to update."}
    return await _request("PATCH", f"companies/{company_id}", json_body=body)


@mcp.tool()
async def activate_company(company_id: int) -> dict:
    """Set a company to active."""
    return await update_company(company_id=company_id, is_active=True)


@mcp.tool()
async def deactivate_company(company_id: int) -> dict:
    """Set a company to inactive. Confirm with the user first."""
    return await update_company(company_id=company_id, is_active=False)


@mcp.tool()
async def update_company_default_location(
    company_id: int,
    city: Optional[str] = None,
    street: Optional[str] = None,
    number: Optional[str] = None,
    floor: Optional[str] = None,
    apartment: Optional[str] = None,
    zip_code: Optional[str] = None,
    notes: Optional[str] = None,
    name: Optional[str] = None,
    default_recipient_name: Optional[str] = None,
    default_phone: Optional[str] = None,
    default_email: Optional[str] = None,
    replace_existing: bool = True,
) -> dict:
    """Change a company's default (pickup) address/contact.

    With replace_existing=True the current default location is looked up and edited
    in place; with False a new default location is created.
    """
    loc = _clean({
        "name": name, "city": city, "street": street, "number": number, "floor": floor,
        "apartment": apartment, "zip_code": zip_code, "notes": notes,
        "default_recipient_name": default_recipient_name,
        "default_phone": default_phone, "default_email": default_email,
    })
    if not loc:
        return {"ok": False, "error": "Pass at least one location field."}
    if replace_existing:
        cur = await get_company(company_id)
        data = cur.get("data") if cur.get("ok") else None
        if isinstance(data, dict):
            company = data.get("company", data)
            location = company.get("location") if isinstance(company, dict) else None
            if isinstance(location, dict) and location.get("id"):
                loc["id"] = location["id"]
    return await update_company(company_id=company_id, default_location_attributes=loc)


@mcp.tool()
async def update_company_time_window(
    company_id: int, default_earliest_at: str, default_latest_at: str
) -> dict:
    """Set a company's default delivery time window, e.g. "09:00" – "17:00"."""
    return await update_company(
        company_id=company_id,
        default_earliest_at=default_earliest_at,
        default_latest_at=default_latest_at,
    )


# --------------------------------------------------------------------------- #
# Utility / reference
# --------------------------------------------------------------------------- #

@mcp.tool()
async def get_reference_codes() -> dict:
    """Return LionWheel's code tables: task statuses (and which can be set via the
    API), cash-on-delivery types, urgency levels and date/money formats."""
    return {
        "task_status": STATUS_CODES,
        "settable_statuses": {s: STATUS_CODES[s] for s in sorted(SETTABLE_STATUSES)},
        "cod_type": COD_TYPES,
        "urgency": URGENCY,
        "formats": {
            "date": "dd/mm/yyyy (this server also accepts yyyy-mm-dd, today, tomorrow, +N)",
            "time": "HH:MM",
            "money_collect": "integer cents (300 = 3.00)",
        },
    }


@mcp.tool()
async def get_server_info() -> dict:
    """Show this MCP server's configuration (never reveals the API key)."""
    return {
        "base_url": BASE_URL,
        "sandbox": "test.lionwheel.com" in BASE_URL,
        "api_key_configured": bool(API_KEY),
        "api_key_type": ("customer (c_key)" if API_KEY.startswith("c_key")
                         else "shipping company" if API_KEY else None),
        "default_company_id": DEFAULT_COMPANY_ID,
        "timezone": TIMEZONE,
        "today": _today().strftime("%d/%m/%Y"),
        "read_only": READ_ONLY,
    }


@mcp.tool()
async def check_connection() -> dict:
    """Verify the server can reach LionWheel and that the API key is accepted."""
    res = await _request("GET", "routes", params={"date": _date("today"), "format": "json"})
    return {
        "ok": res.get("ok", False),
        "status": res.get("status"),
        "message": "Connected to LionWheel." if res.get("ok") else res.get("error"),
    }


@mcp.tool()
async def lionwheel_api_request(
    method: str,
    path: str,
    query: Optional[dict] = None,
    body: Optional[dict] = None,
) -> dict:
    """Advanced: call any LionWheel API v1 endpoint directly (for endpoints not covered
    by other tools). `path` is relative to /api/v1, e.g. "tasks/show/123". The API key
    is added automatically. Use other tools first; confirm write calls with the user.
    """
    method = method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return {"ok": False, "error": "method must be GET, POST, PUT, PATCH or DELETE"}
    if "://" in path or ".." in path:
        return {"ok": False, "error": "path must be relative to the API base, e.g. 'tasks/show/1'"}
    q = {k: v for k, v in (query or {}).items() if k != "key"}
    return await _request(method, path, json_body=body, params=q)


# --------------------------------------------------------------------------- #
# Prompts (ready-made workflows shown in Claude's prompt menu)
# --------------------------------------------------------------------------- #

@mcp.prompt()
def daily_operations_report(date: str = "today") -> str:
    """Build an operations report for a day."""
    return (
        f"Use get_routes_summary for {date}. Then report: number of routes, total stops, "
        "breakdown by status, and highlight FAILED / still UNASSIGNED deliveries. "
        "Suggest concrete next actions (reassign, reschedule, optimize)."
    )


@mcp.prompt()
def customer_order_lookup(order_or_phone: str) -> str:
    """Find a customer's deliveries by order id or phone."""
    return (
        f"Find deliveries for '{order_or_phone}'. If it looks like a phone number use "
        "get_deliveries_by_phone_summary, otherwise use track_order. Reply with each "
        "delivery's status, date and driver in a short table."
    )


@mcp.prompt()
def driver_day_review(driver_id: str, date: str = "today") -> str:
    """Review a driver's day."""
    return (
        f"Use get_driver_route_summary for driver {driver_id} on {date}. Summarize "
        "progress (completed vs remaining), list failed stops, and ask whether to "
        "optimize the remaining route."
    )


# --------------------------------------------------------------------------- #
# HTTP app with optional bearer-token auth
# --------------------------------------------------------------------------- #

class _AuthAndHealth:
    """ASGI wrapper: serves /health without auth and enforces MCP_BEARER_TOKEN."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def _send_json(self, send, status: int, payload: bytes):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": payload})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path in ("/", "/health", "/healthz"):
            return await self._send_json(
                send, 200, b'{"status":"ok","service":"lionwheel-mcp","mcp_path":"/mcp"}'
            )
        if self.token:
            supplied = ""
            for k, v in scope.get("headers", []):
                if k == b"authorization":
                    val = v.decode("latin-1")
                    if val.lower().startswith("bearer "):
                        supplied = val[7:].strip()
            if not supplied:
                qs = parse_qs(scope.get("query_string", b"").decode())
                supplied = (qs.get("token") or [""])[0]
            if not hmac.compare_digest(supplied.encode(), self.token.encode()):
                return await self._send_json(send, 401, b'{"error":"unauthorized"}')
        return await self.app(scope, receive, send)


def build_http_app():
    return _AuthAndHealth(mcp.streamable_http_app(), BEARER_TOKEN)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    transport = os.environ.get("TRANSPORT", "stdio").lower()
    if transport == "http":
        import uvicorn

        port = int(os.environ.get("PORT", "8000"))
        if not BEARER_TOKEN:
            print("WARNING: MCP_BEARER_TOKEN is not set — the /mcp endpoint is public!")
        uvicorn.run(build_http_app(), host="0.0.0.0", port=port, proxy_headers=True,
                    forwarded_allow_ips="*")
    else:
        mcp.run(transport="stdio")
