#!/usr/bin/env python3
"""
UNCOMMON•GOODS (UG) chain-minter / UG 链式铸造脚本（mainnet）

基于 diesel-chain-minter（https://github.com/anchoys1337/diesel-chain-minter）改造。
默认纯 UG runestone（匹配区块 974557 多数 mint），也可切回 UG+DIESEL。

Builds a chain of N_MINTS txs from one P2WPKH key. Each mint tx:
  vout0 — change back to your address (runes land here via pointer)
  vout1 / last — OP_RETURN runestone minting UNCOMMON•GOODS (1:0)
  optional DIESEL alkane protostone when MINT_PAYLOAD="ug_diesel"

CHAIN_MODE: "hybrid" (default, shield + P2A anchors) | "classic" | "anchor"
Default DRY_RUN=True — build and print only, never broadcast until you flip it.

Requirements: pip install embit requests
Key: env UG_WIF (or DIESEL_WIF). Never store WIF in code/repo.
WARNING: real BTC. Start with DRY_RUN=True and small N_MINTS.
Mempool ancestor limit is 25 — keep N_MINTS <= 25.
Upstream tests (optional): python -m unittest test_diesel_chain_mint -v
"""

import json
import math
import os
import sys
import time
from datetime import datetime

import requests
from embit import ec, script
from embit.transaction import Transaction, TransactionInput, TransactionOutput, Witness
from embit.networks import NETWORKS

# ------------------------- CONFIG -------------------------
WIF            = os.environ.get("UG_WIF") or os.environ.get("DIESEL_WIF", "")
                         # compressed WIF for bc1q P2WPKH; env only, never in files.
N_MINTS        = 25      # сколько минт-транзакций в цепочке (1..25)
PARENT_FEERATE = 0.1    # sat/vB для первых N-1; ниже MIN_RELAY_FEERATE не опустится (кламп в plan_fees)
TARGET_FEERATE = 0.6     # sat/vB — ручная ставка пакета: фолбэк, когда DYNAMIC_START не смог узнать сеть
DYNAMIC_START  = True    # пункт 2: старт = медиана сети + START_MARGIN (иначе TARGET_FEERATE)
START_MARGIN   = 0.1     # sat/vB запас над медианой сети при старте (0.5 -> шлём 0.6)
DRY_RUN        = True     # True: build & print only, no broadcast. Flip to False only after verifying.
# Ankr-URL из прежней версии удалён: это JSON-RPC, esplora-пути он не отдаёт (404 на всё)
API            = "https://mempool.space/api"     # UTXO/статусы/бродкаст (esplora REST)
FALLBACK_API   = "https://blockstream.info/api"  # запасной esplora на случай сбоя API
FEE_API        = "https://mempool.space/api"     # порог след. блока (/v1/fees/* есть только тут)
MIN_CHANGE     = 1000    # не оставлять change меньше этого (даст-лимит с запасом)
# Релей-минимум сети: Core 30.0 (окт 2025, PR #33106) снизил дефолтный minrelaytxfee
# с 1.0 до 0.1 sat/vB. Проверено живым пакетом 0.100 sat/vB в мемпуле mempool.space
# (2026-07-18). Не все узлы/пулы обновились — распространение чуть медленнее, но
# пакетная (ancestor) ставка у нас всё равно выше 1 sat/vB, майнингу это не мешает.
MIN_RELAY_FEERATE = 0.1

# ------------------- бот-режим (пункт 4) ------------------
AUTO_MODE      = False   # True: нон-стоп — пакет подтвердился -> сразу строим следующий
                         # из свежего change, пока хватает денег (SystemExit по остатку)
STATE_FILE     = "minter_state.json"  # json-стейт пакета: рестарт продолжает монитор

# --------------------- авто-бамп (RBF) --------------------
MONITOR        = True    # False: отправить и выйти (старое поведение)
MAX_FEERATE    = 5.0    # sat/vB — потолок эффективной ставки пакета, выше не бампим
BUMP_AHEAD     = 0.05    # sat/vB: держим ставку пакета >= порог входа + этот запас —
                         # бампим ПРЕВЕНТИВНО, не дожидаясь выпадения из блока
POLL_INTERVAL  = 10      # секунд между опросами мемпула
BUMP_COOLDOWN  = 30      # секунд минимум между RBF-заменами (защита от шума порога)
# Бродкаст: транзиентный inputs-missing (родитель ещё не распространился по узлам)
# ретраим — критично довести sweep до мемпула, иначе анкер без его защиты угоняет
# burn-бот (инцидент 2026-07-18: снайп d2909c7a за 440 sat забрал анкер, т.к. обрыв
# бродкаста на tx20 не дал отправить sweep 1194 sat).
BROADCAST_RETRIES     = 4   # попыток на одну tx при inputs-missing
BROADCAST_RETRY_DELAY = 2   # секунд между попытками (даём родителю распространиться)

# ------------------ анкорный режим (P2A) ------------------
# Структура «как у образца» (кластер 57ff6b38…, подтверждён): pivot создаёт
# P2A-анкер, (N-2) бесключевых 94-байтных звеньев спускают его лесенкой по
# ANCHOR_FEE, sweep-child сметает остаток и весь DIESEL. ~100 vB/минт vs 140.
#
# ВНИМАНИЕ: "anchor" безопасен ТОЛЬКО через submitpackage своей ноды Core 28+
# (атомарная отправка пакета). При публичном бродкасте tx-за-tx P2A-анкер висит
# в мемпуле как anyone-can-spend — burn-бот его угоняет и CPFP'ит в блок только
# pivot, остальные звенья эвиктятся (проверено вживую 2026-07-18: burn-tx
# 8ee560b8 увела анкер, из 25 минтов прошёл 1). Дефолт — "classic" (все выходы
# под ключом, угона нет, +29% к комиссии). Не ставь "anchor" без своей ноды.
CHAIN_MODE     = "hybrid"   # "hybrid" (shield+anchors, recommended) | "classic" | "anchor" (unsafe without submitpackage)
SHIELD_LINKS   = 9         # классических ключевых звеньев ПЕРЕД pivot'ом. Против угона
                           # хватает и 1 (pivot с неподтверждённым предком не выдернуть 1P1C).
                           # 9 — как у живого конкурента: это ДОЛЯ гарантированных минтов
                           # (щиты подтверждаются даже если анкерный хвост орфанится), а не
                           # усиление защиты анкеров. 9 -> ~116 vB/минт, 3 -> ~105, classic 140.
# "ug" = pure UNCOMMON•GOODS (matches block 974557 majority)
# "ug_diesel" = UG + DIESEL alkane (original repo payload)
MINT_PAYLOAD = "ug"
OP_RETURN_UG = "6a5d0414011400"
OP_RETURN_UG_DIESEL = "6a5d1214011400ff7f818cec82d08bc0a88281d215"
OP_RETURN_HEX = OP_RETURN_UG if MINT_PAYLOAD == "ug" else OP_RETURN_UG_DIESEL

# Pure UG anchors in block 974557 paid ~8 sats; UG+DIESEL sample used 10.
ANCHOR_FEE     = 8 if MINT_PAYLOAD == "ug" else 10  # sat per anchor link
ANCHOR_RESIDUAL = 240      # sat left in last anchor (swept by child)
ANCHOR_SCRIPT_HEX = "51024e73"  # P2A (Pay-to-Anchor): OP_1 PUSH2 0x4e73, spendable without signature
# ----------------------------------------------------------

NET = NETWORKS["main"]
SESSION = requests.Session()

# консоль Windows бывает cp1251: неэнкодимый символ не должен ронять монитор
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def vsize(tx: Transaction) -> int:
    full = len(tx.serialize())
    stripped = Transaction(
        version=tx.version,
        vin=[TransactionInput(i.txid, i.vout, sequence=i.sequence) for i in tx.vin],
        vout=tx.vout,
        locktime=tx.locktime,
    )
    base = len(stripped.serialize())
    weight = base * 3 + full
    return (weight + 3) // 4


def build_signed_tx(prev_txid: bytes, prev_vout: int, prev_value: int,
                    fee: int, key: ec.PrivateKey, spk: script.Script) -> Transaction:
    change = prev_value - fee
    if change < MIN_CHANGE:
        raise SystemExit(f"Недостаточно средств: change={change} sat < {MIN_CHANGE}")
    vin = TransactionInput(prev_txid, prev_vout, sequence=0xFFFFFFFD)  # RBF
    vout = [
        TransactionOutput(change, spk),
        TransactionOutput(0, script.Script(bytes.fromhex(OP_RETURN_HEX))),
    ]
    tx = Transaction(vin=[vin], vout=vout)
    h = tx.sighash_segwit(0, script.p2pkh_from_p2wpkh(spk), prev_value)
    sig = key.sign(h)
    tx.vin[0].witness = Witness([sig.serialize() + b"\x01", key.get_public_key().serialize()])
    return tx


def plan_fees(tx_vsize: int, n: int, parent_feerate: float, target_feerate: float):
    """Комиссии (parent_fee, child_fee): пакет из n tx выходит на target_feerate (CPFP).
    Родитель не опускается ниже MIN_RELAY_FEERATE; +1 sat — запас на то, что
    DER-подпись реальной tx может выйти на байт длиннее пробной (+1 vB)."""
    floor_fee = math.ceil(tx_vsize * MIN_RELAY_FEERATE) + 1
    parent_fee = max(int(tx_vsize * parent_feerate) + 1, floor_fee)
    total_needed = int(tx_vsize * n * target_feerate) + 1
    child_fee = total_needed - parent_fee * (n - 1)
    if child_fee < parent_fee:
        child_fee = parent_fee
    return parent_fee, child_fee


def build_chain(utxo_txid: bytes, utxo_vout: int, utxo_value: int,
                n: int, parent_fee: int, child_fee: int, key, spk):
    """Цепочка из n подписанных tx: каждая тратит vout0 предыдущей, последняя — child."""
    txs = []
    prev_txid, prev_vout, prev_value = utxo_txid, utxo_vout, utxo_value
    for i in range(n):
        fee = child_fee if i == n - 1 else parent_fee
        tx = build_signed_tx(prev_txid, prev_vout, prev_value, fee, key, spk)
        txs.append(tx)
        prev_txid, prev_vout, prev_value = tx.txid(), 0, tx.vout[0].value
    return txs


# ------------------ анкорный режим (P2A) ------------------

def build_pivot_tx(prev_txid: bytes, prev_vout: int, prev_value: int,
                   fee: int, anchor_value: int, key, spk) -> Transaction:
    """Первая tx анкорного пакета (образец f4ceb021…):
    vout0 change (сюда DIESEL по pointer), vout1 P2A-анкер, vout2 OP_RETURN."""
    change = prev_value - fee - anchor_value
    if change < MIN_CHANGE:
        raise SystemExit(f"Недостаточно средств: change={change} sat < {MIN_CHANGE}")
    vin = TransactionInput(prev_txid, prev_vout, sequence=0xFFFFFFFD)  # RBF
    vout = [
        TransactionOutput(change, spk),
        TransactionOutput(anchor_value, script.Script(bytes.fromhex(ANCHOR_SCRIPT_HEX))),
        TransactionOutput(0, script.Script(bytes.fromhex(OP_RETURN_HEX))),
    ]
    tx = Transaction(vin=[vin], vout=vout)
    h = tx.sighash_segwit(0, script.p2pkh_from_p2wpkh(spk), prev_value)
    sig = key.sign(h)
    tx.vin[0].witness = Witness([sig.serialize() + b"\x01", key.get_public_key().serialize()])
    return tx


def build_anchor_tx(prev_txid: bytes, prev_vout: int, prev_value: int) -> Transaction:
    """Бесключевое анкерное звено (образец 46e33586…): тратит P2A БЕЗ подписи,
    vout0 — следующий анкер (-ANCHOR_FEE, DIESEL едет по цепочке), vout1 OP_RETURN.
    Witness нет вообще -> чистая legacy-сериализация, ровно 94 байта."""
    vin = TransactionInput(prev_txid, prev_vout, sequence=0xFFFFFFFF)  # как у образца
    vout = [
        TransactionOutput(prev_value - ANCHOR_FEE,
                          script.Script(bytes.fromhex(ANCHOR_SCRIPT_HEX))),
        TransactionOutput(0, script.Script(bytes.fromhex(OP_RETURN_HEX))),
    ]
    return Transaction(vin=[vin], vout=vout)


def build_sweep_tx(key_txid: bytes, key_vout: int, key_value: int,
                   anc_txid: bytes, anc_vout: int, anc_value: int,
                   fee: int, key, spk) -> Transaction:
    """Sweep-child (образец 57ff6b38…): вход0 — наш change (подписан), вход1 —
    последний анкер (пустой witness). Собирает остаток лесенки и весь DIESEL."""
    change = key_value + anc_value - fee
    if change < MIN_CHANGE:
        raise SystemExit(f"Недостаточно средств: change={change} sat < {MIN_CHANGE}")
    vin = [
        TransactionInput(key_txid, key_vout, sequence=0xFFFFFFFD),  # RBF
        TransactionInput(anc_txid, anc_vout, sequence=0xFFFFFFFF),
    ]
    vout = [
        TransactionOutput(change, spk),
        TransactionOutput(0, script.Script(bytes.fromhex(OP_RETURN_HEX))),
    ]
    tx = Transaction(vin=vin, vout=vout)
    h = tx.sighash_segwit(0, script.p2pkh_from_p2wpkh(spk), key_value)
    sig = key.sign(h)
    tx.vin[0].witness = Witness([sig.serialize() + b"\x01", key.get_public_key().serialize()])
    return tx


def plan_fees_hybrid(shield_vs: int, pivot_vs: int, anchor_vs: int, child_vs: int,
                     s: int, k: int, target_feerate: float):
    """(shield_fee, pivot_fee, child_fee) для гибрида: s щитов + pivot + k анкеров
    + child выходят на target_feerate (CPFP). Щиты и pivot платят минимум релея,
    анкеры — ANCHOR_FEE, child добирает остальное."""
    shield_fee = math.ceil(shield_vs * MIN_RELAY_FEERATE) + 1
    pivot_fee = math.ceil(pivot_vs * MIN_RELAY_FEERATE) + 1
    total_vs = shield_vs * s + pivot_vs + anchor_vs * k + child_vs
    fixed = shield_fee * s + pivot_fee + ANCHOR_FEE * k
    child_fee = math.ceil(total_vs * target_feerate) - fixed
    child_floor = math.ceil(child_vs * MIN_RELAY_FEERATE) + 1
    return shield_fee, pivot_fee, max(child_fee, child_floor)


def plan_fees_anchor(pivot_vs: int, anchor_vs: int, child_vs: int, k: int,
                     target_feerate: float):
    """(pivot_fee, child_fee): анкорный пакет выходит на target_feerate.
    Анкеры платят фиксированные ANCHOR_FEE, child добирает остальное."""
    pivot_fee = math.ceil(pivot_vs * MIN_RELAY_FEERATE) + 1
    total_vs = pivot_vs + anchor_vs * k + child_vs
    child_fee = math.ceil(total_vs * target_feerate) - pivot_fee - ANCHOR_FEE * k
    child_floor = math.ceil(child_vs * MIN_RELAY_FEERATE) + 1
    return pivot_fee, max(child_fee, child_floor)


# ------------------------- API ----------------------------

def api_get(path, want_json=True):
    """GET с фолбэком API → FALLBACK_API (оба esplora-совместимые).
    404 — это ответ («транзакции нет»), а не сбой: второй узел не спрашиваем."""
    last_err = None
    for base in (API, FALLBACK_API):
        try:
            r = SESSION.get(base + path, timeout=15)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json() if want_json else r.text
        except Exception as e:
            last_err = e
    raise RuntimeError(f"GET {path}: {last_err}")


def tip_height() -> int:
    return int(api_get("/blocks/tip/height", want_json=False))


def tx_status(txid_hex):
    """status-dict (confirmed/block_height) или None, если tx нет ни в мемпуле,
    ни в блоке. ВАЖНО: /tx/{txid}/status отдаёт 200 {"confirmed":false} даже для
    НЕсуществующих/эвикнутых tx — presence детектим по полному /tx (404 = нет)."""
    tx = api_get(f"/tx/{txid_hex}")
    return tx["status"] if tx else None


def broadcast_tx(tx):
    """(ok, текст ответа). Сетевой сбой → пробуем FALLBACK_API; «already known» — успех;
    ошибка политики (fee/конфликт) не ретраится — она одинакова на всех узлах."""
    raw = tx.serialize().hex()
    last_err = ""
    for base in (API, FALLBACK_API):
        try:
            r = SESSION.post(f"{base}/tx", data=raw, timeout=30)
        except Exception as e:
            last_err = str(e)
            continue
        text = r.text.strip()
        if r.status_code == 200:
            return True, text
        if "already" in text.lower():  # txn-already-in-mempool / already in block chain
            return True, text
        if r.status_code in (429, 500, 502, 503, 504):  # инфраструктура — пробуем фолбэк
            last_err = f"HTTP {r.status_code}: {text[:120]}"
            continue
        return False, text
    return False, last_err


def next_block_threshold():
    """(ставка входа в следующий блок sat/vB | None, источник).
    Берём feeRange[0] первого projected-блока mempool.space — реальный порог входа.
    Мемпул меньше блока → хватит и минимума релея. Фолбэк — fees/recommended."""
    try:
        blocks = SESSION.get(f"{FEE_API}/v1/fees/mempool-blocks", timeout=15).json()
        if blocks:
            b0 = blocks[0]
            if b0.get("blockVSize", 0) < 950_000:
                # мемпул меньше блока: в следующий блок войдёт всё, что прошло релей
                return MIN_RELAY_FEERATE, "мемпул меньше блока"
            fr = b0.get("feeRange") or []
            if fr:
                # fr[0] бывает выбросом (CPFP-хвост пакета), fr[1] надёжнее как порог
                return float(fr[1] if len(fr) > 1 else fr[0]), "mempool-blocks"
    except Exception:
        pass
    try:
        rec = SESSION.get(f"{FEE_API}/v1/fees/recommended", timeout=15).json()
        return float(rec["fastestFee"]), "fees/recommended"
    except Exception:
        return None, "нет данных"


def network_median_rate():
    """«Текущий средний уровень сети» — медиана первого projected-блока
    mempool.space, не ниже релей-минимума. None при сбое API."""
    try:
        blocks = SESSION.get(f"{FEE_API}/v1/fees/mempool-blocks", timeout=15).json()
        if blocks:
            med = float(blocks[0].get("medianFee") or 0.0)
            return max(med, MIN_RELAY_FEERATE)
    except Exception:
        pass
    return None


def compute_start_rate(median):
    """(стартовая ставка пакета, описание источника). Пункт 2: медиана сети +
    START_MARGIN, потолок MAX_FEERATE; нет данных — ручной TARGET_FEERATE."""
    if median is None:
        return TARGET_FEERATE, f"TARGET_FEERATE={TARGET_FEERATE} (сеть недоступна)"
    rate = min(median + START_MARGIN, MAX_FEERATE)
    return rate, f"медиана сети {median:.2f} + {START_MARGIN}"


# --------------------- состояние пакета -------------------

class Chain:
    """Пакет в мемпуле: транзакции, их комиссии/vsize, глубина подтверждений.
    Абстрагирует форму пакета (classic-цепочка или anchor): rebuild_child(fee)
    пересобирает child с той же структурой входов/выходов и новой комиссией;
    child_key_outpoint — ключевой вход child для outspend-проверки гонки RBF;
    child_funds — сумма значений входов child (пол по change при бампе)."""

    def __init__(self, txs, fees, vsizes, rebuild_child, child_key_outpoint,
                 child_funds, rebuild_info=None):
        self.txs = list(txs)
        self.fees = list(fees)
        self.vsizes = list(vsizes)
        self.rebuild_child = rebuild_child
        self.child_key_outpoint = child_key_outpoint  # (txid_hex, vout)
        self.child_funds = child_funds
        self.rebuild_info = rebuild_info  # сериализуемые параметры rebuild_child (стейт)
        self.first_unconf = 0           # индекс первой неподтверждённой tx

    @property
    def child_fee(self):
        return self.fees[-1]

    @child_fee.setter
    def child_fee(self, v):
        self.fees[-1] = v

    def done(self):
        return self.first_unconf >= len(self.txs)

    def remaining(self):
        return len(self.txs) - self.first_unconf

    def remaining_fees(self):
        return sum(self.fees[self.first_unconf:])

    def remaining_vsize(self):
        return sum(self.vsizes[self.first_unconf:])

    def effective_rate(self):
        """Ancestor-ставка child по неподтверждённому остатку — то, что видит майнер."""
        vs = self.remaining_vsize()
        return self.remaining_fees() / vs if vs else 0.0


def make_rebuild(info, key, spk):
    """Замыкание rebuild_child из сериализуемых параметров (для стейта/подхвата)."""
    if info["kind"] == "classic":
        t, v, val = info["prev"]
        return lambda fee: build_signed_tx(bytes.fromhex(t), v, val, fee, key, spk)
    kp, ap = info["key_prev"], info["anc_prev"]
    return lambda fee: build_sweep_tx(bytes.fromhex(kp[0]), kp[1], kp[2],
                                      bytes.fromhex(ap[0]), ap[1], ap[2], fee, key, spk)


def chain_state_classic(utxo, txs, parent_fee, child_fee, key, spk) -> Chain:
    """Chain для классической цепочки: child тратит vout0 предпоследней tx
    (или исходный UTXO при N_MINTS=1)."""
    n = len(txs)
    if n >= 2:
        prev = (txs[-2].txid().hex(), 0, txs[-2].vout[0].value)
    else:
        prev = (utxo["txid"], utxo["vout"], utxo["value"])
    info = {"kind": "classic", "prev": list(prev)}
    fees = [parent_fee] * (n - 1) + [child_fee]
    vsizes = [vsize(t) for t in txs]
    return Chain(txs, fees, vsizes, make_rebuild(info, key, spk),
                 (prev[0], prev[1]), prev[2], info)


def build_chain_hybrid(utxo_txid: bytes, utxo_vout: int, utxo_value: int,
                       n: int, shield_links: int, shield_fee: int,
                       pivot_fee: int, child_fee: int, key, spk) -> Chain:
    """Пакет «как у живого конкурента» (bc1qmcvv…): shield_links классических
    ключевых P2WPKH-звеньев -> pivot (тратит последнее звено, а не UTXO) ->
    (n-shield_links-2) бесключевых анкеров -> sweep-child.

    ЗАЧЕМ ЩИТ: у pivot'а есть НЕподтверждённый предок (звено), поэтому burn-бот
    не может выдернуть его в блок парой 1P1C (у той только 1 родитель+1 ребёнок),
    а на full-relay узлах угон анкера блокирует BIP125 (заменить надо весь
    sub-chain с дорогим sweep). Именно поэтому конкурента не грабят, а нас — да."""
    assert n >= shield_links + 3, "нужно >= shield_links + 3 tx (щиты + pivot + анкер + child)"
    k = n - shield_links - 2

    # --- цепочка ключевых щитов ---
    txs = []
    prev_txid, prev_vout, prev_value = utxo_txid, utxo_vout, utxo_value
    for _ in range(shield_links):
        s = build_signed_tx(prev_txid, prev_vout, prev_value, shield_fee, key, spk)
        txs.append(s)
        prev_txid, prev_vout, prev_value = s.txid(), 0, s.vout[0].value

    # --- pivot тратит vout0 последнего щита (или UTXO, если щитов нет) ---
    anchor_start = ANCHOR_FEE * k + ANCHOR_RESIDUAL
    pivot = build_pivot_tx(prev_txid, prev_vout, prev_value, pivot_fee,
                           anchor_start, key, spk)
    txs.append(pivot)

    # --- бесключевые анкеры ---
    prev_txid, prev_vout, prev_value = pivot.txid(), 1, anchor_start  # анкер pivot'а — vout1
    for _ in range(k):
        a = build_anchor_tx(prev_txid, prev_vout, prev_value)
        txs.append(a)
        prev_txid, prev_vout, prev_value = a.txid(), 0, a.vout[0].value

    # --- sweep-child (ключевой вход pivot-change + последний анкер) ---
    info = {"kind": "anchor",
            "key_prev": [pivot.txid().hex(), 0, pivot.vout[0].value],
            "anc_prev": [prev_txid.hex(), prev_vout, prev_value]}
    rebuild = make_rebuild(info, key, spk)
    txs.append(rebuild(child_fee))
    fees = [shield_fee] * shield_links + [pivot_fee] + [ANCHOR_FEE] * k + [child_fee]
    vsizes = [vsize(t) for t in txs]
    return Chain(txs, fees, vsizes, rebuild, (info["key_prev"][0], 0),
                 pivot.vout[0].value + prev_value, info)


def build_chain_anchor(utxo_txid: bytes, utxo_vout: int, utxo_value: int,
                       n: int, pivot_fee: int, child_fee: int, key, spk) -> Chain:
    """Чистый анкорный пакет БЕЗ щита (pivot тратит подтверждённый UTXO) —
    уязвим к угону анкера через 1P1C. Оставлен для тестов/совместимости;
    в бою используется build_chain_hybrid."""
    return build_chain_hybrid(utxo_txid, utxo_vout, utxo_value, n, 0,
                              0, pivot_fee, child_fee, key, spk)


# ---------------- стейт и подхват (пункт 4) ---------------

def save_state(chain):
    """json-стейт пакета: рестарт скрипта продолжит монитор с этого места."""
    if chain.rebuild_info is None:
        return
    data = {
        "txs": [t.serialize().hex() for t in chain.txs],
        "fees": chain.fees,
        "rebuild": chain.rebuild_info,
        "child_key_outpoint": list(chain.child_key_outpoint),
        "child_funds": chain.child_funds,
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def clear_state():
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


def chain_from_state(state, key, spk) -> Chain:
    txs = [Transaction.parse(bytes.fromhex(h)) for h in state["txs"]]
    info = state["rebuild"]
    return Chain(txs, state["fees"], [vsize(t) for t in txs],
                 make_rebuild(info, key, spk),
                 tuple(state["child_key_outpoint"]), state["child_funds"], info)


def adopt_mempool_chain(addr, key, spk):
    """Подхват нашего неподтверждённого пакета из мемпула, когда стейта нет
    (осиротевший запуск старой версией / потерянный файл). Восстанавливает
    Chain: порядок топосортировкой, комиссии из API, rebuild по входам child."""
    mem = api_get(f"/address/{addr}/txs/mempool") or []
    if not mem:
        return None
    byid = {t["txid"]: t for t in mem}
    # адресный эндпоинт не видит бесключевые анкерные звенья (они не касаются
    # адреса) — дотягиваем невидимых неподтверждённых предков по входам
    queue = [v["txid"] for t in mem for v in t["vin"] if v["txid"] not in byid]
    while queue:
        txid = queue.pop()
        if txid in byid:
            continue
        tj = api_get(f"/tx/{txid}")
        if not tj or (tj.get("status") or {}).get("confirmed", True):
            continue  # подтверждённый вход — граница пакета
        byid[txid] = tj
        queue.extend(v["txid"] for v in tj["vin"] if v["txid"] not in byid)
    ordered, placed = [], set()
    pool = list(byid.values())
    while pool:
        progressed = False
        for t in list(pool):
            if all(v["txid"] in placed for v in t["vin"] if v["txid"] in byid):
                ordered.append(t)
                placed.add(t["txid"])
                pool.remove(t)
                progressed = True
        if not progressed:
            log("[!] мемпул-набор не выстраивается в один пакет — подхват пропущен")
            return None
    child_j = ordered[-1]
    spk_hex = spk.data.hex()
    key_in = anc_in = None
    for v in child_j["vin"]:
        pv = v.get("prevout") or {}
        item = [v["txid"], v["vout"], pv.get("value", 0)]
        if pv.get("scriptpubkey") == spk_hex:
            key_in = item
        else:
            anc_in = item
    if key_in is None:
        log("[!] у последней tx пакета нет нашего ключевого входа — подхват пропущен")
        return None
    txs = []
    for t in ordered:
        raw = api_get(f"/tx/{t['txid']}/hex", want_json=False)
        if not raw:
            log(f"[!] не получил hex {t['txid'][:16]} — подхват пропущен")
            return None
        txs.append(Transaction.parse(bytes.fromhex(raw)))
    if anc_in:
        info = {"kind": "anchor", "key_prev": key_in, "anc_prev": anc_in}
    else:
        info = {"kind": "classic", "prev": key_in}
    fees = [t["fee"] for t in ordered]
    vsizes = [vsize(t) for t in txs]
    ch = Chain(txs, fees, vsizes, make_rebuild(info, key, spk),
               (key_in[0], key_in[1]), key_in[2] + (anc_in[2] if anc_in else 0), info)
    log(f"Подхвачен пакет из мемпула: {len(txs)} tx, {sum(fees)} sat "
        f"(~{sum(fees) / sum(vsizes):.2f} sat/vB)")
    return ch


# ----------------------- авто-бамп ------------------------

def needs_bump(eff_rate, threshold):
    """Превентивно: ставка обязана держаться не ниже порога входа + BUMP_AHEAD."""
    return eff_rate < threshold + BUMP_AHEAD


def compute_bump_fee(chain, threshold):
    """(новая комиссия child | None, причина отказа).
    Подъём до threshold + BUMP_AHEAD; снизу BIP125-минимум, сверху потолок
    MAX_FEERATE (зажимаем, а не отказываем) и пол по change."""
    target_rate = min(threshold + BUMP_AHEAD, MAX_FEERATE)
    rem_vs = chain.remaining_vsize()
    others = chain.remaining_fees() - chain.child_fee   # комиссии родителей остатка
    new_fee = math.ceil(rem_vs * target_rate) - others
    # BIP125 rules 3+4: строго больше старой, доплата >= 1 sat/vB за vsize замены;
    # +2 — запас на DER-подпись замены длиннее пробной на байт
    min_fee = chain.child_fee + chain.vsizes[-1] + 2
    max_fee = math.floor(rem_vs * MAX_FEERATE) - others
    if min_fee > max_fee:
        return None, f"потолок MAX_FEERATE={MAX_FEERATE} sat/vB достигнут"
    new_fee = min(max(new_fee, min_fee), max_fee)
    if chain.child_funds - new_fee < MIN_CHANGE:
        return None, f"change упал бы ниже {MIN_CHANGE} sat"
    return new_fee, ""


def bump_child(chain, new_fee):
    """RBF-замена child: те же входы, та же структура выходов, выше комиссия."""
    tx = chain.rebuild_child(new_fee)
    ok, text = broadcast_tx(tx)
    if not ok:
        log(f"[!] RBF отклонён: {text}")
        return False
    old_rate = chain.effective_rate()
    chain.txs[-1] = tx
    chain.child_fee = new_fee
    chain.vsizes[-1] = vsize(tx)
    save_state(chain)   # рестарт должен знать актуального child
    log(f"RBF: пакет {old_rate:.2f} -> {chain.effective_rate():.2f} sat/vB, "
        f"новый child {tx.txid().hex()}")
    return True


def adopt_confirmed_spender(chain):
    """Гонка RBF: ключевой вход child потрачен другой ПОДТВЕРЖДЁННОЙ транзакцией.
    Ключ только у нас, значит это старая версия child, успевшая в блок до
    замены, — минт состоялся, пакет фактически завершён."""
    ptxid_hex, pvout = chain.child_key_outpoint
    sp = api_get(f"/tx/{ptxid_hex}/outspend/{pvout}") or {}
    if (sp.get("spent") and (sp.get("status") or {}).get("confirmed")
            and sp.get("txid") != chain.txs[-1].txid().hex()):
        log(f"вход child потрачен подтверждённой tx {sp.get('txid')} "
            f"(старая RBF-версия) — минт состоялся")
        chain.first_unconf = len(chain.txs)
        return True
    return False


def package_is_dead(chain):
    """Вход первой неподтверждённой tx пакета потрачен ЧУЖОЙ ПОДТВЕРЖДЁННОЙ tx
    (напр., P2A-анкер увёл burn-бот) — цепочку уже не восстановить и не сбампить.
    Только подтверждённый чужой расход считаем смертью: неподтверждённый конфликт
    ещё может отвалиться в нашу пользу."""
    i = chain.first_unconf
    if i >= len(chain.txs):
        return False
    v = chain.txs[i].vin[0]
    sp = api_get(f"/tx/{v.txid.hex()}/outspend/{v.vout}") or {}
    ours = {t.txid().hex() for t in chain.txs}
    if (sp.get("spent") and (sp.get("status") or {}).get("confirmed")
            and sp.get("txid") not in ours):
        log(f"[!] вход {v.txid.hex()[:16]}:{v.vout} потрачен чужой подтверждённой "
            f"tx {sp.get('txid')} — пакет мёртв (анкер угнан?)")
        return True
    return False


def resync_confirmations(chain):
    """Первый неподтверждённый индекс бинпоиском (5 запросов на 25 tx вместо 25).
    В цепочке подтверждение tx k означает подтверждение всех её предков,
    поэтому статусы монотонны — свойство сохраняется и после reorg."""
    lo, hi = 0, len(chain.txs)
    while lo < hi:
        mid = (lo + hi) // 2
        st = tx_status(chain.txs[mid].txid().hex())
        if st and st.get("confirmed"):
            lo = mid + 1
        else:
            hi = mid
    chain.first_unconf = lo
    return lo


def ensure_in_mempool(chain):
    """Ребродкаст неподтверждённых, выпавших из мемпула (purge/обрыв бродкаста)."""
    for i in range(chain.first_unconf, len(chain.txs)):
        if tx_status(chain.txs[i].txid().hex()) is not None:
            continue
        ok, text = broadcast_tx(chain.txs[i])
        if ok:
            log(f"  ребродкаст {i + 1}/{len(chain.txs)}: {chain.txs[i].txid().hex()}")
        else:
            low = text.lower()
            if "min relay" in low or "min fee" in low or "mempoolminfee" in low:
                # мемпул переполнен, floor выше ставки родителей: RBF child это
                # не лечит — нужна пересборка цепочки с большим PARENT_FEERATE
                log("[!] мемпул очищен, floor выше ставки родителей — перезапусти "
                    "скрипт с большим PARENT_FEERATE")
            log(f"[!] ребродкаст {i + 1}/{len(chain.txs)} не прошёл: {text}")
            return False
    return True


def monitor_and_bump(chain):
    """Цикл до полного подтверждения пакета. Каждый тик: высота, порог, решение.
    Ставка держится >= порог + BUMP_AHEAD (превентивно); бамп не чаще BUMP_COOLDOWN."""
    n = len(chain.txs)
    log(f"Монитор: опрос каждые {POLL_INTERVAL} с, потолок {MAX_FEERATE} sat/vB. "
        f"Ctrl+C — выход, пакет останется в мемпуле.")
    last_height = None
    last_bump_ts = 0.0
    # True с первого тика: подтверждения могли прийти уже во время бродкаста.
    # Сбрасывается только после УСПЕШНОГО ресинка — упавший тик не теряет событие,
    # иначе бамп посчитает комиссию на устаревший (слишком большой) остаток пакета.
    need_sync = True
    while True:
        try:
            height = tip_height()
            if last_height is not None and height != last_height:
                need_sync = True
            last_height = height

            if need_sync:
                resync_confirmations(chain)
                if chain.done():
                    log(f"[OK] Подтверждено {n}/{n} (высота <= {height}).")
                    return "confirmed"
                if package_is_dead(chain):
                    log(f"[X] Пакет мёртв на {chain.first_unconf}/{n} — "
                        f"бросаю, деньги на подтверждённом change целы.")
                    return "dead"
                # child мог выпасть при purge, не дойти при обрыве бродкаста
                # или проиграть гонку своей же старой RBF-версии
                if tx_status(chain.txs[-1].txid().hex()) is None:
                    if adopt_confirmed_spender(chain):
                        log(f"[OK] Подтверждено {n}/{n} (старой версией child).")
                        return "confirmed"
                    log("child не найден в мемпуле — ребродкаст остатка цепочки")
                    ensure_in_mempool(chain)
                need_sync = False

            threshold, src = next_block_threshold()
            eff = chain.effective_rate()
            thr_s = f"{threshold:.2f}" if threshold is not None else "?"
            log(f"блок {height}, подтверждено {chain.first_unconf}/{n}, "
                f"пакет {eff:.2f} sat/vB, порог {thr_s} ({src})")

            if (threshold is not None and needs_bump(eff, threshold)
                    and time.time() - last_bump_ts >= BUMP_COOLDOWN):
                fee, why = compute_bump_fee(chain, threshold)
                if fee is None:
                    log(f"бамп невозможен: {why} — ждём")
                elif bump_child(chain, fee):
                    last_bump_ts = time.time()
        except (SystemExit, Exception) as e:
            # SystemExit тоже гасим: монитор не должен умереть, пока пакет висит
            log(f"[!] тик пропущен: {e}")
        time.sleep(POLL_INTERVAL)


# ------------------------- main ---------------------------

def build_package(u, key, spk) -> Chain:
    """План комиссий (динамическая ставка) + сборка пакета из UTXO. Печатает расчёт."""
    # --- стартовая ставка пакета (пункт 2: динамика от сети) ---
    if DYNAMIC_START:
        start_rate, start_src = compute_start_rate(network_median_rate())
    else:
        start_rate, start_src = TARGET_FEERATE, f"TARGET_FEERATE={TARGET_FEERATE} (ручной)"
    print(f"Стартовая ставка пакета: {start_rate:.2f} sat/vB ({start_src})")

    # --- план комиссий и сборка по режиму ---
    utxo_txid = bytes.fromhex(u["txid"])
    mode = CHAIN_MODE if N_MINTS >= 3 else "classic"
    if mode in ("hybrid", "anchor"):
        s = SHIELD_LINKS if mode == "hybrid" else 0
        s = min(s, max(0, N_MINTS - 3))         # хватит места на pivot+анкер+child
        if mode == "anchor":
            print("[!] ВНИМАНИЕ: anchor БЕЗ щита — pivot тратит подтверждённый UTXO и "
                  "его могут выдернуть burn-парой 1P1C. Для боя ставь CHAIN_MODE='hybrid'.")
        # пробные сборки для vsize каждой формы tx
        sv = vsize(build_signed_tx(utxo_txid, u["vout"], u["value"], 500, key, spk))
        pv = vsize(build_pivot_tx(utxo_txid, u["vout"], u["value"], 500, 1000, key, spk))
        av = vsize(build_anchor_tx(utxo_txid, 0, 1000))
        cv = vsize(build_sweep_tx(utxo_txid, 0, u["value"], bytes(32), 0,
                                  ANCHOR_RESIDUAL, 500, key, spk))
        k = N_MINTS - s - 2
        shield_fee, pivot_fee, child_fee = plan_fees_hybrid(sv, pv, av, cv, s, k, start_rate)
        package_vsize = sv * s + pv + av * k + cv
        total_fee = shield_fee * s + pivot_fee + ANCHOR_FEE * k + child_fee
        print(f"Режим {mode}: {s} щит x ~{sv} + pivot ~{pv} + {k} анкер x {av} + "
              f"child ~{cv} vB = ~{package_vsize} vB ({package_vsize / N_MINTS:.0f} vB/минт)")
        print(f"Комиссии: {s}x{shield_fee} + pivot {pivot_fee} + {k}x{ANCHOR_FEE} + "
              f"child {child_fee} = {total_fee} sat "
              f"(~{total_fee / package_vsize:.2f} sat/vB эффективно)")
    else:
        probe = build_signed_tx(utxo_txid, u["vout"], u["value"], 500, key, spk)
        tx_vsize = vsize(probe)
        parent_fee, child_fee = plan_fees(tx_vsize, N_MINTS, PARENT_FEERATE, start_rate)
        package_vsize = tx_vsize * N_MINTS
        total_fee = parent_fee * (N_MINTS - 1) + child_fee
        print(f"Режим classic: vsize одной tx ~{tx_vsize} vB, пакет ~{package_vsize} vB")
        print(f"Комиссии: {N_MINTS - 1} x {parent_fee} sat + финальная {child_fee} sat "
              f"= {total_fee} sat (~{total_fee / package_vsize:.2f} sat/vB эффективно)")

    # потолок расходов авто-бампа известен до запуска
    worst_fee = math.ceil(package_vsize * MAX_FEERATE)
    if MONITOR:
        print(f"Авто-бамп: потолок {MAX_FEERATE} sat/vB -> максимум {worst_fee} sat комиссий")
        if u["value"] < worst_fee + MIN_CHANGE:
            real_cap = (u["value"] - MIN_CHANGE) / package_vsize
            print(f"[!] на бамп до потолка не хватит средств: фактический потолок "
                  f"~{real_cap:.2f} sat/vB")
    threshold, src = next_block_threshold()
    if threshold is not None:
        print(f"Порог следующего блока сейчас: ~{threshold:.2f} sat/vB ({src})")

    if u["value"] < total_fee + MIN_CHANGE:
        raise SystemExit(f"Нужно минимум {total_fee + MIN_CHANGE} sat, есть {u['value']}")

    # --- строим цепочку ---
    if mode in ("hybrid", "anchor"):
        chain = build_chain_hybrid(utxo_txid, u["vout"], u["value"],
                                   N_MINTS, s, shield_fee, pivot_fee, child_fee, key, spk)
    else:
        txs_c = build_chain(utxo_txid, u["vout"], u["value"],
                            N_MINTS, parent_fee, child_fee, key, spk)
        chain = chain_state_classic(u, txs_c, parent_fee, child_fee, key, spk)
    print(f"\nПостроено {len(chain.txs)} транзакций, остаток после цепочки: "
          f"{chain.txs[-1].vout[0].value} sat")
    for i, tx in enumerate(chain.txs):
        print(f"  {i + 1:2d}. {tx.txid().hex()}")
    return chain


def broadcast_package(chain):
    """Бродкаст свежего пакета по порядку. Транзиентный inputs-missing (родитель
    ещё не распространился) -> ретрай с паузой: НАДО довести sweep до мемпула,
    иначе анкер защищён лишь дешёвыми анкер-фи и его снайпит burn-бот."""
    n = len(chain.txs)
    for i, tx in enumerate(chain.txs):
        ok, text = False, ""
        for attempt in range(BROADCAST_RETRIES):
            ok, text = broadcast_tx(tx)
            if ok:
                break
            low = text.lower()
            transient = ("miss" in low or "spent" in low or "not found" in low)
            if not transient:
                break                       # ошибка политики — ретрай не поможет
            if attempt < BROADCAST_RETRIES - 1:
                log(f"  tx {i + 1}/{n}: {text} — ретрай {attempt + 1}/{BROADCAST_RETRIES - 1}")
                time.sleep(BROADCAST_RETRY_DELAY)
        if ok:
            print(f"  отправлена {i + 1}/{n}: {text}")
            continue
        # не смогли отправить tx i
        print(f"ОШИБКА на tx {i + 1}: {text}")
        if i == 0:
            clear_state()
            raise SystemExit("Ничего не отправлено. Если мемпул переполнен и floor "
                             "выше минимума — подними PARENT_FEERATE и перезапусти.")
        # дальше по цепочке родитель отсутствует — остальные тоже не сядут; sweep
        # не отправлен -> анкер уязвим. Монитор признает пакет мёртвым и пересоберёт.
        print(f"[!] хвост {i + 1}..{n} (включая sweep) не отправлен — анкер без защиты; "
              "монитор проверит на угон и пересоберёт" if MONITOR else
              f"[!] хвост {i + 1}..{n} не отправлен; MONITOR=False — перезапусти скрипт")
        break


def mint_once(key, spk, addr) -> bool:
    """Один цикл минта: resume из стейта | подхват из мемпула | новый пакет,
    затем монитор до подтверждения. True — пакет подтверждён, можно следующий."""
    chain = None
    if not DRY_RUN:
        state = load_state()
        if state:
            chain = chain_from_state(state, key, spk)
            log(f"Резюмирую пакет из {STATE_FILE}: {len(chain.txs)} tx")
            resync_confirmations(chain)
            if chain.done():
                log("Пакет из стейта уже подтверждён.")
                clear_state()
                return True
            if package_is_dead(chain):
                log("[X] Стейт указывает на мёртвый пакет (анкер угнан?) — "
                    "отбрасываю, строю новый из подтверждённого change.")
                clear_state()
                chain = None
            else:
                ensure_in_mempool(chain)   # дошлём то, что не дошло до рестарта

    if chain is None:
        utxos = api_get(f"/address/{addr}/utxo") or []
        confirmed = [x for x in utxos if x["status"]["confirmed"]]
        if not confirmed:
            if DRY_RUN and utxos:
                u = max(utxos, key=lambda x: x["value"])
                print("[!] подтверждённых UTXO нет — DRY_RUN-превью на неподтверждённом")
            else:
                chain = None if DRY_RUN else adopt_mempool_chain(addr, key, spk)
                if chain is None:
                    raise SystemExit("Нет подтверждённых UTXO и нечего подхватывать "
                                     "(пакет в мемпуле? дождись подтверждения)")
                save_state(chain)
                u = None
        else:
            u = max(confirmed, key=lambda x: x["value"])
        if chain is None:
            print(f"UTXO: {u['txid']}:{u['vout']}  {u['value']} sat")
            chain = build_package(u, key, spk)
            if DRY_RUN:
                print("\nDRY_RUN=True — бродкаст пропущен. Hex последней tx для проверки:")
                print(chain.txs[-1].serialize().hex())
                return False
            save_state(chain)          # до бродкаста: упадём — рестарт дошлёт
            broadcast_package(chain)

    print(f"\nПакет: https://mempool.space/tx/{chain.txs[-1].txid().hex()}?cpfp=true")
    if not MONITOR:
        return False
    outcome = monitor_and_bump(chain)  # "confirmed" | "dead"
    clear_state()
    # "dead": анкер угнан, но подтверждённый change цел — в AUTO_MODE строим
    # следующий пакет из него (тот же смысл «продолжаем»), что и при confirmed
    return outcome in ("confirmed", "dead")


def main():
    assert 1 <= N_MINTS <= 25, "N_MINTS должен быть 1..25 (лимит предков в мемпуле)"
    if not WIF:
        raise SystemExit("No key: set env UG_WIF to a compressed WIF for a bc1q P2WPKH address "
                         "(PowerShell: $env:UG_WIF=\"your_wif\"). Never paste WIF into chat.")
    key = ec.PrivateKey.from_wif(WIF)
    spk = script.p2wpkh(key.get_public_key())
    addr = spk.address(NET)
    print(f"Адрес: {addr}")
    try:
        if AUTO_MODE and not DRY_RUN:
            cycle = 1
            while True:
                log(f"=== Цикл {cycle}: пакет {N_MINTS} минтов ===")
                if not mint_once(key, spk, addr):
                    break              # MONITOR=False: дальше сами
                cycle += 1             # подтвердилось -> change свежий, сразу следующий
        else:
            mint_once(key, spk, addr)
    except KeyboardInterrupt:
        log("Остановлено. Незавершённый пакет остаётся в мемпуле; "
            "перезапуск продолжит монитор из стейта.")


if __name__ == "__main__":
    main()
