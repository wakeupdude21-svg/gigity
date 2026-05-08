# Repins Eman

Hey Utsab, you useless fuck. You couldn't build a sniper if your life depended on it so I did it for you. Again. You're welcome. Don't thank me. Actually don't even talk to me.

It's fully automated now. One command. It authenticates itself. It runs forever. You don't have to type anything. I literally made it idiot-proof for you specifically.

---

## What this does, since you clearly still an't read code

Steals Minecraft usernames before your two braincells can register the name even dropped. Sub-millisecond precision. Async workers. Spin-loop timing. TCP pre-warmed. Auto-tuned delay. Discord bot. Rich embeds. The works.

No NameMC. No third-party crap. Pure Mojang endpoints only. You're welcome, again.

---

## Setup (read this slowly, I know you struggle)

**1. Install dependencies**
```bash
pip install -r requirements.txt
```

**2. Configure (pick one)**

**Option A — Local (config.json)**
```bash
cp config.example.json config.json
cp proxies.example.txt proxies.txt
```
Then fill in your tokens in `config.json`.

**Option B — Railway / hosting (env vars, no config file needed)**
Set these in your Railway dashboard → Variables:
| Variable | What |
|---|---|
| `DISCORD_BOT_TOKEN` | Your Discord bot token |
| `DISCORD_WEBHOOK_URL` | Webhook for snipe notifications |
| `SNIPE_WORKERS` | Number of workers (default: 5) |
| `SNIPE_DELAY` | Fire delay in ms (default: 0) |

> **⚠️ NEVER put secrets in config.json if you're pushing to GitHub. The `.gitignore` excludes it, but still. Use env vars.**

**3. Run it**
```bash
python main.py
```

**4. Login (Device Code — works headless)**
In Discord, run `!login`. The bot gives you a code and URL. Go to [microsoft.com/devicelogin](https://microsoft.com/devicelogin) on your phone/laptop, enter the code, sign in. Done. Token cached 23 hours.

No browser needed on the server. Works on Railway, Docker, whatever.

---

## Railway Deployment (since you'll forget)

1. Push to GitHub (secrets are gitignored, relax)
2. Create a new Railway project → Deploy from GitHub
3. Set env vars: `DISCORD_BOT_TOKEN`, `DISCORD_WEBHOOK_URL`
4. Railway auto-detects the `Procfile` → runs `python main.py`
5. In Discord, run `!login` → enter the code on your phone
6. Snipe away

---

## Discord bot commands (I know you'll forget these too)

| Command | What it does |
|---|---|
| `!snipe <name> [drop_time] [delay_ms] [workers]` | Snipe a name now, or schedule it for a UTC ISO drop time |
| `!autosnipe <name> [delay_ms] [workers]` | Polls every 5s until it drops, then fires automatically |
| `!superfastsnipe <name>` | 10 workers, 0ms delay, TCP pre-warmed. Instant. Don't think, just use it |
| `!scrapetime <name>` | Checks availability + holder UUID + how long they've had it. Actually accurate |
| `!cancel <name>` | Stop stalking that name |
| `!status` | See what's running |
| `!setdelay [ms]` | Override the auto-tuned delay. Or don't pass a value to see what it is |
| `!help` | Sends you this info in Discord since you'll lose this README anyway |

---

## How the timing works (since you'll ask)

1. Resolves DNS once at startup — no DNS overhead per request
2. Warms up the TCP connection 10s before the scheduled drop
3. Sleeps until 200ms before fire time (saves CPU)
4. Spin-loops on `perf_counter_ns` for the final 200ms — sub-millisecond accuracy
5. All workers fire at the exact same nanosecond
6. Auto-tunes the delay offset after every timed snipe

It's fast. Faster than you deserve.

---

## FAQ

**Q: How do I use this?**
A: There's a whole section above. Literacy is free.

**Q: It's not working!**
A: Skill issue.

**Q: The token expired!**
A: It auto-refreshes when you restart. Restart it, genius.

**Q: Can you add [feature]?**
A: No.
