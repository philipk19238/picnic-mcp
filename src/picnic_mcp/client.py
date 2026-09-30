"""PicnicClient — the Picnic (trypicnic.com) API as an HttpBaseClient subclass.

Picnic's order app is an Otter/CloudKitchens white-label storefront. All of
the Picnic-specific GraphQL operations (viewer, cart, orders, hubs, ...) go
through a gateway on the app's own origin:

    POST https://order.trypicnic.com/api/picnic/graphql?operation=<Name>

The operation documents are scraped from the web app's bundles into
`operations/*.graphql` by `scripts/extract_operations.py`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from functools import cache
from importlib.resources import files
from typing import Any

import httpx

from .auth import PicnicCredentials
from .http_base_client import HttpBaseClient, ResponseError, map_status_to_response_error

PICNIC = "picnic"
ORIGIN = "https://order.trypicnic.com"
SESSION_COOKIE = "_oauth2_proxy"


@cache
def load_operation(name: str) -> str:
    """Return the GraphQL source for a scraped operation (without the route header)."""
    text = files(__package__).joinpath("operations", f"{name}.graphql").read_text()
    return "\n".join(line for line in text.splitlines() if not line.startswith("# route:"))


class PicnicClient(HttpBaseClient):
    base_url = ORIGIN

    def __init__(
        self,
        credentials: PicnicCredentials,
        *,
        http: httpx.AsyncClient | None = None,
        on_credentials_rotated: Callable[[PicnicCredentials], None] | None = None,
    ):
        super().__init__(PICNIC, http=http)
        self._credentials = credentials
        self._on_credentials_rotated = on_credentials_rotated

    def default_headers(self) -> dict[str, str]:
        # Mirrors what the web app's Apollo links send.
        return {
            "Accept": "application/json",
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/",
            "User-Agent": "Mozilla/5.0 (picnic-mcp)",
            "application-name": "d2c-facility-app",
            "consistent-authz": "true",
        }

    async def build_auth_headers(self) -> dict[str, str]:
        return self._credentials.to_headers()

    def on_response(self, response: httpx.Response) -> None:
        # oauth2-proxy re-issues the session cookie once it's old enough;
        # swap it into our cookie string so the session keeps living.
        rotated = response.cookies.get(SESSION_COOKIE)
        if not rotated or not self._credentials.cookie:
            return
        parts = [p.strip() for p in self._credentials.cookie.split(";") if p.strip()]
        parts = [p for p in parts if not p.startswith(f"{SESSION_COOKIE}=")]
        self._credentials = replace(
            self._credentials, cookie="; ".join([*parts, f"{SESSION_COOKIE}={rotated}"])
        )
        if self._on_credentials_rotated:
            self._on_credentials_rotated(self._credentials)

    def map_http_error(self, response: httpx.Response, body: Any) -> Exception:
        # The gateway answers auth failures with `{"error":"Unauthorized"}`,
        # sometimes without a JSON content-type.
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                pass
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            return map_status_to_response_error(
                response.status_code,
                f"Picnic API error: {body['error']}",
                {"vendor_error": body["error"]},
            )
        return super().map_http_error(response, body)

    # ── GraphQL ──────────────────────────────────────────────────────────

    async def graphql(self, operation: str, variables: dict[str, Any] | None = None) -> Any:
        """Run a scraped operation by name and return its `data`.

        GraphQL errors arrive with HTTP 200, so they're mapped to
        `ResponseError` here: UNAUTHENTICATED → 401, FORBIDDEN → 403,
        NOT_FOUND → 404, invalid input → 400, anything else → 502.
        """
        payload = await self.post(
            "/api/picnic/graphql",
            {"operationName": operation, "query": load_operation(operation),
             "variables": variables or {}},
            endpoint=f"{PICNIC}/{operation}",
            query={"operation": operation},
        )
        errors = (payload or {}).get("errors")
        if errors:
            raise _graphql_error(operation, errors, payload)
        return payload["data"]

    # ── Session ──────────────────────────────────────────────────────────

    @property
    def credentials(self) -> PicnicCredentials:
        return self._credentials

    async def refresh_session(self) -> dict[str, Any]:
        """Touch the session so oauth2-proxy can refresh it.

        The login requests `offline_access`, so the `_oauth2_proxy` cookie
        carries a refresh token. Once the cookie passes the proxy's refresh
        interval, any authenticated request makes the proxy redeem it and
        re-issue the cookie; `on_response` picks that up. This endpoint is
        just the cheapest authenticated request. Returns the account info.
        """
        return await self.get("/api/iam/v0/session/info", endpoint=f"{PICNIC}/sessionInfo")

    # ── Public API methods ───────────────────────────────────────────────
    #
    # Thin 1:1 wrappers over the scraped operations. Shapes of the input
    # objects were recovered from the web app's call sites:
    #
    #   HubOrderConstraint = {hubId, routeId, deliveryWindowStart, deliveryWindowEnd}
    #   CartDomain         = {hubDomain: {hubId, deliveryWindowStart}}
    #   CustomerItem       = {customerItemId, storeId, name, quantity, note, price,
    #                         modifiers: [{customerItemId, modifierGroupId, name,
    #                                      quantity, price, modifiers}]}
    #   Money              = {currencyCode, units, nanos}

    async def get_viewer(self) -> dict[str, Any]:
        """The signed-in eater: id, home hub, preferences, organizations."""
        data = await self.graphql("PicnicViewer", {"includeGiveReferralCouponCode": False})
        return data["picnicEater"]

    async def get_hub(self, hub_id: str, start_date: str, end_date: str) -> dict[str, Any]:
        """A delivery hub plus its delivery windows (`individualRoutes`) between
        two local dates (YYYY-MM-DD, end exclusive)."""
        data = await self.graphql(
            "DeliveryHubById", {"hubId": hub_id, "startDate": start_date, "endDate": end_date}
        )
        return data["deliveryHubById"]["hub"]

    async def get_hub_main_content(self, hub_order_constraint: dict[str, Any]) -> dict[str, Any]:
        """Restaurants (`storeTiles`) and items available for one delivery window."""
        data = await self.graphql(
            "HubsMainContent",
            {"hubsMainContentInput": {"hubOrderConstraint": hub_order_constraint}},
        )
        return data["hubsMainContent"]

    async def search(self, query: str, hub_order_constraint: dict[str, Any]) -> dict[str, Any]:
        """Full-text search over items and restaurants for one delivery window."""
        data = await self.graphql(
            "SearchItemsAndStoresForHub",
            {"input": {"query": query, "hubOrderConstraint": hub_order_constraint}},
        )
        return data["searchItemsAndStoresForHub"]

    async def get_store_content(
        self, store_id: str, facility_id: str, hub_order_constraint: dict[str, Any]
    ) -> dict[str, Any]:
        """A restaurant and its menu (categories, items, modifier groups)."""
        data = await self.graphql(
            "storeContent",
            {
                "storeConstraints": {
                    "storeId": store_id,
                    "facilityConstraints": {
                        "facilityId": facility_id,
                        "customerInteractionSource": "CUSTOMER_INTERACTION_SOURCE_EATER_APP",
                        "serviceSlug": PICNIC,
                        "fulfillmentMode": "FULFILLMENT_MODE_PICKUP",
                        "orderConstraint": {"hubOrderConstraint": hub_order_constraint},
                    },
                }
            },
        )
        return data["storeContent"]

    # Cart. Every cart mutation takes the cart domain plus `cartVersion: 0`
    # (the web app always sends 0; the server resolves the latest version).

    async def get_cart(self, domain: dict[str, Any]) -> dict[str, Any]:
        return (await self.graphql("GetCart", {"cartDomainInput": domain}))["getCart"]

    async def add_cart_item(
        self, domain: dict[str, Any], customer_item: dict[str, Any], cart_item_id: str
    ) -> dict[str, Any]:
        data = await self.graphql(
            "AddItem",
            {"input": {"domain": domain, "cartVersion": 0, "cartItemId": cart_item_id,
                       "customerItem": customer_item}},
        )
        return data["addItem"]

    async def update_cart_item_quantity(
        self, domain: dict[str, Any], cart_item_id: str, quantity: int
    ) -> dict[str, Any]:
        data = await self.graphql(
            "UpdateItemQuantity",
            {"input": {"domain": domain, "cartVersion": 0, "cartItemId": cart_item_id,
                       "quantity": quantity}},
        )
        return data["updateItemQuantity"]

    async def remove_cart_item(self, domain: dict[str, Any], cart_item_id: str) -> dict[str, Any]:
        data = await self.graphql(
            "RemoveItem",
            {"input": {"domain": domain, "cartVersion": 0, "cartItemId": cart_item_id}},
        )
        return data["removeItem"]

    async def empty_cart(self, domain: dict[str, Any]) -> dict[str, Any]:
        data = await self.graphql("EmptyCart", {"input": {"domain": domain, "cartVersion": 0}})
        return data["emptyCart"]

    # Meal calendar. Office orders are calendar entries: each entry turns
    # into a real order at its `placeOrderAt` (the entry id becomes the
    # order id). Rotation-generated entries come from the eater's rotation.

    async def get_schedule(self) -> dict[str, Any]:
        data = await self.graphql("GetCalendaringSchedule")
        return data["getCalendaringSchedule"]["enrichedSchedule"]

    async def add_schedule_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        data = await self.graphql(
            "AddCalendaringScheduleEntry", {"input": {"calendaringScheduleEntry": entry}}
        )
        return data["addCalendaringScheduleEntry"]["schedule"]

    async def remove_schedule_entry(self, entry_id: str) -> dict[str, Any]:
        data = await self.graphql(
            "RemoveCalendaringScheduleEntry", {"input": {"entryId": entry_id}}
        )
        return data["removeCalendaringScheduleEntry"]["schedule"]

    # Orders.

    async def list_orders(self) -> list[dict[str, Any]]:
        """Recent orders and upcoming schedule entries, newest first."""
        data = await self.graphql("QueryPicnicOrders", {"input": {}})
        return [edge["node"] for edge in data["queryPicnicOrders"]["edges"]]

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """Receipt for a placed order (not for entries still waiting to be placed)."""
        data = await self.graphql("PicnicOrderById", {"input": {"id": order_id}})
        return data["picnicOrderById"]

    async def cancel_order(
        self, order_id: str, email: str, reason: str = "REASON_OTHER"
    ) -> dict[str, Any]:
        """Cancel a placed order. `reason` is one of REASON_ACCIDENTAL_ORDER,
        REASON_CUSTOMER_MISORDERED, REASON_ADDRESS_INCOMPLETE,
        REASON_CUSTOMER_FORGOT_TO_USE_COUPON, REASON_CUSTOMER_SELECTED_WRONG_PAYMENT,
        REASON_OTHER."""
        data = await self.graphql(
            "CancelPicnicOrder",
            {"input": {"orderId": order_id, "reason": reason,
                       "cancellingParty": {"personType": "PERSON_TYPE_EATER", "email": email}}},
        )
        return data["cancelPicnicOrder"]


_GRAPHQL_STATUS = {
    "UNAUTHENTICATED": 401,
    "AUTHENTICATION_ERROR": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "BAD_USER_INPUT": 400,
    "ValidationError": 400,
}


def _graphql_error(operation: str, errors: list[dict[str, Any]], payload: dict) -> ResponseError:
    first = errors[0]
    ext = first.get("extensions") or {}
    code = ext.get("code") or ext.get("errorCode") or ext.get("classification")
    message = first.get("message", "unknown GraphQL error")
    # graphql-java rejects bad variables with messages like
    # "Field 'x' has coerced Null value for NonNull type ..." and no code.
    is_input_error = "coerced" in message or "invalid value" in message
    return ResponseError(
        f"Picnic {operation} failed: {message}",
        _GRAPHQL_STATUS.get(code, 400 if is_input_error else 502),
        {"graphql_errors": errors, "code": code,
         "trace_id": (payload.get("extensions") or {}).get("traceId")},
    )
