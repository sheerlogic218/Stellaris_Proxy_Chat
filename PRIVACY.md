# Privacy

This covers the **central Proxy Chat bot**, run by its operator for a few approved
Discord servers. If you run the bot yourself (local mode), everything stays on your PC
in `state.json` and none of this applies.

## What the bot stores

| What | Why |
|---|---|
| Your server's id, its Proxy Chat categories and the bot's channel ids | To manage only its own channels |
| Discord user ids of players who used `/claim`, and which empire each claimed | To give each player access to the right voice channels |
| Empire, ruler and federation names, and who has contact with whom, from the game | To decide who may join which channel |
| Ids of the admin roles your server set with `/proxychat roles` | To know who may use `/proxychat` |
| Relay keys: a public key, a label and the user id of the admin who paired it | To accept game events from your hosts |
| An audit log of admin actions (who did what, when) | To find out what happened if something goes wrong |

It does **not** store:
- message content (it never reads messages);
- voice;
- IP addresses of relays;
- anything else from the game's log. The relay only sends the mod's own records, never raw log lines.

## How long it's kept

| Data | Kept for |
|---|---|
| A campaign nobody has played | 30 days after its last event (the current campaign is kept) |
| Audit log entries | 90 days |
| Events waiting to be applied | Up to 1 day |
| Everything about a server that removed the bot | 7 days after removal, then deleted |

Backups are encrypted and kept for 14 days.

## Getting data deleted

- **A player:** an admin can clear your claim with `/proxychat unassign`, or all claims with `/proxychat claims clear`.
- **A server owner:** remove the bot from the server. Everything about the server is deleted 7 days later. You can also ask the operator to delete it sooner.
