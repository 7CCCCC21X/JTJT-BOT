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


# Etherscan 免费套餐不覆盖的链自动降级到 Blockscout(免费、无需 key、接口兼容)
BLOCKSCOUT_URLS = {
    "eth": "https://eth.blockscout.com/api",
    "base": "https://base.blockscout.com/api",
    "arb": "https://arbitrum.blockscout.com/api",
    "polygon": "https://polygon.blockscout.com/api",
}
_fallback_chains: set[str] = set()


def _plan_blocked(text) -> bool:
    t = str(text).lower()
    return ("not supported for this chain" in t
            or "upgrade your api plan" in t)


async def _get_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    resp = await client.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _parse_list(data: dict) -> list[dict]:
    if data.get("status") == "1":
        return data.get("result") or []
    message = str(data.get("message", ""))
    result = data.get("result")
    if "No transactions found" in message or result == []:
        return []
    if "Invalid API Key" in str(result) or "Invalid API Key" in message:
        raise EtherscanError(
            "Etherscan API Key 无效。请在 Railway 的 Variables 里把 "
            "ETHERSCAN_API_KEY 换成 etherscan.io/myapikey 申请的 key"
            "(V2 多链通用,BscScan 旧 key 不可用)")
    raise EtherscanError(f"{message}: {result}")


async def _query(client: httpx.AsyncClient, chain: str, params: dict) -> list[dict]:
    if chain not in _fallback_chains:
        data = await _get_json(client, API_URL, {
            "chainid": CHAINS[chain]["chain_id"],
            "apikey": API_KEY,
            **params,
        })
        try:
            return _parse_list(data)
        except EtherscanError as e:
            if _plan_blocked(e) and chain in BLOCKSCOUT_URLS:
                _fallback_chains.add(chain)
                log.info("chain %s not in Etherscan free plan, "
                         "switching to Blockscout", chain)
            else:
                raise
    if chain in BLOCKSCOUT_URLS:
        data = await _get_json(client, BLOCKSCOUT_URLS[chain], dict(params))
        return _parse_list(data)
    raise EtherscanError(
        f"{CHAINS[chain]['name']} 不在 Etherscan 免费套餐内,且暂无备用数据源")


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


async def _proxy(client: httpx.AsyncClient, chain: str, action: str,
                 extra: dict | None = None):
    """proxy 模块 (JSON-RPC 透传),返回 result 原文;免费套餐不覆盖时走 Blockscout。"""
    params = {"module": "proxy", "action": action, **(extra or {})}
    if chain not in _fallback_chains:
        data = await _get_json(client, API_URL, {
            "chainid": CHAINS[chain]["chain_id"],
            "apikey": API_KEY,
            **params,
        })
        result = data.get("result")
        if not (_plan_blocked(result) or _plan_blocked(data.get("message", ""))):
            return result
        if chain in BLOCKSCOUT_URLS:
            _fallback_chains.add(chain)
            log.info("chain %s not in Etherscan free plan, "
                     "switching to Blockscout", chain)
        else:
            return result
    data = await _get_json(client, BLOCKSCOUT_URLS[chain], params)
    return data.get("result")


async def check_chain(chain: str) -> tuple[int, str]:
    """连通性检测:返回 (最新区块, 数据源名)。失败抛异常。"""
    async with httpx.AsyncClient() as client:
        res = await _proxy(client, chain, "eth_blockNumber")
    block = int(str(res), 16)  # 出错时 res 是错误文本,这里会抛 ValueError
    source = "Blockscout" if chain in _fallback_chains else "Etherscan"
    return block, source


async def detect_address(address: str) -> dict[str, dict]:
    """识别地址在每条链上的类型: eoa(普通地址) / contract(合约) / token(代币合约)。

    有合约代码 → contract;合约且有代币转账记录 → token(带 symbol)。
    """
    result: dict[str, dict] = {}
    async with httpx.AsyncClient() as client:
        for chain in CHAINS:
            info = {"type": "eoa", "symbol": None}
            try:
                code = await _proxy(client, chain, "eth_getCode",
                                    {"address": address, "tag": "latest"})
                if isinstance(code, str) and code.startswith("0x") and len(code) > 4:
                    info["type"] = "contract"
                    await asyncio.sleep(REQUEST_GAP)
                    rows = await _query(client, chain, {
                        "module": "account", "action": "tokentx",
                        "contractaddress": address,
                        "page": 1, "offset": 1, "sort": "desc",
                        "startblock": 0, "endblock": 999999999,
                    })
                    if rows:
                        info["type"] = "token"
                        info["symbol"] = rows[0].get("tokenSymbol") or None
            except Exception as e:
                log.debug("detect failed on %s: %s", chain, e)
            result[chain] = info
            await asyncio.sleep(REQUEST_GAP)
    return result


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
