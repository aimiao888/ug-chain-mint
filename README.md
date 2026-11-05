# UNCOMMON•GOODS 链式铸造（ug-chain-mint）

基于 [diesel-chain-minter](https://github.com/anchoys1337/diesel-chain-minter) 改造的本地脚本：用单笔 funding UTXO 搭出最多 **25 笔**祖先链，批量 mint **UNCOMMON•GOODS**（符文 id `1:0`）。

默认：

- `DRY_RUN = True`（只构建并打印，不广播）
- `CHAIN_MODE = "hybrid"`（护盾 + P2A 锚点，比纯 anchor 更安全）
- `MINT_PAYLOAD = "ug"`（纯 UG，对齐区块 974557 多数交易；锚点费 8 sats）

也可改成 `MINT_PAYLOAD = "ug_diesel"` 走原仓库的 UG+DIESEL payload（锚点费 10 sats）。

## 安装（Windows PowerShell）

```powershell
cd "E:\Aimiao\Documents\Grok Bot\ug-chain-mint"
py -3 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

若 `py` 不可用，试 `python -m venv venv`。

## 配置

1. 打开 `ug_chain_mint.py` 顶部常量，按需改：
   - `N_MINTS`（1–25）
   - `CHAIN_MODE`：`hybrid`（推荐）/ `classic` / `anchor`
   - `MINT_PAYLOAD`：`ug` 或 `ug_diesel`
   - `PARENT_FEERATE` / `TARGET_FEERATE` / `MAX_FEERATE` 等
2. **只通过环境变量**提供私钥，不要写进任何文件、不要贴到聊天：

```powershell
$env:UG_WIF="你的压缩WIF"   # 对应 bc1q… P2WPKH 地址
```

也可复制 `.env.example` 为 `.env` 后本地填写，但本脚本**不会自动读 .env**——请用上面的 `$env:UG_WIF=...`。

## 干跑（推荐先做）

```powershell
cd "E:\Aimiao\Documents\Grok Bot\ug-chain-mint"
.\venv\Scripts\Activate.ps1
python ug_chain_mint.py
```

未设置 `UG_WIF` 时应干净退出并提示设置密钥。设置密钥后，干跑会拉取 UTXO、构建链、打印费用与末笔 hex，**不会广播**。

## 上线前检查清单

1. 干跑输出的结构、费率、总费用看起来合理。
2. 把 `DRY_RUN = False`（改脚本顶部常量）。
3. 先用较小的 `N_MINTS`（例如 3–5）试一笔。
4. 推荐保持 `CHAIN_MODE = "hybrid"`。
5. 纯 `anchor` 模式在公开 API 逐笔广播时，P2A 可能被抢跑；只有自己的 Bitcoin Core 28+ `submitpackage` 才相对安全。

## 风险

- **P2A sniping**：纯锚点环任何人都能花未确认锚点；hybrid 有护盾，仍非零风险。
- **非原子广播**：父交易未传开时子交易会失败；脚本有重试，但不能保证整包同时进池。
- **费率抬升 / RBF**：mempool 拥堵时可能需要 bump，最坏费用见启动前打印。
- **真实 BTC**：出错可能损失手续费或被抢链；务必先干跑。

## 上游致谢

逻辑与交易结构来自 [anchoys1337/diesel-chain-minter](https://github.com/anchoys1337/diesel-chain-minter)。本目录里的 `diesel_chain_mint.py` / `test_diesel_chain_mint.py` 仅作参考；UG 纯 payload 不保证上游单测全部通过。

## 文件

| 文件 | 说明 |
|------|------|
| `ug_chain_mint.py` | 主脚本（默认 dry-run） |
| `requirements.txt` | `embit`、`requests` |
| `.env.example` | `UG_WIF=` 模板 |
| `diesel_chain_mint.py` | 上游原版参考 |
| `test_diesel_chain_mint.py` | 上游测试（可选） |
