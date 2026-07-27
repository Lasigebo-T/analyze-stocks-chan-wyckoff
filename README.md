# analyze-stocks-chan-wyckoff

一个可复用的 Codex Skill，用缠论结构、威科夫量价和条件情景树分析上市股票，并为做T降成本生成带触发、失效和经济账的执行方案。

## 核心方法

Skill 将分析固定为：

> 一锚三层、双证三景、一表一账

- **一锚**：冻结数据来源、截止时间、复权、持仓与风险口径；
- **三层**：持仓层、操作层、执行层；
- **双证**：缠论结构证据与威科夫量价证据独立判定后交叉验证；
- **三景**：向上延续、区间震荡、向下恶化；
- **一表**：触发—动作—仓位—失效—禁止动作；
- **一账**：按成交现金流、费用和滑点计算做T后的经济成本。

它不会把中枢等同于威科夫交易区，不会把Spring自动等同于一买，也不会把价格到达某一区间直接翻译成买卖指令。

## 安装

把仓库中的 `analyze-stocks-chan-wyckoff` 文件夹复制到 Codex Skills 目录：

```text
~/.codex/skills/analyze-stocks-chan-wyckoff
```

重新打开相关 Codex 会话后，可显式调用：

```text
$analyze-stocks-chan-wyckoff
```

## 使用示例

```text
使用 $analyze-stocks-chan-wyckoff 分析000977。
我持有3000股，经济成本88元，只想倒T降成本、不加仓。
请分别给出缠论结构、威科夫量价证据、三种条件情景，
以及高抛、买回、失效和只成交一腿时的处置。
```

```text
使用 $analyze-stocks-chan-wyckoff 审计这张图中的“三买+Spring”标注。
若数据不足，请降级结论，不得猜测精确价位。
```

```text
使用 $analyze-stocks-chan-wyckoff 对所附历史OHLCV逐根复盘。
分别报告事件时间、确认时间、最早可成交时间，并纳入费用与滑点。
```

## 成本计算器

```bash
python analyze-stocks-chan-wyckoff/scripts/t_cost.py \
  --shares 3000 \
  --cost 88 \
  --trade 1000,88,82,16 \
  --trade 1000,87,83,16
```

每个 `--trade` 参数依次为：

```text
股数,卖价,买回价,全部费用与滑点
```

使用 `--json` 可获得机器可读结果。

## 目录

```text
analyze-stocks-chan-wyckoff/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   ├── analysis-protocol.md
│   ├── chan-wyckoff-boundaries.md
│   └── t-trading.md
└── scripts/
    └── t_cost.py
```

## 边界

- 需要可靠OHLCV才能做精确缠论与威科夫判断；
- 当前行情、交易规则与费率必须按分析截止日重新核验；
- 技术标签不是无条件订单，Skill不承诺收益或回本；
- 真实执行需考虑流动性、滑点、部分成交、涨跌停、停牌和跳空。

## License

MIT
