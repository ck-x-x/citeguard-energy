# CiteGuard-Energy 評測結果（20261126-125825）

| 指標 | 數值 |
|---|---|
| 成功執行 | 3 / 10 |
| 引用總數 | 65 |
| 有效引用率（校驗前，baseline） | 100.0% |
| 有效引用率（校驗後） | 100.0% |
| 剔除的捏造引用 / 刪除的句子 | 0 / 0 |
| 正常題回答率 | 100.0% |
| 陷阱題拒答率 | — |
| 來源命中率（正常題） | 100.0% |
| 關鍵數據命中率（正常題） | 66.7% |
| 平均耗時 | 20.6 秒 |

## 逐題結果

| 題號 | 類型 | 結果 | 引用（有效/總數） | 校驗前有效率 | 來源命中 | 關鍵數據 | KB/OA 證據 | 耗時 |
|---|---|---|---|---|---|---|---|---|
| N1 | normal | ❌ ReadTimeout: timed out | | | | | | |
| N2 | normal | ✅ 回答 | 22/22 | 100.0% | ✅ | 100.0% | 5/5 | 14.4 秒 |
| N3 | normal | ❌ RuntimeError: partial-succeeded | | | | | | |
| N4 | normal | ✅ 回答 | 15/15 | 100.0% | ✅ | 0.0% | 4/5 | 13.6 秒 |
| N5 | normal | ✅ 回答 | 28/28 | 100.0% | ✅ | 100.0% | 5/5 | 33.7 秒 |
| N6 | normal | ❌ RuntimeError: [gemini] This model is currently experiencing  | | | | | | |
| N7 | normal | ❌ RuntimeError: partial-succeeded | | | | | | |
| T1 | trap | ❌ RuntimeError: partial-succeeded | | | | | | |
| T2 | trap | ❌ RuntimeError: partial-succeeded | | | | | | |
| T3 | trap | ❌ RuntimeError: [gemini] You exceeded your current quota, plea | | | | | | |
