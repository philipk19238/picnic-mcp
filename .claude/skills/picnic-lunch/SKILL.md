---
name: picnic-lunch
description: Order, change, skip or look up office lunch on Picnic (trypicnic.com) with the picnic MCP tools, including signing the user in. Use when the user asks about lunch, their meal schedule, Picnic restaurants or menus, or wants to log in to Picnic.
---

# Picnic lunch

Picnic delivers lunch to the user's office once per weekday. Each weekday is a
slot on their **meal calendar**. A meal on the calendar becomes a real order at
its `place_order_at` time (~9:30am local). Before then it can be swapped or
removed freely; after that it can only be cancelled. The company stipend is
applied automatically.

## 0. Is the server installed?

If no `picnic` tools (`whoami`, `get_meal_schedule`, …) are available, tell the
user to run this from their clone of the repo, then restart Claude Code:

```sh
claude mcp add --scope user picnic -- uv --directory "$PWD" run picnic-mcp serve
```

## 1. Sign in

Call `whoami`. If it succeeds, skip ahead. If it says "Not signed in" or
"session expired":

1. Ask for the email they use for Picnic (their work email).
2. Call `login_start(email)`. For a text instead of an email, pass `channel="sms"`.
3. Tell them a code was sent, and ask them to paste it.
4. Call `login_verify(code)`. If the code is rejected, ask again. After a few
   failures, start over with `login_start`.

The session is saved to `~/.config/picnic-mcp/credentials.json` and refreshes
itself as it's used, so this is normally a one-time step.

## 2. Common tasks

| User wants | Do |
| --- | --- |
| "What's for lunch tomorrow?" | `get_meal_schedule`, then report the entry for that date |
| See options | `list_restaurants(date)` (with an optional `cuisine`) or `search_menu(query, date)` |
| Details of a dish | `get_menu(store_id, date)` returns item ids, prices, and modifier groups with min/max |
| Order a day | `get_menu` → pick an item and required modifiers → **confirm** → `schedule_meal(date, items)` |
| Change a day's meal | same as ordering, with `replace=true` |
| Skip a day | **confirm** → `cancel_meal(entry_id)` |
| Past orders / receipt | `list_orders`, `get_order(order_id)` |

Tips:
- `date` takes `YYYY-MM-DD`, `today` or `tomorrow`.
- An item's `modifiers` are option ids from its modifier groups. If a required
  group is missing, the error lists the valid options, so fix it and retry.
- To check the price against the stipend before committing, use
  `add_to_cart`: the cart shows `discount` (the stipend) and `total_due`. Then call
  `schedule_meal(date, from_cart=true)`.
- Prefer meals with `total_due` of 0. Meals over the stipend need card
  checkout, which these tools can't do, so send the user to order.trypicnic.com.

## Rules

- **Always confirm** the day, restaurant, dish and modifiers before calling
  `schedule_meal` or `cancel_meal`. These place or cancel real orders.
- Don't replace or cancel a day whose status is `ordered` without saying that
  it's already been placed.
