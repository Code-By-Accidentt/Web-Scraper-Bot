# 🔍 Website Contract Monitor Bot

![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python)
![License](https://img.shields.io/github/license/Tomashevskyi-Vladyslav/Website_Contract_Monitor_Bot)
![Issues](https://img.shields.io/github/issues/Tomashevskyi-Vladyslav)

> **Automated Web Watcher for Smart‑Contract Addresses**
> Tracks multiple websites (and their common sub‑pages) for Ethereum & Solana addresses, validates them over RPC, sends Telegram alerts, **auto‑pauses** when too many contracts appear, and keeps a full change‑log diff of every page.

---

## ✨ Key Features

* **Multi‑chain validation** – Ethereum (Mainnet, Base, Arbitrum, Optimism) + Solana
* **Dynamic page rendering** with Selenium + Chrome Driver
* **Content diff engine** – detects added/removed text & attribute changes
* **Threshold‑based auto‑pause** – stops parsing when ≥ 5 confirmed contracts are found at once and ships a text file with all addresses
* **Per‑site notification switch** – `/notify` & `/nonotifi` commands
* **Persistent state** – monitored sites, diffs, caches, and rejected addresses survive restarts
* **Thread‑pool architecture** – one global executor per root site; inner pools for sub‑URLs
* **Telegram bot control** – add/remove/pause/resume/list sites in real time
* **Snapshot clean‑up** – orphaned HTML snapshots are purged automatically

---

## 📂 Repository Layout

```text
.
├── main.py                    # Main entry‑point (this repo’s giant script)
├── rebooter.py                # A mandatory rebooter so that the bot continues working immediately after rebooting.
├── requirements.txt           # Exact, pinned runtime deps
├── snapshots/                 # HTML snapshots for diffing (auto‑generated)
├── monitored_sites.txt        # URLs added via /add (one per line)
├── changes_count.json         # Per‑URL diff statistics (auto)
├── found_wallets.txt          # ✅ Confirmed contracts (auto, rotates at 120)
├── rejected_contracts.txt     # 🚫 Non‑contract Solana addresses (auto, rotates at 100)
└── notifications_status.json  # Per‑site ON/OFF flags
```

---

## 🚀 Quick Start

```bash
# 1.  Clone the repo
$ git clone https://github.com/Tomashevskyi-Vladyslav/Website_Contract_Monitor_Bot.git
$ cd Website_Contract_Monitor_Bot

# 2.  Create Python 3.10+ venv & install deps
$ python -m venv .venv && source .venv/bin/activate
$ pip install -r requirements.txt

# 3.  Export mandatory tokens (or put them in .env)
$ export TELEGRAM_BOT_TOKEN="123456:ABC…"
$ export ETH_RPC_URL="https://mainnet.infura.io/v3/<KEY>"

# 4.  Run the bot
$ python bot.py
```

> **Docker?**  A minimal `Dockerfile` is included – `docker compose up -d` spins everything in one command.

---

## ⚙️ Configuration

### Environment Variables

| Variable             | Required | Default                               | Description                                                              |
| -------------------- | -------- | ------------------------------------- | ------------------------------------------------------------------------ |
| `TELEGRAM_BOT_TOKEN` | ✅        | –                                     | Bot token from [@BotFather](https://t.me/BotFather)                      |
| `CHAT_IDS`           | ❌        | hard‑coded list                       | Comma‑separated chat IDs that receive alerts                             |
| `ETH_RPC_URL`        | ✅        | Infura endpoint                       | Ethereum (or compatible) full node                                       |
| `SOLANA_RPC_URL`     | ❌        | `https://api.mainnet-beta.solana.com` | Solana RPC endpoint                                                      |
| `*_RPC_URL`          | ❌        | public nodes                          | **Base**, **Arbitrum**, **Optimism**, **BSC**, **Fantom**, **Avalanche** |

### Thresholds & Limits

| Constant                        | Purpose                           | Default |
| ------------------------------- | --------------------------------- | ------- |
| `CONTRACT_PAUSE_THRESHOLD`      | Max contracts before *auto‑pause* | `5`     |
| `found_wallets.txt` rotate      | Max cached addresses before wipe  | `120`   |
| `rejected_contracts.txt` rotate | Max pseudo‑tokens before wipe     | `100`   |

Feel free to tweak those values at the top of `bot.py`.

---

## 💬 Telegram Commands

```text
/add <url>       – start monitoring the site
/remove <url>    – stop monitoring
/pause <url>     – temporary pause
/resume <url>    – resume after pause
/list            – show sites + status
/nonotifi <url>  – mute all diff alerts
/notify <url>    – un‑mute alerts
/help            – command reference (in chat)
```

> **Tip:** `/list` returns both *notification* status (ON/OFF) and *running* status (RUNNING/PAUSED) for every URL.

---

## 🔍 How It Works

1. **Listener thread** polls the Telegram Bot API ➜  handles user commands.
2. Each **root URL** gets its own monitoring thread:

   * Builds a list of common sub‑URLs (`/docs`, `/app`, `app.<domain>`, etc.)
   * Performs a quick `HEAD` request – announces newly discovered sub‑domains.
   * Fetches the fully rendered HTML with *headless Chrome*.
   * Extracts potential wallets via regex ➜ validates on‑chain.
   * Runs a **double‑pass diff** (download → wait 2 s → re‑download) to kill false positives.
3. If ≥ `CONTRACT_PAUSE_THRESHOLD` **validated** contracts appear on **one page**:

   * Sends them as a `.txt` document.
   * Auto‑pauses the root site (and all its sub‑pages).
4. All snapshots, counters and caches are flushed to disk every time a diff or command modifies the state.

---

## 🛠 Development

```bash
# run linters & tests
autopep8 -r --in‑place bot.py
pytest -q
```

### Useful Make Targets

```makefile
make install   # poetry install
make run       # python bot.py
make lint      # ruff + mypy
```

---

## 🤝 Contributing

1. **Fork** the repo
2. Create your feature branch: `git checkout -b feat/amazing`
3. Commit your changes: `git commit -m 'feat: add amazing feature'`
4. Push to the branch: `git push origin feat/amazing`
5. Open a **Pull Request**

Please run `pre‑commit` before pushing

---

## 📜 License

Distributed under the MIT License.  See `LICENSE` for more information.

---

## Acknowledgements

* [web3.py](https://github.com/ethereum/web3.py)
* [solana‑py](https://github.com/michaelhly/solana-py)
* [Selenium](https://www.selenium.dev/documentation/) & [ChromeDriver](https://developer.chrome.com/docs/chromedriver/get-started)
* [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/bs4/doc/)
