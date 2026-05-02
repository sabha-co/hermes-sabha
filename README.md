# hermes-sabha

Run a [Hermes Agent](https://github.com/NousResearch/hermes-agent) bot on a [Sabha](https://sabha.co) workspace.

Drop-in plugin — clone into `~/.hermes/plugins/`, point it at a Sabha bot key, restart the gateway. No fork of Hermes required.

## Install

```bash
git clone https://github.com/ashwinmk/hermes-sabha ~/.hermes/plugins/sabha
hermes plugins enable sabha    # user-installed plugins are opt-in
hermes gateway setup           # walks you through Sabha credentials
hermes gateway restart
```

Verify it loaded:

```bash
hermes status
```

You should see Sabha listed under Messaging Platforms with a `(plugin)` tag.

## Get a Sabha bot key

1. Open your Sabha workspace as an administrator.
2. Settings → Integrations → Bots → **New bot**.
3. Copy the bot key (format `123-AbCdEfGh…`) and the workspace base URL (e.g. `https://sabha.co/1000006`).
4. Add the bot to the rooms you want it to participate in.

## Configure

Easiest path is the wizard (`hermes gateway setup`, pick Sabha). It writes to `~/.hermes/.env`. The variables it sets:

| Variable | Required | Purpose |
| --- | --- | --- |
| `SABHA_BASE_URL` | yes | Workspace URL, e.g. `https://sabha.co/1000006` |
| `SABHA_BOT_KEY` | yes | Bot key from step 3 above |
| `SABHA_ALLOWED_USERS` | recommended | Comma-separated Sabha user ids permitted to talk to the bot |
| `SABHA_ALLOW_ALL_USERS` | optional | Set `true` to skip the allowlist (open access) |
| `SABHA_WEBSOCKET_URL` | optional | Override if your deployment terminates `/cable` somewhere unusual |
| `SABHA_API_BASE_URL` | optional | Override if your bot API isn't at `{BASE_URL}/api/bots` |

Equivalent `config.yaml` block:

```yaml
gateway:
  platforms:
    sabha:
      enabled: true
      extra:
        base_url: https://sabha.co/1000006
        bot_key: "${SABHA_BOT_KEY}"
```

## Try it

DM the bot, or @-mention it in a room it's a member of. The agent replies in the same room — or in a thread if you replied to a message in a non-DM room.

```
You: @{42} what's on the calendar today?
Bot: …
```

To mention users from agent output, use the literal `@{user_id}` token (numeric id) — Sabha rewrites it into a real mention server-side. The user id appears in the inbound message payload the agent sees.

## What's supported

- **Text in / text out**, with Sabha-flavoured markdown rendering (bold, italics, headings up to h6, code fences, lists, links, blockquotes).
- **Streaming replies** via in-place edits — the bot's message updates as the agent generates, instead of arriving all at once.
- **Threading** — replying to a message inside a non-DM room continues in a thread automatically.
- **Inbound attachments** — images, audio and documents are downloaded and made available to the agent's vision/audio tools.
- **Outbound attachments** — `send_image`, `send_document`.
- **Edit / delete / react** on prior bot messages.
- **Allowlist** integration with Hermes' standard `*_ALLOWED_USERS` mechanism.

## Not yet supported (v0.1)

- Typing indicator while the agent is thinking.
- Listing rooms / users from agent tool calls.
- Running multiple Sabha bots from one Hermes installation (use a second `HERMES_HOME` for now).
- Custom per-room system prompts.

## Notes for Sabha-specific behaviour

- **Markdown tables** render as preformatted text — Sabha's HTML allowlist doesn't include `<table>`. If your agent likes tables, prefer lists or short paragraphs; the platform hint shipped with the plugin nudges it that way.
- **Replies vs threads** — replying to a top-level message in a public/private room threads automatically; DMs always reply inline.
- **Attachment URLs are short-lived** — Sabha's signed URLs expire in roughly an hour. The plugin caches inbound attachments to disk on arrival, so the agent can still process a file the user sent earlier in the turn.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `hermes status` doesn't list Sabha | Plugin didn't load — check `~/.hermes/plugins/sabha/plugin.yaml` exists and `hermes gateway logs` for import errors. |
| `auth_failed` in logs at startup | `SABHA_BOT_KEY` rejected — regenerate the bot key in Sabha settings. |
| Bot ignores group messages | In rooms (not DMs) the bot only replies when @-mentioned. Mention it explicitly. |
| `config_missing` fatal error | `SABHA_BASE_URL` or `SABHA_BOT_KEY` isn't set in the environment Hermes is running under. |
| Bot replies in the wrong place after a reconnect | Restart the gateway; the WebSocket should resubscribe and resume. File an issue if it persists. |

## License

MIT.
