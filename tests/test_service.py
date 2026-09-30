import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from picnic_mcp.service import (
    LineItem,
    ModifierChoice,
    PicnicInputError,
    PicnicService,
    meal_contents_hash,
    money,
)

USD = lambda dollars: {"currencyCode": "USD", "units": int(dollars), "nanos": round(dollars % 1 * 1e9)}  # noqa: E731

HUB_ID, ROUTE_ID, FACILITY, STORE = "hub-1", "route-1", "fac-1", "store-1"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_day(offset: int) -> tuple[str, str]:
    """(local date, UTC window start) for a noon-ET delivery `offset` days out."""
    day = (datetime.now(UTC) + timedelta(days=offset)).date()
    return day.isoformat(), f"{day.isoformat()}T16:00:00Z"


def _item(item_id, name, price, groups=()):
    return {"key": item_id, "value": {
        "id": item_id, "storeId": STORE, "name": name, "priceData": {"price": USD(price)},
        "isSuspended": False, "itemStatus": "ITEM_STATUS_AVAILABLE",
        "modifierGroupIds": list(groups),
    }}


def _group(group_id, name, item_ids, lo, hi):
    return {"key": group_id, "value": {
        "id": group_id, "name": name, "itemIds": item_ids,
        "selectionData": {"minimumNumberOfChoices": lo, "maximumNumberOfChoices": hi},
    }}


MENU = {
    "storeId": STORE, "brandName": "Bowls", "tags": ["TAG_ASIAN"], "menu": {
        "menuInfos": [{"categoryIds": ["cat"]}],
        "categories": [{"key": "cat", "value": {"name": "Bowls", "itemIds": ["bowl"]}}],
        "items": [
            _item("bowl", "Two Protein Bowl", 16.5, ["rice", "p1", "p2"]),
            _item("white", "White Rice", 0), _item("brown", "Brown Rice", 0),
            _item("chicken", "Chicken", 0), _item("salmon", "Salmon", 2.5),
        ],
        "modifierGroups": [
            _group("rice", "Rice", ["white", "brown"], 1, 1),
            _group("p1", "Protein", ["chicken", "salmon"], 1, 1),
            _group("p2", "Second protein", ["chicken", "salmon"], 1, 1),
        ],
    },
}


class FakeClient:
    """Just enough of PicnicClient for the service, recording writes."""

    def __init__(self, entries=()):
        self.entries = list(entries)
        self.calls: list[tuple] = []
        self.windows = [_window_day(d) for d in (1, 2, 3)]

    async def get_viewer(self):
        return {"hubId": HUB_ID}

    async def get_hub(self, hub_id, start, end):
        return {"id": HUB_ID, "name": "Office", "timezone": "America/New_York", "individualRoutes": [
            {"routeId": ROUTE_ID, "restrictedFacility": {"id": "hub-fac"}, "windowDisplayName": "Lunch",
             "deliveryWindowStart": start_utc, "deliveryWindowEnd": start_utc.replace("16:00", "16:30"),
             "minOrderingLeadTime": start_utc.replace("16:00", "15:00")}
            for _, start_utc in self.windows
        ]}

    async def get_hub_main_content(self, hoc):
        return {"taggedLayout": {"storeTiles": [{"key": STORE, "value": {
            "storeId": STORE, "facilityId": FACILITY, "brandName": "Bowls", "tags": []}}]}}

    async def get_store_content(self, store_id, facility_id, hoc):
        assert facility_id == FACILITY
        return MENU

    async def get_schedule(self):
        return {"rotationEnabled": True, "storeDataMap": [], "calendaringScheduleEntries": self.entries}

    async def add_schedule_entry(self, entry):
        self.calls.append(("add", entry))
        self.entries.append({**entry, "deliveryInfo": {**entry["deliveryInfo"], "timezone": "America/New_York"},
                             "state": {}})

    async def remove_schedule_entry(self, entry_id):
        self.calls.append(("remove", entry_id))
        self.entries = [e for e in self.entries if e["id"] != entry_id]

    async def refresh_session(self):
        return {"username": "me@example.com"}

    async def cancel_order(self, order_id, email, reason):
        self.calls.append(("cancel_order", order_id, email, reason))
        return {"orderId": order_id, "success": "true"}


def _entry(entry_id, start, order_id=None):
    return {"id": entry_id, "source": "USER_ADDED",
            "deliveryInfo": {"hubId": HUB_ID, "deliveryWindowStart": start, "timezone": "America/New_York"},
            "meal": {"mealItems": []}, "state": {"orderId": order_id}}


BOWL = LineItem(store_id=STORE, item_id="bowl", modifiers=[
    ModifierChoice(item_id="brown"), ModifierChoice(item_id="chicken"), ModifierChoice(item_id="salmon"),
])


def test_money():
    assert money(USD(16.5)) == 16.5
    assert money({"units": -4, "nanos": -340000000}) == -4.34
    assert money(None) is None


def test_meal_hash_sorts_items_and_recurses_into_modifiers():
    items = [
        {"customerItemId": "b", "quantity": 1, "modifiers": [
            {"customerItemId": "z", "quantity": 1}, {"customerItemId": "y", "quantity": 2}]},
        {"customerItemId": "a", "quantity": 3},
    ]
    expected = hashlib.sha256(b"a:3+b:1+y:2+z:1").hexdigest()
    assert meal_contents_hash(items) == expected


async def test_window_resolution():
    svc = PicnicService(FakeClient())
    day, start = _window_day(2)
    assert (await svc.window(day)).start == start
    assert (await svc.window()).local_date == _window_day(1)[0]
    with pytest.raises(PicnicInputError, match="No delivery window"):
        await svc.window("1999-01-01")


async def test_build_item_fills_shared_option_groups_in_order():
    svc = PicnicService(FakeClient())
    w = await svc.window()
    item = await svc.build_customer_item(w, BOWL)
    assert [(m["modifierGroupId"], m["customerItemId"]) for m in item["modifiers"]] == [
        ("rice", "brown"), ("p1", "chicken"), ("p2", "salmon")]
    assert item["price"] == USD(16.5)


async def test_build_item_reports_missing_required_group_with_options():
    svc = PicnicService(FakeClient())
    w = await svc.window()
    with pytest.raises(PicnicInputError, match=r"'Rice' needs exactly 1.*Brown Rice \(brown\)"):
        await svc.build_customer_item(w, LineItem(store_id=STORE, item_id="bowl"))


async def test_build_item_rejects_foreign_option():
    svc = PicnicService(FakeClient())
    w = await svc.window()
    line = LineItem(store_id=STORE, item_id="bowl", modifiers=[ModifierChoice(item_id="pizza")])
    with pytest.raises(PicnicInputError, match="isn't a modifier"):
        await svc.build_customer_item(w, line)


async def test_schedule_meal_sends_hashed_entry():
    client = FakeClient()
    day, start = _window_day(1)
    result = await PicnicService(client).schedule_meal(day, [BOWL])
    (op, entry), = client.calls
    assert op == "add" and entry["deliveryInfo"] == {"hubId": HUB_ID, "deliveryWindowStart": start}
    assert entry["meal"]["mealContentsHash"] == meal_contents_hash(entry["meal"]["mealItems"])
    assert result["scheduled"]["entry_id"] == entry["id"]


async def test_schedule_meal_requires_replace_for_existing_entry():
    day, start = _window_day(1)
    client = FakeClient([_entry("old", start)])
    with pytest.raises(PicnicInputError, match="replace=true"):
        await PicnicService(client).schedule_meal(day, [BOWL])
    result = await PicnicService(client).schedule_meal(day, [BOWL], replace=True)
    assert [c[0] for c in client.calls] == ["remove", "add"]
    assert result["replaced_entry_id"] == "old"


async def test_schedule_meal_refuses_to_replace_placed_order():
    day, start = _window_day(1)
    client = FakeClient([_entry("old", start, order_id="order-1")])
    with pytest.raises(PicnicInputError, match="already placed"):
        await PicnicService(client).schedule_meal(day, [BOWL], replace=True)
    assert client.calls == []


async def test_cancel_removes_unplaced_entry_but_cancels_placed_order():
    _, start1 = _window_day(1)
    _, start2 = _window_day(2)
    client = FakeClient([_entry("e1", start1), _entry("e2", start2, order_id="o2")])
    svc = PicnicService(client)
    assert (await svc.cancel("e1"))["status"] == "removed_from_calendar"
    assert (await svc.cancel("o2"))["status"] == "order_cancelled"
    assert client.calls == [("remove", "e1"), ("cancel_order", "o2", "me@example.com", "REASON_OTHER")]
