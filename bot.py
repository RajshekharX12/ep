#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# +888 Code Fetcher — BOT TOKEN ONLY build (Aiogram v3 + Playwright)
# - Requires only BOT_TOKEN in .env
# - Session auto-saved to ./fragment_state.json after headful login
# - DM & Inline: send "0708 3255", "88807083255", or "+888 0708 3255" → returns OTP
# - Buttons: Login/Refresh Session, Disconnect, Help; and per-number: Refresh/Open/Number/Send/Close
# - All dynamic/system messages are HTML-escaped to avoid Telegram "Unsupported start tag" errors

import os, re, time, asyncio, contextlib, traceback, html
from dataclasses import dataclass
from typing import Optional, Dict, Tuple

# ================ Helpers & Env =================
def safe_html(text: Optional[str]) -> str:
    """Escape any string so Telegram HTML parse_mode never breaks."""
    return html.escape(text or "")

def _clean_token(val: Optional[str]) -> str:
    if not val:
        return ""
    return val.replace("\ufeff", "").replace("\u200b", "").strip().strip('"').strip("'")

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BOT_TOKEN = _clean_token(os.getenv("BOT_TOKEN"))
FRAGMENT_STATE = "fragment_state.json"
FRAGMENT_ORIGIN = "https://fragment.com"

if not BOT_TOKEN or not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", BOT_TOKEN):
    raise SystemExit(
        "BOT_TOKEN missing/invalid.\n"
        "Put exactly this in ./.env (no quotes, no spaces):\n"
        "BOT_TOKEN=123456789:AA... (your token)\n"
    )

# ================ Aiogram v3 ====================
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineQuery,
    InlineQueryResultArticle, InputTextMessageContent
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================ Playwright ====================
from playwright.async_api import async_playwright, Browser, BrowserContext, TimeoutError as PWTimeout

# ================ Number utils ==================
def normalize_to_888_digits(s: str) -> Optional[str]:
    """
    Accepts '0708 3255', '+888 0708 3255', '88807083255', etc.
    Returns digits like '88807083255' or None if invalid.
    """
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if not digits.startswith("888"):
        if 3 <= len(digits) <= 15:
            digits = "888" + digits
    if not digits.startswith("888") or len(digits) < 7:
        return None
    return digits

def fragment_links(digits: str) -> Tuple[str, str]:
    base = f"{FRAGMENT_ORIGIN}/number/{digits}"
    return base, f"{base}/code"

# ================ Fragment Client ===============
@dataclass
class CodeResult:
    digits: str
    code: Optional[str]
    url: str
    hint: str

class FragmentClient:
    """
    Headless fetch for OTP using saved storage_state.
    Headful login flow to generate/update storage_state when needed.
    Only needs BOT_TOKEN; session is saved to FRAGMENT_STATE automatically.
    """
    def __init__(self, state_file: str):
        self.state_file = state_file
        self._pw = None
        self._headless_browser: Optional[Browser] = None
        self._headless_ctx: Optional[BrowserContext] = None

        self._login_browser: Optional[Browser] = None
        self._login_ctx: Optional[BrowserContext] = None

        self._lock = asyncio.Lock()
        self._cache: Dict[str, Tuple[float, Optional[str]]] = {}  # digits -> (ts, code)

    async def start(self):
        if self._pw:
            return
        self._pw = await async_playwright().start()
        # headless context launches lazily on first fetch

    async def stop(self):
        with contextlib.suppress(Exception):
            if self._login_ctx: await self._login_ctx.close()
        with contextlib.suppress(Exception):
            if self._login_browser: await self._login_browser.close()
        with contextlib.suppress(Exception):
            if self._headless_ctx: await self._headless_ctx.close()
        with contextlib.suppress(Exception):
            if self._headless_browser: await self._headless_browser.close()
        with contextlib.suppress(Exception):
            if self._pw: await self._pw.stop()
        self._pw = None

    async def _ensure_headless(self):
        if self._headless_ctx:
            return
        self._headless_browser = await self._pw.chromium.launch(headless=True)
        if os.path.exists(self.state_file):
            self._headless_ctx = await self._headless_browser.new_context(storage_state=self.state_file)
        else:
            self._headless_ctx = await self._headless_browser.new_context()

    # ---------- Login flow (headful) ----------
    async def start_headful_login(self) -> Tuple[bool, str]:
        await self.start()
        if self._login_browser:
            return True, "Headful window already open. Log in, then tap '✅ Save Session'."
        try:
            self._login_browser = await self._pw.chromium.launch(headless=False)
            self._login_ctx = await self._login_browser.new_context()
            page = await self._login_ctx.new_page()
            await page.goto(FRAGMENT_ORIGIN, wait_until="domcontentloaded")
            return True, "Headful window opened. Log in to Fragment, then come back and tap '✅ Save Session'."
        except Exception as e:
            with contextlib.suppress(Exception):
                if self._login_ctx: await self._login_ctx.close()
            with contextlib.suppress(Exception):
                if self._login_browser: await self._login_browser.close()
            self._login_ctx = None
            self._login_browser = None
            return False, f"Cannot open a visible browser here ({e}). Run once on a desktop to save session."

    async def save_headful_session(self) -> Tuple[bool, str]:
        if not self._login_ctx:
            return False, "No headful session in progress. Tap '🔑 Login/Refresh Session' first."
        try:
            await self._login_ctx.storage_state(path=self.state_file)
            with contextlib.suppress(Exception):
                await self._login_ctx.close()
            with contextlib.suppress(Exception):
                await self._login_browser.close()
            self._login_ctx = None
            self._login_browser = None

            # reset headless to reload new session on next fetch
            with contextlib.suppress(Exception):
                if self._headless_ctx: await self._headless_ctx.close()
            with contextlib.suppress(Exception):
                if self._headless_browser: await self._headless_browser.close()
            self._headless_ctx = None
            self._headless_browser = None
            self._cache.clear()
            return True, f"Session saved to {self.state_file}. You can fetch codes now."
        except Exception as e:
            return False, f"Failed to save session: {e}"

    async def disconnect(self) -> Tuple[bool, str]:
        with contextlib.suppress(Exception):
            if self._login_ctx: await self._login_ctx.close()
        with contextlib.suppress(Exception):
            if self._login_browser: await self._login_browser.close()
        with contextlib.suppress(Exception):
            if self._headless_ctx: await self._headless_ctx.close()
        with contextlib.suppress(Exception):
            if self._headless_browser: await self._headless_browser.close()
        self._login_ctx = None
        self._login_browser = None
        self._headless_ctx = None
        self._headless_browser = None
        self._cache.clear()
        with contextlib.suppress(Exception):
            if os.path.exists(self.state_file):
                os.remove(self.state_file)
        return True, "Disconnected. Session file removed."

    # ---------- OTP fetch ----------
    async def get_code(self, digits: str, ttl: int = 15, force_refresh: bool = False) -> CodeResult:
        await self.start()
        url = fragment_links(digits)[1]  # /code page

        # cache
        now = time.time()
        if not force_refresh:
            hit = self._cache.get(digits)
            if hit and (now - hit[0] <= ttl) and hit[1]:
                return CodeResult(digits, hit[1], url, "cached")

        # ensure headless
        try:
            await self._ensure_headless()
        except Exception as e:
            return CodeResult(digits, None, url, f"browser_not_ready: {e}")

        async with self._lock:
            page = await self._headless_ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except PWTimeout:
                with contextlib.suppress(Exception):
                    await page.close()
                return CodeResult(digits, None, url, "timeout_opening_code_page")

            code: Optional[str] = None
            try:
                await page.wait_for_timeout(800)  # let JS populate
                await page.wait_for_function(
                    """() => {
                        const rx = /\\b\\d{5,6}\\b/;
                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                        while (walker.nextNode()) {
                            if (rx.test(walker.currentNode.textContent)) return true;
                        }
                        return false;
                    }""",
                    timeout=9000
                )
                full_text = await page.evaluate("document.body.innerText")
                m = re.search(r"\b\d{5,6}\b", full_text)
                if m:
                    code = m.group(0)
            except PWTimeout:
                code = None
            except Exception:
                code = None
            finally:
                with contextlib.suppress(Exception):
                    await page.close()

            self._cache[digits] = (time.time(), code)
            return CodeResult(digits, code, url, "ok" if code else "no_code_visible")

# ================ Keyboards =====================
def main_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 Login/Refresh Session", callback_data="login:start")
    kb.button(text="🔌 Disconnect", callback_data="session:disconnect")
    kb.button(text="ℹ️ Help", callback_data="ui:help")
    kb.adjust(1, 1, 1)
    return kb

def code_card_kb(digits: str) -> InlineKeyboardBuilder:
    base, code_url = fragment_links(digits)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Refresh", callback_data=f"code:refresh:{digits}")
    kb.button(text="🔗 Open Code Page", url=code_url)
    kb.button(text="☎️ Number Page", url=base)
    kb.button(text="📋 Send Digits", callback_data=f"code:digits:{digits}")
    kb.button(text="❌ Close", callback_data="ui:close")
    kb.adjust(2, 2, 1)
    return kb

# ================ Router & Handlers =============
router = Router()
frag = FragmentClient(FRAGMENT_STATE)

@router.message(CommandStart())
async def on_start(m: Message, bot: Bot):
    me = await bot.me()
    await m.answer(
        "👋 <b>+888 Code Fetcher</b>\n"
        "Send me any +888 number in any format:\n"
        "• <code>0708 3255</code>\n"
        "• <code>88807083255</code>\n"
        "• <code>+888 0708 3255</code>\n\n"
        "I’ll open the Fragment <i>code page</i> and return the current login code.\n"
        f"Inline works too: type <code>@{safe_html(me.username)}</code> <code>07083255</code>.\n\n"
        "First time? Press <b>Login/Refresh Session</b> to log in to Fragment.",
        reply_markup=main_menu_kb().as_markup()
    )

@router.message(Command("menu"))
async def on_menu(m: Message):
    await m.answer("Main menu:", reply_markup=main_menu_kb().as_markup())

@router.message(Command("checkenv"))
async def check_env(m: Message):
    masked = BOT_TOKEN[:9] + "..." + BOT_TOKEN[-6:]
    await m.answer(
        "✅ Env loaded.\n"
        f"Session file: <code>{safe_html(FRAGMENT_STATE)}</code>\n"
        f"BOT_TOKEN: <code>{safe_html(masked)}</code>"
    )

@router.callback_query(F.data == "ui:help")
async def ui_help(c: CallbackQuery):
    await c.message.edit_text(
        "ℹ️ <b>How to use</b>\n"
        "1) Tap <b>Login/Refresh Session</b> → A browser opens. Log in to Fragment and connect TON.\n"
        "2) Come back and press <b>Save Session</b> (button will appear).\n"
        "3) Send any +888 number; I’ll fetch the current login code.\n\n"
        "Buttons on each result: <b>Refresh</b>, <b>Open Code Page</b>, <b>Number Page</b>, <b>Send Digits</b>, <b>Close</b>.",
        reply_markup=main_menu_kb().as_markup()
    )
    await c.answer()

@router.callback_query(F.data == "ui:close")
async def ui_close(c: CallbackQuery):
    with contextlib.suppress(Exception):
        await c.message.delete()
    await c.answer()

# ---- Login flow ----
@router.callback_query(F.data == "login:start")
async def login_start(c: CallbackQuery):
    ok, msg = await frag.start_headful_login()
    kb = InlineKeyboardBuilder()
    if ok:
        kb.button(text="✅ Save Session", callback_data="login:save")
    kb.button(text="❌ Cancel", callback_data="login:cancel")
    kb.adjust(2 if ok else 1)
    await c.message.answer(
        f"{'✅' if ok else '⚠️'} {safe_html(msg)}",
        reply_markup=kb.as_markup()
    )
    await c.answer()

@router.callback_query(F.data == "login:save")
async def login_save(c: CallbackQuery):
    ok, msg = await frag.save_headful_session()
    await c.message.answer(
        ("✅ " if ok else "⚠️ ") + safe_html(msg),
        reply_markup=main_menu_kb().as_markup()
    )
    await c.answer()

@router.callback_query(F.data == "login:cancel")
async def login_cancel(c: CallbackQuery):
    with contextlib.suppress(Exception):
        if frag._login_ctx: await frag._login_ctx.close()
    with contextlib.suppress(Exception):
        if frag._login_browser: await frag._login_browser.close()
    frag._login_ctx = None
    frag._login_browser = None
    await c.message.answer("Login flow cancelled.", reply_markup=main_menu_kb().as_markup())
    await c.answer()

# ---- Session disconnect ----
@router.callback_query(F.data == "session:disconnect")
async def session_disconnect(c: CallbackQuery):
    _, msg = await frag.disconnect()
    await c.message.answer(f"🔌 {safe_html(msg)}", reply_markup=main_menu_kb().as_markup())
    await c.answer()

# ---- DM: number text ----
@router.message(F.text)
async def on_text_number(m: Message):
    target = normalize_to_888_digits(m.text or "")
    if not target:
        return await m.answer("Send a +888 number like <code>0708 3255</code> or <code>88807083255</code>.")
    await m.answer(f"⏳ Getting code for <b>+{safe_html(target)}</b> …")
    res = await frag.get_code(target)
    if res.code:
        await m.answer(
            f"🔑 <b>Code for +{safe_html(res.digits)}</b>: <code>{safe_html(res.code)}</code>\n🔗 {safe_html(res.url)}",
            reply_markup=code_card_kb(res.digits).as_markup()
        )
    else:
        msg = "⚠️ No code visible yet" if "browser_not_ready" not in (res.hint or "") else f"⚠️ {safe_html(res.hint)}"
        await m.answer(
            f"{msg} for <b>+{safe_html(res.digits)}</b>.\n"
            f"Open the code page once (button below) after saving a session.\n\n"
            f"🔗 {safe_html(res.url)}",
            reply_markup=code_card_kb(res.digits).as_markup()
        )

# ---- Callbacks for code card ----
@router.callback_query(F.data.startswith("code:refresh:"))
async def cb_refresh(c: CallbackQuery):
    digits = c.data.split(":")[2]
    res = await frag.get_code(digits, force_refresh=True)
    if res.code:
        text = f"🔑 <b>Code for +{safe_html(res.digits)}</b>: <code>{safe_html(res.code)}</code>\n🔗 {safe_html(res.url)}"
    else:
        text = (
            f"⚠️ No code visible yet for <b>+{safe_html(res.digits)}</b>.\n"
            f"Try opening the code page once in your browser.\n\n"
            f"🔗 {safe_html(res.url)}"
        )
    with contextlib.suppress(Exception):
        await c.message.edit_text(text, reply_markup=code_card_kb(digits).as_markup())
    await c.answer("Updated.")

@router.callback_query(F.data.startswith("code:digits:"))
async def cb_digits(c: CallbackQuery):
    digits = c.data.split(":")[2]
    await c.message.answer(f"📋 Digits: <code>{safe_html(digits)}</code>")
    await c.answer()

# ---- Inline ----
@router.inline_query()
async def on_inline(iq: InlineQuery):
    q = (iq.query or "").strip()
    digits = normalize_to_888_digits(q)

    if not digits:
        return await iq.answer(
            results=[
                InlineQueryResultArticle(
                    id="help",
                    title="Type 07083255 or +888 0708 3255",
                    input_message_content=InputTextMessageContent(
                        "Send me a +888 number; I’ll fetch the current login code."
                    ),
                    description="Smart parser; auto-adds 888 if missing."
                )
            ],
            cache_time=2,
            is_personal=True
        )

    res = await frag.get_code(digits)
    title = f"+{digits} • Login code" if res.code else f"+{digits} • Open code page"
    text = (
        f"🔑 Code for +{safe_html(digits)}: <code>{safe_html(res.code)}</code>\n🔗 {safe_html(res.url)}"
        if res.code else
        f"🔗 Open code page for +{safe_html(digits)}:\n{safe_html(res.url)}"
    )
    kb = code_card_kb(digits).as_markup()
    await iq.answer(
        results=[
            InlineQueryResultArticle(
                id=f"otp-{digits}",
                title=title,
                input_message_content=InputTextMessageContent(text, parse_mode="HTML"),
                description="Live OTP fetched" if res.code else "Tap to open the code page",
                url=res.url,
                reply_markup=kb
            )
        ],
        cache_time=1,
        is_personal=True
    )

# ================ Runner ========================
async def main():
    await frag.start()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    print("Bot online. Enable Inline in @BotFather to use inline queries.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
