# Travian Is a Passion — Administration Bot

A conservative Discord administration bot for the existing **Travian Is a Passion** international community.

It inspects first, previews every managed change, and only applies changes after an administrator presses **Confirm Setup**. It never deletes channels, roles, messages, or permission overwrites.

## What it does

- `/server inspect` — exports the live category, channel, role, and relevant permission structure without changing anything.
- `/server setup` — builds an ephemeral dry-run plan with **Confirm Setup** and **Cancel** buttons.
- Creates only missing tribe roles: Romans, Gauls, Teutons, Egyptians, Huns, Spartans, and Vikings.
- Creates only missing server-speed roles: x1, x2, x3, x5, and x10.
- Protects only the staff channels explicitly listed in `STAFF_CHANNEL_NAMES`.
- Makes only the channels listed in `READ_ONLY_CHANNEL_NAMES` publicly readable but not publicly writable.
- Preserves every unrelated permission bit when changing a channel overwrite.
- Posts one managed welcome message and one managed rules message if they are absent.
- Adds a persistent **Agree to Rules** button that grants the existing Member role.
- Welcomes new members in the configured welcome channel.
- `/travel` — calculates one-way or round-trip movement, coordinates/map wrapping, server speed, and Tournament Square.
- `/resources` — totals resources and optionally calculates storage buildings and merchants.

## 1. Create the Discord application from your phone or computer

The Discord Developer Portal is easier from a desktop browser, but it also works from a phone browser in desktop-site mode.

1. Open <https://discord.com/developers/applications> and sign in.
2. Tap/click **New Application** and name it `Travian Is a Passion Bot`.
3. Open **Bot** in the left menu.
4. Press **Reset Token** / **Copy Token**. Save it privately. Never paste it in Discord, screenshots, GitHub, or this chat.
5. Under **Privileged Gateway Intents**, enable **Server Members Intent**. Message Content Intent is not required.
6. Open **OAuth2 → URL Generator**.
7. Select scopes:
   - `bot`
   - `applications.commands`
8. Select bot permissions:
   - View Channels
   - Send Messages
   - Embed Links
   - Attach Files
   - Read Message History
   - Manage Roles
   - Manage Channels
9. Open the generated URL, choose **Travian Is a Passion**, and authorize it.

Do not give the bot Administrator permission. The listed permissions are sufficient and safer.

## 2. Put the bot role in the correct position

In **Server Settings → Roles**, move `Travian Is a Passion Bot`:

- above `Member`, all tribe roles, and all server-speed roles;
- it may remain below Founder, Administrator, and Moderator.

Discord only lets a bot assign or manage roles below its own highest role.

## 3. Obtain the server ID

1. In Discord **User Settings → Advanced**, enable **Developer Mode**.
2. Open the server menu and choose **Copy Server ID**.
3. Keep this number for `DISCORD_GUILD_ID` on Railway.

## 4. Optional local test

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`, insert the token and server ID, then run:

```bash
python bot.py
```

On Windows, activate the environment with `.venv\Scripts\activate`.

## 5. Deploy to Railway

1. Create a private GitHub repository and upload all project files. Do **not** upload `.env`; `.gitignore` excludes it.
2. Go to <https://railway.app>, sign in, and choose **New Project → Deploy from GitHub repo**.
3. Select the private repository.
4. Open the Railway service, then **Variables**.
5. Add:
   - `DISCORD_TOKEN` = the private bot token
   - `DISCORD_GUILD_ID` = your copied server ID
6. Add any optional values from `.env.example` only if you want to change the defaults.
7. Railway will use `railway.toml` and start `python bot.py` as a worker.
8. Check **Deployments → View Logs**. A successful start shows `Logged in as ...` and `Synced ... commands`.

There is no web port and no health-check URL; this is a continuously running Discord worker.

## 6. Safely configure the existing server

Run these inside **Travian Is a Passion**:

1. `/server inspect` — download and review the read-only server report.
2. `/server setup` — review the dry-run preview.
3. Check every listed role, staff channel, permission change, and message.
4. Press **Confirm Setup** only if the preview is correct. Otherwise press **Cancel** and adjust Railway variables.
5. Run `/server setup` again. It should say that no changes are needed.

Only users with **Manage Server** can inspect, preview, or confirm setup. A preview expires after five minutes. The bot recalculates the plan immediately before applying it, so it will not blindly apply a stale preview.

## Channel and role matching

Matching ignores emoji, spaces, hyphens, underscores, and capitalization. For example, the existing role `🛡️ Administrator` matches `Administrator`, and `staff-chat` matches `Staff Chat`.

The bot does not create channels. If a configured channel is missing or named differently, the dry run shows a warning and skips it. This prevents duplicates.

The default protected channels are:

- `staff-chat`
- `moderation-logs`

For each matching channel, setup changes only these fields:

- `@everyone`: View Channel = denied
- Founder, Administrator, Moderator: View Channel, Send Messages, Read Message History = allowed

All other existing overwrite values remain untouched.

The default read-only channels are `welcome`, `rules`, and `announcements`. For these, setup allows `@everyone` to view/read, denies `@everyone` sending, and allows the three staff roles to send. If another non-staff role is already explicitly allowed to send there, the preview warns you but does not remove that permission.

## Commands for members

### Travel time

Use a known distance:

```text
/travel unit_speed:14 server_speed:x1 tournament_square:0 distance:114.2
```

Or calculate from coordinates:

```text
/travel unit_speed:14 server_speed:x3 tournament_square:10 origin:160|30 target:154|37
```

Set `round_trip:true` to calculate return time. The default `map_size:401` is the standard map from -200 to +200 and supports travel across map edges.

The command follows the official movement scaling: x1 = ×1, x2/x3/x5 = ×2, and x10 = ×4. Tournament Square adds 20% speed per level after the first 20 fields.

### Resources

```text
/resources wood:80000 clay:65000 iron:70000 crop:25000 merchant_capacity:500
```

Warehouse and granary options calculate how many buildings of the supplied capacity are needed for the largest relevant resource stack.

## Troubleshooting

- **Slash commands do not appear:** confirm `DISCORD_GUILD_ID`, reinvite with the `applications.commands` scope, and redeploy.
- **Missing Access / Missing Permissions:** verify the bot permissions and its role position.
- **Member role cannot be granted:** move the bot role above Member.
- **Welcome messages do not fire:** enable Server Members Intent in the Developer Portal and redeploy.
- **Rules or welcome message would be duplicated:** the bot recognizes messages it manages by an embed footer marker and skips them.
- **A channel was not detected:** set its exact readable name in the appropriate Railway variable and rerun `/server setup`.

## Security notes

- The token is read only from `DISCORD_TOKEN`.
- `.env` is ignored by Git.
- The bot never prints the token.
- If the token is ever exposed, reset it immediately in the Developer Portal and replace the Railway variable.
- Keep the GitHub repository private unless you have confirmed it contains no secrets.
