# picnic-mcp

Order office lunch from [Picnic](https://trypicnic.com) by chatting with Claude:
"what's for lunch tomorrow?", "find me something high-protein under the stipend
for Friday", "skip Monday".

Unofficial, and not affiliated with Picnic.

## Setup (2 minutes)

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) (`brew install uv`)
and a Picnic account.

```sh
git clone https://github.com/philipk19238/picnic-mcp.git && cd picnic-mcp

# 1. Add the server to Claude Code (all projects)
claude mcp add --scope user picnic -- uv --directory "$PWD" run picnic-mcp serve

# 2. Add the skill that teaches Claude how to log you in and order
mkdir -p ~/.claude/skills && cp -R .claude/skills/picnic-lunch ~/.claude/skills/
```

Restart Claude Code, then say **"log me in to Picnic"**. Claude asks for your
work email, Picnic emails you a code, and you paste it back. That's it. The
session is saved to `~/.config/picnic-mcp/credentials.json` and refreshes
itself as you use it.

Prefer the terminal? `uv run picnic-mcp login you@company.com` (add `--sms` to
get the code by text).

## What you can ask

- "What's on my lunch schedule this week?"
- "What restaurants are there tomorrow? Anything Asian?"
- "Show me the menu for NAYA and order a chicken bowl for Thursday"
- "Swap Friday's lunch for something with more protein"
- "Skip Monday"

Claude always confirms before it schedules, swaps or cancels a meal.

**How ordering works:** each weekday is a slot on your Picnic meal calendar.
Whatever is in the slot is ordered automatically at ~9:30am that day. Until
then you can change it freely; after that it can only be cancelled. The
company stipend is applied automatically. Meals over the stipend need card
checkout, which you finish on order.trypicnic.com.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Claude has no Picnic tools | Run `claude mcp get picnic` and check the path in step 1, then restart Claude Code |
| "Not signed in" / "session expired" | Say "log me in to Picnic" again |
| Login says the account may not exist | Use the email your company registered with Picnic |
| Check your session | `uv run picnic-mcp auth status` |
| Sign out | `uv run picnic-mcp auth clear` |

## Development

```sh
uv run pytest                               # tests
uv run picnic-mcp -v whoami                 # quick check with request logging
uv run python scripts/extract_operations.py # re-pull GraphQL operations from the web app
```

| Path | What |
| --- | --- |
| `src/picnic_mcp/server.py` | MCP tools |
| `src/picnic_mcp/service.py` | Meal calendar, cart and menu logic |
| `src/picnic_mcp/client.py` | GraphQL client for `order.trypicnic.com` |
| `src/picnic_mcp/login.py` | Email/SMS one-time-code login |
| `src/picnic_mcp/http_base_client.py` | Shared HTTP client base ([design](https://gist.github.com/alvinsng/fb7474edafef5e3f4a0e63e3c4888946)) |
| `.claude/skills/picnic-lunch/` | Claude skill: login and ordering playbook |
