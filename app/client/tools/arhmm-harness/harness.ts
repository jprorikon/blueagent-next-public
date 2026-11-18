// Golden fixture 產生器：用 airi 的 TS 實作（MIT）跑出 AR-HMM 的
// ① 擬合診斷 ② 固定 seed 生成的前 N 幀（含每個狀態編號）。
// C# 移植（app/client/shared/BlueAgent.Logic/Motion/ArHmm.cs）要逐值對上這份。
//
// 用法（在裝了 node 的機器）：
//   npm i es-toolkit tsx && npx tsx harness.ts > arhmm-golden.json
import { createArHmmModel } from './src/ar-hmm'

const CHANNELS = 3
const FRAMES = 240
const ORDER = 2
const STATE_COUNT = 3
const RIDGE = 0.01
const ITERATIONS = 4
const SEED = 12345
const GENERATE = 40

// 決定性合成序列：不用亂數（C# 那邊照同一條公式重建，避免兩邊 PRNG 不一致）
function trainingFrames(): number[][] {
  const rows: number[][] = []
  for (let i = 0; i < FRAMES; i++) {
    const t = i / 30
    rows.push([
      Math.sin(t * 1.1) * 0.8 + Math.sin(t * 0.37) * 0.3,
      Math.cos(t * 0.9) * 0.6 + Math.sin(t * 0.21) * 0.2,
      Math.sin(t * 2.3 + 1.0) * 0.25,
    ])
  }
  return rows
}

const frames = trainingFrames()
const sequence = {
  sampleRateHz: 30,
  sourceDurationMs: (FRAMES / 30) * 1000,
  frames,
}

const model = createArHmmModel(sequence, {
  stateCount: STATE_COUNT,
  order: ORDER,
  ridge: RIDGE,
  iterations: ITERATIONS,
})

const generator = model.toGenerator({ seed: SEED })
const generated: Array<{ values: number[]; state: number }> = []
for (let i = 0; i < GENERATE; i++) generated.push(generator.next())

const out = {
  meta: {
    source: 'moeru-ai/airi packages/motion-driver-magic (MIT) - src/ar-hmm.ts',
    channels: CHANNELS,
    frames: FRAMES,
    order: ORDER,
    stateCount: STATE_COUNT,
    ridge: RIDGE,
    iterations: ITERATIONS,
    seed: SEED,
    generated: GENERATE,
  },
  sequence,
  diagnostics: model.diagnostics,
  generated,
}

process.stdout.write(JSON.stringify(out))
