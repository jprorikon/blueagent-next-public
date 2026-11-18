# AR-HMM golden fixture 產生器（airi，MIT）

`app/client/fixtures/arhmm-golden.json` 不是我們自己算的：它由 **上游 airi 的 TypeScript 實作**跑出來，
C# 移植（`app/client/shared/BlueAgent.Logic/Motion/ArHmm.cs`）逐值對它（測試：`ArHmmTest.cs`）。

- 上游：<https://github.com/moeru-ai/airi> — `packages/motion-driver-magic/src/`
  （`ar-hmm.ts`、`shared/{var,numeric,random,channels}.ts`、`fit.ts`、`types.ts`）
- 授權：MIT（移植檔頭與 `Motion/LICENSE-airi.txt` 已保留出處）
- 產生日期：2026-10-31（上游 main）

## 怎麼重產

需要 node（本機 NAS 沒有 node，用容器或別的機器）：

```bash
# 1) 取上游原始碼（保持 src/ 結構，harness.ts 相對 import 才對得上）
mkdir -p airi-harness/src/shared
for f in ar-hmm.ts var.ts fit.ts types.ts index.ts; do
  curl -sL "https://raw.githubusercontent.com/moeru-ai/airi/main/packages/motion-driver-magic/src/$f" \
    -o "airi-harness/src/$f"
done
for f in channels numeric random var; do
  curl -sL "https://raw.githubusercontent.com/moeru-ai/airi/main/packages/motion-driver-magic/src/shared/$f.ts" \
    -o "airi-harness/src/shared/$f.ts"
done
cp harness.ts airi-harness/

# 2) 跑（在 GPU 機上用 docker，或任何有 node 的機器）
cd airi-harness
docker run --rm -v "$PWD":/w -w /w node:20 bash -lc \
  'npm i --silent es-toolkit tsx typescript && npx tsx harness.ts > arhmm-golden.json'
```

把 `arhmm-golden.json` 放回 `app/client/fixtures/`（csproj 會複製到輸出目錄的 `fixtures/`）。

## 內容

- 合成訓練序列：3 通道 × 240 幀 @30Hz（`sin/cos` 固定公式，兩邊都照抄 ⇒ 不用對 PRNG）
- 擬合：`stateCount=3, order=2, ridge=0.01, iterations=4`
- 生成：`seed=12345`，40 幀，含每幀的隱狀態
- 對拍判據：診斷值（平均對數似然／狀態佔用率／平均停留）誤差 < 1e-9；每幀每個值與隱狀態完全一致

## 跑 C# 測試（本機沒有 dotnet 時）

```bash
docker run --rm -v "$PWD/unity":/w -w /w mcr.microsoft.com/dotnet/sdk:8.0 \
  bash -lc 'dotnet test shared/BlueAgent.Logic.Tests/BlueAgent.Logic.Tests.csproj --filter FullyQualifiedName~ArHmmTest'
```
