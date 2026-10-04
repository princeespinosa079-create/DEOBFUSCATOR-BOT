# DeobfBot — Discord Deobfuscation Bot (Pure Python)

A Discord bot with a `.deob` command. Attach an obfuscated Lua/Luau script,
pick the obfuscator from a dashboard, and get the deobfuscated file back.

**Runs on Render's normal Python service — no Docker, no compiler needed.**
`setup_runtime.py` auto-downloads prebuilt Linux binaries (Node.js + Luau) at
build time.

## What it does

```
User:  .deob  (+ attach script.lua)
Bot:   [Dashboard embed]  Choose the obfuscator below.
                          > 1. Luraph V15
                          > 2. Prometheus (WeAreDevs & Some forks)
       [Luraph V15] [Prometheus]   (gray buttons)

User clicks a button:
Bot:   [Deobfuscating...]  ⏳ Processing...

Bot finishes:
Bot:   @User Here you go!
       [File Preview embed]  ``` first 5 lines of deobfuscated file ```
       footer: Today at HH:MM AM/PM PH
       + full deobfuscated file attached
```

## Engines included
- **Luraph V15** → `engines/deobf/` (Python, dynamic devirtualizer + trace)
- **Prometheus** (WeAreDevs & forks) → `engines/prometheus/` (runs on the downloaded Node.js binary)

## Deploy on Render (free tier)

1. Upload **all these files** (unzipped) to a GitHub repo:
   `bot.py`, `setup_runtime.py`, `render.yaml`, `requirements.txt`,
   `config.json`, `README.md`, and the whole `engines/` folder.
2. Render → **New + → Web Service** → connect the repo. It auto-reads `render.yaml`.
3. Add environment variable: `DISCORD_TOKEN` = your bot token (secret).
4. Deploy. First build downloads the binaries (~2 min).
5. **Free tier sleeps** — add a free pinger (UptimeRobot) hitting your
   `*.onrender.com` URL every 5 min to keep the bot online. Or switch
   `render.yaml` to `type: worker` + `plan: starter` for 24/7 uptime.

### Discord bot setup
- https://discord.com/developers/applications → your bot → **Bot** tab →
  enable **Message Content Intent**.
- Invite with `bot` scope + Send Messages, Embed Links, Attach Files,
  Read Message History permissions.

## Run locally
```bash
pip install -r requirements.txt
python setup_runtime.py     # downloads node + luau binaries (Linux/Mac)
python bot.py
```
(On Windows, build luau with `cd engines/deobf && python deobf/build_luau.py`,
and install Node.js normally.)

## Usage
In any channel the bot can see: `.deob` with a `.lua` / `.luau` / `.txt` file
attached. Only the person who ran `.deob` can click the buttons.

## Notes
- Max file size: 25 MB. Deobfuscation can take up to ~5 min for big scripts.
- The Luau binary used here is the official stock build (no Vector3 patch).
  Scripts that heavily use Vector3 math may degrade to a behaviour trace;
  most scripts deobfuscate fully. The Prometheus engine is unaffected.
