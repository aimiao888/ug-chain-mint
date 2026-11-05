#!/usr/bin/env python3
"""Тесты diesel_chain_mint: сборка/подпись цепочки и логика авто-бампа.

Сеть полностью замокана, ключ фиктивный — боевой WIF (DIESEL_WIF) не нужен.
Запуск: python -m unittest test_diesel_chain_mint -v
"""

import os
import re
import tempfile
import unittest
from unittest import mock

import requests
from embit import ec, script

import diesel_chain_mint as m

# фиктивные ключ и UTXO
KEY = ec.PrivateKey(b"\x01" * 32)
SPK = script.p2wpkh(KEY.get_public_key())
FAKE_TXID = bytes(range(32))
UTXO = {"txid": FAKE_TXID.hex(), "vout": 1, "value": 200_000}

# фиксируем конфиг: тесты не должны зависеть от правок констант в скрипте
CFG = {
    "PARENT_FEERATE": 0.1,
    "MIN_RELAY_FEERATE": 0.1,
    "MAX_FEERATE": 10.0,
    "BUMP_AHEAD": 0.05,
    "MIN_CHANGE": 1000,
    "ANCHOR_FEE": 10,
    "ANCHOR_RESIDUAL": 240,
    "SHIELD_LINKS": 2,
    "START_MARGIN": 0.1,
    "BROADCAST_RETRIES": 3,
    "BROADCAST_RETRY_DELAY": 0,
    "STATE_FILE": os.path.join(tempfile.gettempdir(), "test_minter_state.json"),
}
ANCHOR_HEX = "51024e73"  # P2A: OP_1 PUSH2 0x4e73


def with_cfg(cls):
    for k, v in CFG.items():
        cls = mock.patch.object(m, k, v)(cls)
    return cls


def make_chain(n=5, value=200_000, target=3.0):
    """Классическая цепочка из n подписанных tx от фиктивного UTXO + объект Chain."""
    probe = m.build_signed_tx(FAKE_TXID, UTXO["vout"], value, 500, KEY, SPK)
    tx_vsize = m.vsize(probe)
    parent_fee, child_fee = m.plan_fees(tx_vsize, n, m.PARENT_FEERATE, target)
    txs = m.build_chain(FAKE_TXID, UTXO["vout"], value, n, parent_fee, child_fee, KEY, SPK)
    return m.chain_state_classic(dict(UTXO, value=value), txs, parent_fee, child_fee, KEY, SPK)


@with_cfg
class BuildTests(unittest.TestCase):
    def test_tx_structure(self):
        tx = m.build_signed_tx(FAKE_TXID, 0, 100_000, 500, KEY, SPK)
        self.assertEqual(len(tx.vout), 2)
        self.assertEqual(tx.vout[0].value, 99_500)
        self.assertEqual(tx.vout[0].script_pubkey.data, SPK.data)
        self.assertEqual(tx.vout[1].value, 0)
        # payload байт-в-байт, порядок выходов: vout0 change, vout1 OP_RETURN
        self.assertEqual(tx.vout[1].script_pubkey.data, bytes.fromhex(m.OP_RETURN_HEX))
        self.assertEqual(tx.vin[0].sequence, 0xFFFFFFFD)  # сигнал RBF
        self.assertEqual(len(tx.vin[0].witness.items), 2)
        self.assertTrue(100 < m.vsize(tx) < 200)

    def test_dust_guard(self):
        with self.assertRaises(SystemExit):
            m.build_signed_tx(FAKE_TXID, 0, 1_400, 500, KEY, SPK)  # change 900 < 1000

    def test_plan_fees_reaches_target(self):
        parent, child = m.plan_fees(141, 25, 0.1, 3.0)
        # пол релея 0.1 sat/vB (Core 30+): ceil(141*0.1)+1 = 16, а не 1 sat/vB
        self.assertEqual(parent, 16)
        total = parent * 24 + child
        self.assertGreaterEqual(total / (141 * 25), 3.0)
        self.assertGreaterEqual(child, parent)

    def test_plan_fees_parent_floor_at_min_relay(self):
        # заявленная ставка ниже релей-минимума зажимается к полу с запасом:
        # родитель обязан пройти minrelaytxfee=0.1 sat/vB даже при vsize +1 vB
        parent, _ = m.plan_fees(141, 25, 0.01, 3.0)
        self.assertEqual(parent, 16)
        self.assertGreaterEqual(parent / (141 + 1), m.MIN_RELAY_FEERATE)

    def test_plan_fees_single_tx(self):
        _, child = m.plan_fees(141, 1, 0.1, 3.0)
        self.assertGreaterEqual(child / 141, 3.0)

    def test_chain_links(self):
        ch = make_chain(n=3)
        txs = ch.txs
        self.assertEqual(len(txs), 3)
        self.assertEqual(txs[0].vin[0].txid, FAKE_TXID)
        self.assertEqual(txs[0].vin[0].vout, UTXO["vout"])
        for prev, cur in zip(txs, txs[1:]):
            # каждая tx тратит vout0 предыдущей
            self.assertEqual(cur.vin[0].txid, prev.txid())
            self.assertEqual(cur.vin[0].vout, 0)
        v = UTXO["value"]
        for tx in txs[:-1]:
            self.assertEqual(v - tx.vout[0].value, ch.fees[0])
            v = tx.vout[0].value
        self.assertEqual(v - txs[-1].vout[0].value, ch.child_fee)


@with_cfg
class ChainStateTests(unittest.TestCase):
    def test_effective_rate_full_and_partial(self):
        ch = make_chain(n=5)
        full = ch.effective_rate()
        self.assertAlmostEqual(full, ch.remaining_fees() / ch.remaining_vsize())
        self.assertGreaterEqual(full, 3.0)
        ch.first_unconf = 2  # два дешёвых родителя подтвердились
        self.assertEqual(ch.remaining(), 3)
        self.assertGreater(ch.effective_rate(), full)  # ставка остатка выросла
        ch.first_unconf = 5
        self.assertTrue(ch.done())
        self.assertEqual(ch.effective_rate(), 0.0)

    def test_child_funds_single_tx_spends_utxo(self):
        ch = make_chain(n=1)
        self.assertEqual(ch.child_key_outpoint, (FAKE_TXID.hex(), UTXO["vout"]))
        self.assertEqual(ch.child_funds, UTXO["value"])

    def test_child_funds_chain_spends_last_parent(self):
        ch = make_chain(n=3)
        self.assertEqual(ch.child_key_outpoint, (ch.txs[-2].txid().hex(), 0))
        self.assertEqual(ch.child_funds, ch.txs[-2].vout[0].value)


@with_cfg
class BumpLogicTests(unittest.TestCase):
    def test_needs_bump_proactive_margin(self):
        # превентивно: бампим, когда ставка НЕ дотягивает до порога + BUMP_AHEAD
        self.assertTrue(m.needs_bump(3.0, 3.0))     # ровно на пороге — уже мало
        self.assertTrue(m.needs_bump(3.0, 2.96))    # запас < 0.05
        self.assertFalse(m.needs_bump(3.0, 2.95))   # ровно порог+0.05 — достаточно
        self.assertFalse(m.needs_bump(3.0, 2.5))    # большой запас

    def test_bump_fee_adaptive_jump(self):
        ch = make_chain(n=5)
        fee, why = m.compute_bump_fee(ch, threshold=6.0)
        self.assertIsNotNone(fee, why)
        self.assertGreaterEqual(fee, ch.child_fee + ch.vsizes[-1])  # BIP125 rule 4
        others = ch.remaining_fees() - ch.child_fee
        new_eff = (others + fee) / ch.remaining_vsize()
        self.assertGreaterEqual(new_eff, 6.05 - 1e-9)  # прыжок к порогу + BUMP_AHEAD
        self.assertLessEqual(new_eff, 10.0 + 0.1)

    def test_bump_lands_above_trigger(self):
        # после бампа условие needs_bump при том же пороге должно погаснуть,
        # иначе монитор бампил бы каждый тик
        ch = make_chain(n=5)
        fee, _ = m.compute_bump_fee(ch, threshold=6.0)
        ch.child_fee = fee
        self.assertFalse(m.needs_bump(ch.effective_rate(), 6.0))

    def test_bump_fee_bip125_minimum(self):
        ch = make_chain(n=5)
        # порог ниже текущей ставки: расчётная комиссия меньше старой, но замена
        # обязана доплатить минимум 1 sat/vB за свой размер (+2 — запас на то,
        # что DER-подпись замены может выйти на байт длиннее пробной)
        fee, _ = m.compute_bump_fee(ch, threshold=ch.effective_rate() * 0.5)
        self.assertEqual(fee, ch.child_fee + ch.vsizes[-1] + 2)

    def test_bump_fee_fractional_ceiling_clamps(self):
        # дробный потолок: ceil(запрос) на 1 sat выше floor(потолка) не должен
        # приводить к отказу — комиссию зажимаем к потолку
        ch = make_chain(n=3)
        with mock.patch.object(m, "MAX_FEERATE", 5.501):
            fee, why = m.compute_bump_fee(ch, threshold=50.0)
        self.assertIsNotNone(fee, why)
        others = ch.remaining_fees() - ch.child_fee
        new_eff = (others + fee) / ch.remaining_vsize()
        self.assertLessEqual(new_eff, 5.501)

    def test_bump_fee_ceiling(self):
        ch = make_chain(n=5)
        fee, _ = m.compute_bump_fee(ch, threshold=50.0)  # порог выше потолка
        self.assertIsNotNone(fee)  # первый бамп упирается ровно в MAX_FEERATE
        ch.child_fee = fee
        fee2, why2 = m.compute_bump_fee(ch, threshold=50.0)
        self.assertIsNone(fee2)
        self.assertIn("MAX_FEERATE", why2)

    def test_bump_fee_change_floor(self):
        ch = make_chain(n=2, value=3_000)  # денег впритык
        fee, why = m.compute_bump_fee(ch, threshold=50.0)
        self.assertIsNone(fee)
        self.assertIn("change", why)


@mock.patch.object(m, "log", new=lambda *a: None)  # тихий вывод тестов
@with_cfg
class BumpChildTests(unittest.TestCase):
    def test_replacement_same_input_higher_fee(self):
        ch = make_chain(n=3)
        old_child = ch.txs[-1]
        new_fee = ch.child_fee + ch.vsizes[-1] + 10
        with mock.patch.object(m, "broadcast_tx", return_value=(True, "txid")):
            self.assertTrue(m.bump_child(ch, new_fee))
        new_child = ch.txs[-1]
        self.assertNotEqual(new_child.txid(), old_child.txid())
        # тот же вход, та же структура выходов, только комиссия выше
        self.assertEqual(new_child.vout[0].script_pubkey.data, SPK.data)
        self.assertEqual(new_child.vin[0].txid, old_child.vin[0].txid)
        self.assertEqual(new_child.vin[0].vout, old_child.vin[0].vout)
        self.assertEqual(ch.child_funds - new_child.vout[0].value, new_fee)
        self.assertEqual(new_child.vout[1].script_pubkey.data, bytes.fromhex(m.OP_RETURN_HEX))
        self.assertEqual(ch.child_fee, new_fee)

    def test_broadcast_fail_keeps_state(self):
        ch = make_chain(n=3)
        old_child = ch.txs[-1]
        old_fee = ch.child_fee
        with mock.patch.object(m, "broadcast_tx", return_value=(False, "insufficient fee")):
            self.assertFalse(m.bump_child(ch, old_fee + 500))
        self.assertIs(ch.txs[-1], old_child)
        self.assertEqual(ch.child_fee, old_fee)


@mock.patch.object(m, "log", new=lambda *a: None)  # тихий вывод тестов
@with_cfg
class ResyncTests(unittest.TestCase):
    def _statuses(self, ch, confirmed_upto):
        conf = {ch.txs[i].txid().hex() for i in range(confirmed_upto)}
        return lambda txid_hex: {"confirmed": txid_hex in conf}

    def test_resync_prefix(self):
        ch = make_chain(n=5)
        for k in (0, 2, 5):
            ch.first_unconf = 0
            with mock.patch.object(m, "tx_status", side_effect=self._statuses(ch, k)) as p:
                self.assertEqual(m.resync_confirmations(ch), k)
                self.assertLessEqual(p.call_count, 4)  # бинпоиск, не 25 запросов
            self.assertEqual(ch.first_unconf, k)

    def test_rebroadcast_missing_only(self):
        ch = make_chain(n=4)
        present = {ch.txs[i].txid().hex() for i in (0, 1)}
        sent = []

        def fake_status(txid_hex):
            return {"confirmed": False} if txid_hex in present else None

        def fake_broadcast(tx):
            sent.append(tx.txid().hex())
            return True, "ok"

        with mock.patch.object(m, "tx_status", side_effect=fake_status), \
             mock.patch.object(m, "broadcast_tx", side_effect=fake_broadcast):
            self.assertTrue(m.ensure_in_mempool(ch))
        # дошлёт только выпавшие, по порядку цепочки
        self.assertEqual(sent, [ch.txs[2].txid().hex(), ch.txs[3].txid().hex()])


@with_cfg
class NetworkParsingTests(unittest.TestCase):
    def _resp(self, payload=None, code=200, text=""):
        r = mock.Mock()
        r.status_code = code
        r.text = text
        r.json.return_value = payload
        return r

    def test_threshold_full_projected_block(self):
        blocks = [{"blockVSize": 998_000, "feeRange": [4.2, 5.0, 8.0], "medianFee": 6.0}]
        with mock.patch.object(m.SESSION, "get", return_value=self._resp(blocks)):
            rate, src = m.next_block_threshold()
        # feeRange[0] бывает выбросом (CPFP-хвост), берём feeRange[1]
        self.assertEqual(rate, 5.0)
        self.assertEqual(src, "mempool-blocks")

    def test_threshold_single_element_feerange(self):
        blocks = [{"blockVSize": 998_000, "feeRange": [3.3], "medianFee": 3.3}]
        with mock.patch.object(m.SESSION, "get", return_value=self._resp(blocks)):
            rate, _ = m.next_block_threshold()
        self.assertEqual(rate, 3.3)

    def test_threshold_small_mempool(self):
        # мемпул меньше блока: в след. блок войдёт всё >= минимума релея — честный
        # порог 0.1, а не 1.0 (иначе монитор зря бампил бы дешёвый пакет)
        blocks = [{"blockVSize": 300_000, "feeRange": [1.0, 2.0], "medianFee": 1.5}]
        with mock.patch.object(m.SESSION, "get", return_value=self._resp(blocks)):
            rate, _ = m.next_block_threshold()
        self.assertEqual(rate, m.MIN_RELAY_FEERATE)

    def test_network_median_rate(self):
        blocks = [{"blockVSize": 300_000, "feeRange": [0.1, 2.0], "medianFee": 0.5}]
        with mock.patch.object(m.SESSION, "get", return_value=self._resp(blocks)):
            self.assertEqual(m.network_median_rate(), 0.5)
        # медиана ниже релей-минимума зажимается к полу
        blocks = [{"blockVSize": 100_000, "feeRange": [0.05], "medianFee": 0.05}]
        with mock.patch.object(m.SESSION, "get", return_value=self._resp(blocks)):
            self.assertEqual(m.network_median_rate(), m.MIN_RELAY_FEERATE)
        with mock.patch.object(m.SESSION, "get", side_effect=requests.ConnectionError("down")):
            self.assertIsNone(m.network_median_rate())

    def test_start_rate_median_plus_margin(self):
        # пункт 2: старт = медиана сети + START_MARGIN (0.5 -> 0.6)
        rate, _ = m.compute_start_rate(0.5)
        self.assertAlmostEqual(rate, 0.6)

    def test_start_rate_clamped_to_max(self):
        rate, _ = m.compute_start_rate(50.0)
        self.assertEqual(rate, m.MAX_FEERATE)

    def test_start_rate_fallback_manual(self):
        # сеть недоступна -> ручной TARGET_FEERATE
        with mock.patch.object(m, "TARGET_FEERATE", 1.7):
            rate, src = m.compute_start_rate(None)
        self.assertEqual(rate, 1.7)
        self.assertIn("TARGET_FEERATE", src)

    def test_threshold_fallback_recommended(self):
        def fake_get(url, timeout=15):
            if "mempool-blocks" in url:
                raise requests.ConnectionError("down")
            return self._resp({"fastestFee": 7})

        with mock.patch.object(m.SESSION, "get", side_effect=fake_get):
            rate, src = m.next_block_threshold()
        self.assertEqual(rate, 7.0)
        self.assertEqual(src, "fees/recommended")

    def test_threshold_all_down(self):
        with mock.patch.object(m.SESSION, "get", side_effect=requests.ConnectionError("down")):
            rate, _ = m.next_block_threshold()
        self.assertIsNone(rate)

    def test_broadcast_already_in_mempool_is_success(self):
        tx = make_chain(n=1).txs[0]
        resp = self._resp(code=400, text="sendrawtransaction: txn-already-in-mempool")
        with mock.patch.object(m.SESSION, "post", return_value=resp):
            ok, _ = m.broadcast_tx(tx)
        self.assertTrue(ok)

    def test_broadcast_policy_error_no_second_try(self):
        tx = make_chain(n=1).txs[0]
        post = mock.Mock(return_value=self._resp(code=400, text="min relay fee not met"))
        with mock.patch.object(m.SESSION, "post", post):
            ok, text = m.broadcast_tx(tx)
        self.assertFalse(ok)
        self.assertEqual(post.call_count, 1)  # ошибка политики одинакова на всех узлах
        self.assertIn("relay", text)

    def test_broadcast_infra_error_uses_fallback(self):
        # 503/429 — инфраструктура, не политика: обязан попробовать второй узел
        tx = make_chain(n=1).txs[0]
        post = mock.Mock(side_effect=[self._resp(code=503, text="<html>gateway</html>"),
                                      self._resp(code=200, text="txid")])
        with mock.patch.object(m.SESSION, "post", post):
            ok, _ = m.broadcast_tx(tx)
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 2)

    def test_tx_status_absent_returns_none(self):
        # /tx/{txid}/status отдаёт 200 {"confirmed":false} даже для эвикнутых/
        # несуществующих tx — presence детектим по полному /tx/{txid} (404=нет)
        with mock.patch.object(m, "api_get", return_value=None) as g:
            self.assertIsNone(m.tx_status("ab" * 32))
        self.assertEqual(g.call_args[0][0], f"/tx/{'ab' * 32}")  # полный, не /status

    def test_tx_status_present_returns_status(self):
        full = {"txid": "ab" * 32, "status": {"confirmed": True, "block_height": 900_000}}
        with mock.patch.object(m, "api_get", return_value=full):
            st = m.tx_status("ab" * 32)
        self.assertEqual(st, full["status"])
        self.assertTrue(st["confirmed"])


@mock.patch.object(m, "log", new=lambda *a: None)  # тихий вывод тестов
@with_cfg
class DeadPackageTests(unittest.TestCase):
    """P2A-угон: вход первой неподтверждённой tx потрачен ЧУЖОЙ подтверждённой
    транзакцией (burn-бот увёл анкер) — пакет мёртв, монитор не должен крутиться."""

    def test_dead_when_input_spent_by_foreign_confirmed(self):
        ch = m.build_chain_anchor(FAKE_TXID, UTXO["vout"], 200_000, 5, 17, 2_000, KEY, SPK)
        ch.first_unconf = 1                       # pivot подтверждён, анкер-звено — нет
        anchor_in = ch.txs[1].vin[0]              # тратит анкер pivot'а (vout1)
        spend = {"spent": True, "txid": "cd" * 32, "status": {"confirmed": True}}

        def fake(path, want_json=True):
            if path == f"/tx/{anchor_in.txid.hex()}/outspend/{anchor_in.vout}":
                return spend
            return None
        with mock.patch.object(m, "api_get", side_effect=fake):
            self.assertTrue(m.package_is_dead(ch))

    def test_alive_when_input_spent_by_our_own_tx(self):
        ch = make_chain(n=3)
        ch.first_unconf = 1
        our_in = ch.txs[1].vin[0]
        spend = {"spent": True, "txid": ch.txs[1].txid().hex(),
                 "status": {"confirmed": True}}
        with mock.patch.object(m, "api_get", return_value=spend):
            self.assertFalse(m.package_is_dead(ch))  # это наша же tx, не угон

    def test_alive_when_foreign_spender_unconfirmed(self):
        ch = make_chain(n=3)
        ch.first_unconf = 1
        spend = {"spent": True, "txid": "cd" * 32, "status": {"confirmed": False}}
        with mock.patch.object(m, "api_get", return_value=spend):
            self.assertFalse(m.package_is_dead(ch))  # неподтверждён — ещё поборемся

    def test_alive_when_unspent(self):
        ch = make_chain(n=3)
        ch.first_unconf = 1
        with mock.patch.object(m, "api_get", return_value={"spent": False}):
            self.assertFalse(m.package_is_dead(ch))

    def test_done_package_not_dead(self):
        ch = make_chain(n=3)
        ch.first_unconf = 3  # всё подтверждено
        self.assertFalse(m.package_is_dead(ch))  # без сетевых вызовов


@with_cfg
class AnchorBuildTests(unittest.TestCase):
    """Структура «как у конкурента»: pivot с P2A-анкером -> цепочка бесключевых
    94-байтных анкерных tx -> sweep-child (2 входа). Образец: кластер 57ff6b38…"""

    def test_pivot_structure(self):
        tx = m.build_pivot_tx(FAKE_TXID, 0, 100_000, 17, 470, KEY, SPK)
        self.assertEqual(len(tx.vout), 3)
        # порядок как у образца f4ceb021: vout0 change, vout1 анкер, vout2 OP_RETURN
        self.assertEqual(tx.vout[0].value, 100_000 - 17 - 470)
        self.assertEqual(tx.vout[0].script_pubkey.data, SPK.data)
        self.assertEqual(tx.vout[1].value, 470)
        self.assertEqual(tx.vout[1].script_pubkey.data, bytes.fromhex(ANCHOR_HEX))
        self.assertEqual(tx.vout[2].value, 0)
        self.assertEqual(tx.vout[2].script_pubkey.data, bytes.fromhex(m.OP_RETURN_HEX))
        self.assertEqual(len(tx.vin[0].witness.items), 2)  # подписан нашим ключом
        self.assertTrue(145 < m.vsize(tx) < 160)

    def test_anchor_tx_is_94_bytes_keyless(self):
        tx = m.build_anchor_tx(FAKE_TXID, 1, 470)
        self.assertEqual(len(tx.vout), 2)
        self.assertEqual(tx.vout[0].value, 470 - m.ANCHOR_FEE)
        self.assertEqual(tx.vout[0].script_pubkey.data, bytes.fromhex(ANCHOR_HEX))
        self.assertEqual(tx.vout[1].value, 0)
        self.assertEqual(tx.vout[1].script_pubkey.data, bytes.fromhex(m.OP_RETURN_HEX))
        # бесключевая трата P2A: пустой witness, чистая legacy-сериализация 94 байта
        self.assertEqual(len(tx.vin[0].witness.items), 0)
        self.assertEqual(len(tx.serialize()), 94)
        self.assertEqual(m.vsize(tx), 94)

    def test_sweep_structure(self):
        anc_txid = bytes([9]) * 32
        tx = m.build_sweep_tx(FAKE_TXID, 0, 50_000, anc_txid, 0, 240, 3_000, KEY, SPK)
        self.assertEqual(len(tx.vin), 2)
        self.assertEqual(tx.vin[0].txid, FAKE_TXID)          # ключевой вход
        self.assertEqual(len(tx.vin[0].witness.items), 2)
        self.assertEqual(tx.vin[1].txid, anc_txid)           # анкер — без подписи
        self.assertEqual(len(tx.vin[1].witness.items), 0)
        self.assertEqual(tx.vout[0].value, 50_000 + 240 - 3_000)
        self.assertEqual(tx.vout[0].script_pubkey.data, SPK.data)
        self.assertEqual(tx.vout[1].script_pubkey.data, bytes.fromhex(m.OP_RETURN_HEX))
        self.assertTrue(170 < m.vsize(tx) < 190)

    def test_anchor_chain_topology(self):
        ch = m.build_chain_anchor(FAKE_TXID, UTXO["vout"], 200_000, 6, 17, 3_000, KEY, SPK)
        txs = ch.txs
        self.assertEqual(len(txs), 6)  # pivot + 4 анкера + sweep-child
        pivot, anchors, child = txs[0], txs[1:-1], txs[-1]
        # лесенка анкеров: старт = ANCHOR_FEE*k + ANCHOR_RESIDUAL, шаг -ANCHOR_FEE
        self.assertEqual(pivot.vout[1].value, m.ANCHOR_FEE * 4 + m.ANCHOR_RESIDUAL)
        v, prev = pivot.vout[1].value, pivot
        for a in anchors:
            self.assertEqual(a.vin[0].txid, prev.txid())
            self.assertEqual(a.vin[0].vout, 1 if prev is pivot else 0)
            v -= m.ANCHOR_FEE
            self.assertEqual(a.vout[0].value, v)
            prev = a
        self.assertEqual(v, m.ANCHOR_RESIDUAL)
        # child: вход0 = change pivot'а (ключевой), вход1 = последний анкер
        self.assertEqual(child.vin[0].txid, pivot.txid())
        self.assertEqual(child.vin[0].vout, 0)
        self.assertEqual(child.vin[1].txid, anchors[-1].txid())
        self.assertEqual(child.vin[1].vout, 0)
        self.assertEqual(child.vout[0].value,
                         pivot.vout[0].value + m.ANCHOR_RESIDUAL - 3_000)
        # согласованность Chain-состояния
        self.assertEqual(ch.fees, [17] + [m.ANCHOR_FEE] * 4 + [3_000])
        self.assertEqual(ch.vsizes[1:-1], [94] * 4)
        self.assertEqual(ch.child_funds, pivot.vout[0].value + m.ANCHOR_RESIDUAL)
        self.assertEqual(ch.child_key_outpoint, (pivot.txid().hex(), 0))

    def test_anchor_chain_rebuild_child(self):
        ch = m.build_chain_anchor(FAKE_TXID, UTXO["vout"], 200_000, 5, 17, 2_000, KEY, SPK)
        old = ch.txs[-1]
        tx2 = ch.rebuild_child(2_500)
        self.assertNotEqual(tx2.txid(), old.txid())
        self.assertEqual(tx2.vin[0].txid, old.vin[0].txid)   # те же входы
        self.assertEqual(tx2.vin[1].txid, old.vin[1].txid)
        self.assertEqual(ch.child_funds - tx2.vout[0].value, 2_500)

    def test_plan_fees_anchor_reaches_target(self):
        pivot_fee, child_fee = m.plan_fees_anchor(153, 94, 181, 23, 1.2)
        total_vs = 153 + 94 * 23 + 181
        total_fee = pivot_fee + m.ANCHOR_FEE * 23 + child_fee
        self.assertGreaterEqual(total_fee / total_vs, 1.2)
        self.assertGreaterEqual(pivot_fee / (153 + 1), m.MIN_RELAY_FEERATE)


@with_cfg
class HybridBuildTests(unittest.TestCase):
    """Гибрид «как у живого конкурента» (bc1qmcvv…): SHIELD_LINKS классических
    ключевых звеньев ЗАЩИЩАЮТ pivot — он тратит неподтверждённое звено, а не
    подтверждённый UTXO, поэтому burn-бот не может выдернуть его парой 1P1C."""

    def test_hybrid_topology(self):
        # 2 щита + pivot + 3 анкера + sweep = 7
        ch = m.build_chain_hybrid(FAKE_TXID, UTXO["vout"], 200_000, 7, 2,
                                  15, 17, 3_000, KEY, SPK)
        txs = ch.txs
        self.assertEqual(len(txs), 7)
        shields, pivot, anchors, child = txs[:2], txs[2], txs[3:-1], txs[-1]
        # щиты — обычные ключевые P2WPKH-звенья (2 выхода, подписаны)
        self.assertEqual(shields[0].vin[0].txid, FAKE_TXID)
        for s in shields:
            self.assertEqual(len(s.vout), 2)
            self.assertEqual(s.vout[0].script_pubkey.data, SPK.data)
            self.assertEqual(len(s.vin[0].witness.items), 2)
        # цепочка щитов -> pivot тратит vout0 последнего щита (НЕ исходный UTXO!)
        self.assertEqual(shields[1].vin[0].txid, shields[0].txid())
        self.assertEqual(pivot.vin[0].txid, shields[1].txid())
        self.assertEqual(pivot.vin[0].vout, 0)
        self.assertEqual(len(pivot.vout), 3)                    # change+анкер+OP_RETURN
        self.assertEqual(len(anchors), 3)
        self.assertEqual(pivot.vout[1].value, m.ANCHOR_FEE * 3 + m.ANCHOR_RESIDUAL)
        # child по-прежнему sweep pivot-change + последний анкер, бампится
        self.assertEqual(child.vin[0].txid, pivot.txid())
        self.assertEqual(child.vin[1].txid, anchors[-1].txid())
        self.assertEqual(ch.child_key_outpoint, (pivot.txid().hex(), 0))
        self.assertEqual(ch.fees, [15, 15, 17] + [m.ANCHOR_FEE] * 3 + [3_000])
        self.assertEqual(ch.vsizes[3:-1], [94] * 3)            # анкеры ровно 94 vB
        self.assertTrue(130 < ch.vsizes[0] < 150)             # щит ~140 vB

    def test_hybrid_pivot_has_unconfirmed_parent(self):
        # суть защиты: вход pivot'а — это tx из нашего же пакета, а не внешний UTXO
        ch = m.build_chain_hybrid(FAKE_TXID, UTXO["vout"], 200_000, 6, 1,
                                  15, 17, 2_000, KEY, SPK)
        pivot = ch.txs[2]
        ours = {t.txid() for t in ch.txs}
        self.assertIn(pivot.vin[0].txid, ours)  # предок pivot'а — внутри пакета

    def test_hybrid_rebuild_child(self):
        ch = m.build_chain_hybrid(FAKE_TXID, UTXO["vout"], 200_000, 6, 1,
                                  15, 17, 2_000, KEY, SPK)
        tx2 = ch.rebuild_child(2_300)
        self.assertEqual(len(tx2.vin), 2)
        self.assertEqual(tx2.vin[0].txid, ch.txs[-1].vin[0].txid)
        self.assertEqual(ch.child_funds - tx2.vout[0].value, 2_300)

    def test_hybrid_zero_shields_equals_anchor(self):
        # SHIELD_LINKS=0 вырождается в чистый anchor (обратная совместимость)
        a = m.build_chain_anchor(FAKE_TXID, UTXO["vout"], 200_000, 5, 17, 2_000, KEY, SPK)
        h = m.build_chain_hybrid(FAKE_TXID, UTXO["vout"], 200_000, 5, 0,
                                 15, 17, 2_000, KEY, SPK)
        self.assertEqual([t.txid() for t in h.txs], [t.txid() for t in a.txs])

    def test_plan_fees_hybrid_reaches_target(self):
        # s щитов по shield_fee + pivot + k анкеров + child -> target
        s, k = 3, 20
        shield_fee, pivot_fee, child_fee = m.plan_fees_hybrid(140, 153, 94, 181, s, k, 1.2)
        total_vs = 140 * s + 153 + 94 * k + 181
        total_fee = shield_fee * s + pivot_fee + m.ANCHOR_FEE * k + child_fee
        self.assertGreaterEqual(total_fee / total_vs, 1.2)
        self.assertGreaterEqual(shield_fee / (140 + 1), m.MIN_RELAY_FEERATE)
        self.assertGreaterEqual(pivot_fee / (153 + 1), m.MIN_RELAY_FEERATE)


@mock.patch.object(m, "log", new=lambda *a: None)  # тихий вывод тестов
@with_cfg
class StateTests(unittest.TestCase):
    """json-стейт: сохранение/загрузка пакета для рестарта (пункт 4)."""

    def tearDown(self):
        try:
            os.remove(CFG["STATE_FILE"])
        except OSError:
            pass

    def _roundtrip(self, ch):
        m.save_state(ch)
        st = m.load_state()
        self.assertIsNotNone(st)
        ch2 = m.chain_from_state(st, KEY, SPK)
        self.assertEqual([t.txid() for t in ch2.txs], [t.txid() for t in ch.txs])
        self.assertEqual(ch2.fees, ch.fees)
        self.assertEqual(ch2.vsizes, ch.vsizes)
        self.assertEqual(ch2.child_funds, ch.child_funds)
        self.assertEqual(ch2.child_key_outpoint, ch.child_key_outpoint)
        return ch2

    def test_roundtrip_classic_and_rebuild(self):
        ch = make_chain(n=3)
        ch2 = self._roundtrip(ch)
        tx2 = ch2.rebuild_child(ch.child_fee + 200)  # замыкание восстановлено
        self.assertEqual(tx2.vin[0].txid, ch.txs[-1].vin[0].txid)
        self.assertEqual(ch.child_funds - tx2.vout[0].value, ch.child_fee + 200)

    def test_roundtrip_anchor_and_rebuild(self):
        ch = m.build_chain_anchor(FAKE_TXID, UTXO["vout"], 200_000, 5, 17, 2_000, KEY, SPK)
        ch2 = self._roundtrip(ch)
        tx2 = ch2.rebuild_child(2_300)
        self.assertEqual(len(tx2.vin), 2)  # sweep: ключевой вход + анкер
        self.assertEqual(tx2.vin[0].txid, ch.txs[-1].vin[0].txid)
        self.assertEqual(tx2.vin[1].txid, ch.txs[-1].vin[1].txid)

    def test_clear_state(self):
        m.save_state(make_chain(n=3))
        m.clear_state()
        self.assertIsNone(m.load_state())

    def test_load_absent_is_none(self):
        self.assertIsNone(m.load_state())


@mock.patch.object(m, "log", new=lambda *a: None)  # тихий вывод тестов
@with_cfg
class AdoptMempoolTests(unittest.TestCase):
    """Подхват нашего пакета из мемпула без стейта (осиротевший запуск)."""

    ADDR = "bc1qtestaddr"

    def _fake_api(self, ch, first_prevout, address_sees=None):
        """esplora-ответы: /address/../txs/mempool видит ТОЛЬКО транзакции,
        касающиеся адреса (address_sees, по умолчанию все); /tx/{txid} и
        /tx/{txid}/hex доступны для всех. Списки перемешаны."""
        byid = {t.txid().hex(): t for t in ch.txs}
        prevouts = {(UTXO["txid"], UTXO["vout"]): first_prevout}
        for t in ch.txs:
            for i, o in enumerate(t.vout):
                prevouts[(t.txid().hex(), i)] = {"value": o.value,
                                                "scriptpubkey": o.script_pubkey.data.hex()}
        jsons = {}
        for t, fee in zip(ch.txs, ch.fees):
            jsons[t.txid().hex()] = {
                "txid": t.txid().hex(),
                "fee": fee,
                "status": {"confirmed": False},
                "vin": [{"txid": v.txid.hex(), "vout": v.vout,
                         "prevout": prevouts[(v.txid.hex(), v.vout)]} for v in t.vin],
            }
        if address_sees is None:
            address_sees = list(byid)
        mem = [jsons[t] for t in address_sees]
        mem.reverse()  # нарочно не в порядке цепочки

        def fake(path, want_json=True):
            if path == f"/address/{self.ADDR}/txs/mempool":
                return mem
            mt = re.match(r"/tx/([0-9a-f]{64})/hex$", path)
            if mt and mt.group(1) in byid:
                return byid[mt.group(1)].serialize().hex()
            mt = re.match(r"/tx/([0-9a-f]{64})$", path)
            if mt and mt.group(1) in jsons:
                return jsons[mt.group(1)]
            return None
        return fake

    def test_adopt_classic(self):
        ch = make_chain(n=3)
        spk_hex = SPK.data.hex()
        fake = self._fake_api(ch, {"value": UTXO["value"], "scriptpubkey": spk_hex})
        with mock.patch.object(m, "api_get", side_effect=fake):
            ad = m.adopt_mempool_chain(self.ADDR, KEY, SPK)
        self.assertIsNotNone(ad)
        self.assertEqual([t.txid() for t in ad.txs], [t.txid() for t in ch.txs])
        self.assertEqual(ad.fees, ch.fees)
        self.assertEqual(ad.child_funds, ch.child_funds)
        self.assertEqual(ad.child_key_outpoint, ch.child_key_outpoint)
        tx2 = ad.rebuild_child(ch.child_fee + 150)  # бамп подхваченного пакета работает
        self.assertEqual(tx2.vin[0].txid, ch.txs[-1].vin[0].txid)

    def test_adopt_anchor_address_sees_only_pivot_and_child(self):
        # РЕАЛЬНОЕ поведение esplora: бесключевые анкерные звенья не касаются
        # адреса и невидимы в /address/../txs/mempool — подхват обязан дотянуть
        # их по входам child'а через /tx/{txid}
        ch = m.build_chain_anchor(FAKE_TXID, UTXO["vout"], 200_000, 5, 17, 2_000, KEY, SPK)
        spk_hex = SPK.data.hex()
        visible = [ch.txs[0].txid().hex(), ch.txs[-1].txid().hex()]  # pivot + child
        fake = self._fake_api(ch, {"value": 200_000, "scriptpubkey": spk_hex},
                              address_sees=visible)
        with mock.patch.object(m, "api_get", side_effect=fake):
            ad = m.adopt_mempool_chain(self.ADDR, KEY, SPK)
        self.assertIsNotNone(ad)
        self.assertEqual(len(ad.txs), 5)   # все звенья, включая невидимые анкеры
        self.assertEqual([t.txid() for t in ad.txs], [t.txid() for t in ch.txs])
        self.assertEqual(ad.fees, ch.fees)
        self.assertAlmostEqual(ad.effective_rate(), ch.effective_rate())
        tx2 = ad.rebuild_child(2_400)
        self.assertEqual(len(tx2.vin), 2)  # распознан sweep с анкерным входом
        self.assertEqual(tx2.vin[1].txid, ch.txs[-1].vin[1].txid)

    def test_adopt_empty_mempool_is_none(self):
        with mock.patch.object(m, "api_get", return_value=[]):
            self.assertIsNone(m.adopt_mempool_chain(self.ADDR, KEY, SPK))


@mock.patch.object(m, "log", new=lambda *a: None)  # тихий вывод тестов
@with_cfg
class AdoptTests(unittest.TestCase):
    """Гонка RBF: старая версия child успела в блок — минт состоялся, пакет завершён."""

    def test_confirmed_old_child_adopted(self):
        ch = make_chain(n=3)
        spend = {"spent": True, "txid": "ab" * 32, "status": {"confirmed": True}}
        with mock.patch.object(m, "api_get", return_value=spend):
            self.assertTrue(m.adopt_confirmed_spender(ch))
        self.assertTrue(ch.done())

    def test_unconfirmed_spender_not_adopted(self):
        ch = make_chain(n=3)
        spend = {"spent": True, "txid": "ab" * 32, "status": {"confirmed": False}}
        with mock.patch.object(m, "api_get", return_value=spend):
            self.assertFalse(m.adopt_confirmed_spender(ch))
        self.assertFalse(ch.done())

    def test_unspent_not_adopted(self):
        ch = make_chain(n=3)
        with mock.patch.object(m, "api_get", return_value={"spent": False}):
            self.assertFalse(m.adopt_confirmed_spender(ch))
        self.assertFalse(ch.done())


@mock.patch.object(m, "log", new=lambda *a: None)  # тихий вывод тестов
@with_cfg
class MonitorTests(unittest.TestCase):
    """Цикл монитора со скриптованными ответами API; sleep взрывается,
    если монитор не завершился за отведённое число тиков."""

    def test_first_tick_resyncs_and_exits_when_confirmed(self):
        ch = make_chain(n=3)
        with mock.patch.object(m, "tip_height", return_value=100), \
             mock.patch.object(m, "tx_status", return_value={"confirmed": True}), \
             mock.patch.object(m, "next_block_threshold", return_value=(1.0, "t")), \
             mock.patch.object(m.time, "sleep",
                               side_effect=TimeoutError("монитор обязан выйти на первом тике")):
            self.assertEqual(m.monitor_and_bump(ch), "confirmed")
        self.assertTrue(ch.done())

    def test_dead_package_exits_without_spinning(self):
        # анкер угнан: первая неподтверждённая tx не восстановима -> монитор
        # обязан выйти с "dead", а не крутиться и не пытаться бампить
        ch = m.build_chain_anchor(FAKE_TXID, UTXO["vout"], 200_000, 5, 17, 2_000, KEY, SPK)

        def fake_status(txid_hex):
            return {"confirmed": True} if txid_hex == ch.txs[0].txid().hex() else None
        with mock.patch.object(m, "tip_height", return_value=100), \
             mock.patch.object(m, "tx_status", side_effect=fake_status), \
             mock.patch.object(m, "package_is_dead", return_value=True) as dead, \
             mock.patch.object(m, "bump_child") as bump, \
             mock.patch.object(m, "next_block_threshold", return_value=(1.0, "t")), \
             mock.patch.object(m.time, "sleep",
                               side_effect=TimeoutError("монитор обязан выйти")):
            self.assertEqual(m.monitor_and_bump(ch), "dead")
        dead.assert_called_once()
        bump.assert_not_called()

    def test_failed_resync_retried_next_tick(self):
        # тик 1: статусы недоступны (исключение) -> тик 2 обязан повторить ресинк
        ch = make_chain(n=3)
        statuses = iter([RuntimeError("api down")])

        def fake_status(_txid):
            v = next(statuses, {"confirmed": True})
            if isinstance(v, Exception):
                raise v
            return v

        ticks = {"n": 0}

        def sleep_guard(_s):
            ticks["n"] += 1
            if ticks["n"] > 1:
                raise TimeoutError("ресинк не повторился на втором тике")

        with mock.patch.object(m, "tip_height", return_value=100), \
             mock.patch.object(m, "tx_status", side_effect=fake_status), \
             mock.patch.object(m, "next_block_threshold", return_value=(1.0, "t")), \
             mock.patch.object(m.time, "sleep", side_effect=sleep_guard):
            m.monitor_and_bump(ch)
        self.assertTrue(ch.done())

    def test_missing_child_with_confirmed_spender_exits(self):
        # родители подтверждены, наш child неизвестен: его вход потратила старая
        # RBF-версия -> монитор должен признать успех и выйти, а не ребродкастить вечно
        ch = make_chain(n=3)

        def fake_status(txid_hex):
            if txid_hex == ch.txs[-1].txid().hex():
                return None
            return {"confirmed": True}

        with mock.patch.object(m, "tip_height", return_value=100), \
             mock.patch.object(m, "tx_status", side_effect=fake_status), \
             mock.patch.object(m, "package_is_dead", return_value=False), \
             mock.patch.object(m, "adopt_confirmed_spender", return_value=True) as adopt, \
             mock.patch.object(m, "ensure_in_mempool") as ensure, \
             mock.patch.object(m, "next_block_threshold", return_value=(1.0, "t")), \
             mock.patch.object(m.time, "sleep",
                               side_effect=TimeoutError("монитор обязан выйти")):
            self.assertEqual(m.monitor_and_bump(ch), "confirmed")
        adopt.assert_called_once()
        ensure.assert_not_called()


@mock.patch.object(m, "log", new=lambda *a: None)  # тихий вывод тестов
@with_cfg
class BroadcastTests(unittest.TestCase):
    """Бродкаст пакета: главное — довести sweep до мемпула. Транзиентный
    inputs-missing (лаг пропагации родителя) ретраим, иначе анкер остаётся без
    защиты sweep'а и его снайпит burn-бот (инцидент 2026-07-18, d2909c7a)."""

    def _hybrid(self):
        return m.build_chain_hybrid(FAKE_TXID, UTXO["vout"], 200_000, 6, 1,
                                    15, 17, 2_000, KEY, SPK)

    def test_transient_inputs_missing_is_retried_then_sweep_sent(self):
        ch = self._hybrid()
        sweep_id = ch.txs[-1].txid().hex()
        calls = {"n": 0}
        # 3-я tx (анкер) один раз отвечает inputs-missing (лаг), потом ок
        flaky = {ch.txs[3].txid().hex(): 1}

        def fake_bcast(tx):
            calls["n"] += 1
            h = tx.txid().hex()
            if flaky.get(h):
                flaky[h] -= 1
                return False, "bad-txns-inputs-missingorspent"
            return True, h
        sent = []
        def rec(tx):
            r = fake_bcast(tx)
            if r[0]: sent.append(tx.txid().hex())
            return r
        with mock.patch.object(m, "broadcast_tx", side_effect=rec):
            m.broadcast_package(ch)
        # sweep обязан быть отправлен, несмотря на залипшую середину
        self.assertIn(sweep_id, sent)
        self.assertEqual(len(sent), len(ch.txs))

    def test_first_tx_failure_still_aborts(self):
        ch = self._hybrid()
        with mock.patch.object(m, "broadcast_tx",
                               return_value=(False, "min relay fee not met")), \
             mock.patch.object(m, "clear_state"):
            with self.assertRaises(SystemExit):
                m.broadcast_package(ch)

    def test_sniped_middle_gives_up_after_retries(self):
        # родитель угнан навсегда: ретраи исчерпаны, дальше по цепочке бессмысленно —
        # прекращаем (монитор потом признает пакет мёртвым и пересоберёт)
        ch = self._hybrid()
        dead_from = ch.txs[3].txid().hex()
        sent = []

        def fake(tx):
            if tx.txid().hex() == dead_from or sent and tx.txid().hex() in _after(ch, dead_from):
                return False, "bad-txns-inputs-missingorspent"
            sent.append(tx.txid().hex())
            return True, tx.txid().hex()

        def _after(chain, txid_hex):
            ids = [t.txid().hex() for t in chain.txs]
            i = ids.index(txid_hex)
            return set(ids[i:])
        with mock.patch.object(m, "broadcast_tx", side_effect=fake), \
             mock.patch.object(m.time, "sleep"):
            m.broadcast_package(ch)  # не должно кинуть — просто дойдёт до предела и вернётся
        # щиты+pivot ушли, анкер-хвост нет
        self.assertIn(ch.txs[0].txid().hex(), sent)
        self.assertNotIn(ch.txs[-1].txid().hex(), sent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
