"""FastMCP server exposing Picnic (trypicnic.com) office lunch ordering.

Tools are thin adapters over PicnicService; see service.py for how Picnic's
meal calendar, carts and orders fit together.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .auth import MissingCredentialsError, PicnicCredentials
from .client import PicnicClient
from .http_base_client import ResponseError
from .login import LoginError, PendingLogin, start_login, verify_login
from .service import LineItem, PicnicInputError, PicnicService

mcp = FastMCP(
    "picnic",
    instructions="""\
Order office lunch from Picnic (trypicnic.com).

How it works: lunch is delivered to the user's office hub once per weekday.
Each weekday is a slot on the user's *meal calendar*. A calendar entry holds a
meal and is turned into a real order automatically at its `place_order_at`
time (~9:30am local); until then it can be changed or removed freely. After
that it's a placed order and can only be cancelled. The company stipend is
applied automatically; `total_due` in a cart is what the user would pay.

If a tool reports the session expired (or no credentials exist), sign the
user in with login_start(email) and then login_verify(code) using the code
Picnic sends them.

Typical flow: list_restaurants / search_menu → get_menu (for item and
modifier ids) → schedule_meal with the chosen items (or build a cart with
add_to_cart, then schedule_meal from_cart=true). To change a day's meal use
schedule_meal with replace=true. To skip a day use cancel_meal.

`date` arguments take YYYY-MM-DD, "today" or "tomorrow"; omitted means the
next delivery window still open. Always confirm with the user before
scheduling, replacing or cancelling meals.""",
)

READ = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}

DateArg = Annotated[
    str | None, Field(description='YYYY-MM-DD, "today" or "tomorrow"; default: next open window')
]


@asynccontextmanager
async def _service() -> AsyncIterator[PicnicService]:
    # Credentials are re-read per call so `picnic-mcp auth set` takes effect
    # without restarting the server; rotated session cookies are persisted.
    try:
        credentials = PicnicCredentials.load()
    except MissingCredentialsError as err:
        raise ToolError(
            "Not signed in to Picnic. Ask for the user's email and call login_start."
        ) from err
    async with PicnicClient(credentials, on_credentials_rotated=PicnicCredentials.save) as client:
        try:
            yield PicnicService(client)
        except PicnicInputError as err:
            raise ToolError(str(err)) from err
        except ResponseError as err:
            if err.status_code == 401:
                raise ToolError(
                    "Picnic session expired. Sign in again with login_start(email) and "
                    "login_verify(code)."
                ) from err
            raise ToolError(f"Picnic API error ({err.status_code}): {err.message}") from err


# ── Login ────────────────────────────────────────────────────────────────

# One pending login per email while we wait for the user's code.
_pending_logins: dict[str, PendingLogin] = {}


@mcp.tool(annotations=WRITE)
async def login_start(
    email: str,
    channel: Literal["email", "sms"] = "email",
) -> dict[str, Any]:
    """Start signing in to Picnic: Picnic sends a one-time code to the
    account's email (or phone with channel="sms"). Then call login_verify."""
    if old := _pending_logins.pop(email, None):
        await old.aclose()
    try:
        _pending_logins[email] = await start_login(email, channel)
    except LoginError as err:
        raise ToolError(str(err)) from err
    return {"status": "code_sent", "email": email, "next": "ask the user for the code, then login_verify"}


@mcp.tool(annotations=WRITE)
async def login_verify(
    code: str,
    email: Annotated[str | None, Field(description="Needed only if several logins are pending")] = None,
) -> dict[str, Any]:
    """Finish signing in with the one-time code and save the session."""
    if email is None and len(_pending_logins) == 1:
        email = next(iter(_pending_logins))
    pending = _pending_logins.get(email or "")
    if pending is None:
        raise ToolError("No login in progress; call login_start first.")
    try:
        credentials = await verify_login(pending, code)
    except LoginError as err:
        raise ToolError(str(err)) from err
    _pending_logins.pop(pending.email, None)
    credentials.save()
    return {"status": "logged_in", "email": pending.email}


# ── Account & delivery ───────────────────────────────────────────────────


@mcp.tool(annotations=READ)
async def whoami() -> dict[str, Any]:
    """The signed-in Picnic account and their delivery hub."""
    async with _service() as svc:
        session = await svc.client.refresh_session()
        hub = await svc.hub()
        return {
            "email": session.get("username"),
            "account_id": session.get("accountId"),
            "hub": {"id": hub["id"], "name": hub["name"], "floor": hub.get("floor"),
                    "pickup": hub.get("eaterPickupInstructions"), "timezone": hub["timezone"]},
        }


@mcp.tool(annotations=READ)
async def list_delivery_windows(
    days: Annotated[int, Field(ge=1, le=21)] = 7,
) -> list[dict[str, Any]]:
    """Upcoming delivery windows at the user's hub (one per delivery day)."""
    async with _service() as svc:
        return [w.view() for w in await svc.delivery_windows(days)]


# ── Restaurants & menus ──────────────────────────────────────────────────


@mcp.tool(annotations=READ)
async def list_restaurants(
    date: DateArg = None,
    cuisine: Annotated[str | None, Field(description="Filter, e.g. 'Asian', 'Salads'")] = None,
) -> dict[str, Any]:
    """Restaurants delivering to the user's hub on a given day."""
    async with _service() as svc:
        result = await svc.list_restaurants(date)
        if cuisine:
            needle = cuisine.lower()
            result["restaurants"] = [
                r for r in result["restaurants"]
                if any(needle in c.lower() for c in r["cuisines"]) or needle in r["name"].lower()
            ]
        return result


@mcp.tool(annotations=READ)
async def search_menu(
    query: Annotated[str, Field(description="Dish, ingredient or restaurant, e.g. 'katsu'")],
    date: DateArg = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    """Search dishes and restaurants available on a given day."""
    async with _service() as svc:
        result = await svc.search(query, date)
        result["total_items"] = len(result["items"])
        result["items"] = result["items"][:limit]
        return result


@mcp.tool(annotations=READ)
async def get_menu(store_id: str, date: DateArg = None) -> dict[str, Any]:
    """A restaurant's menu for a given day: categories of items (with prices)
    plus the modifier groups referenced by `modifier_group_ids`, each with
    min/max choices and option ids to use when ordering."""
    async with _service() as svc:
        return await svc.get_menu(store_id, date)


# ── Cart ─────────────────────────────────────────────────────────────────


@mcp.tool(annotations=READ)
async def get_cart(date: DateArg = None) -> dict[str, Any]:
    """The cart for a delivery day, with stipend discount and total due."""
    async with _service() as svc:
        return await svc.get_cart(date)


@mcp.tool(annotations=WRITE)
async def add_to_cart(item: LineItem, date: DateArg = None) -> dict[str, Any]:
    """Add a menu item (with modifier choices) to a day's cart. Required
    modifier groups must be satisfied; the error lists valid options.
    Returns the updated cart including what the stipend covers."""
    async with _service() as svc:
        return await svc.add_to_cart(item, date)


@mcp.tool(annotations=WRITE)
async def update_cart_item(
    cart_item_id: str,
    quantity: Annotated[int, Field(ge=0, description="0 removes the item")],
    date: DateArg = None,
) -> dict[str, Any]:
    """Change the quantity of a cart line (0 removes it)."""
    async with _service() as svc:
        return await svc.update_cart_item(cart_item_id, quantity, date)


@mcp.tool(annotations=DESTRUCTIVE)
async def clear_cart(date: DateArg = None) -> dict[str, Any]:
    """Remove everything from a day's cart."""
    async with _service() as svc:
        return await svc.clear_cart(date)


# ── Meal calendar & orders ───────────────────────────────────────────────


@mcp.tool(annotations=READ)
async def get_meal_schedule(include_past: bool = False) -> dict[str, Any]:
    """The user's meal calendar: one entry per delivery day with its meal,
    status (scheduled / ordered / cancelled) and when it will be ordered."""
    async with _service() as svc:
        return await svc.get_schedule(include_past)


@mcp.tool(annotations=DESTRUCTIVE)
async def schedule_meal(
    date: Annotated[str, Field(description='Delivery day: YYYY-MM-DD, "today" or "tomorrow"')],
    items: Annotated[
        list[LineItem] | None, Field(description="Items to order; omit when from_cart=true")
    ] = None,
    from_cart: Annotated[bool, Field(description="Use (and then empty) the day's cart")] = False,
    replace: Annotated[
        bool, Field(description="Swap out a meal already scheduled that day (not yet ordered)")
    ] = False,
) -> dict[str, Any]:
    """Order lunch for a day by putting a meal on the calendar. Picnic places
    the order automatically at `place_order_at`; until then it can be
    replaced or cancelled. Confirm the meal with the user first."""
    async with _service() as svc:
        return await svc.schedule_meal(date, items, from_cart=from_cart, replace=replace)


@mcp.tool(annotations=DESTRUCTIVE)
async def cancel_meal(
    entry_or_order_id: Annotated[
        str, Field(description="`entry_id` from get_meal_schedule or `order_id` from list_orders")
    ],
    reason: Literal[
        "REASON_OTHER", "REASON_ACCIDENTAL_ORDER", "REASON_CUSTOMER_MISORDERED",
        "REASON_ADDRESS_INCOMPLETE", "REASON_CUSTOMER_FORGOT_TO_USE_COUPON",
        "REASON_CUSTOMER_SELECTED_WRONG_PAYMENT",
    ] = "REASON_OTHER",
) -> dict[str, Any]:
    """Skip a day's lunch. Removes the calendar entry if it hasn't been
    ordered yet; otherwise cancels the placed order (which Picnic may refuse
    close to delivery). Confirm with the user first."""
    async with _service() as svc:
        return await svc.cancel(entry_or_order_id, reason)


@mcp.tool(annotations=READ)
async def list_orders() -> list[dict[str, Any]]:
    """Recent and upcoming orders (placed, scheduled and rotation-generated)."""
    async with _service() as svc:
        return await svc.list_orders()


@mcp.tool(annotations=READ)
async def get_order(order_id: str) -> dict[str, Any]:
    """Full receipt for a placed order (payments, items, delivery status)."""
    async with _service() as svc:
        return await svc.get_order(order_id)
