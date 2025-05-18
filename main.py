import requests, time, random, re, os, json, logging, threading, base58
from bs4 import BeautifulSoup, Comment
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from web3 import Web3
from eth_abi import abi
from solana.rpc.api import Client as SolanaClient

# ─── universal import ───
try:
    # new branch solana-py (≥ 0.31)
    from solders.pubkey import Pubkey as SolanaPubKey

    def to_pubkey(addr: str) -> SolanaPubKey:
        return SolanaPubKey.from_string(addr)

except ModuleNotFoundError:
    # old branch solana-py (≤ 0.30)
    from solana.publickey import PublicKey as SolanaPubKey

    def to_pubkey(addr: str) -> SolanaPubKey:
        return SolanaPubKey(addr)


# ──────────────── RPC END‑POINTS ────────────────
ETH_RPC_URL = os.getenv("ETH_RPC_URL",  "https://mainnet.infura.io/v3/YOUR_INFURA_KEY")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
w3 = Web3(Web3.HTTPProvider(ETH_RPC_URL))
solana_client = SolanaClient(SOLANA_RPC_URL)


# ──────────────── TELEGRAM ────────────────
TELEGRAM_BOT_TOKEN = "7902331563:AAHZMdJ8ODtn6cJnw3Tx8sJIw1CoVtCv7_0"
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
CHAT_IDS = [995250840, 697976816]


# ──────────────── FILES ────────────────
MONITORED_SITES_FILE = "monitored_sites.txt"
CHANGES_COUNT_FILE = "changes_count.json"
WALLETS_FILE = "found_wallets.txt"
REJECTED_CONTRACTS_FILE = "rejected_contracts.txt"
NOTIFICATIONS_STATUS_FILE = "notifications_status.json"
SNAPSHOT_DIR = "snapshots"
os.makedirs(SNAPSHOT_DIR, exist_ok=True)


# ──────────────── GLOBALS ────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

monitored_sites, site_threads = set(), {}
stop_flags, changes_count_dict = defaultdict(threading.Event), {}
site_notifications_dict = defaultdict(lambda: True)

CONTRACT_PAUSE_THRESHOLD = 5 # Contract limit
paused_sites = set() # Stores paused URLs

# Global set of "already-announced" subdomains + lock in case of flow competition
announced_subdomains : set[str] = set()
announced_subdomains_lock = threading.Lock()

# ──────────────── wallets / rejected ────────────────
known_wallets = set()
rejected_contracts = set() 


# ─────── GRANT AUTO-CREATION FOR CACHES ───────
# Opening in 'a' mode will create the file if it doesn't exist, and won't overwrite it if it already exists.
for _f in (WALLETS_FILE, REJECTED_CONTRACTS_FILE):
    open(_f, "a", encoding="utf-8").close()


# ──────────────── transferring data from a document ────────────────
if os.path.exists(WALLETS_FILE):
    with open(WALLETS_FILE) as f:
        known_wallets = {line.strip() for line in f}


if os.path.exists(REJECTED_CONTRACTS_FILE):
    with open(REJECTED_CONTRACTS_FILE) as f:
        rejected_contracts = {line.strip() for line in f}


# A dictionary that tracks whether each site has notifications ON/OFF:
# True = ON, False = OFF
site_notifications_dict = defaultdict(lambda: True)


# ──────────────── locks ────────────────
sites_lock = threading.Lock()
wallets_lock = threading.Lock()
rejected_lock  = threading.Lock()


print(rejected_contracts)
patterns = {
#    "Bitcoin": r"\b(1|3|bc1)[a-zA-HJ-NP-Z0-9]{25,39}\b",
    "Ethereum": r"\b0x[a-fA-F0-9]{40}\b",
    "Solana": r"[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]{32,44}"
}

executor = ThreadPoolExecutor(max_workers=15)

# ================== ADDITIONAL URL PATTERNS ==================
SUBURL_PATTERNS = [
    "https://app.{domain}",
    "https://app.{domain}/leaderboard",
    "https://{domain}/dashboard",
    "https://docs.{domain}",
    "https://{domain}/app",
    "https://doc.{domain}",
    "https://{domain}/create",
]

def derive_sub_urls(base_url: str) -> list:
    """For a given base_url, returns a list of sub-URLs based on patterns."""

    # extract clean domain without protocol and path
    domain = base_url.split("://", 1)[1].split("/", 1)[0]
    return [pattern.format(domain=domain) for pattern in SUBURL_PATTERNS]

# ──────────────── HELPERS ────────────────
ERC20_SIGS = ["0x18160ddd", "0x95d89b41", "0x313ce567"]  # totalSupply/symbol/decimals


# ───────── EV M  NETWORKS  ─────────
EVM_NETWORKS = [
    {"name": "Ethereum", "rpc": ETH_RPC_URL}, # mainnet
    {"name": "Base"     , "rpc": os.getenv("BASE_RPC_URL","https://base-rpc.publicnode.com")},
    {"name": "Arbitrum" , "rpc": os.getenv("ARB_RPC_URL", "https://arb1.arbitrum.io/rpc")}, 
    {"name": "Optimism" , "rpc": os.getenv("OPT_RPC_URL", "https://mainnet.optimism.io")}, 

    {"name": "BSC", "rpc": os.getenv("BSC_RPC_URL",   "https://bsc-rpc.publicnode.com")},
    {"name": "Fantom", "rpc": os.getenv("FANTOM_RPC_URL","https://rpc.soniclabs.com")},
    {"name": "Avalanche", "rpc": os.getenv("AVAX_RPC_URL",  "https://avax.meowrpc.com")},
]


def _looks_like_erc20(w3, checksum):
    """ERC-20 hint: contract responds to at least one of the three standard signatures."""
    for sig in ERC20_SIGS:
        try:
            data = Web3.to_bytes(hexstr=sig)# <— explicit conversion
            if w3.eth.call({"to": checksum, "data": data}) not in (b"", b"\x00"):
                return True
        except Exception:
            pass
    return False


def is_evm_contract(addr: str, erc20_only=True):
    if not Web3.is_address(addr):
        return False, None

    for net in EVM_NETWORKS:
        w3_local = Web3(Web3.HTTPProvider(net["rpc"], request_kwargs={"timeout": 3}))
        if not w3_local.is_connected():
            continue

        checksum = Web3.to_checksum_address(addr)
        try:
            code = w3_local.eth.get_code(checksum)
        except Exception:
            continue

        if code and len(code) > 0:
            if not erc20_only or _looks_like_erc20(w3_local, checksum):
                return True, net["name"]
    return False, None


def is_solana_contract(addr: str) -> bool:
    """Returns True if:
    • Account executable (smart contract)
    • OR mint account of SPL token (owner = Tokenkeg…, data = 82 bytes)

    Works on solana-py 0.27-0.30 (dict API) and 0.31+ (solders API)."""
    try:
        resp = solana_client.get_account_info(to_pubkey(addr))

        # ─── NEW solders‑API (0.31+) ────────────────────────
        if hasattr(resp, "value"):
            acct = resp.value          # None or solders.account.Account
            if acct is None:
                logging.info(f"[RPC] {addr} → account not found")
                return False

            if acct.executable:
                return True

            # mint account?
            if str(acct.owner) == TOKEN_PROGRAM_ID and len(acct.data) == 82:
                return True

            logging.info(f"[RPC] {addr} → exists, not executable/mint")
            return False

        # ─── OLD dict‑API (≤0.30) ─────────────────────────
        if isinstance(resp, dict):
            info = resp.get("result", {}).get("value")
            if info is None:
                logging.info(f"[RPC] {addr} → account not found")
                return False

            if info.get("executable", False):
                return True

            if (info.get("owner") == TOKEN_PROGRAM_ID and
                len(info.get("data", ["", ""])[0]) == 82):
                return True

            logging.info(f"[RPC] {addr} → exists, not executable/mint")
            return False

        logging.error(f"[RPC] unexpected type: {type(resp)}")
        return False

    except Exception as e:
        logging.error(f"[RPC] Solana error for {addr}: {e}")
        return False
    

def split_valid_invalid(wallets: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    
    """Takes a list [(chain, addr)…] and returns two:
    • valid - addresses that actually have a contract/mint (via RPC)
    • invalid - everything else"""
    
    valid, invalid = [], []

    for chain, addr in wallets:
        
        if chain == "Ethereum":
            ok, _ = is_evm_contract(addr, erc20_only=True)
        
        elif chain == "Solana":
            ok = is_valid_solana_address(addr) and is_solana_contract(addr)
        
        else: # Bitcoin and others
            ok = False
        (valid if ok else invalid).append((chain, addr))

    return valid, invalid


def is_valid_bitcoin_address(addr: str) -> bool:
    if addr.startswith("bc1"):  # seg‑wit, we skip
        return False
    try:
        base58.b58decode(addr)
        return True
    except Exception:
        return False


def is_valid_ethereum_address(addr: str) -> bool:
    return addr.startswith("0x") and len(addr) == 42


def is_valid_solana_address(addr: str) -> bool:
    try:
        return len(base58.b58decode(addr)) == 32
    except Exception:
        return False


def canonical_address(chain: str, addr: str) -> str:
    """Unified view for checking duplicates."""
    if chain == "Ethereum":
        try:
            return Web3.to_checksum_address(addr)
        except Exception:
            return addr.lower()
    return addr  # Solana/Bitcoin are case sensitive find_wallet


def clear_wallets_if_threshold():
    """When 120 unique addresses are reached, we clear the file and reset the set."""
    if len(known_wallets) < 120:
        return # Function exit

    if len(known_wallets) >= 120:
        # reset the file
        open(WALLETS_FILE, 'w', encoding='utf-8').close()
        # reset in-memory set
        known_wallets.clear()
        
        logging.info("⚠️ Limit of 120 wallets reached - file and set cleared.")
        
        return # Function exit


def is_already_known(chain: str, addr: str) -> bool:
    """Checking if the contract is in the cache.
    For Ethereum, we use the checksum form to avoid false duplicates.
    True if the address has already been confirmed *or* has already been rejected."""

    canon = canonical_address(chain, addr)
    with wallets_lock, rejected_lock:
        if chain == "Solana" and canon in rejected_contracts:
            return True
        return canon in known_wallets


# ──────────────── REJECTED (non-contracts) HELPERS ────────────────
def clear_rejected_if_threshold():
    """When 35 records are reached, we clear the rejected_contracts file/set."""
    if len(rejected_contracts) >= 100:
        open(REJECTED_CONTRACTS_FILE, "w", encoding="utf-8").close()
        rejected_contracts.clear()
        logging.info("⚠️  Limit of 100 rejected Solana addresses reached -> cache cleared.")
        
        return# Function exit
    
    else :

        return # Function exit


def save_rejected(addr: str) -> None:
    """Caches Solana address as 'non-smart':
    • canonicalizes it (to avoid case-specific duplicates),
    • flushes cache when limit of 35 is reached,
    • immediately flushes buffer to disk and logs current count."""
    # 1) Canonize the address as we do for valid contracts
    addr = canonical_address("Solana", addr)

    
    # 2) If it is already in the cache, skip it
    if addr in rejected_contracts:
        return

    # 3) Overflow Cleaning
    if len(rejected_contracts) >= 35:
        open(REJECTED_CONTRACTS_FILE, "w", encoding="utf-8").close()
        rejected_contracts.clear()
        logging.info("⚠️  Limit of 35 rejected Solana addresses reached – cache cleared.")

    # 4) Writing to file and memory
    with open(REJECTED_CONTRACTS_FILE, "a", encoding="utf-8") as f:
        f.write(addr + "\n")
        f.flush()
        os.fsync(f.fileno())
    rejected_contracts.add(addr)

    # 5) We log the fact and the current state of the cache
    logging.info(f"🚫 Solana {addr} recorded as NOT a smart-contract " f"(total rejected now: {len(rejected_contracts)})")
    
    return


# === STATE PERSISTENCE ===
def save_state():
    """Save all relevant data (monitored sites, changes_count_dict, site_notifications_dict)."""
    logging.info("Saving state to files...")

    # 1) Save monitored sites
    with sites_lock:
        with open(MONITORED_SITES_FILE, "w", encoding="utf-8") as f:
            for site in monitored_sites:
                f.write(site + "\n")

    # 2) Save changes_count_dict
    # We only save the keys that are currently being tracked
    with sites_lock:
        # Generate a set of allowed keys: base + all sub-URLs
        allowed = set()
        for site in monitored_sites:
            allowed.add(site)
            allowed.update(derive_sub_urls(site))
        filtered = {u: list(v) for u, v in changes_count_dict.items() if u in allowed}
        with open(CHANGES_COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump(filtered, f, ensure_ascii=False, indent=2)

    # 3) Save site_notifications_dict
    with open(NOTIFICATIONS_STATUS_FILE, "w", encoding="utf-8") as f:
        normal_dict = dict(site_notifications_dict)
        json.dump(normal_dict, f, ensure_ascii=False, indent=2)


def load_state():
    """Load monitored_sites, changes_count_dict, site_notifications_dict from disk."""
    logging.info("Loading state from files...")

    # A) Load the sites
    if os.path.exists(MONITORED_SITES_FILE):
        with open(MONITORED_SITES_FILE, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        for site in lines:
            monitored_sites.add(site)

    # B) Load the changes_count
    if os.path.exists(CHANGES_COUNT_FILE):
        with open(CHANGES_COUNT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for url, counts in data.items():
            changes_count_dict[url] = tuple(counts)

    # C) Load the per-site notification statuses
    if os.path.exists(NOTIFICATIONS_STATUS_FILE):
        with open(NOTIFICATIONS_STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for url, status in data.items():
            site_notifications_dict[url] = bool(status)

    # D) Start monitoring threads for each site
    for url in monitored_sites:
        stop_flags[url].clear()
        future = executor.submit(monitor_website, url)
        site_threads[url] = future


# === TELEGRAM COMMUNICATION ===
def send_telegram_message(text, chat_ids=None):
    if chat_ids is None:
        chat_ids = CHAT_IDS

    for chat_id in chat_ids:
        try:
            response = requests.post(f"{TELEGRAM_API_URL}/sendMessage", data={"chat_id": chat_id, "text": text}, timeout=5)
            if response.status_code != 200:
                logging.error(f"Error sending message to chat {chat_id}: {response.text}")

        except Exception as e:
            logging.error(f"Exception when sending message to chat {chat_id}: {e}")


def send_contracts_document(site_root: str, page_url: str, wallets: list[tuple[str, str]]) -> None:
    
    """Writes addresses to a temporary txt file, sends it to all CHAT_IDS and
    adds addresses to the cache known_wallets/WALLETS_FILE.
    wallets -> [(chain, addr), …] — both parts are needed for correct canonicalization"""

    # ---------------- we form a list of lines for the file ----------------
    addr_list = [addr for _, addr in wallets] # just addresses for .txt

    ts = int(time.time())
    fname_txt = f"contracts_{sanitize_url(site_root)}_{ts}.txt"
    with open(fname_txt, "w", encoding="utf-8") as f:
        for a in sorted(addr_list):
            f.write(a + "\n")

    # ---------------- send the file ----------------
    for cid in CHAT_IDS:
        with open(fname_txt, "rb") as doc:
            requests.post(f"{TELEGRAM_API_URL}/sendDocument", data = {"chat_id": cid, "caption": (f"⚠️ On the page {page_url} " f"discovered {len(addr_list)} contracts")}, files = {"document": doc}, timeout = 10)

    # ---------------- delete the file ----------------
    try:
        os.remove(fname_txt)
    
    except OSError:
        pass

    # ---------------- we fix the addresses as known ----------------
    with wallets_lock:
        clear_wallets_if_threshold()

        # canonicalization now takes into account the real network
        canon = {canonical_address(chain, addr) for chain, addr in wallets}

        new_items = canon - known_wallets
        if new_items:
            with open(WALLETS_FILE, "a", encoding="utf-8") as f:
                for a in new_items:
                    f.write(a + "\n")
            known_wallets.update(new_items)


def get_telegram_updates(offset=None, timeout=5):
    params = {"timeout": timeout, "offset": offset}
    try:
        response = requests.get(f"{TELEGRAM_API_URL}/getUpdates", params=params, timeout=timeout+5)

        if response.status_code == 200:
            data = response.json()
            return data.get("result", []) if data.get("ok") else []
        
        elif response.status_code == 409:
            return []   # just skip the iteration
        
        else:
            logging.error(f"HTTP {response.status_code} on getUpdates: {response.text}")
    
    except Exception as e:
        logging.error(f"Exception in getUpdates: {e}")
    
    return []


def telegram_listener():
    logging.info("Starting a thread to poll messages in Telegram...")
    last_update_id = 0

    while True:
        updates = get_telegram_updates(offset=last_update_id+1, timeout=30)
        for update in updates:
            if 'update_id' in update:
                last_update_id = update['update_id']

            message = update.get("message")
            if not message:
                continue

            chat_id = message["chat"]["id"]
            text = message.get("text", "").strip()

            # --- Parse Commands ---
            if text.startswith("/help"):
                send_telegram_message(
                    "Available commands:\n"
                    "/add <url> — start monitoring the site\n"
                    "/remove <url> — stop monitoring the site\n"
                    "/pause <url>    — pause monitoring\n"
                    "/resume <url>   — resume monitoring\n"
                    "/list — list sites and show each site's notification status\n"
                    "/nonotifi <url> — disable notifications for a specific site\n"
                    "/notify <url> — enable notifications for a specific site\n"
                    "/help — this help message",
                    [chat_id]
                )

            elif text.startswith("/add"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    send_telegram_message("Usage: /add <url>", [chat_id])
                    continue
                url = parts[1].strip()
                add_site(url, chat_id)

            elif text.startswith("/remove"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    send_telegram_message("Usage: /remove <url>", [chat_id])
                    continue
                url = parts[1].strip()
                remove_site(url, chat_id)

            elif text.startswith("/pause"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2: send_telegram_message("Usage: /pause <url>", [chat_id]); continue
                pause_site(parts[1].strip(), chat_id)

            elif text.startswith("/resume"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2: send_telegram_message("Usage: /resume <url>", [chat_id]); continue
                resume_site(parts[1].strip(), chat_id)
            

            elif text.startswith("/list"):
                with sites_lock:
                    if monitored_sites:
                        lines = []
                        for s in monitored_sites:
                            status_text = "ON" if site_notifications_dict[s] else "OFF"
                            run_text    = "PAUSED" if s in paused_sites else "RUNNING"
                            lines.append(f"- {s} [notif: {status_text}] [{run_text}]")

                        current_list = "➡️List of monitored sites:\n" + "\n".join(lines)

                    else:
                        current_list = "‼️Currently, no site is monitored‼️"
                send_telegram_message(current_list, [chat_id])

            elif text.startswith("/nonotifi"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    send_telegram_message("Usage: /nonotifi <url>", [chat_id])
                    continue
                url = parts[1].strip()
                disable_site_notifications(url, chat_id)

            elif text.startswith("/notify"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    send_telegram_message("Usage: /notify <url>", [chat_id])
                    continue
                url = parts[1].strip()
                enable_site_notifications(url, chat_id)


# === PAUSE / RESUME HELPERS ====================================  ⚙
# ──────────────── AUTO PAUSE ─────────────────────────────────────
def _auto_pause_site(url: str, triggered_page: str):

    """Pauses the site (root + all sub-URLs):
    • records in paused_sites
    • stops the current stream via stop_flags
    • sends a notification listing the sub-domains"""

    with sites_lock:
        if url in monitored_sites:
            paused_sites.add(url)
        stop_flags[url].set()

    subs = derive_sub_urls(url)
    reason = f" (trigger: {triggered_page})" if triggered_page else ""
    send_telegram_message(
        "⏸️ Parsing paused{}\n"
        "Website: {}\n"
        "Sub-pages:\n• {}".format(reason, url, "\n• ".join(subs)))
    save_state()


def pause_site(url: str, chat_id):
    with sites_lock:
        if url not in monitored_sites:
            send_telegram_message(f"❌ {url} not tracked", [chat_id]); return
        if url in paused_sites:
            send_telegram_message(f"⏸️ {url} already on pause", [chat_id]);     return
        paused_sites.add(url)
        stop_flags[url].set()
    save_state()
    send_telegram_message(f"⏸️ Parsing {url} stopped.", [chat_id])

def resume_site(url: str, chat_id):
    with sites_lock:
        if url not in monitored_sites:
            send_telegram_message(f"❌ {url} not tracked.", [chat_id]); return
        if url not in paused_sites:
            send_telegram_message(f"▶️ {url} already works.", [chat_id]);     return
        paused_sites.remove(url)
        stop_flags[url].clear()
        site_threads[url] = executor.submit(monitor_website, url)
    save_state()
    send_telegram_message(f"▶️ Parsing of {url} has been resumed.", [chat_id])


# === COMMAND HANDLERS FOR NOTIFICATIONS-ON/OFF (per site) ===

def disable_site_notifications(url, chat_id):
    """Turn OFF notifications for url (if it's monitored)."""
    with sites_lock:
        if url not in monitored_sites:
            send_telegram_message(f"❌Site {url} is not in the monitored list!", [chat_id])
            return
        site_notifications_dict[url] = False
    save_state()
    send_telegram_message(f"🔇 Notifications disabled for site: {url}", [chat_id])


def enable_site_notifications(url, chat_id):
    """Turn ON notifications for url (if it's monitored)."""
    with sites_lock:
        if url not in monitored_sites:
            send_telegram_message(f"❌Site {url} is not in the monitored list!", [chat_id])
            return
        site_notifications_dict[url] = True
    save_state()
    send_telegram_message(f"🔔 Notifications enabled for site: {url}", [chat_id])


# === SITE MANAGEMENT FUNCTIONS ===

def add_site(url, chat_id):
    with sites_lock:
        if url in monitored_sites:
            send_telegram_message(f"‼️Website {url} is already tracked‼️", [chat_id])
            return

        monitored_sites.add(url)
        site_notifications_dict[url] = True

        if url not in changes_count_dict:
            changes_count_dict[url] = (0, 0, 0)

        stop_flags[url].clear()
        future = executor.submit(monitor_website, url)
        site_threads[url] = future

    save_state()
    send_telegram_message(f"✅Site {url} added to tracking!✅", [chat_id])


def remove_site(url, chat_id):
    with sites_lock:
        if url not in monitored_sites:
            send_telegram_message(f"❌Site {url} is not in the monitored list!", [chat_id])
            return

        monitored_sites.remove(url)
        stop_flags[url].set()
        
        # 1) Remove the site from the list and stop monitoring
        if url in site_threads:
            del site_threads[url]

        # 2) Clear all change counters for the URL itself and its subdomains
        # so that no old records remain in memory or in the file
        if url in changes_count_dict:
            keys_to_remove = [url] + derive_sub_urls(url)
            for k in keys_to_remove:
                changes_count_dict.pop(k, None)

        # 3) Clear notification status
        if url in site_notifications_dict:
            del site_notifications_dict[url]

    # 4) Delete unnecessary snapshots from the disk
    remove_unused_snapshots(monitored_sites)

    # 5) Save the state so that CHANGES_COUNT_FILE is overwritten without the deleted keys
    save_state()
    send_telegram_message(f"✅Site {url} removed from tracking!✅", [chat_id])

def remove_unused_snapshots(current_sites):
    logging.info("Checking which snapshots are no longer needed...")
    sanitized_current = {sanitize_url(u) for u in current_sites}
    for file_name in os.listdir(SNAPSHOT_DIR):
        if file_name.endswith("_snapshot.html"):
            base_name = file_name.replace("_snapshot.html", "")
            if base_name not in sanitized_current:
                full_path = os.path.join(SNAPSHOT_DIR, file_name)
                os.remove(full_path)
                logging.info(f"Snapshot removed: {file_name}, site is no longer monitored.")

# === AUXILIARY FUNCTIONS ===
def sanitize_url(url: str) -> str:
    return url.replace("http://", "").replace("https://", "").replace("/", "_")


def get_snapshot_filename(url: str) -> str:
    return os.path.join(SNAPSHOT_DIR, f"{sanitize_url(url)}_snapshot.html")


def save_to_file(filename, content):
    with open(filename, "w", encoding="utf-8") as file:
        file.write(content)


def load_from_file(filename):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as file:
            return file.read()
    return ""


def get_dynamic_page_html(url):
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.get(url)
    time.sleep(8)
    page_source = driver.page_source
    driver.quit()
    return page_source


def clean_html(html):
    soup = BeautifulSoup(html, 'html.parser')

    # Deleting comments
    for comment in soup.find_all(text=lambda t: isinstance(t, Comment)):
        comment.extract()

    for tag in soup(['script', 'style', 'meta', 'noscript', 'iframe']):
        tag.decompose()

    for element in soup.find_all():
        # Remove "dynamic" attributes
        for attr in ["data-id", "session-id", "nonce", "csrf-token", "timestamp", "random", "dynamic-id", "tracking-id", "ng-.*", "data-v-.*"]:
            if attr in element.attrs:
                del element.attrs[attr]

    # Remove images with '?...' or timestamp in the link
    for img in soup.find_all('img'):
        for attr in ['src', 'data-src', 'lazy-src', 'srcset']:
            if attr in img.attrs and ('?' in img[attr] or 'timestamp' in img[attr]):
                img.decompose()

    # Remove tags that contain only numbers
    for tag in soup.find_all():
        if tag.string and re.match(r'^[0-9]+$', tag.string.strip()):
            tag.decompose()

    # Remove all class/id attributes containing numbers and letters:
    for tag in soup.find_all(True):
        for attr in ['class', 'id']:
            if attr in tag.attrs:
                if re.search(r'[a-f0-9]{5,}', ' '.join(tag[attr])):
                    del tag.attrs[attr]

    # Remove dates in dd/mm/yyyy format
    for tag in soup.find_all():
        if tag.string and re.search(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b', tag.string):
            tag.decompose()

    # Remove empty tags (which do not contain text and have no attributes)
    for tag in soup.find_all():
        if not tag.attrs and not tag.get_text(strip=True):
            tag.decompose()

    return str(soup)


def normalize_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup.find_all():
        # Sort the attributes to have consistent ordering
        tag.attrs = dict(sorted(tag.attrs.items()))
    return str(soup)


def compare_html(old_html, new_html):
    old_html = normalize_html(old_html)
    new_html = normalize_html(new_html)

    old_soup = BeautifulSoup(old_html, 'html.parser')
    new_soup = BeautifulSoup(new_html, 'html.parser')

    old_text = set(el.get_text(strip=True) for el in old_soup.find_all(['p', 'a', 'span', 'div', 'li', 'strong', 'table', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']))
    new_text = set(el.get_text(strip=True) for el in new_soup.find_all(['p', 'a', 'span', 'div', 'li', 'strong', 'table', 'h1', 'h2','h3', 'h4', 'h5', 'h6']))

    def make_hashable(attrs):
        return tuple((key, tuple(value) if isinstance(value, list) else value) for key, value in sorted(attrs.items()))

    old_attrs = set(make_hashable(tag.attrs) for tag in old_soup.find_all() if tag.attrs)
    new_attrs = set(make_hashable(tag.attrs) for tag in new_soup.find_all() if tag.attrs)

    removed_elements = old_text - new_text
    added_elements = new_text - old_text
    changed_attrs = old_attrs.symmetric_difference(new_attrs)

    return removed_elements, added_elements, changed_attrs


# ──────────────── ADDRESS SCRAPER ────────────────
def find_wallets(html: str):
    for chain, pat in patterns.items():
        for m in re.findall(pat, html):
            if   chain == "Bitcoin" and is_valid_bitcoin_address(m):   yield chain, m
            elif chain == "Ethereum" and is_valid_ethereum_address(m): yield chain, m
            elif chain == "Solana"  and is_valid_solana_address(m):    yield chain, m


def save_wallet(chain: str, address: str, src: str):
    """Write to file + notification, protection from repeats."""
    addr = canonical_address(chain, address)
    with wallets_lock:
        if addr in known_wallets:
            return
        
        clear_wallets_if_threshold()

        with open(WALLETS_FILE, "a") as f:
            f.write(addr + "\n")
        known_wallets.add(addr)
        

    logging.info(f"✅ {chain} contract {addr} confirmed on {src}")
    text = f"🔔 Contract found on {src}:\n{addr}"
    for cid in CHAT_IDS:
        try :
            requests.post(f"{TELEGRAM_API_URL}/sendMessage", timeout=5, data={"chat_id": cid, "text": text})
        except Exception as e:
            logging.error(f"Telegram error: {e}")

def notify_if_valid_contract(chain: str, address: str, src: str):
    """First, we check whether we have seen this address before.
    If yes, we skip further checks.
    If not, we perform cheap validation + "expensive" RPC check"""

    # ─── check the cache ────────────────────────────────
    if is_already_known(chain, address):
        logging.info(f"⚠️  {chain} contract {address} already found earlier - skipping checks")
        return # ← exit without base58 / RPC
    
    # ─── Cheap + expensive validation ────────────────────────────────
    valid = False 
    if chain == "Ethereum":
        valid = is_evm_contract(address, erc20_only=True)
    elif chain == "Solana":
        valid = is_valid_solana_address(address) and is_solana_contract(address)
    elif chain == "Bitcoin":
        valid = False  # Bitcoin has no smart contracts

    if valid:
        save_wallet(chain, address, src)

    else:
        if chain == "Solana":
            save_rejected(address) # cache non-smart
        logging.info(f"❌ {chain} {address} is invalid / no contract")


# ============= MAIN MONITOR FUNCTION (PER SITE) =============
def monitor_website(url: str) -> None:
    """
    Main monitoring loop for a single site.
    • Initializes local snapshots (old_htmls)
    • On each iteration, iterates over the base URL + all sub-URLs
    • HEAD request ➜ get HTML (x2 to confirm changes)
    • Search and validate contract addresses ➜ notify_if_valid_contract()
    • Detect content changes ➜ Telegram notification (if enabled)
    • On each iteration: generate a list of URLs (root + sub-URLs)
    • For EACH URL, create a separate future in the local ThreadPoolExecutor
    • All key actions (HEAD, diff, contract search) happen inside check_single_url()
    • In notifications, we always specify the URL where we found changes"""

    logging.info(f"▶️ monitor started for {url}")

    local_pool = ThreadPoolExecutor(max_workers=8)

    # local cache, in which we will put "already declared" specifically
    # for this root site (so that the Telegram message is sent exactly once)
    announced_local: set[str] = set()

    # ============= HTML Snapshot Cache =============
    old_htmls: dict[str, str] = {}
    html_lock = threading.Lock()
    for u in [url] + derive_sub_urls(url):
        old_htmls[u] = load_from_file(get_snapshot_filename(u)) or ""
        changes_count_dict.setdefault(u, (0, 0, 0))

    # ============= worker for each page =============
    def check_single_url(u: str) -> None:
        # 1) HEAD
        try:
            head = requests.head(u, timeout=5, allow_redirects=True)
            logging.info(f"[{url}] HEAD {u} → {head.status_code}")

            if head.status_code == 200 and u != url:
                with announced_subdomains_lock:
                    if u not in announced_subdomains:
                        announced_subdomains.add(u)
                        announced_local.add(u)
                        send_telegram_message(f"🔍 Subdomain {u} found\n" f"Starting page parsing.")

            if head.status_code != 200:
                return
            
        except Exception as e:
            logging.info(f"[{url}] HEAD failed for {u}: {e}")
            return

        # 2) Page Loading (First)
        raw1 = get_dynamic_page_html(u)

        # -------- 2.a Find addresses on the page --------
        wallets_raw = list(find_wallets(raw1))

        # -------- 2.b We leave only really NEW ones --------
        new_wallets = []                # [(chain, addr), …]
        for chain, addr in wallets_raw:
            if not is_already_known(chain, addr):
                new_wallets.append((chain, addr)) # addr unknown yet

        unique_new_addrs = {addr for _, addr in new_wallets} # a lot of new ones

        # --- AUTOPAUSE: too many addresses + sending a document ---------------
        if len(unique_new_addrs) >= CONTRACT_PAUSE_THRESHOLD:
            
            # 1) Check each address via RPC
            valid, invalid = split_valid_invalid(new_wallets)

            # 1.1) All invalid Solana addresses are added to the pseudo-token cache
            for chain, addr in invalid:
                if chain == "Solana": 
                    save_rejected(addr)

            # 2) Further actions depend on the number of real contracts
            if len(valid) == 0:
                logging.info(f"[{u}] After checking, not a single real contract remained")
                return # we continue monitoring without pause
            
            if len(valid) < CONTRACT_PAUSE_THRESHOLD:
                # (<5) – send one message at a time
                for chain, addr in valid:
                    save_wallet(chain, addr, u)  # we fix it without any unnecessary re-checking

            # 3) send txt file
            else:
                # (≥5) – form txt + auto-pause
                send_contracts_document(url, u, valid)
                send_telegram_message(
                    f"⏸️ There are {len(valid)} confirmed contracts found on page {u}.\n" f"The document with addresses has been sent.")
                _auto_pause_site(url, triggered_page=u)
            
            # After processing a large DIFF block, no logic is needed
            return  # no diff, no validations
        

        # Validation of addresses (their < threshold)
        for chain, addr in new_wallets:
            notify_if_valid_contract(chain, addr, u)

        # ------- DIFF LOGIC ---------------------------------
        clean1 = clean_html(raw1)
        if not clean1:
            return

        with html_lock:
            prev = old_htmls.get(u, "")

        # If there is no photo → save and exit
        if prev == "":
            save_to_file(get_snapshot_filename(u), clean1)
            with html_lock:
                old_htmls[u] = clean1
            logging.info(f"[{url}] initial snapshot stored for {u}")
            return

        # diff #1
        rem1, add1, ch1 = compare_html(prev, clean1)
        diff1 = len(rem1) + len(add1) + len(ch1)
        if diff1 == 0:
            return  # no changes

        # 3) Confirming download
        time.sleep(2)
        raw2 = get_dynamic_page_html(u)

        # Revalidation (new addresses may have appeared)
        for chain, addr in find_wallets(raw2):
            if not is_already_known(chain, addr):
                notify_if_valid_contract(chain, addr, u)

        clean2 = clean_html(raw2)
        rem2, add2, ch2 = compare_html(prev, clean2)
        diff2 = len(rem2) + len(add2) + len(ch2)

        # 4) Confirmation of changes
        if diff2 == diff1 and diff2 > 0:
            save_to_file(get_snapshot_filename(u), clean2)
            with html_lock:
                old_htmls[u] = clean2
            changes_count_dict[u] = (len(rem2), len(add2), len(ch2))

            if site_notifications_dict[url]:
                msg = (
                    f"‼️ Changes to {u} ‼️\n"
                    f"• Removed: {len(rem2)}\n"
                    f"• Added: {len(add2)}\n"
                    f"• Changed attributes: {len(ch2)}\n"
                    f"➡️ {u}"
                )
                send_telegram_message(msg)
                save_state()
        else:
            logging.info(f"[{u}] diff mismatch (first={diff1}, second={diff2})")

    # ============= гmain domain loop =============
    while not stop_flags[url].is_set():
        all_urls = [url] + derive_sub_urls(url)
        futures = [local_pool.submit(check_single_url, u) for u in all_urls]

        for f in futures: # waiting for subtasks
            try:
                f.result()
            except Exception as e:
                logging.error(f"[{url}] worker raised: {e}")

        time.sleep(random.randint(5, 15))

    local_pool.shutdown(wait=True)
    logging.info(f"⏹️ monitor stopped for {url}")


# === MAIN ===
def main():
    load_state()
    listener_thread = threading.Thread(target=telegram_listener, daemon=True)
    listener_thread.start()
    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
