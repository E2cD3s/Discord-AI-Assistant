# Discord AI Assistant

A Discord bot that connects an AI assistant to text and voice channels. It
listens for wake words in chat and exposes a small collection of slash
commands for managing its behaviour.

## Configuration

Copy `config.example.yaml` to `config.yaml` and update the values for your
environment.

```bash
cp config.example.yaml config.yaml
```

At a minimum you must provide your bot token. Guild IDs are optional but can be
specified to speed up command registration in specific servers. Wake words are
used by the listener in text channels and should be provided in lower case.

## Commands

The bot is driven through Discord slash commands. Once the bot starts it will
register global commands, which can take up to an hour to propagate. For faster
iteration during development, populate the `guild_ids` list in the configuration
file so commands are synced immediately for the specified guilds.

Available commands:

- `/join` – Ask the bot to join the voice channel you are currently in.
- `/leave` – Disconnect the bot from its active voice channel.
- `/ask <question>` – Send a prompt to the assistant and receive a response
  directly in the invoking channel.

If you add or modify commands you can manually sync them by running
`await bot.tree.sync()` inside a Python REPL or by restarting the bot after
updating the configuration.
