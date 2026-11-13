# hermes-sabha

Run a [Hermes Agent](https://github.com/NousResearch/hermes-agent) in a [Sabha](https://sabha.co) workspace.

Hermes joins Sabha as an **app**. It hears events over the app WebSocket and answers through the
apps API, so it needs no public URL and can run on a laptop behind NAT. It brings its own model
and tools; Sabha is where people talk to it.

## Install

```bash
hermes plugins install sabha-co/hermes-sabha
hermes plugins enable sabha-platform
hermes gateway setup      # pick Sabha
hermes gateway restart
```

`hermes status` should then list Sabha under messaging platforms.

## Create the app in Sabha

1. As an admin: **Settings → Apps → New**.
2. From the app's page, copy the **API base URL**, the **WebSocket URL** and the **key**. The key is
   shown once.
3. Add the app to the rooms it should work in. DMs work without that.

A new app's key can post and read, which is all Hermes needs: it answers in DMs people open with
it, and never opens one itself. Leave **Start direct messages** and **Create and join rooms** off
under "What its key can do" on the app's page.

## Configure

`hermes gateway setup` writes these to `~/.hermes/.env`:

| Variable | Required | |
|---|---|---|
| `SABHA_API_URL` | yes | API base URL, e.g. `https://chat.example.com/api/v1` |
| `SABHA_WEBSOCKET_URL` | yes | WebSocket URL, e.g. `wss://chat.example.com/cable`. Paste it as shown; the key is sent as a header and dropped from the URL |
| `SABHA_APP_KEY` | yes | The app's key, `sabha_app_…` |
| `SABHA_ALLOWED_USERS` | recommended | Comma-separated Sabha user ids allowed to talk to the agent (the number in `/users/42`) |
| `SABHA_ALLOW_ALL_USERS` | | `true` lets anyone in the workspace use it |
| `SABHA_HOME_CHANNEL` | | Room id for cron results (the number in `/rooms/12`) |
| `SABHA_REPLY_MODE` | | `thread` (default) answers a room mention in a thread; `off` answers in the room |

With no allowlist, someone new gets a pairing code to approve with `hermes pairing approve sabha <code>`.

The same settings can live in `config.yaml`:

```yaml
platforms:
  sabha:
    enabled: true
    extra:
      api_url: https://chat.example.com/api/v1
      websocket_url: wss://chat.example.com/cable
      reply_mode: thread
```

## How it behaves

- **Who it hears.** Sabha sends an app a room message only when it's mentioned. In a DM, or a
  thread the app is in, it gets every message. It never hears its own messages, and hears another
  app only when that app mentions it by name.
- **Threads.** A room mention is answered in a thread on that message, and the thread is its own
  Hermes session. Follow-ups in the thread need no mention. DMs never thread.
- **Formatting.** The agent writes Markdown, which is rendered to the rich text Sabha stores. Raw
  HTML is escaped. Tables don't render. `@{42}` mentions user 42 if they're in the room.
- **Working.** While the agent works, a shell with a status line shows where its reply will land:
  "Thinking…", then Hermes' live phrases as it runs tools. The reply replaces it. A sender outside
  the allowlist sees nothing.
- **Streaming.** With Hermes' streaming on (`streaming.enabled: true` in `config.yaml`; it's off
  by default), answers stream in by editing the reply in place. Otherwise each reply lands whole.
- **Files.** Attachments people send are downloaded for the agent. Files the agent sends are posted
  as attachments, with any caption as a message just before.
- **Commands.** On connect, Hermes declares its commands, so they show in Sabha's `/` menu in
  rooms, DMs and threads the app is in. That's the first 25 of the list Hermes gives Slack, minus
  names Sabha keeps (`/help`) or another app already holds. Every command, those included, also
  runs as `/hermes <name>`. The answer goes privately to whoever ran it. `/hermes <question>` is
  answered in the open, like a mention. A message whose text
  starts with `/` (`@Hermes /new` in a room) still reaches Hermes as a command and is answered in
  the open.
- **Retries.** Every post carries an id of its own. When a post times out or fails with a 5xx,
  Hermes retries it, and Sabha answers a post it already saved with that message instead of
  posting it twice. On a Sabha too old to take the id, the plugin posts without it, and a
  retried post can then land twice.
- **Cron.** `deliver=sabha` posts to `SABHA_HOME_CHANNEL`, from the gateway or from a separate
  `hermes cron` process.

Messages sent while the socket is down, or while the gateway restarts, are replayed when it
reconnects, each once. Sabha keeps the last 5 minutes or 100 events, so a longer outage loses the
oldest. The plugin keeps its place in `~/.hermes/plugin-data/sabha-platform/replay.json`.

## Not yet

- Hermes' approval and clarify buttons as Sabha Block Kit cards (Sabha supports them; they come
  back as `block_actions` events).
- Webhook delivery instead of the WebSocket.

## Development

```bash
uv run --no-project --with pytest --with markdown-it-py --with aiohttp python -m pytest
```

The tests cover routing, formatting, retries and the API client, which don't need Hermes
installed. The adapter itself is checked against a running Hermes gateway and Sabha. The full API reference is `GET /skill` on any
Sabha workspace.
