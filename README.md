# cliproxy-hub

A setup kit for a [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) hub for
Claude Code. One host holds the Claude logins. Each client reaches the host through
Tailscale. The kit adds what the upstream proxy does not have: separate pools, a router
that spends first the quota that expires first, a spend guard, and a status endpoint for a
statusline.

The idea comes from this guide:
<https://gist.github.com/t3dotgg/59a521d2ee8d70aaf90e280c175072af>.

## Read this first

- **Terms.** A proxy for subscription accounts can be against the terms of your plan. An
  account can get a ban. You carry that risk. Read the terms of your plan before you start.
- **Your own accounts only.** Do not put the account of a different person in a pool. Do not
  serve traffic for other people.
- **Claude Code only.** Use the pooled accounts through Claude Code, and through no other client.
- **No datacenter host.** Run the hub on a machine with a home or office connection. The
  guide above explains why.
- **Work accounts.** An employer owns its seats. Get approval before you pool them.

## What you get

| Part | Purpose |
|---|---|
| Two pools | `personal` and `work` run as two proxy processes. Their accounts never mix. |
| Router | Sets the priority of each seat from its 5-hour and weekly quota. |
| Spend guard | Takes a seat out of the pool before it can bill extra usage. |
| Status endpoint | `GET /status/<pool>` gives the state of each seat as JSON. |
| Login script | Adds accounts with no SSH tunnel. It stays open for several accounts. |
| Backup script | Writes one encrypted archive of the hub state to the client. |
| Shell function | Selects the pool from the current directory. |

## Requirements

- Hub host: Linux with Docker and Docker Compose, Tailscale with HTTPS enabled, SSH access,
  and `sudo` without a password for the SSH user.
- Client: `bash`, `ssh`, `curl`, `jq`, `gpg`, Tailscale on the same tailnet, and Claude Code.

## Setup

1. Copy `hub.env.example` to `hub.env` and set your values. `hub.env` holds no secret, and
   git ignores it.
2. Run `./deploy.sh`. It copies `remote/` to the host, creates the keys on the host, starts
   three containers and opens the ports on the tailnet.
3. Run `./login.sh`. Select a pool, press `c` to copy the URL, and open it in the browser
   of the account. The browser then fails on a `localhost:54545` page. Paste that address
   at the prompt. Press `a` for one more account.
4. Run `./verify.sh`. The model count must be above zero for each pool.
5. Add `source /path/to/cliproxy-hub/client/claude-hub.zsh` to `~/.zshrc`, with the
   settings that the file describes.
6. On each other client, set `hub.env` and run `./client-setup.sh`.
7. Limit the hub ports on your tailnet. See `docs/tailnet-policy.example.hujson`.

## How a session uses the hub

The shell function adds three variables to a session:

| Variable | Value |
|---|---|
| `ANTHROPIC_BASE_URL` | The pool URL. Only model requests go there. |
| `ANTHROPIC_CUSTOM_HEADERS` | `X-Api-Key: <pool access key>` |
| `CLAUDE_HUB_POOL` | The pool name, for a statusline |

The session keeps its normal config dir and its claude.ai login. Session history, memory,
Artifacts and connectors work as in a direct session. Do not put the key in
`ANTHROPIC_AUTH_TOKEN`. That variable replaces the claude.ai login, and then Artifacts and
connectors fail.

`CLAUDE_DIRECT=1 claude` skips the hub. `CLAUDE_POOL=work claude` forces a pool. If the hub
does not answer in 3 seconds, the function uses the direct login and says so.

## Router

The proxy sends each new session to the seat with the largest priority. Seats with the
same priority take new sessions in turn. The router sets the priority each 60 seconds.

| Seat state | Condition | Priority |
|---|---|---|
| `open` | 5-hour use under 95 percent | 100, 90, 80 by score |
| `warm` | 5-hour use from 95 percent | 40, 30 by score |
| `five_hour_full` | 5-hour use from 98 percent, or in cooldown | 5 |
| `week_full` | 7-day use from 97 percent | 0 |

The score is the weekly quota that is left, divided by the hours to the weekly reset.
Seats whose score is within 15 percent of the best one share a priority. A session that
already runs stays on its seat, so the prompt cache stays valid. The `ROUTER_*` values in
`remote/compose.yaml` set each limit.

A seat change is costly. The session sends its whole history again as a cache write. Keep
the context of a long session small.

## Spend guard

An account with extra usage bills each request after its included quota ends. The router
reads the `Overage-Status` signal of each seat. If extra usage is open on a seat and a
window is at 97 percent, the router disables that seat until the reset of that window. It
never enables a seat that a person disabled. `ROUTER_SPEND_GUARD=0` turns the guard off.

## Status endpoint

```
curl -s -H "X-Api-Key: $(cat ~/.config/cliproxy/work.key)" "$(cat ~/.config/cliproxy/work.status-url)"
```

The answer lists each seat with `priority`, `state`, `week_used`, `week_hours_left`,
`week_reset_epoch`, `five_hour_used`, `five_hour_hours_left`, `five_hour_reset_epoch` and
`unavailable`. It also gives `next_seat`, `extra_usage_open` and `spend_guard_blocked`.
`./status.sh` prints the same data as a table.

The pool configs set `passthrough-headers: true`. Claude Code then gets the rate-limit
numbers of the seat that served a request. A statusline can compare the reset times in
those numbers with the reset times from the status endpoint to find the seat of a session.
The [chrysaki-claude](https://github.com/Kiriketsuki/chrysaki-claude) statusline does this.

## Files

| File | Runs on | Purpose |
|---|---|---|
| `hub.env.example` | client | Template for the settings |
| `deploy.sh` | client | Copies `remote/` to the host, starts the containers, opens the ports |
| `client-setup.sh` | client | Fetches the keys and writes the URLs |
| `login.sh` | client | Signs accounts in. Takes `--host` and `--dir`. |
| `verify.sh` | client | Checks each pool through the tailnet |
| `status.sh` | client | Shows the state of each seat |
| `backup.sh` | client | Writes one encrypted archive of the hub state |
| `client/claude-hub.zsh` | client | The shell function |
| `remote/` | host | Compose file, config template, first-run script, router |

## Move the hub to a new host

Never run two hosts with the same auth files. A token refresh on one host makes the other
copy invalid.

1. Install Docker and Tailscale on the new host.
2. On the old host, stop the containers: `docker compose -p claude-hub down`.
3. Copy the hub directory to the new host as root. Keep the file modes.
4. Change `HUB_SSH`, `HUB_DIR` and `HUB_FQDN` in `hub.env`.
5. Run `./deploy.sh`. It keeps the configs, keys and logins that it finds.
6. On each client, run `./client-setup.sh`.
7. On the old host, remove the serve entries and delete the hub directory.

## Limits

- The hub cannot tell a client which seat serves a session. The reset-time match gives it.
- Claude Desktop chat does not use the hub. It talks to claude.ai, not to the API endpoint.
- The router tests need Python 3.11 or later: `cd remote/router && python3 -m unittest test_router`.
