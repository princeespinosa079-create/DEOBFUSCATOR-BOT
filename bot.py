"""
DeobfBot — Discord bot with a .deob command that deobfuscates Lua/Luau scripts.

Backends (run as subprocesses per job, as recommended by both engines):
  - Luraph V15  -> engines/deobf/deobf/deob.py --obfuscator luraph_v15
  - Prometheus  -> engines/prometheus/bin/pdeobf.js (Node.js)

Flow:
  .deob (+ file attachment)
    -> Dashboard embed + two gray buttons
    -> click a button -> "Deobfuscating..." loading embed
    -> reply to user: "@user Here you go!" + File Preview embed (first 5 lines)
       + footer "Today at HH:MM AM/PM PH" + full deobfuscated file attached
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord.ext import commands

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINES_DIR = os.path.join(BASE_DIR, "engines")
PROMETHEUS_DIR = os.path.join(ENGINES_DIR, "prometheus")
DEOBF_DIR = os.path.join(ENGINES_DIR, "deobf")
DEOB_PY = os.path.join(DEOBF_DIR, "deobf", "deob.py")

# Node.js: use the binary downloaded by setup_runtime.py if present, else
# fall back to whatever `node` is on PATH.
_LOCAL_NODE = os.path.join(ENGINES_DIR, "node", "node")
NODE_BIN = _LOCAL_NODE if os.path.exists(_LOCAL_NODE) else "node"

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# Philippines time (UTC+8). Use zoneinfo if available, else fixed offset.
try:
    from zoneinfo import ZoneInfo
    PH_TZ = ZoneInfo("Asia/Manila")
except Exception:
    PH_TZ = timezone(timedelta(hours=8))

ALLOWED_EXT = {".lua", ".luau", ".txt"}
MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB
SUBPROCESS_TIMEOUT = 300  # seconds, hard cap for one deobfuscation job

EMBED_COLOR = 0x2B2D31  # dark gray ("gray" theme)


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
    # env var overrides config file
    cfg["token"] = os.environ.get("DISCORD_TOKEN", cfg.get("token", ""))
    cfg["prefix"] = os.environ.get("BOT_PREFIX", cfg.get("prefix", "."))
    return cfg


def ph_now_str():
    """'Today at 10:30 PM PH' style footer timestamp."""
    now = datetime.now(PH_TZ)
    return now.strftime("Today at %I:%M %p PH")


def start_health_server():
    """If $PORT is set (Render Web Service), bind a tiny HTTP server on it in
    a daemon thread so the platform sees the service as live. A Background
    Worker does not need this (no $PORT is set)."""
    port = os.environ.get("PORT")
    if not port:
        return
    try:
        port = int(port)
    except ValueError:
        return

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("0.0.0.0", port), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print(f"[+] health server listening on :{port}")


# ---------------------------------------------------------------------------
# Deobfuscation backends (blocking — run in a thread executor)
# ---------------------------------------------------------------------------
class DeobfError(Exception):
    """Raised when a deobfuscation job fails; message is user-facing."""


def _read_head(path, n=5, max_chars=1800):
    """First n non-empty-ish lines of a file, for the File Preview embed."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = []
            for line in f:
                lines.append(line.rstrip("\n"))
                if len(lines) >= n:
                    break
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n..."
        return text
    except Exception as e:
        return f"(could not read preview: {e})"


def run_prometheus(input_path, output_path):
    """Node.js Prometheus deobfuscator. Returns (output_path, notes_str)."""
    cmd = [NODE_BIN, os.path.join(PROMETHEUS_DIR, "bin", "pdeobf.js"),
           input_path, "-o", output_path, "-q"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=SUBPROCESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise DeobfError("Timed out while deobfuscating (Prometheus).")
    except FileNotFoundError:
        raise DeobfError("Node.js is not installed on the server (needed for Prometheus).")

    if proc.returncode == 0:
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise DeobfError("Prometheus produced an empty output.")
        return output_path
    if proc.returncode == 3:
        raise DeobfError("This file does not look like a Prometheus-obfuscated script. "
                         "Try the Luraph V15 option instead.")
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    err = re.sub(r"\x1b\[[0-9;]*m", "", err)  # strip ANSI color codes
    raise DeobfError(f"Prometheus failed (exit {proc.returncode}).\n```\n{err[:800]}\n```")


def run_luraph(input_path, output_path):
    """Python multi-obfuscator, forced to luraph_v15. Returns output_path."""
    py = sys.executable or "python3"
    cmd = [py, DEOB_PY, input_path, "-o", output_path,
           "--obfuscator", "luraph_v15", "--no-pypy", "--timeout", "240"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=SUBPROCESS_TIMEOUT,
                              cwd=DEOBF_DIR)
    except subprocess.TimeoutExpired:
        raise DeobfError("Timed out while deobfuscating (Luraph V15). Big scripts can take a while.")

    if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return output_path

    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    # also include last lines of stdout (it prints progress there)
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    tail = "\n".join((out + "\n" + err).splitlines()[-15:])
    if not tail.strip():
        tail = "(no error output captured)"
    raise DeobfError(f"Luraph V15 deobfuscation failed (exit {proc.returncode}).\n```\n{tail[:1200]}\n```")


BACKENDS = {
    "luraph": ("Luraph V15", run_luraph),
    "prometheus": ("Prometheus", run_prometheus),
}


def deobfuscate_sync(backend_key, input_bytes, original_name):
    """Blocking worker. Writes input to a temp dir, runs the backend, returns
    (output_path, preview_text, original_name). Temp dir lives until caller
    deletes it (after the file is uploaded to Discord)."""
    label, fn = BACKENDS[backend_key]
    workdir = tempfile.mkdtemp(prefix="deobfjob_")
    ext = os.path.splitext(original_name)[1] or ".lua"
    input_path = os.path.join(workdir, "input" + ext)
    output_path = os.path.join(workdir, "deobfuscated" + ext)
    with open(input_path, "wb") as f:
        f.write(input_bytes)
    try:
        out = fn(input_path, output_path)
    except DeobfError:
        # clean up on failure
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    preview = _read_head(out, n=5)
    return out, preview, workdir


# ---------------------------------------------------------------------------
# Discord UI: Dashboard view with two gray buttons
# ---------------------------------------------------------------------------
class DashboardView(discord.ui.View):
    def __init__(self, author: discord.User, attachment: discord.Attachment):
        super().__init__(timeout=120)
        self.author = author
        self.attachment = attachment
        self.used = False
        self.message = None  # set by the command after sending

    async def _check(self, interaction: discord.Interaction) -> bool:
        if self.used:
            await interaction.response.send_message(
                "This dashboard has already been used. Run `.deob` again.",
                ephemeral=True)
            return False
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "Only the person who ran `.deob` can pick an option.",
                ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Luraph V15", style=discord.ButtonStyle.secondary,
                       custom_id="pick_luraph")
    async def luraph_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        await self._start(interaction, "luraph")

    @discord.ui.button(label="Prometheus", style=discord.ButtonStyle.secondary,
                       custom_id="pick_prometheus")
    async def prometheus_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        await self._start(interaction, "prometheus")

    async def _start(self, interaction: discord.Interaction, backend_key: str):
        self.used = True
        self.stop()
        label = BACKENDS[backend_key][0]

        # 1. replace dashboard with loading embed, disable buttons
        loading = discord.Embed(
            title="Deobfuscating...",
            description="\u23F3 Processing...",
            color=EMBED_COLOR,
        )
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=loading, view=self)

        # 2. download the attachment
        try:
            input_bytes = await self.attachment.read()
        except Exception as e:
            fail = discord.Embed(title="Error",
                                 description=f"Could not download the attached file: {e}",
                                 color=0xE74C3C)
            await interaction.edit_original_response(embed=fail, view=None)
            return

        # 3. run deobfuscation in a worker thread (blocking subprocess)
        loop = asyncio.get_running_loop()
        try:
            out_path, preview, workdir = await loop.run_in_executor(
                None, deobfuscate_sync, backend_key, input_bytes, self.attachment.filename)
        except DeobfError as e:
            fail = discord.Embed(title="Deobfuscation Failed",
                                 description=str(e), color=0xE74C3C)
            fail.set_footer(text=ph_now_str())
            await interaction.edit_original_response(embed=fail, view=None)
            return
        except Exception as e:
            fail = discord.Embed(title="Unexpected Error",
                                 description=f"```\n{type(e).__name__}: {e}\n```",
                                 color=0xE74C3C)
            await interaction.edit_original_response(embed=fail, view=None)
            return

        # 4. build result: reply to user with mention + File Preview embed + file
        ext = os.path.splitext(self.attachment.filename)[1] or ".lua"
        result_filename = "deobfuscated" + ext
        file = discord.File(out_path, filename=result_filename)

        preview_embed = discord.Embed(
            title="File Preview",
            description=f"```lua\n{preview}\n```",
            color=EMBED_COLOR,
        )
        preview_embed.set_footer(text=ph_now_str())

        content = f"{interaction.user.mention} Here you go!"

        # send the result as a reply to the original command message
        try:
            await interaction.channel.send(
                content=content,
                embed=preview_embed,
                file=file,
                reference=interaction.message.reference or interaction.message,
                mention_author=True,
            )
        except Exception:
            # fallback: plain send if reference fails
            await interaction.channel.send(content=content, embed=preview_embed, file=file)

        # 5. update the loading message to a clean "done" state
        done = discord.Embed(
            title="Deobfuscation Complete",
            description=f"**{label}** finished on `{self.attachment.filename}`.\n"
                        f"Full file posted below.",
            color=0x2ECC71,
        )
        done.set_footer(text=ph_now_str())
        await interaction.edit_original_response(embed=done, view=None)

        # 6. clean up temp dir (file already uploaded to Discord)
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None and not self.used:
            try:
                expired = discord.Embed(
                    title="Dashboard",
                    description="This dashboard has expired. Run `.deob` again.",
                    color=EMBED_COLOR,
                )
                await self.message.edit(embed=expired, view=self)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
def main():
    cfg = load_config()
    if not cfg.get("token"):
        print("ERROR: no bot token. Set DISCORD_TOKEN env var or 'token' in config.json.")
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True  # needed for prefix commands
    intents.members = False

    bot = commands.Bot(command_prefix=cfg["prefix"], intents=intents,
                       help_command=None, case_insensitive=True)

    @bot.event
    async def on_ready():
        print(f"[+] Logged in as {bot.user} (ID {bot.user.id})")
        print(f"    Prefix: {cfg['prefix']}deob")
        print(f"    Prometheus engine: {os.path.exists(os.path.join(PROMETHEUS_DIR, 'bin', 'pdeobf.js'))}")
        print(f"    Luraph engine:     {os.path.exists(DEOB_PY)}")

    @bot.command(name="deob")
    async def deob(ctx: commands.Context):
        """Deobfuscate an attached Lua/Luau script. Usage: .deob (attach a file)"""
        if not ctx.message.attachments:
            emb = discord.Embed(
                title="Dashboard",
                description="No file attached.\nAttach a `.lua` / `.luau` / `.txt` script with your `.deob` message.",
                color=0xE74C3C,
            )
            await ctx.reply(embed=emb, mention_author=False)
            return

        att = ctx.message.attachments[0]
        ext = os.path.splitext(att.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            emb = discord.Embed(
                title="Dashboard",
                description=f"Unsupported file type `{ext or '(none)'}`.\n"
                            "Please attach a `.lua`, `.luau`, or `.txt` script.",
                color=0xE74C3C,
            )
            await ctx.reply(embed=emb, mention_author=False)
            return

        if att.size > MAX_FILE_BYTES:
            emb = discord.Embed(
                title="Dashboard",
                description=f"File is too large ({att.size / 1024 / 1024:.1f} MB). Max is 25 MB.",
                color=0xE74C3C,
            )
            await ctx.reply(embed=emb, mention_author=False)
            return

        dashboard = discord.Embed(
            title="Dashboard",
            description=("Choose the obfuscator below.\n"
                         "> 1. Luraph V15\n"
                         "> 2. Prometheus (WeAreDevs & Some forks)"),
            color=EMBED_COLOR,
        )
        view = DashboardView(author=ctx.author, attachment=att)
        msg = await ctx.reply(embed=dashboard, view=view, mention_author=False)
        view.message = msg

    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        emb = discord.Embed(title="Error", description=f"```\n{error}\n```", color=0xE74C3C)
        await ctx.reply(embed=emb, mention_author=False)

    start_health_server()
    bot.run(cfg["token"])


if __name__ == "__main__":
    main()
