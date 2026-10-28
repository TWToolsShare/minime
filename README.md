# minime

<p align="center">
  <b>English</b> · <a href="https://github.com/TWToolsShare/minime/blob/main/README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/TWToolsShare/minime/main/docs/brand/logo-dark.svg" alt="minime" width="340">
</p>

<p align="center">Turn the work you do with AI into a second self you can always ask.</p>

The decisions you made, the pitfalls you hit, and the conventions you set in Trae, Qoder, or WorkBuddy are scattered across chat logs. A few days later, they are hard to find again. minime collects those traces on your machine, turns them into searchable knowledge, and hands them back to AI through chat or MCP — so it answers with your past judgement instead of generic advice.

- **Read-only collection**: never modifies the data of the apps it reads
- **Local-first**: your knowledge base, config, and logs stay on your own machine
- **Traceable answers**: every answer cites its sources and links back to the original session or file
- **Gets better with use**: mark answers as useful or irrelevant and retrieval ranking adjusts

Current version: `v0.1.0-pre.9`. Supports macOS, Windows, and Linux.

<p align="center">
  <img src="https://raw.githubusercontent.com/TWToolsShare/minime/main/docs/panel.png" alt="minime panel: knowledge browser" width="900">
</p>

---

## Quick start

### 1. Install

Requires Python 3.10+. pipx is recommended — it creates an isolated environment and does not touch your system Python:

```bash
pipx install \
  --index-url "https://repo.repsy.io/tengwang/pypi/simple/" \
  --pip-args="--prerelease=allow" minime

# Optional enhancements: MCP server, Trae cloud sessions, vector search acceleration
pipx inject minime mcp cryptography numpy
```

> The three optional dependencies live on upstream PyPI, so they are installed with `inject` rather than an extra. minime works without them; you only lose MCP, cloud session decryption, and vector acceleration.

### 2. First build

```bash
minime setup
```

This collects the data already on your machine, cleans it into knowledge entries, and builds the knowledge graph and capability profile. The first run takes a few minutes.

The scheduled task is installed automatically on first use: it runs every 30 minutes to collect, extract turns, and clean. Once you configure a model endpoint and enable **LLM enrichment** in Settings, it also enriches automatically — no manual top-up needed.

### 3. Open the panel

```bash
minime web
```

Then open **http://127.0.0.1:8765/**.

For the first run, this order works well:

1. Go to **Progress** → click **Full update** → wait for it to finish
2. Go to **Chat** → ask a question you have thought about recently
3. If the answer is not good enough → go back to **Progress** and run more enrichment

---

## What is in the panel

There are 11 tabs. Three of them cover most daily use:

| Tab | What it does | When to use it |
|---|---|---|
| **Chat** | Ask and follow up against your knowledge base, with citations | Most of the time |
| **Progress** | See how far collection, cleaning, and enrichment have gone | When you want to know "how much is left" |
| **Knowledge** | Browse and filter knowledge by project, tech, or type | When you want to read specific entries |

The rest are there when you need them:

| Tab | What it does |
|---|---|
| Knowledge graph | Relationships between projects, technologies, and concepts |
| Profile | Eight capability dimensions inferred from your work traces |
| Packs | Export and import shareable knowledge packs |
| Workspaces | Which workspaces were collected and how many sessions each has |
| Sources | Status, schedule, and manual trigger for every data source |
| Logs | All operations, filterable by level and keyword |
| Settings | Directories, models, and collection intervals |
| Archive | Where raw data lives and how much space it uses |

---

## Everyday use

### Ask questions

Open **Chat**, type your question at the bottom, press Enter to send, Shift+Enter for a new line.

Answers follow two rules:

- **If the knowledge base has evidence** → give the conclusion and cite sources as `[1] [2]`; click a citation to read the original entry
- **If it does not** → say "not in the knowledge base" first, then offer general advice labelled "general experience"

This lets you tell your own experience apart from model guesses at a glance.

You can follow up within the same conversation; it remembers the last six turns. The left sidebar lists past conversations — switch, rename, or delete them. Refreshing the page does not lose anything.

Prefer the command line?

```bash
minime chat "What architecture decisions have I made?"
minime chat --session S18e10c207b6f "What was the reasoning?"   # continue a conversation
minime chat --list                                               # list conversations
```

### Finish pending enrichment

Open **Progress** and look at the pending count:

- **Enrich a batch**: processes 12 turns — good for a quick top-up
- **Enrich all**: processes everything; takes longer and can be stopped from the top bar
- Command line: `minime llm --run 50` or `minime llm --all`; runs in the background and keeps going after you close the terminal

Progress is measured as "processed turns / turns with meaningful changes". A turn may produce zero entries or three, so counting turns is more accurate than counting entries.

### Tell it what is useful

Every card in the Knowledge tab has **Adopt / Cite / Irrelevant** buttons:

- Adopt or cite → this entry becomes easier to retrieve later
- Irrelevant → lowers its ranking and slightly reduces confidence; repeated marks move it to "demoted" rather than deleting it, and you can restore it from **Status → Demoted**

Knowledge cited in a chat answer is also boosted automatically. That is the "gets better with use" loop.

---

## What it can collect

| Source | Configuration | What is collected |
|---|---|---|
| **Trae** | None | Session snapshots, cloud sessions, workspace file history, `~/.trae/memory` |
| **Local docs / notes / code** | Required | Settings → **Local sources → Directories**, one path per line |
| **Obsidian** | Usually none | Vaults are discovered automatically; frontmatter, `[[wikilinks]]`, and `#tags` are supported |
| **WorkBuddy / WorkBuddy AI** | None | Sessions, long-term memory, and filled-in identity files |
| **Qoder** | None | Local sessions, message text, and per-turn changed files |
| **Doubao** | Required | Recommended: export JSON / Markdown to a fixed directory and set **Doubao export directory** in Settings |

A few notes:

- **Local directories**: changes take effect on the next collection; use `minime collect --force` to collect immediately.
- **Obsidian privacy**: add `private: true` or `minime: ignore` to a note's frontmatter and the whole note is skipped.
- **Doubao**: the export-directory route needs no system permissions. Only direct reads from the Doubao app container require macOS Full Disk Access for the terminal running minime (not for Doubao itself), and content stored in IndexedDB still needs the export route.
- **Collection intervals**: each source can be tuned in Settings → **Scheduled collection → Source intervals**. Changing intervals does not require reinstalling the scheduled task; changing the trigger interval does.
- **Scheduled collection**: launchd on macOS, Task Scheduler on Windows, and `minime watch` on Linux. The task is installed automatically on first run; it collects, extracts turns, and cleans, and also enriches when LLM enrichment is enabled.

### Recommended scan directories

**No local directory is scanned by default** — you choose them. The rule is simple: **only add directories where you keep your own docs, notes, or code; never add a whole disk or system directories.** Too many roots slow collection down, pull in noise, and may pick up private files.

In Settings → **Local sources → Directories**, add one path per line (or set them all at once with `minime set collect.local.roots`, comma-separated). Common setups:

| Your situation | Suggested directories |
|---|---|
| Only use AI coding assistants (Trae / Cursor, etc.) | Nothing to add — sessions, snapshots, and file history are collected automatically |
| You have personal notes (other than Obsidian) | `~/notes`, `~/Documents/notes` |
| You write code | Your code root, e.g. `~/code`, `~/projects`, `~/workspace` |
| You hoard docs and references | `~/Documents` (unless it contains huge media/backup folders) |
| Multiple disks / external drive | Use the mount path directly, e.g. `/Volumes/data/docs` |
| Windows | `D:\work`, `C:\Users\<you>\Documents\notes` |

Practical tips:

- **Prefer leaf directories** (e.g. `~/code/my-project`) over broad roots like `~` or `~/Documents`.
- **Don't add Obsidian vaults** — they are discovered automatically; adding them collects everything twice.
- **Avoid** `~/Library`, `~/Downloads` (usually temporary), `.git`, and `node_modules` — these are excluded by default, but a huge root still wastes time.
- **Max file size** defaults to 256 KB (plenty for code/text); larger files are skipped. Tune with `max_file_bytes`.
- Start with 1–2 core directories, run `minime collect --force`, and check the file count under **Sources → local_docs** before adding more — easier to diagnose than a big batch at once.

The structure in `config.json` (`~/Library/Application Support/minime/config.json` on macOS, `%LOCALAPPDATA%\minime\config.json` on Windows):

```json
{
  "collect": {
    "local": {
      "roots": ["~/notes", "~/code", "/Volumes/data/docs"],
      "max_files": 20000,
      "max_file_bytes": 262144
    }
  }
}
```

---

## Connect to Trae / Claude Code / Cursor

Let your AI client call the knowledge base directly:

```json
{
  "mcpServers": {
    "minime": {
      "command": "minime",
      "args": ["mcp"]
    }
  }
}
```

> If `minime` is not on PATH, set `command` to the absolute path of your Python interpreter and `args` to `["/absolute/path/to/minime/mcp_server/server.py"]`.

To share one memory across AIs or machines, use HTTP mode:

```bash
minime mcp --http --port 8791
# client URL: http://<host>:8791/mcp
```

Once connected, you can say things like "use minime to look up how I handled this before". There are 18 tools; the four most used are:

| Tool | Purpose |
|---|---|
| `minime_decide` | Make a decision suggestion based on your past experience |
| `minime_recall` | Search knowledge |
| `minime_chat` | Multi-turn Q&A |
| `minime_troubleshoot` | Look up troubleshooting experience |

---

## Share your experience

Package your knowledge so someone else can reuse your judgement:

```bash
minime pack export --name my-worker --author alice
minime pack inspect my-worker.minime.zip      # see what is inside first
minime pack import my-worker.minime.zip --dry-run
minime pack import my-worker.minime.zip
```

- Exports exclude raw conversations by default; absolute paths are normalized to `~`, and keys/tokens are replaced with `***`
- Packs are grouped into collections; after import everything is disabled until you enable the collections you trust
- You can only export packs you created; imported packs are read-only and cannot be re-exported or deleted

---

## Data and privacy

| Item | Detail |
|---|---|
| Collection | Read-only; original app data is never modified |
| Data location | macOS `~/Library/Application Support/minime`; Windows `%LOCALAPPDATA%\minime`; Linux `~/.local/share/minime` |
| Custom location | Set `MINIME_HOME` |
| Network | Nothing is uploaded automatically; the panel binds to `127.0.0.1` and rejects non-loopback Host headers |
| Config file | `config.json` is written atomically with mode `600`; guard it if it contains keys |
| Exports | Desensitized by default; sensitive attributes are excluded |

Upgrading, changing versions, or changing installation methods never touches the data directory. Back up by copying the whole data directory; to move machines, copy it over or set the same `MINIME_HOME` on the new machine.

When migrating from a source checkout to the pipx install:

```bash
minime migrate --from /path/to/old/minime
```

---

## Common commands

```bash
# Daily
minime web                     Open the panel
minime status                  Collection / enrichment / schedule / service at a glance
minime chat "question"         Ask from the command line

# Updates and enrichment
minime update                  Incremental update (collect + clean)
minime collect --force         Re-collect everything now
minime llm --run 20            Enrich one batch in the background
minime llm --all               Enrich every pending turn

# Configuration
minime config                  Show the effective configuration
minime set llm.enabled true    Change one setting
minime set collect.local.roots ~/notes

# Diagnostics and data
minime doctor                  Self-check; exits 0 when everything passes
minime logs -f                 Follow the runtime log
minime restore                 Rebuild the knowledge base without re-collecting
minime diag                    Export a desensitized diagnostic bundle

# Scheduled collection
minime install                 Install (or reinstall) the scheduled task; auto-installed on first run
minime uninstall               Remove the scheduled task; data is kept
```

---

## FAQ

**`minime: command not found` after installation**
pipx's bin directory is not on PATH. Run `pipx ensurepath`, or open a new terminal. `minime link` can also register the command in `~/.local/bin`.

**The pending count in Progress never moves**
Scheduled runs now include enrichment, so two things to check: make sure an LLM endpoint is configured and enabled in Settings (without it, minime only collects and cleans and never enriches); and that the scheduled task is still loaded (it is installed automatically on first run — if you removed it with `minime uninstall`, reinstall with `minime install`). You can always top up manually with **Enrich a batch / Enrich all** or `minime llm --run 20`.

**It says "not in the knowledge base"**
Check whether Progress still has pending turns; then check Sources and Workspaces to confirm the content was collected at all.

**A source is red**
Open Sources — the red row tells you the next action; `minime doctor` gives more detail. One failing source does not affect the others.

**Doubao says Operation not permitted**
This is macOS privacy protection, not a broken app. The recommended fix is to export Doubao conversations to a fixed directory and set **Doubao export directory** in Settings; no permissions are needed.

**A config change has no effect**
`web.*` requires a panel restart, `schedule.*` requires `minime install`, and `collect.*` applies on the next collection. `minime set` tells you which step is needed.

**The knowledge base was cleared**
The data is not really lost — raw archives are still there. Run `minime restore` to rebuild without re-collecting.

**Where are the logs?**
`store/logs/minime.jsonl` under the data directory, or the Logs tab in the panel.

**How do I report a problem?**
Run `minime diag`. It produces a desensitized bundle (self-check + progress + config + recent logs) that you can share directly.

---

## More documentation

- [User guide (Chinese)](https://github.com/TWToolsShare/minime/blob/main/docs/使用手册.md) — the complete end-user manual
- [Changelog](https://github.com/TWToolsShare/minime/blob/main/CHANGELOG.md)
- [Contributing (Chinese)](https://github.com/TWToolsShare/minime/blob/main/CONTRIBUTING.md) — development setup, tests, and commit conventions
- [Security policy (Chinese)](https://github.com/TWToolsShare/minime/blob/main/SECURITY.md) — how to report security issues privately

The user guide, contributing guide, and security policy are currently written in Chinese.

## License

MIT
