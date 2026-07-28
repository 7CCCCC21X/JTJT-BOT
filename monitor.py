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


# Etherscan 免费套餐不覆盖的链自动降级到备用数据源。
# 候选类型: "scan" = etherscan/blockscout 风格 GET 接口; "rpc" = 公共 JSON-RPC 节点
# (rpc 源用 eth_getLogs 监听代币 Transfer 事件,无法查询原生币普通交易)。
# 逐个探测,第一个可用的胜出并缓存。可用 FALLBACK_API_<链> 环境变量插队自定义:
#   FALLBACK_API_BSC=https://xxx/api          (scan 风格)
#   FALLBACK_API_BSC=rpc:https://xxx-rpc.com  (RPC 节点)
FALLBACKS: dict[str, list[tuple[str, str, bool]]] = {
    "eth": [("scan", "https://eth.blockscout.com/api", False),
            ("rpc", "https://ethereum-rpc.publicnode.com", False)],
    "bsc": [("rpc", "https://bsc.drpc.org", False),
            ("rpc", "https://1rpc.io/bnb", False),
            ("rpc", "https://bsc.meowrpc.com", False),
            ("rpc", "https://binance.llamarpc.com", False),
            ("rpc", "https://bsc-rpc.publicnode.com", False),
            ("rpc", "https://bsc-dataseed.bnbchain.org", False),
            ("rpc", "https://bsc-dataseed1.bnbchain.org", False)],
    "base": [("scan", "https://base.blockscout.com/api", False),
             ("rpc", "https://mainnet.base.org", False)],
    "arb": [("scan", "https://arbitrum.blockscout.com/api", False),
            ("rpc", "https://arb1.arbitrum.io/rpc", False)],
    "polygon": [("scan", "https://polygon.blockscout.com/api", False),
                ("rpc", "https://polygon-rpc.com", False)],
}
_fallback_chains: set[str] = set()
_fallback_urls: dict[str, tuple[str, str, bool]] = {}  # 探测成功后缓存
_source_cooldown: dict[tuple, float] = {}  # 被限流的源 -> 冷却到期时间
COOLDOWN_SECS = int(os.environ.get("SOURCE_COOLDOWN", "300"))


def _is_rate_limited(err) -> bool:
    t = str(err).lower()
    return ("429" in t or "too many" in t or "usage limit" in t
            or "rate limit" in t or "quota" in t)

TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068"
                  "fc378daa952ba7f163c4a11628f55a4df523b3ef")
RPC_LOG_SPAN = int(os.environ.get("RPC_LOG_SPAN", "1000"))  # getLogs 最大回看区块数
_rpc_spans: dict[str, int] = {}  # 各节点实测可用的回看窗口


def _classify_env_source(env: str) -> tuple[str, str, bool]:
    """解析 FALLBACK_API_<链> 的值,无前缀时自动判断类型。"""
    env = env.strip()
    if env.startswith("rpc:"):
        return ("rpc", env[4:].rstrip("/"), False)
    if env.startswith("scan:"):
        return ("scan", env[5:].rstrip("/"), False)
    url = env.rstrip("/")
    # etherscan/blockscout 风格的接口都以 /api 结尾;
    # 其余(nodereal、ankr、drpc 等节点地址)一律按 JSON-RPC 处理
    if url.endswith("/api"):
        return ("scan", url, False)
    return ("rpc", url, False)


def _candidates(chain: str) -> list[tuple[str, str, bool]]:
    lst: list[tuple[str, str, bool]] = []
    env = os.environ.get(f"FALLBACK_API_{chain.upper()}")
    if env:
        # 支持逗号分隔多个源,按顺序排在内置候选之前
        for part in env.split(","):
            if part.strip():
                lst.append(_classify_env_source(part))
    lst.extend(FALLBACKS.get(chain, []))
    return lst


async def _rpc_call(client: httpx.AsyncClient, url: str, method: str, params: list):
    resp = await client.post(url, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise EtherscanError(f"RPC {method}: {data['error'].get('message', 'error')}")
    return data.get("result")


_token_meta: dict[tuple, tuple[str, int]] = {}  # (rpc_url, contract) -> (symbol, decimals)
_enhanced_unsupported: set[str] = set()  # 确认不支持增强接口的节点
_enhanced_ok: dict[str, bool] = {}       # 增强接口最近一次是否成功(失败时启用哨兵)
_nr_margins: dict[str, tuple[int, float]] = {}  # url -> (可用边距, 探测时间); -1=暂不可用


async def _nr_probe_ok(client, url: str, address: str, to_block: int) -> bool:
    """探测 NodeReal 索引是否已覆盖 to_block(小范围 from 查询)。"""
    try:
        await _rpc_call(client, url, "nr_getTransactionByAddress", [{
            "category": ["external"], "address": address, "addressType": "from",
            "order": "desc", "excludeZeroValue": False, "maxCount": "0x1",
            "fromBlock": hex(max(to_block - 2000, 1)),
            "toBlock": hex(max(to_block, 1))}])
        return True
    except EtherscanError as e:
        if "not reached" in str(e).lower():
            return False
        raise


async def _nr_working_margin(client, url: str, address: str, latest: int) -> int | None:
    """二分定位 NodeReal 索引落后链头多少块,结果缓存 10 分钟。"""
    import time as _time
    cached = _nr_margins.get(url)
    if cached and _time.time() - cached[1] < 600:
        return None if cached[0] < 0 else cached[0]
    lo, hi = 0, None
    for m in (100, 20_000, 100_000, 300_000, 700_000, 1_500_000):
        if await _nr_probe_ok(client, url, address, latest - m):
            hi = m
            break
        lo = m
        await asyncio.sleep(REQUEST_GAP)
    if hi is None:
        _nr_margins[url] = (-1, _time.time())
        return None
    for _ in range(3):  # 细化,减少"跳过头"错过的区块
        if hi - lo <= 2000:
            break
        mid = (lo + hi) // 2
        await asyncio.sleep(REQUEST_GAP)
        if await _nr_probe_ok(client, url, address, latest - mid):
            hi = mid
        else:
            lo = mid
    _nr_margins[url] = (hi, _time.time())
    return hi


def _is_nodereal(url: str) -> bool:
    return "nodereal.io" in url


def _iso_to_epoch(raw) -> str:
    """时间戳兼容:ISO 字符串 / 十六进制 / 十进制秒。"""
    if not raw:
        return "0"
    s = str(raw)
    if s.startswith("0x"):
        try:
            return str(int(s, 16))
        except ValueError:
            return "0"
    if s.isdigit():
        return s
    from datetime import datetime
    try:
        return str(int(datetime.fromisoformat(
            s.replace("Z", "+00:00")).timestamp()))
    except ValueError:
        return "0"


async def _nr_asset_transfers(client: httpx.AsyncClient, url: str,
                              params: dict) -> list[dict] | None:
    """NodeReal 增强接口 nr_getTransactionByAddress 查原生交易历史。

    要点: excludeZeroValue 必须为 false(合约调用都是 0 值交易);
    支持大区块范围。节点不支持该方法时返回 None(并记忆,之后不再尝试)。
    """
    if url in _enhanced_unsupported:
        return None
    start = int(params.get("startblock", 0) or 0)
    limit = min(int(params.get("offset", 50) or 50), 1000)
    rows, seen = [], set()
    try:
        # 三个实测出的规则:
        # 1) toBlock 必须是明确的十六进制区块号("latest" 会静默返回空)
        # 2) fromBlock~toBlock 范围必须小于 2,000,000 个区块
        # 3) 索引落后链头,toBlock 太新会报 "blockNum not reached" → 往回退再试
        latest = int(str(await _rpc_call(client, url, "eth_blockNumber", [])), 16)
        margin = await _nr_working_margin(client, url,
                                          params.get("address", ""), latest)
        if margin is None:
            # 索引落后过多:本轮放弃增强查询,交给哨兵兜底,不报错
            _enhanced_ok[url] = False
            log.warning("NodeReal index too far behind on %s, "
                        "falling back to sentinel mode", url)
            return []
        to_block = max(latest - margin, 1)
        frm_block = min(max(start, to_block - 1_990_000, 1), to_block)
        base = {
            "category": ["external"],
            "address": params.get("address", ""),
            "fromBlock": hex(frm_block),
            "toBlock": hex(to_block),
            "excludeZeroValue": False,
            "maxCount": hex(limit),
            "order": "desc" if params.get("sort") == "desc" else "asc",
        }
        results = []
        try:
            for direction in ("from", "to"):
                res = await _rpc_call(client, url, "nr_getTransactionByAddress",
                                      [{**base, "addressType": direction}])
                results.append(res)
                await asyncio.sleep(REQUEST_GAP)
        except EtherscanError as e:
            if "not reached" in str(e).lower():
                # 缓存的边距失效(索引回退),下轮重新探测
                _nr_margins.pop(url, None)
                _enhanced_ok[url] = False
                return []
            raise
        _enhanced_ok[url] = True
        for res in results:
            for t in (res or {}).get("transfers") or []:
                key = t.get("uniqueId") or (t.get("hash"), t.get("from"),
                                            t.get("to"), str(t.get("value")))
                if key in seen:
                    continue
                seen.add(key)
                raw = (t.get("rawContract") or {}).get("value")
                if raw:
                    value = str(int(str(raw), 16))
                else:
                    value = str(int(round(float(t.get("value") or 0) * 1e18)))
                block_raw = str(t.get("blockNum") or t.get("blockNumber") or "0x0")
                block = int(block_raw, 16) if block_raw.startswith("0x") else int(block_raw)
                ts = (t.get("metadata") or {}).get("blockTimestamp") or t.get("blockTimeStamp")
                rows.append({
                    "hash": t.get("hash") or t.get("transactionHash") or "",
                    "blockNumber": str(block),
                    "timeStamp": _iso_to_epoch(ts),
                    "from": t.get("from", "") or "",
                    "to": t.get("to", "") or "",
                    "value": value,
                    "isError": "0",
                    "transactionIndex": "0",
                })
            await asyncio.sleep(REQUEST_GAP)
    except EtherscanError as e:
        if _is_rate_limited(e):
            raise
        msg = str(e).lower()
        if ("not exist" in msg or "not found" in msg or "unsupported" in msg
                or "not available" in msg or "method" in msg):
            _enhanced_unsupported.add(url)
            log.info("enhanced API unavailable on %s: %s", url, e)
            return None
        raise
    rows.sort(key=lambda r: int(r["blockNumber"]),
              reverse=(params.get("sort") == "desc"))
    return rows[:limit]


async def _token_info(client: httpx.AsyncClient, url: str, contract: str) -> tuple[str, int]:
    key = (url, contract.lower())
    if key in _token_meta:
        return _token_meta[key]
    symbol, decimals = "TOKEN", 18
    try:  # decimals()
        res = await _rpc_call(client, url, "eth_call",
                              [{"to": contract, "data": "0x313ce567"}, "latest"])
        if res and res not in ("0x", "0x0"):
            decimals = int(res, 16)
    except Exception:
        pass
    try:  # symbol(),兼容 string 和 bytes32 两种返回
        res = await _rpc_call(client, url, "eth_call",
                              [{"to": contract, "data": "0x95d89b41"}, "latest"])
        if res and res != "0x":
            raw = bytes.fromhex(res[2:])
            if len(raw) >= 64:
                length = int.from_bytes(raw[32:64], "big")
                text = raw[64:64 + length].decode("utf-8", "ignore")
            else:
                text = raw.rstrip(b"\x00").decode("utf-8", "ignore")
            symbol = text.strip() or "TOKEN"
    except Exception:
        pass
    _token_meta[key] = (symbol, decimals)
    return symbol, decimals


async def _rpc_tokentx(client: httpx.AsyncClient, url: str, params: dict) -> list[dict]:
    """用 eth_getLogs 拉取 ERC-20 Transfer 事件,适配成 tokentx 行格式。"""
    latest = int(str(await _rpc_call(client, url, "eth_blockNumber", [])), 16)
    want_start = int(params.get("startblock", 0) or 0)
    span = _rpc_spans.get(url, RPC_LOG_SPAN)

    def build_filters(frm: str, to: str) -> list[dict]:
        if "contractaddress" in params and "address" not in params:
            return [{"address": params["contractaddress"],
                     "topics": [TRANSFER_TOPIC],
                     "fromBlock": frm, "toBlock": to}]
        padded = "0x" + params["address"].lower().replace("0x", "").rjust(64, "0")
        return [{"topics": [TRANSFER_TOPIC, padded],
                 "fromBlock": frm, "toBlock": to},
                {"topics": [TRANSFER_TOPIC, None, padded],
                 "fromBlock": frm, "toBlock": to}]

    logs = None
    while logs is None:
        start = want_start
        if start <= 0 or latest - start > span:
            start = max(latest - span, 0)
        try:
            collected = []
            for f in build_filters(hex(start), hex(latest)):
                collected.extend(
                    await _rpc_call(client, url, "eth_getLogs", [f]) or [])
                await asyncio.sleep(REQUEST_GAP)
            logs = collected
            _rpc_spans[url] = span  # 记住该节点实测可用的窗口
        except EtherscanError as e:
            if _is_rate_limited(e):
                raise  # 限流类错误,重试只会更糟,交给上层冷却
            # 节点限制范围/结果数时,缩小回看窗口重试
            if ("limit" in str(e).lower() or "range" in str(e).lower()) and span > 50:
                span = max(span // 4, 50)
                continue
            raise
    rows, seen = [], set()
    for lg in logs:
        topics = lg.get("topics") or []
        if len(topics) != 3:  # 只要 ERC-20(ERC-721 是 4 个 topic)
            continue
        k = (lg.get("transactionHash"), lg.get("logIndex"))
        if k in seen:
            continue
        seen.add(k)
        data_hex = lg.get("data") or "0x0"
        if data_hex == "0x":
            data_hex = "0x0"
        symbol, decimals = await _token_info(client, url, lg.get("address", ""))
        rows.append({
            "hash": lg.get("transactionHash", ""),
            "blockNumber": str(int(str(lg.get("blockNumber", "0x0")), 16)),
            "timeStamp": "0",
            "from": "0x" + topics[1][-40:],
            "to": "0x" + topics[2][-40:],
            "value": str(int(data_hex, 16)),
            "tokenSymbol": symbol,
            "tokenDecimal": str(decimals),
            "logIndex": str(int(str(lg.get("logIndex", "0x0")), 16)),
            "transactionIndex": "0",
        })
    rows.sort(key=lambda r: int(r["blockNumber"]),
              reverse=(params.get("sort") == "desc"))
    limit = int(params.get("offset", 50) or 50)
    return rows[:limit]


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


async def _fallback_do(client: httpx.AsyncClient, cand: tuple[str, str, bool],
                       params: dict) -> list[dict]:
    typ, url, with_key = cand
    if typ == "rpc":
        action = params.get("action")
        if action == "tokentx":
            return await _rpc_tokentx(client, url, params)
        if action == "txlist":
            # NodeReal 有增强接口可以按地址查原生交易;其他 RPC 节点没有索引
            if _is_nodereal(url):
                rows = await _nr_asset_transfers(client, url, params)
                if rows is not None:
                    return rows
            return []
        raise EtherscanError(f"RPC 源不支持 {action}")
    p = dict(params)
    if with_key:
        p["apikey"] = API_KEY
    else:
        p.pop("apikey", None)
    # Blockscout 的 tokentx 需要 address 参数;只有 contractaddress 时
    # (代币合约监控)改走它的 v2 接口查该代币的全部转账
    if ("blockscout" in url and p.get("action") == "tokentx"
            and "contractaddress" in p and "address" not in p):
        return await _blockscout_token_transfers(client, url, p)
    data = await _get_json(client, url, p)
    return _parse_list(data)


async def _fallback_query(client: httpx.AsyncClient, chain: str,
                          params: dict) -> list[dict]:
    import time as _time
    now = _time.time()
    errors = []
    tried: list[tuple] = []
    # 按候选顺序尝试(环境变量配置的专属源永远排最前),不固守上次的赢家:
    # 否则公共节点一旦被缓存,专属源就再也轮不上了
    ordered = _candidates(chain)
    # 跳过还在冷却期的源;若全部在冷却,则只温和地试冷却最早到期的那一个,
    # 避免每轮把所有源轰一遍、让限流计数器永远无法恢复
    available = [c for c in ordered if _source_cooldown.get(c, 0) <= now]
    if not available and ordered:
        available = [min(ordered, key=lambda c: _source_cooldown.get(c, 0))]

    for cand in available:
        tried.append(cand)
        try:
            rows = await _fallback_do(client, cand, params)
            _source_cooldown.pop(cand, None)
            if _fallback_urls.get(chain) != cand:
                _fallback_urls[chain] = cand
                log.info("chain %s using fallback source %s", chain, cand[1])
            return rows
        except Exception as e:
            errors.append(f"{cand[1]}: {str(e)[:120]}")
            if _fallback_urls.get(chain) == cand:
                _fallback_urls.pop(chain, None)
            if _is_rate_limited(e):
                _source_cooldown[cand] = _time.time() + COOLDOWN_SECS
                log.warning("source %s rate-limited, cooling down %ss",
                            cand[1], COOLDOWN_SECS)
        await asyncio.sleep(REQUEST_GAP)
    cooling = len(ordered) - len(tried)
    suffix = f"(另有 {cooling} 个源限流冷却中)" if cooling > 0 else ""
    raise EtherscanError(
        f"{CHAINS[chain]['name']} 的备用数据源均不可用{suffix}: " + " | ".join(errors))


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
            if _plan_blocked(e) and _candidates(chain):
                _fallback_chains.add(chain)
                log.info("chain %s not in Etherscan free plan, "
                         "switching to fallback sources", chain)
            else:
                raise
    if _candidates(chain):
        return await _fallback_query(client, chain, params)
    raise EtherscanError(
        f"{CHAINS[chain]['name']} 不在 Etherscan 免费套餐内,且暂无备用数据源")


def _adapt_v2_transfer(item: dict) -> dict:
    """把 Blockscout v2 transfer 条目转成 etherscan tokentx 行格式。"""
    total = item.get("total") or {}
    token = item.get("token") or {}
    ts = 0
    raw_ts = item.get("timestamp")
    if raw_ts:
        from datetime import datetime
        try:
            ts = int(datetime.fromisoformat(
                str(raw_ts).replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return {
        "hash": item.get("transaction_hash") or item.get("tx_hash") or "",
        "blockNumber": str(item.get("block_number") or 0),
        "timeStamp": str(ts),
        "from": (item.get("from") or {}).get("hash", ""),
        "to": (item.get("to") or {}).get("hash", ""),
        "value": str(total.get("value") or "0"),
        "tokenSymbol": token.get("symbol") or "",
        "tokenDecimal": str(total.get("decimals")
                            or token.get("decimals") or 18),
        "logIndex": str(item.get("log_index") or ""),
        "transactionIndex": "0",
    }


async def _blockscout_token_transfers(client: httpx.AsyncClient, api_url: str,
                                      params: dict) -> list[dict]:
    base_url = api_url.rsplit("/api", 1)[0]
    url = f"{base_url}/api/v2/tokens/{params['contractaddress']}/transfers"
    data = await _get_json(client, url, {})
    items = data.get("items") or []
    rows = [_adapt_v2_transfer(i) for i in items]
    start = int(params.get("startblock", 0) or 0)
    rows = [r for r in rows if int(r["blockNumber"] or 0) >= start]
    limit = int(params.get("offset", 50) or 50)
    return rows[:limit]  # v2 默认新→旧排序,取最新的一页


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

    # 数据源查不到普通交易/合约调用时(无增强接口的 RPC),用 nonce+余额哨兵兜底
    rpc_url = _rpc_source(watch.chain)
    if (rpc_url and _is_nodereal(rpc_url) and rpc_url not in _enhanced_unsupported
            and _enhanced_ok.get(rpc_url, False)):
        rpc_url = None  # 增强接口本轮已提供完整交易,无需哨兵
    if watch.kind == "address" and rpc_url:
        try:
            notice = await _rpc_activity_probe(client, rpc_url, watch)
            if notice:
                fresh.append(notice)
        except Exception as e:
            log.debug("activity probe failed for %s: %s", watch.address, e)
    return fresh


async def _proxy(client: httpx.AsyncClient, chain: str, action: str,
                 extra: dict | None = None):
    """proxy 模块 (JSON-RPC 透传),返回 result 原文;免费套餐不覆盖时走备用源。"""
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
        if _candidates(chain):
            _fallback_chains.add(chain)
            log.info("chain %s not in Etherscan free plan, "
                     "switching to fallback sources", chain)
        else:
            return result
    # 已探测出的源优先,否则依次尝试候选
    tried = ([_fallback_urls[chain]] if chain in _fallback_urls else []) \
        + [c for c in _candidates(chain)
           if c != _fallback_urls.get(chain)]
    extra = extra or {}
    for typ, url, with_key in tried:
        try:
            if typ == "rpc":
                rpc_args = {
                    "eth_blockNumber": [],
                    "eth_getCode": [extra.get("address"), "latest"],
                }.get(action)
                if rpc_args is None:
                    continue
                res = await _rpc_call(client, url, action, rpc_args)
            else:
                p = dict(params)
                if with_key:
                    p["apikey"] = API_KEY
                data = await _get_json(client, url, p)
                res = data.get("result")
            if isinstance(res, str) and res.startswith("0x"):
                return res
        except Exception as e:
            log.debug("proxy fallback %s failed: %s", url, e)
        await asyncio.sleep(REQUEST_GAP)
    return None


def _rpc_source(chain: str) -> str | None:
    """该链当前是否走 RPC 数据源;是则返回节点 URL。"""
    cand = _fallback_urls.get(chain)
    if chain in _fallback_chains and cand and cand[0] == "rpc":
        return cand[1]
    return None


async def address_summary(chain: str, address: str) -> str:
    """RPC 链上的地址概况(累计发出交易数、余额),给 /recent 补充信息。"""
    url = _rpc_source(chain)
    if not url:
        return ""
    try:
        async with httpx.AsyncClient() as client:
            nonce = int(str(await _rpc_call(
                client, url, "eth_getTransactionCount", [address, "latest"])), 16)
            await asyncio.sleep(REQUEST_GAP)
            balance = int(str(await _rpc_call(
                client, url, "eth_getBalance", [address, "latest"])), 16)
        native = CHAINS[chain]["native"]
        return (f"\n\n📇 地址概况: 累计发出 {nonce:,} 笔交易 · "
                f"余额 {_amount(str(balance), 18)} {native}")
    except Exception as e:
        log.debug("address summary failed: %s", e)
        return ""


def rpc_limit_note(chain: str) -> str:
    url = _rpc_source(chain)
    if not url:
        return ""
    if (_is_nodereal(url) and url not in _enhanced_unsupported
            and _enhanced_ok.get(url, False)):
        return ""  # NodeReal 增强接口可查全量交易,无需提示
    return ("\n\nℹ️ 该链当前使用公共 RPC 数据源,历史查询仅覆盖代币转账事件;"
            "合约调用和原生币交易请点上方地址到浏览器查看。")


async def _rpc_activity_probe(client: httpx.AsyncClient, url: str, watch) -> dict | None:
    """RPC 链的活动哨兵:nonce 增加 = 发出了新交易(含合约调用),余额变化 = 有收支。"""
    nonce = int(str(await _rpc_call(
        client, url, "eth_getTransactionCount", [watch.address, "latest"])), 16)
    await asyncio.sleep(REQUEST_GAP)
    balance = int(str(await _rpc_call(
        client, url, "eth_getBalance", [watch.address, "latest"])), 16)
    first_run = watch.nonce is None
    changes = []
    if not first_run:
        if nonce > (watch.nonce or 0):
            changes.append(f"📤 发出了 {nonce - (watch.nonce or 0)} 笔新交易(含合约调用)")
        old_balance = int(watch.balance or 0)
        if balance != old_balance:
            delta = balance - old_balance
            sign = "+" if delta > 0 else "-"
            native = CHAINS[watch.chain]["native"]
            changes.append(f"💰 {native} 余额 {sign}{_amount(str(abs(delta)), 18)}"
                           f"(现 {_amount(str(balance), 18)} {native})")
    watch.nonce = nonce
    watch.balance = str(balance)
    if changes:
        return {"_type": "notice", "notice": "\n".join(changes)}
    return None


def _snip(x, limit: int = 220) -> str:
    """截断并抹掉 URL 里的 API key,用于诊断输出。"""
    import re as _re
    s = str(x).replace("\n", " ")
    s = _re.sub(r"/v[0-9]/[0-9a-fA-F-]{20,}", "/v1/***", s)
    s = _re.sub(r"(apikey=)[0-9A-Za-z]+", r"\1***", s)
    return s[:limit] + ("…" if len(s) > limit else "")


async def debug_report(chain: str, address: str) -> str:
    """逐个数据源实测并返回原始结果,用于 /debug 诊断。"""
    import json as _json
    import time as _time
    out = [f"链: {CHAINS[chain]['name']} (chainid {CHAINS[chain]['chain_id']})",
           f"降级模式: {'是' if chain in _fallback_chains else '否(仍走 Etherscan)'}"]
    src = _fallback_urls.get(chain)
    out.append(f"当前锁定源: {_snip(src[1], 80) if src else '未选定'}")
    cooling = [f"{_snip(c[1], 40)}({int(t - _time.time())}s)"
               for c, t in _source_cooldown.items() if t > _time.time()]
    if cooling:
        out.append("冷却中: " + ", ".join(cooling))
    if _enhanced_unsupported:
        out.append(f"已标记不支持增强接口: {len(_enhanced_unsupported)} 个节点")
    out.append("")

    async with httpx.AsyncClient() as client:
        try:
            data = await _get_json(client, API_URL, {
                "chainid": CHAINS[chain]["chain_id"], "apikey": API_KEY,
                "module": "account", "action": "txlist", "address": address,
                "page": 1, "offset": 2, "sort": "desc",
                "startblock": 0, "endblock": 999999999,
            })
            out.append(f"[Etherscan] txlist → status={data.get('status')} "
                       f"message={data.get('message')} result={_snip(data.get('result'))}")
        except Exception as e:
            out.append(f"[Etherscan] txlist → 异常: {_snip(e)}")
        await asyncio.sleep(REQUEST_GAP)

        for typ, url, with_key in _candidates(chain)[:3]:
            name = url.split("//")[-1].split("/")[0]
            if typ != "rpc":
                try:
                    p = {"module": "account", "action": "txlist", "address": address,
                         "page": 1, "offset": 2, "sort": "desc"}
                    if with_key:
                        p["apikey"] = API_KEY
                    data = await _get_json(client, url, p)
                    out.append(f"[{name}] txlist → status={data.get('status')} "
                               f"result={_snip(data.get('result'))}")
                except Exception as e:
                    out.append(f"[{name}] txlist → 异常: {_snip(e)}")
                await asyncio.sleep(REQUEST_GAP)
                continue
            try:
                bn = await _rpc_call(client, url, "eth_blockNumber", [])
                latest = int(str(bn), 16)
                out.append(f"[{name}] blockNumber → {latest:,}")
            except Exception as e:
                out.append(f"[{name}] blockNumber → 异常: {_snip(e)}")
                continue
            await asyncio.sleep(REQUEST_GAP)
            if _is_nodereal(url):
                # 索引进度探测:找出 NodeReal 索引实际推进到哪里
                async def _nr_probe(frm_b: int, to_b: int, count: str = "0x3"):
                    resp = await client.post(url, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "nr_getTransactionByAddress",
                        "params": [{"category": ["external"], "address": address,
                                    "addressType": "from", "order": "desc",
                                    "excludeZeroValue": False, "maxCount": count,
                                    "fromBlock": hex(max(frm_b, 1)),
                                    "toBlock": hex(max(to_b, 1))}],
                    }, timeout=30)
                    return resp.json()

                good_margin = None
                for margin in (100, 10_000, 100_000, 300_000, 1_000_000):
                    try:
                        data = await _nr_probe(latest - margin - 2000,
                                               latest - margin)
                        if data.get("error"):
                            out.append(f"[{name}] 索引探测 -{margin:,}块 → "
                                       f"{_snip(data['error'].get('message'), 60)}")
                        else:
                            n = len((data.get('result') or {}).get('transfers') or [])
                            out.append(f"[{name}] 索引探测 -{margin:,}块 → OK,{n} 条")
                            if good_margin is None:
                                good_margin = margin
                    except Exception as e:
                        out.append(f"[{name}] 索引探测 -{margin:,}块 → 异常: {_snip(e)}")
                    await asyncio.sleep(REQUEST_GAP)
                if good_margin is not None:
                    try:
                        to_b = latest - good_margin
                        data = await _nr_probe(to_b - 1_990_000, to_b, "0x5")
                        out.append(f"[{name}] 宽范围查询(-{good_margin:,}块起,199万范围) → "
                                   f"{_snip(data, 400)}")
                    except Exception as e:
                        out.append(f"[{name}] 宽范围查询 → 异常: {_snip(e)}")
                    await asyncio.sleep(REQUEST_GAP)
                else:
                    out.append(f"[{name}] ⚠️ 索引落后超过 100 万块,增强查询暂不可用"
                               f"(监控由 nonce/余额哨兵兜底)")
            try:
                padded = "0x" + address.lower().replace("0x", "").rjust(64, "0")
                logs = await _rpc_call(client, url, "eth_getLogs", [{
                    "topics": [TRANSFER_TOPIC, padded],
                    "fromBlock": hex(max(latest - 200, 0)), "toBlock": hex(latest),
                }])
                out.append(f"[{name}] getLogs(近200块,转出) → {len(logs or [])} 条")
            except Exception as e:
                out.append(f"[{name}] getLogs → 异常: {_snip(e)}")
            await asyncio.sleep(REQUEST_GAP)
    return "\n".join(out)


def _source_name(chain: str) -> str:
    if chain not in _fallback_chains:
        return "Etherscan"
    if chain in _fallback_urls:
        from urllib.parse import urlparse
        return urlparse(_fallback_urls[chain][1]).netloc
    return "备用源"


async def check_chain(chain: str) -> tuple[int, str]:
    """连通性检测:返回 (最新区块, 数据源名)。失败抛异常。"""
    async with httpx.AsyncClient() as client:
        res = await _proxy(client, chain, "eth_blockNumber")
    block = int(str(res), 16)  # 出错时 res 是错误文本/None,这里会抛异常
    return block, _source_name(chain)


async def detect_address(address: str) -> dict[str, dict]:
    """识别地址在每条链上的类型: eoa(普通地址) / contract(合约) / token(代币合约)。

    有合约代码 → contract;合约且有代币转账记录 → token(带 symbol)。
    """
    result: dict[str, dict] = {}
    async with httpx.AsyncClient() as client:
        for chain in CHAINS:
            info = {"type": "eoa", "symbol": None}
            code = None
            try:
                code = await _proxy(client, chain, "eth_getCode",
                                    {"address": address, "tag": "latest"})
            except Exception as e:
                log.debug("getCode failed on %s: %s", chain, e)
            is_contract = (isinstance(code, str) and code.startswith("0x")
                           and len(code) > 4)
            # getCode 拿不到明确结果(接口不可用/被套餐限制)时也去探测代币转账,
            # 避免像 Base 这种链把代币合约误判成普通地址
            code_unknown = not (isinstance(code, str) and code.startswith("0x"))
            if is_contract or code_unknown:
                await asyncio.sleep(REQUEST_GAP)
                try:
                    rows = await _query(client, chain, {
                        "module": "account", "action": "tokentx",
                        "contractaddress": address,
                        "page": 1, "offset": 1, "sort": "desc",
                        "startblock": 0, "endblock": 999999999,
                    })
                    if rows:
                        info["type"] = "token"
                        info["symbol"] = rows[0].get("tokenSymbol") or None
                    elif is_contract:
                        info["type"] = "contract"
                except Exception as e:
                    log.debug("tokentx probe failed on %s: %s", chain, e)
                    if is_contract:
                        info["type"] = "contract"
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
    if tx.get("_type") == "notice":
        label = html.escape(watch.label or _short(watch.address))
        return (f"🔔 <b>[{chain['name']}] {label}</b> 检测到链上动作\n"
                f"{tx.get('notice', '')}\n"
                f"<a href=\"{explorer}/address/{watch.address}\">🔗 在浏览器查看详情</a>")
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
