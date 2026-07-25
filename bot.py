"""JTJT 链上监控 Telegram Bot.

监控指定地址的交易 / 代币合约的所有转账,新动作实时推送到 Telegram。
数据源: Etherscan V2 多链 API (ETH / BSC / Base / Arbitrum / Polygon 共用一个 key)。
"""

import asyncio
import logging
import os
import re
import time

import httpx
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import monitor
from chains import CHAINS, DEFAULT_CHAIN, resolve_chain
from storage import Store, Watch

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
ALLOWED_CHAT_IDS = {
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",")
    if x.lstrip("-").isdigit()
}

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

store = Store()

STATS = {"started": time.time(), "last_poll": 0.0, "polls": 0, "alerts": 0, "errors": 0}

HELP = f"""🤖 <b>链上监控机器人</b>

直接发送一个 <code>0x...</code> 地址即可开始添加,或用命令:

/menu — 打开按钮菜单
/add &lt;地址&gt; [链] [备注] — 监控地址的转入/转出(原生币+代币)
/addtoken &lt;合约&gt; [链] [备注] — 监控代币合约的<b>所有</b>转账
/recent &lt;地址&gt; [链] — 查看地址近10条交易
/label &lt;地址&gt; [链] &lt;备注&gt; — 修改已监控地址的备注
/remove &lt;地址&gt; [链] — 取消监控
/list — 查看当前监控列表
/status — 查看运行状态
/test — 发送示例推送并检测 API 连通性
/chains — 支持的链
/id — 显示当前 chat id
/cancel — 取消当前添加流程

<b>示例:</b>
<code>/add 0xEe7b429ea01f76102f053213463d4e95d5d24ae8 bsc 部署者</code>

默认链: {DEFAULT_CHAIN},轮询间隔: {POLL_INTERVAL} 秒"""


def _authorized(chat_id: int) -> bool:
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


# ---------- 卡片键盘 ----------

CHAIN_ICONS = {"eth": "⟠", "bsc": "🟡", "base": "🔵", "arb": "🔷", "polygon": "🟣"}


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ 监控地址", callback_data="menu:add_addr"),
         InlineKeyboardButton("🪙 监控代币", callback_data="menu:add_token")],
        [InlineKeyboardButton("📜 查近10条交易", callback_data="menu:recent"),
         InlineKeyboardButton("📋 监控列表", callback_data="menu:list")],
        [InlineKeyboardButton("📊 运行状态", callback_data="menu:status"),
         InlineKeyboardButton("🧪 测试推送", callback_data="menu:test")],
        [InlineKeyboardButton("❓ 帮助", callback_data="menu:help")],
    ])


def kind_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 地址监控(转入/转出)", callback_data="kind:address")],
        [InlineKeyboardButton("🪙 代币监控(该代币全部转账)", callback_data="kind:token")],
        [InlineKeyboardButton("📜 查看近10条交易", callback_data="kind:recent")],
        [InlineKeyboardButton("❌ 取消", callback_data="cancel")],
    ])


def chain_multi_kb(selected: set[str]) -> InlineKeyboardMarkup:
    """多选链卡片:点击切换选中,✅ 表示已选,选完点「完成」。"""
    def btn(key: str) -> InlineKeyboardButton:
        mark = "✅ " if key in selected else ""
        return InlineKeyboardButton(
            f"{mark}{CHAIN_ICONS[key]} {CHAINS[key]['name']}",
            callback_data=f"chsel:{key}")
    return InlineKeyboardMarkup([
        [btn("eth"), btn("bsc")],
        [btn("base"), btn("arb")],
        [btn("polygon")],
        [InlineKeyboardButton("✔️ 完成", callback_data="chsel:done"),
         InlineKeyboardButton("❌ 取消", callback_data="cancel")],
    ])


def rchain_kb() -> InlineKeyboardMarkup:
    """单选链卡片(近10条交易查询用)。"""
    def btn(key: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            f"{CHAIN_ICONS[key]} {CHAINS[key]['name']}", callback_data=f"rchain:{key}")
    return InlineKeyboardMarkup([
        [btn("eth"), btn("bsc")],
        [btn("base"), btn("arb")],
        [btn("polygon"),
         InlineKeyboardButton("❌ 取消", callback_data="cancel")],
    ])


def label_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ 跳过备注", callback_data="label:skip"),
         InlineKeyboardButton("❌ 取消", callback_data="cancel")],
    ])


# ---------- 核心逻辑 ----------

async def create_watch(chat_id: int, address: str, chain: str, kind: str, label: str) -> str:
    """添加一条监控并返回确认消息 (HTML)。以链上最新一笔为基准,不推历史。"""
    watch = Watch(chat_id, chain, address, kind, label)
    try:
        async with httpx.AsyncClient() as client:
            rows = await monitor._query(client, chain, {
                "module": "account",
                "action": "tokentx" if kind == "token" else "txlist",
                **({"contractaddress": address} if kind == "token" else {"address": address}),
                "page": 1, "offset": 1, "sort": "desc",
            })
        if rows:
            watch.last_block = int(rows[0].get("blockNumber", 0) or 0)
            key = f'{rows[0].get("hash", "")}:{"token" if kind == "token" else "native"}:{rows[0].get("logIndex", "")}:{rows[0].get("from", "")}:{rows[0].get("to", "")}:{rows[0].get("value", "")}'
            watch.remember(key)
    except Exception as e:
        log.warning("baseline fetch failed: %s", e)

    if not store.add(watch):
        return "已在监控列表里了。"
    kind_txt = "代币合约(全部转账)" if kind == "token" else "地址"
    explorer = CHAINS[chain]["explorer"]
    return (f"✅ 已开始监控{kind_txt}\n"
            f"<a href=\"{explorer}/address/{address}\">{address}</a>\n"
            f"链: {CHAINS[chain]['name']}"
            + (f"\n备注: {label}" if label else ""))


def list_text(chat_id: int) -> str:
    watches = store.for_chat(chat_id)
    if not watches:
        return "当前没有监控。发送一个 0x 地址,或用 /add 添加。"
    lines = []
    for w in watches:
        c = CHAINS[w.chain]
        kind = "🪙代币" if w.kind == "token" else "👤地址"
        label = f" ({w.label})" if w.label else ""
        lines.append(
            f"{kind} [{c['name']}]{label}\n"
            f"<a href=\"{c['explorer']}/address/{w.address}\">{w.address}</a>")
    return "\n\n".join(lines)


def status_text(chat_id: int) -> str:
    up = int(time.time() - STATS["started"])
    days, rem = divmod(up, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    uptime = (f"{days}天 " if days else "") + f"{hours}小时 {minutes}分"
    if STATS["last_poll"]:
        ago = int(time.time() - STATS["last_poll"])
        last = f"{ago} 秒前"
    else:
        last = "尚未执行"
    mine = store.for_chat(chat_id)
    chains_used = sorted({CHAINS[w.chain]["name"] for w in mine})
    return (f"📊 <b>运行状态</b>\n\n"
            f"⏱ 运行时长: {uptime}\n"
            f"🔄 轮询间隔: {POLL_INTERVAL} 秒\n"
            f"🕐 上次轮询: {last}(累计 {STATS['polls']} 次)\n"
            f"📨 已推送提醒: {STATS['alerts']} 条\n"
            f"⚠️ 轮询错误: {STATS['errors']} 次\n\n"
            f"👀 本会话监控数: {len(mine)}"
            + (f"({'、'.join(chains_used)})" if chains_used else "")
            + f"\n🌐 全局监控数: {len(store.watches)}")


async def run_test(chat_id: int, bot) -> None:
    """发送一条示例推送,并检测各链 API 连通性。"""
    sample = Watch(chat_id, DEFAULT_CHAIN, "0x" + "ee" * 20, "address", "测试地址")
    sample_tx = {
        "_type": "token", "hash": "0x" + "ab" * 32,
        "from": sample.address, "to": "0x" + "46" * 20,
        "value": "1250000000000000000000",
        "tokenSymbol": "ALIGN", "tokenDecimal": "18",
        "blockNumber": "49070475",
    }
    await bot.send_message(chat_id, "🧪 示例推送如下:")
    await bot.send_message(chat_id, monitor.format_tx(sample, sample_tx),
                           parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    lines = ["🌐 <b>API 连通性检测</b>\n"]
    async with httpx.AsyncClient() as client:
        for key in ("eth", "bsc", "base"):
            c = CHAINS[key]
            data = {}
            try:
                r = await client.get(monitor.API_URL, params={
                    "chainid": c["chain_id"], "module": "proxy",
                    "action": "eth_blockNumber", "apikey": monitor.API_KEY,
                }, timeout=15)
                data = r.json()
                block = int(str(data.get("result", "")), 16)
                lines.append(f"✅ {c['name']}: 最新区块 {block:,}")
            except (ValueError, TypeError):
                lines.append(f"❌ {c['name']}: {data.get('result') or data.get('message', '响应异常')}")
            except Exception as e:
                lines.append(f"❌ {c['name']}: {e}")
            await asyncio.sleep(monitor.REQUEST_GAP)
    await bot.send_message(chat_id, "\n".join(lines), parse_mode=ParseMode.HTML)


# ---------- 命令 ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update.effective_chat.id):
        return
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML,
                                    reply_markup=menu_kb())


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update.effective_chat.id):
        return
    await update.message.reply_text("📱 <b>主菜单</b> — 请选择:",
                                    parse_mode=ParseMode.HTML, reply_markup=menu_kb())


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat id: <code>{update.effective_chat.id}</code>",
                                    parse_mode=ParseMode.HTML)


async def cmd_chains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update.effective_chat.id):
        return
    lines = [f"• <code>{key}</code> — {c['name']} (chainid {c['chain_id']})"
             for key, c in CHAINS.items()]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update.effective_chat.id):
        return
    await update.message.reply_text(status_text(update.effective_chat.id),
                                    parse_mode=ParseMode.HTML)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update.effective_chat.id):
        return
    await run_test(update.effective_chat.id, context.bot)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.chat_data.pop("pending", None):
        await update.message.reply_text("已取消当前添加流程。")
    else:
        await update.message.reply_text("当前没有进行中的操作。")


def _parse_add_args(args: list[str]) -> tuple[str, str, str] | str:
    if not args:
        return "用法: /add <地址> [链] [备注]"
    address = args[0]
    if not ADDR_RE.match(address):
        return "❌ 地址格式不对,应为 0x 开头的 40 位十六进制。"
    chain = DEFAULT_CHAIN
    label_parts = args[1:]
    if label_parts:
        maybe = resolve_chain(label_parts[0])
        if maybe:
            chain = maybe
            label_parts = label_parts[1:]
    return address, chain, " ".join(label_parts)[:40]


async def _add_by_command(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str):
    chat_id = update.effective_chat.id
    if not _authorized(chat_id):
        return
    parsed = _parse_add_args(list(context.args))
    if isinstance(parsed, str):
        await update.message.reply_text(parsed)
        return
    address, chain, label = parsed
    text = await create_watch(chat_id, address, chain, kind, label)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True,
                                    reply_markup=_recent_buttons([chain], address))


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _add_by_command(update, context, "address")


async def cmd_addtoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _add_by_command(update, context, "token")


async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _authorized(chat_id):
        return
    args = list(context.args)
    if not args:
        await update.message.reply_text(
            "用法: /recent <地址> [链]\n"
            "也可以直接发送地址,然后点「📜 查看近10条交易」。")
        return
    address = args[0]
    if not ADDR_RE.match(address):
        await update.message.reply_text("❌ 地址格式不对,应为 0x 开头的 40 位十六进制。")
        return
    chain = DEFAULT_CHAIN
    if len(args) > 1:
        chain = resolve_chain(args[1])
        if not chain:
            await update.message.reply_text("❌ 不认识这条链,/chains 查看支持列表。")
            return
    await update.message.reply_text(f"⏳ 正在查询 {CHAINS[chain]['name']} 上的交易…")
    await _send_recent(context.bot, chat_id, chain, address)


async def cmd_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _authorized(chat_id):
        return
    args = list(context.args)
    if len(args) < 2:
        await update.message.reply_text(
            "用法: /label <地址> [链] <备注>\n"
            "例: /label 0xEe7b...4ae8 bsc 部署者钱包")
        return
    address = args[0]
    if not ADDR_RE.match(address):
        await update.message.reply_text("❌ 地址格式不对,应为 0x 开头的 40 位十六进制。")
        return
    rest = args[1:]
    chain = resolve_chain(rest[0]) if resolve_chain(rest[0]) else None
    if chain and len(rest) > 1:
        rest = rest[1:]
    else:
        chain = None
    label = " ".join(rest)[:40]
    matched = [w for w in store.for_chat(chat_id)
               if w.address == address.lower() and (chain is None or w.chain == chain)]
    if not matched:
        await update.message.reply_text("没找到对应的监控,先用 /add 或 /addtoken 添加。")
        return
    for w in matched:
        w.label = label
    store.save()
    await update.message.reply_text(
        f"✏️ 已更新 {len(matched)} 条监控的备注为「{label}」")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _authorized(chat_id):
        return
    args = list(context.args)
    if not args:
        await update.message.reply_text("用法: /remove <地址> [链]")
        return
    address = args[0]
    chain = resolve_chain(args[1]) if len(args) > 1 else None
    if len(args) > 1 and not chain:
        await update.message.reply_text("❌ 不认识这条链,/chains 查看支持列表。")
        return
    removed = 0
    for c in ([chain] if chain else list(CHAINS)):
        removed += store.remove(chat_id, c, address)
    await update.message.reply_text(
        f"🗑 已移除 {removed} 条监控。" if removed else "没找到对应的监控。")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _authorized(chat_id):
        return
    await update.message.reply_text(list_text(chat_id), parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


# ---------- 卡片流程(回复消息添加) ----------

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理普通文本:进行中的添加/查询流程,或直接发来的 0x 地址。"""
    chat_id = update.effective_chat.id
    if not _authorized(chat_id) or not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    pending = context.chat_data.get("pending")

    if pending:
        step = pending.get("step")
        if step == "address":
            if not ADDR_RE.match(text):
                await update.message.reply_text(
                    "❌ 地址格式不对,请回复 0x 开头的 40 位地址,或 /cancel 取消。")
                return
            pending["address"] = text
            pending["step"] = "chain"
            if pending.get("mode") == "recent":
                await update.message.reply_text(
                    f"📜 查询 <code>{text}</code> 近10条交易\n请选择链:",
                    parse_mode=ParseMode.HTML, reply_markup=rchain_kb())
            else:
                pending["chains"] = []
                await update.message.reply_text(
                    f"地址: <code>{text}</code>\n"
                    "请选择所在链(<b>可多选</b>,选完点「✔️ 完成」):",
                    parse_mode=ParseMode.HTML, reply_markup=chain_multi_kb(set()))
        elif step == "label":
            pending["label"] = text[:40]
            await _finalize_pending(context, chat_id)
        return

    if ADDR_RE.match(text):
        context.chat_data["pending"] = {"address": text, "step": "kind"}
        await update.message.reply_text(
            f"检测到地址 <code>{text}</code>\n要做什么?",
            parse_mode=ParseMode.HTML, reply_markup=kind_kb())


async def _send_recent(bot, chat_id: int, chain: str, address: str):
    try:
        txs = await monitor.fetch_recent(chain, address, 10)
    except Exception as e:
        await bot.send_message(chat_id, f"❌ 查询失败: {e}")
        return
    await bot.send_message(chat_id, monitor.format_recent(chain, address, txs),
                           parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def _recent_buttons(chains: list[str], address: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📜 {CHAINS[c]['name']} 近10条交易",
                              callback_data=f"recent:{c}:{address}")]
        for c in chains
    ])


async def _finalize_pending(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    pending = context.chat_data.pop("pending", None)
    if not pending or "address" not in pending:
        return
    chains = pending.get("chains") or ([pending["chain"]] if pending.get("chain") else [])
    if not chains:
        return
    address = pending["address"]
    parts = []
    for c in chains:
        parts.append(await create_watch(chat_id, address, c,
                                        pending.get("kind", "address"),
                                        pending.get("label", "")))
    await context.bot.send_message(chat_id, "\n\n".join(parts),
                                   parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True,
                                   reply_markup=_recent_buttons(chains, address))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.message:
        return
    chat_id = q.message.chat.id
    if not _authorized(chat_id):
        await q.answer()
        return
    data = q.data or ""
    pending = context.chat_data.get("pending")

    if data == "cancel":
        await q.answer()
        context.chat_data.pop("pending", None)
        await q.edit_message_text("已取消。")
        return

    if data.startswith("menu:"):
        await q.answer()
        action = data.split(":", 1)[1]
        if action == "add_addr":
            context.chat_data["pending"] = {"kind": "address", "step": "address"}
            await q.edit_message_text(
                "➕ 监控地址(转入/转出)\n\n请直接回复要监控的地址(0x 开头),或 /cancel 取消。")
        elif action == "add_token":
            context.chat_data["pending"] = {"kind": "token", "step": "address"}
            await q.edit_message_text(
                "🪙 监控代币合约(全部转账)\n\n请直接回复代币合约地址(0x 开头),或 /cancel 取消。")
        elif action == "recent":
            context.chat_data["pending"] = {"mode": "recent", "step": "address"}
            await q.edit_message_text(
                "📜 查询近10条交易\n\n请直接回复要查询的地址(0x 开头),或 /cancel 取消。")
        elif action == "list":
            await q.edit_message_text(list_text(chat_id), parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True, reply_markup=menu_kb())
        elif action == "status":
            await q.edit_message_text(status_text(chat_id), parse_mode=ParseMode.HTML,
                                      reply_markup=menu_kb())
        elif action == "test":
            await q.edit_message_text("🧪 正在发送测试…")
            await run_test(chat_id, context.bot)
        elif action == "help":
            await q.edit_message_text(HELP, parse_mode=ParseMode.HTML,
                                      reply_markup=menu_kb())
        return

    if data.startswith("kind:"):
        await q.answer()
        if not pending or "address" not in pending:
            await q.edit_message_text("会话已过期,请重新发送地址。")
            return
        kind = data.split(":", 1)[1]
        if kind == "recent":
            pending["mode"] = "recent"
            pending["step"] = "chain"
            await q.edit_message_text(
                f"📜 查询 <code>{pending['address']}</code> 近10条交易\n请选择链:",
                parse_mode=ParseMode.HTML, reply_markup=rchain_kb())
        else:
            pending["kind"] = "token" if kind == "token" else "address"
            pending["step"] = "chain"
            pending["chains"] = []
            kind_txt = "代币监控" if pending["kind"] == "token" else "地址监控"
            await q.edit_message_text(
                f"{kind_txt}: <code>{pending['address']}</code>\n"
                "请选择所在链(<b>可多选</b>,选完点「✔️ 完成」):",
                parse_mode=ParseMode.HTML, reply_markup=chain_multi_kb(set()))
        return

    if data.startswith("chsel:"):
        if not pending or "address" not in pending:
            await q.answer()
            await q.edit_message_text("会话已过期,请重新发送地址。")
            return
        key = data.split(":", 1)[1]
        if key == "done":
            selected = pending.get("chains") or []
            if not selected:
                await q.answer("请至少选择一条链", show_alert=True)
                return
            await q.answer()
            pending["step"] = "label"
            names = "、".join(CHAINS[c]["name"] for c in selected)
            await q.edit_message_text(
                f"链: {names}\n"
                f"地址: <code>{pending['address']}</code>\n\n"
                "请直接回复备注文字(如「部署者钱包」),或点击跳过:",
                parse_mode=ParseMode.HTML, reply_markup=label_kb())
        elif key in CHAINS:
            await q.answer()
            selected = pending.setdefault("chains", [])
            if key in selected:
                selected.remove(key)
            else:
                selected.append(key)
            await q.edit_message_reply_markup(reply_markup=chain_multi_kb(set(selected)))
        return

    if data.startswith("rchain:"):
        await q.answer()
        if not pending or "address" not in pending:
            await q.edit_message_text("会话已过期,请重新发送地址。")
            return
        chain = data.split(":", 1)[1]
        if chain not in CHAINS:
            return
        address = pending["address"]
        context.chat_data.pop("pending", None)
        await q.edit_message_text(f"⏳ 正在查询 {CHAINS[chain]['name']} 上的交易…")
        await _send_recent(context.bot, chat_id, chain, address)
        return

    if data.startswith("recent:"):
        await q.answer()
        try:
            _, chain, address = data.split(":", 2)
        except ValueError:
            return
        if chain in CHAINS and ADDR_RE.match(address):
            await _send_recent(context.bot, chat_id, chain, address)
        return

    if data == "label:skip":
        await q.answer()
        if pending is not None:
            pending["label"] = ""
        await q.edit_message_text("⏳ 正在添加…")
        await _finalize_pending(context, chat_id)
        return

    await q.answer()


# ---------- 轮询 ----------

async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    STATS["polls"] += 1
    STATS["last_poll"] = time.time()
    if not store.watches:
        return
    dirty = False
    async with httpx.AsyncClient() as client:
        for watch in list(store.watches.values()):
            try:
                txs = await monitor.fetch_new_txs(client, watch)
            except Exception as e:
                STATS["errors"] += 1
                log.warning("poll failed for %s/%s: %s", watch.chain, watch.address, e)
                continue
            if txs:
                dirty = True
                shown = txs[:monitor.MAX_ALERTS_PER_POLL]
                for tx in shown:
                    try:
                        await context.bot.send_message(
                            watch.chat_id, monitor.format_tx(watch, tx),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True)
                        STATS["alerts"] += 1
                    except Exception as e:
                        log.warning("send failed to %s: %s", watch.chat_id, e)
                if len(txs) > len(shown):
                    extra = len(txs) - len(shown)
                    c = CHAINS[watch.chain]
                    await context.bot.send_message(
                        watch.chat_id,
                        f"…另有 {extra} 笔新交易,"
                        f"<a href=\"{c['explorer']}/address/{watch.address}\">在浏览器查看</a>",
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)
            elif watch.seen:
                dirty = True  # last_block/seen may have advanced
            await asyncio.sleep(monitor.REQUEST_GAP)
    if dirty:
        store.save()


async def post_init(app: Application):
    """注册 Telegram 原生命令菜单(输入框左侧的菜单按钮)。"""
    await app.bot.set_my_commands([
        BotCommand("menu", "打开按钮菜单"),
        BotCommand("add", "监控地址 <地址> [链] [备注]"),
        BotCommand("addtoken", "监控代币合约 <合约> [链] [备注]"),
        BotCommand("recent", "查近10条交易 <地址> [链]"),
        BotCommand("list", "查看监控列表"),
        BotCommand("status", "查看运行状态"),
        BotCommand("test", "测试推送与 API 检测"),
        BotCommand("label", "修改备注 <地址> [链] <备注>"),
        BotCommand("remove", "取消监控 <地址> [链]"),
        BotCommand("chains", "支持的链"),
        BotCommand("cancel", "取消当前添加流程"),
        BotCommand("help", "帮助"),
    ])
    # 输入框左侧的蓝色「菜单」按钮,点开即命令列表
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def main():
    if not BOT_TOKEN:
        raise SystemExit("缺少环境变量 TELEGRAM_BOT_TOKEN")
    if not monitor.API_KEY:
        raise SystemExit("缺少环境变量 ETHERSCAN_API_KEY")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("chains", cmd_chains))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler(["addtoken", "add_token"], cmd_addtoken))
    app.add_handler(CommandHandler(["recent", "last", "txs"], cmd_recent))
    app.add_handler(CommandHandler(["label", "note"], cmd_label))
    app.add_handler(CommandHandler(["remove", "rm", "del"], cmd_remove))
    app.add_handler(CommandHandler(["list", "ls"], cmd_list))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL, first=5)

    log.info("bot starting, poll interval %ss, %d watches loaded",
             POLL_INTERVAL, len(store.watches))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
