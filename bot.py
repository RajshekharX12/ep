#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, time, asyncio, contextlib, json, traceback
from dataclasses import dataclass
from typing import Optional, Dict, Tuple

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineQuery,
    InlineQueryResultArticle, InputTextMessageContent
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from playwright.async_api import async_playwright, Browser, BrowserContext, TimeoutError as PWTimeout

# ===================== Config =====================
BOT_TOKEN       = os.getenv("BOT_TOKEN", "").strip()
FRAGMENT_STATE  = os.getenv("FRAGMENT_STATE", "fragment_state.json").strip()
FRAGMENT_ORIGIN = "https://fragment.com"
if not BOT_TOKEN:
    raise SystemExit("Set BOT_TOKEN in .env (BOT_TOKEN=...)")

# ===================== Helpers =====================
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

# ===================== Fragment Client =====================
@dataclass
class CodeResult:
    digits: str
    code: Optional[str]
    url: str
    hint: str

class FragmentClient:
    """
    - Headless context for fast code reads (uses saved storage state).
    - Headful login flow you can start from chat to capture a new session.
    """
    def __init__(self, state_file: str):
        self.state_file = state_file
        self._pw = None
        self._headless_browser: Optional[Browser] = None
        self._headless_ctx: Optional[BrowserContext] = None

        self._login_browser: Optional[Browser] = None
        self._login_ctx: Optional[BrowserContext] = None

        self._lock = asyncio.Lock()
        self._cache: Dict[str, Tuple[float, Optional[str]]] = {}  # digits -> (timestamp, code)

    async def start(self):
        if self._pw:
            return
        self._pw = await async_playwright().start()
        await self._start_headless()

    async def _start_headless(self):
        # (Re)create headless context from current storage state file (if any)
        with contextlib.suppress(Exception):
            if self._headless_ctx:
                await self._headless_ctx.close()
        with contextlib.suppress(Exception):
            if self._headless_browser:
                await self._headless_browser.close()

        self._headless_browser = await self._pw.chromium.launch(headless=True)
        if os.path.exists(self.state_file):
            self._headless_ctx = await self._headless_browser.new_context(storage_state=self.state_file)
        else:
            self._headless_ctx = await self._headless_browser.new_context()

    async def stop(self):
        with contextlib.suppress(Exception):
            if self._login_ctx:
                await self._login_ctx.close()
        with contextlib.suppress(Exception):
            if self._login_browser:
                await self._login_browser.close()
        with contextlib.suppress(Exception):
            if self._headless_ctx:
                await self._headless_ctx.close()
        with contextlib.suppress(Exception):
            if self._headless_browser:
                await self._headless_browser.close()
        with contextlib.suppress(Exception):
            if self._pw:
                await self._pw.stop()
        self._pw = None

    async def _ensure(self):
        if not self._pw:
            await self.start()

    # ---------- Login (headful) ----------
    async def start_headful_login(self) -> Tuple[bool, str]:
        """
        Opens a real Chromium window on this machine so you can log in.
        Returns (ok, message).
        """
        await self._ensure()
        # If already open, reuse
        if self._login_browser:
            return True, "Headful window already open. Log in, then tap '✅ Save Session'."

        try:
            self._login_browser = await self._pw.chromium.launch(headless=False)
            self._login_ctx = await self._login_browser.new_context()
            page = await self._login_ctx.new_page()
            await page.goto(FRAGMENT_ORIGIN, wait_until="domcontentloaded")
            return True, "Headful window opened. Log in on Fragment, then come back and tap '✅ Save Session'."
        except Exception as e:
            # Likely headless server without UI
            msg = f"Could not open a headful browser here ({e}). Run this bot on a desktop/laptop once to save the session."
            # cleanup if half-open
            with contextlib.suppress(Exception):
                if self._login_ctx: await self._login_ctx.close()
            with contextlib.suppress(Exception):
                if self._login_browser: await self._login_browser.close()
            self._login_ctx = None
            self._login_browser = None
            return False, msg

    async def save_headful_session(self) -> Tuple[bool, str]:
        """
        Saves current headful storage to file and reloads headless context with it.
        """
        if not self._login_ctx:
            return False, "No headful session in progress. Tap '🔑 Login/Refresh Session' first."
        try:
            await self._login_ctx.storage_state(path=self.state_file)
            # close login window
            with contextlib.suppress(Exception):
                await self._login_ctx.close()
            with contextlib.suppress(Exception):
                await self._login_browser.close()
            self._login_ctx = None
            self._login_browser = None
            # reload headless with new state
            await self._start_headless()
            # drop cache
            self._cache.clear()
            return True, f"Session saved ({self.state_file}). You can fetch codes now."
        except Exception as e:
            return False, f"Failed to save session: {e}"

    async def disconnect(self) -> Tuple[bool, str]:
        """
        Wipes saved state and cache. (This just disconnects the bot from your Fragment session.)
        """
        with contextlib.suppress(Exception):
            if self._login_ctx: await self._login_ctx.close()
        with contextlib.suppress(Exception):
            if self._login_browser: await self._login_browser.close()
        self._login_ctx = None
        self._login_browser = None
        self._cache.clear()
        with contextlib.suppress(Exception):
            if os.path.exists(self.state_file):
                os.remove(self.state_file)
        await self._start_headless()
        return True, "Disconnected. Session file removed."

    # ---------- OTP fetch ----------
    async def get_code(self, digits: str, ttl: int = 15, force_refresh: bool = False) -> CodeResult:
        """
        Load /code page for a number and extract a 5/6-digit OTP if visible.
        """
        await self._ensure()
        url = fragment_links(digits)[1]  # code page

        now = time.time()
        if not force_refresh:
            hit = self._cache.get(digits)
            if hit and (now - hit[0] <= ttl) and hit[1]:
                return CodeResult(digits, hit[1], url, "cached")

        async with self._lock:
            page = await self._headless_ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except PWTimeout:
                await page.close()
                return CodeResult(digits, None, url, "timeout opening code page")

            code: Optional[str] = None
            try:
                # let page JS populate value
                await page.wait_for_timeout(800)
                await page.wait_for_function(
                    """() => {
                        const rx = /\\b\\d{5,6}\\b/;
                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                        while (walker.nextNode()) {
                            if (rx.test(walker.currentNode.textContent)) return true;
                        }
                        return false;
                    }""",
                    timeout=8000
                )
                full_text = await page.evaluate("document.body.innerText")
                m = re.search(r"\\b\\d{5,6}\\b", full_text)
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

frag = FragmentClient(FRAGMENT_STATE)
router = Router()

# ===================== UI Builders =====================
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

def login_flow_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Save Session", callback_data="login:save")
    kb.button(text="❌ Cancel", callback_data="login:cancel")
    kb.adjust(2)
    return kb

# ===================== Handlers =====================
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
        f"Inline works too: type <code>@{me.username} 07083255</code>.\n\n"
        "First time? Press <b>Login/Refresh Session</b> to log in to Fragment.",
        reply_markup=main_menu_kb().as_markup()
    )

@router.message(Command("menu"))
async def on_menu(m: Message):
    await m.answer("Main menu:", reply_markup=main_menu_kb().as_markup())

@router.callback_query(F.data == "ui:help")
async def ui_help(c: CallbackQuery):
    await c.message.edit_text(
        "ℹ️ <b>How to use</b>\n"
        "1) Tap <b>Login/Refresh Session</b> → A browser opens. Log in to Fragment and connect TON.\n"
        "2) Come back and press <b>Save Session</b>.\n"
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
    await c.message.answer(
        f"{'✅' if ok else '⚠️'} {msg}\n\n"
        "When you finish logging in, tap <b>Save Session</b>.",
        reply_markup=login_flow_kb().as_markup()
    )
    await c.answer()

@router.callback_query(F.data == "login:save")
async def login_save(c: CallbackQuery):
    ok, msg = await frag.save_headful_session()
    await c.message.answer(("✅ " if ok else "⚠️ ") + msg, reply_markup=main_menu_kb().as_markup())
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
    await c.message.answer(f"🔌 {msg}", reply_markup=main_menu_kb().as_markup())
    await c.answer()

# ---- DM: number text ----
@router.message(F.text)
async def on_text_number(m: Message):
    target = normalize_to_888_digits(m.text)
    if not target:
        return await m.answer("Send a +888 number like <code>0708 3255</code> or <code>88807083255</code>.")
    await m.answer(f"⏳ Getting code for <b>+{target}</b> …")
    res = await frag.get_code(target)
    if res.code:
        await m.answer(
            f"🔑 <b>Code for +{res.digits}</b>: <code>{res.code}</code>\n🔗 {res.url}",
            reply_markup=code_card_kb(res.digits).as_markup()
        )
    else:
        await m.answer(
            f"⚠️ No code visible yet for <b>+{res.digits}</b>.\n"
            f"Open the code page once (below); if the session is valid it usually appears quickly.\n\n"
            f"🔗 {res.url}",
            reply_markup=code_card_kb(res.digits).as_markup()
        )

# ---- Callbacks for code card ----
@router.callback_query(F.data.startswith("code:refresh:"))
async def cb_refresh(c: CallbackQuery):
    digits = c.data.split(":")[2]
    res = await frag.get_code(digits, force_refresh=True)
    if res.code:
        text = f"🔑 <b>Code for +{res.digits}</b>: <code>{res.code}</code>\n🔗 {res.url}"
    else:
        text = (
            f"⚠️ No code visible yet for <b>+{res.digits}</b>.\n"
            f"Try opening the code page once in your browser.\n\n"
            f"🔗 {res.url}"
        )
    with contextlib.suppress(Exception):
        await c.message.edit_text(text, reply_markup=code_card_kb(digits).as_markup())
    await c.answer("Updated.")

@router.callback_query(F.data.startswith("code:digits:"))
async def cb_digits(c: CallbackQuery):
    digits = c.data.split(":")[2]
    await c.message.answer(f"📋 Digits: <code>{digits}</code>")
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
        f"🔑 Code for +{digits}: <code>{res.code}</code>\n🔗 {res.url}"
        if res.code else
        f"🔗 Open code page for +{digits}:\n{res.url}"
    )
    # Provide inline keyboard (same as DM, but Telegram will send it with the result)
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

# ===================== Runner =====================
async def main():
    await frag.start()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    print("Bot online. Enable Inline in @BotFather for inline use.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
