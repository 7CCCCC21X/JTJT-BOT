"""JTJT 链上监控 Telegram Bot.

监控指定地址的交易 / 代币合约的所有转账,新动作实时推送到 Telegram。
数据源: Etherscan V2 多链 API (ETH / BSC / Base / Arbitrum / Polygon 共用一个 key)。
"""

import asyncio
import logging
import os
import re

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
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

HELP = f"""🤖 <b>链上监控机器人</b>

<b>命令:</b>
/add &lt;地址&gt; [链] [备注] — 监控地址的转入/转出(原生币+代币)
/addtoken &lt;合约&gt; [链] [备注] — 监控代币合约的<b>所有</b>转账
/label &lt;地址&gt; [链] &lt;备注&gt; — 修改已监控地址的备注
/remove &lt;地址&gt; [链] — 取消监控
/list — 查看当前监控列表
/chains — 支持的链
/id — 显示当前 chat id

<b>示例:</b>
<code>/add 0xEe7b429ea01f76102f053213463d4e95d5d24ae8 bsc 部署者</code>
<code>/addtoken 0x53f39e5C53EE40bbc3Da97C3B47BD2968d110a8D eth ALIGN</code>

默认链: {DEFAULT_CHAIN},轮询间隔: {POLL_INTERVAL} 秒"""


def _authorized(chat_id: int) -> bool:
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update.effective_chat.id):
        return
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat id: <code>{update.effective_chat.id}</code>",
                                    parse_mode=ParseMode.HTML)


async def cmd_chains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update.effective_chat.id):
        return
    lines = [f"• <code>{key}</code> — {c['name']} (chainid {c['chain_id']})"
             for key, c in CHAINS.items()]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def _parse_add_args(args: list[str]) -> tuple[str, str, str] | str:
    """Return (address, chain, label) or an error message."""
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


async def _add_watch(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str):
    chat_id = update.effective_chat.id
    if not _authorized(chat_id):
        return
    parsed = _parse_add_args(list(context.args))
    if isinstance(parsed, str):
        await update.message.reply_text(parsed)
        return
    address, chain, label = parsed
    watch = Watch(chat_id, chain, address, kind, label)

    # baseline: start from the current chain tip so history doesn't flood the chat
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
        await update.message.reply_text("已在监控列表里了。")
        return
    kind_txt = "代币合约(全部转账)" if kind == "token" else "地址"
    explorer = CHAINS[chain]["explorer"]
    await update.message.reply_text(
        f"✅ 已开始监控{kind_txt}\n"
        f"<a href=\"{explorer}/address/{address}\">{address}</a>\n"
        f"链: {CHAINS[chain]['name']}"
        + (f"\n备注: {label}" if label else ""),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _add_watch(update, context, "address")


async def cmd_addtoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _add_watch(update, context, "token")


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


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _authorized(chat_id):
        return
    watches = store.for_chat(chat_id)
    if not watches:
        await update.message.reply_text("当前没有监控,/add 添加一个。")
        return
    lines = []
    for w in watches:
        c = CHAINS[w.chain]
        kind = "🪙代币" if w.kind == "token" else "👤地址"
        label = f" ({w.label})" if w.label else ""
        lines.append(
            f"{kind} [{c['name']}]{label}\n"
            f"<a href=\"{c['explorer']}/address/{w.address}\">{w.address}</a>")
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    if not store.watches:
        return
    dirty = False
    async with httpx.AsyncClient() as client:
        for watch in list(store.watches.values()):
            try:
                txs = await monitor.fetch_new_txs(client, watch)
            except Exception as e:
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


def main():
    if not BOT_TOKEN:
        raise SystemExit("缺少环境变量 TELEGRAM_BOT_TOKEN")
    if not monitor.API_KEY:
        raise SystemExit("缺少环境变量 ETHERSCAN_API_KEY")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("chains", cmd_chains))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler(["addtoken", "add_token"], cmd_addtoken))
    app.add_handler(CommandHandler(["label", "note"], cmd_label))
    app.add_handler(CommandHandler(["remove", "rm", "del"], cmd_remove))
    app.add_handler(CommandHandler(["list", "ls"], cmd_list))

    app.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL, first=5)

    log.info("bot starting, poll interval %ss, %d watches loaded",
             POLL_INTERVAL, len(store.watches))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
