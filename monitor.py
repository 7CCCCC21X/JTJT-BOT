"""Etherscan V2 polling and Telegram message formatting."""

import asyncio
import html
import logging
import os
import time
from decimal import Decimal

import httpx

from chains import CHAINS
from storage import Watch

log = logging.getLogger(__name__)

API_URL = "https://api.etherscan.io/v2/api"
API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")

# free tier is 5 req/s — space calls out a little
REQUEST_GAP = float(os.environ.get("REQUEST_GAP", "0.25"))
MAX_ALERTS_PER_POLL = int(os.environ.get("MAX_ALERTS_PER_POLL", "8"))


class EtherscanError(Exception):
    pass


async def _query(client: httpx.AsyncClient, chain: str, params: dict) -> list[dict]:
    q = {
        "chainid": CHAINS[chain]["chain_id"],
        "apikey": API_KEY,
        **params,
    }
    resp = await client.get(API_URL, params=q, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "1":
        return data.get("result") or []
    message = str(data.get("message", ""))
    result = data.get("result")
    if "No transactions found" in message or result == []:
        return []
    raise EtherscanError(f"{message}: {result}")


async def fetch_new_txs(client: httpx.AsyncClient, watch: Watch) -> list[dict]:
    """Fetch transactions for a watch since its last seen block.

    Returns a list of dicts, each tagged with "_type": "native" | "token".
    """
    start = max(watch.last_block, 0)
    common = {
        "module": "account",
        "startblock": start,
        "endblock": 999999999,
        "page": 1,
        "offset": 50,
        "sort": "asc",
    }
    txs: list[dict] = []

    if watch.kind == "token":
        rows = await _query(client, watch.chain, {
            **common, "action": "tokentx", "contractaddress": watch.address,
        })
        for r in rows:
            r["_type"] = "token"
        txs.extend(rows)
    else:
        rows = await _query(client, watch.chain, {
            **common, "action": "txlist", "address": watch.address,
        })
        for r in rows:
            r["_type"] = "native"
        txs.extend(rows)

        await asyncio.sleep(REQUEST_GAP)
        rows = await _query(client, watch.chain, {
            **common, "action": "tokentx", "address": watch.address,
        })
        for r in rows:
            r["_type"] = "token"
        txs.extend(rows)

    fresh = []
    for tx in txs:
        key = f'{tx.get("hash", "")}:{tx.get("_type")}:{tx.get("logIndex", "")}:{tx.get("from", "")}:{tx.get("to", "")}:{tx.get("value", "")}'
        if key in watch.seen:
            continue
        watch.remember(key)
        fresh.append(tx)
        block = int(tx.get("blockNumber", 0) or 0)
        if block > watch.last_block:
            watch.last_block = block

    fresh.sort(key=lambda t: (int(t.get("blockNumber", 0) or 0),
                              int(t.get("transactionIndex", 0) or 0)))
    return fresh


async def fetch_recent(chain: str, address: str, limit: int = 10) -> list[dict]:
    """Fetch the latest transactions (native + token) for an address, newest first."""
    common = {
        "module": "account",
        "startblock": 0,
        "endblock": 999999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
    }
    async with httpx.AsyncClient() as client:
        native = await _query(client, chain, {**common, "action": "txlist", "address": address})
        for r in native:
            r["_type"] = "native"
        await asyncio.sleep(REQUEST_GAP)
        token = await _query(client, chain, {**common, "action": "tokentx", "address": address})
        for r in token:
            r["_type"] = "token"
    merged = native + token
    merged.sort(key=lambda t: (int(t.get("blockNumber", 0) or 0),
                               int(t.get("timeStamp", 0) or 0)), reverse=True)
    return merged[:limit]


def _age(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}秒前"
    if seconds < 3600:
        return f"{seconds // 60}分钟前"
    if seconds < 86400:
        return f"{seconds // 3600}小时前"
    return f"{seconds // 86400}天前"


def format_recent(chain_key: str, address: str, txs: list[dict]) -> str:
    chain = CHAINS[chain_key]
    explorer = chain["explorer"]
    target = address.lower()
    header = (f"📜 <b>[{chain['name']}]</b> "
              f"<a href=\"{explorer}/address/{address}\">{_short(target)}</a> "
              f"近 {len(txs)} 条交易")
    if not txs:
        return header + "\n\n(没有查到交易)"
    now = time.time()
    lines = []
    for tx in txs:
        sender = (tx.get("from") or "").lower()
        receiver = (tx.get("to") or "").lower()
        if receiver == target and sender != target:
            arrow = "📥"
        elif sender == target and receiver != target:
            arrow = "📤"
        else:
            arrow = "🔁"
        if tx.get("_type") == "token":
            symbol = html.escape(tx.get("tokenSymbol") or "TOKEN")
            amount = f"{_amount(tx.get('value', '0'), int(tx.get('tokenDecimal') or 18))} {symbol}"
        else:
            amount = f"{_amount(tx.get('value', '0'), 18)} {chain['native']}"
            if tx.get("isError") == "1":
                arrow += "⚠️"
        ts = int(tx.get("timeStamp", 0) or 0)
        age = _age(now - ts) if ts else "?"
        lines.append(
            f"{arrow} <b>{amount}</b> · {_short(sender)} → {_short(receiver)}"
            f" · {age} · <a href=\"{explorer}/tx/{tx.get('hash', '')}\">查看</a>")
    return header + "\n\n" + "\n".join(lines)


def _short(addr: str) -> str:
    if not addr:
        return "—"
    return addr[:8] + "…" + addr[-6:] if len(addr) > 16 else addr


def _amount(raw: str, decimals: int) -> str:
    try:
        value = Decimal(raw or "0") / (Decimal(10) ** decimals)
    except Exception:
        return raw or "0"
    if value == 0:
        return "0"
    text = f"{value:,.6f}".rstrip("0").rstrip(".")
    return text or "0"


def format_tx(watch: Watch, tx: dict) -> str:
    chain = CHAINS[watch.chain]
    explorer = chain["explorer"]
    tx_hash = tx.get("hash", "")
    sender = (tx.get("from") or "").lower()
    receiver = (tx.get("to") or "").lower()
    target = watch.address

    if watch.kind == "token":
        direction = "🔄 代币转账"
    elif sender == target and receiver == target:
        direction = "🔁 自转"
    elif sender == target:
        direction = "📤 转出"
    elif receiver == target:
        direction = "📥 转入"
    else:
        direction = "🔔 相关交易"

    if tx.get("_type") == "token":
        symbol = html.escape(tx.get("tokenSymbol") or "TOKEN")
        decimals = int(tx.get("tokenDecimal") or 18)
        amount = f"{_amount(tx.get('value', '0'), decimals)} {symbol}"
    else:
        amount = f"{_amount(tx.get('value', '0'), 18)} {chain['native']}"
        if tx.get("isError") == "1":
            direction += " ⚠️失败"

    label = html.escape(watch.label or _short(target))
    lines = [
        f"{direction} <b>[{chain['name']}] {label}</b>",
        f"金额: <b>{amount}</b>",
        f"From: <a href=\"{explorer}/address/{sender}\">{_short(sender)}</a>",
        f"To: <a href=\"{explorer}/address/{receiver}\">{_short(receiver)}</a>",
        f"区块: {tx.get('blockNumber', '?')}",
        f"<a href=\"{explorer}/tx/{tx_hash}\">🔗 查看交易</a>",
    ]
    return "\n".join(lines)
