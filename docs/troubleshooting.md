# Troubleshooting

## AttributeError: `'DiscordAssistantBot' object has no attribute 'tree'`

This error started appearing after the project switched from the Pycord client shim
back to the upstream `discord.py` voice implementation in commit `8f8e2e7`. That refactor
removed the compatibility code that used to create `commands.Bot.tree` manually for
older Discord library builds that did not expose application command trees. As a result,
environments that still relied on those older packages began to fail when slash commands
were registered.

The regression has since been corrected by restoring the fallback creation of the command
tree inside `DiscordAssistantBot.__init__`. If you are still encountering the error, make
sure you are running a version that includes the updated initializer (or upgrade your
Discord library to >=2.0 where `commands.Bot.tree` is always present).
