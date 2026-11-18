#!/usr/bin/env python3
"""情緒原型表：Kotlin → C#（**產生器**，不要手改產物）。

為什麼要這支：`engine/expression/EmotionArchetypeRegistry.kt` 本身**是產生的**
（上游 `node tools/gen_archetype_registry.mjs --write`）——它是**資料**（15 個情緒 ×
baseTendency ＋ 變體，每個值都是 [min,max] 範圍），手抄 200 行一定抄錯，而且抄錯的地方
不會有任何症狀（只是某個情緒的表情幅度差一點）。=> 用機器翻，再用 fixture 整表對拍。

用法：
  python3 tools/gen_emotion_archetypes_cs.py            # 寫檔
  python3 tools/gen_emotion_archetypes_cs.py --check     # 只比對，不寫（CI／驗收用）

來源：`blueagent/live2d/android/live2d/src/main/kotlin/dev/deepsea/live2d/engine/expression/EmotionArchetypeRegistry.kt`
產物：`blueagent-next/app/client/shared/BlueAgent.Logic/Engine/EmotionArchetypes.cs`
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
UNITY_REPO = os.path.dirname(HERE)
WS = os.path.dirname(os.path.dirname(os.path.dirname(UNITY_REPO)))
SRC = os.path.join(
    WS, "blueagent/live2d/android/live2d/src/main/kotlin/dev/deepsea/live2d/engine/expression/EmotionArchetypeRegistry.kt"
)
DST = os.path.join(UNITY_REPO, "shared/BlueAgent.Logic/Engine/EmotionArchetypes.cs")

RE_ARCHETYPE = re.compile(r'^\s*"(\w+)" to EmotionArchetype\($')
RE_BASE = re.compile(r"baseTendency = linkedMapOf\((.*)\),\s*$")
RE_VARIANT = re.compile(r'^\s*"(\w+)" to EmotionVariant\($')
RE_RANGES = re.compile(r"ranges = linkedMapOf\((.*)\),\s*$")
RE_TAGS = re.compile(r"tags = listOf\((.*)\),\s*$")
RE_RANGE = re.compile(r'"(\w+)" to NumberRange\((-?[\d.]+), (-?[\d.]+)\)')


def parse_ranges(body: str):
    """`"mouthSmile" to NumberRange(0.02, 0.12), …` → [(key, min, max), …]（**保持順序**）。"""
    try:
        return [(m.group(1), float(m.group(2)), float(m.group(3))) for m in RE_RANGE.finditer(body)]
    except ValueError as e:  # 覆蓋率優先：任何不認得的寫法都要當場炸，不要安靜地漏抄
        raise SystemExit(f"NumberRange 解析失敗：{body!r}（{e}）")


def parse_tags(body: str):
    body = body.strip()
    if body == "":
        return []
    return re.findall(r'"([^"]+)"', body)


def parse(text: str):
    archetypes = []
    cur = None
    variant = None
    for line in text.splitlines():
        m = RE_ARCHETYPE.match(line)
        if m:
            cur = {"emotion": m.group(1), "base": [], "variants": []}
            archetypes.append(cur)
            variant = None
            continue
        if cur is None:
            continue
        m = RE_BASE.search(line)
        if m and not variant:
            cur["base"] = parse_ranges(m.group(1))
            continue
        m = RE_VARIANT.match(line)
        if m:
            variant = {"name": m.group(1), "ranges": [], "tags": []}
            cur["variants"].append(variant)
            continue
        if variant is not None:
            m = RE_RANGES.search(line)
            if m:
                variant["ranges"] = parse_ranges(m.group(1))
                continue
            m = RE_TAGS.search(line)
            if m:
                variant["tags"] = parse_tags(m.group(1))
                continue
    return archetypes


def cs_num(v: float) -> str:
    """與 Kotlin 的 `Double.toString()` 等價的 C# 字面值（整數帶 `.0`）。"""
    if v == int(v):
        return f"{int(v)}.0"
    return repr(v)


def emit(archetypes) -> str:
    out = []
    out.append("using System.Collections.Generic;")
    out.append("")
    out.append("namespace BlueAgent.Logic.Engine")
    out.append("{")
    out.append("    /// <summary>")
    out.append("    /// 情緒原型表 —— **產生物，不要手改**（`tools/gen_emotion_archetypes_cs.py` 從 Kotlin 的")
    out.append("    /// `expression/EmotionArchetypeRegistry.kt` 翻過來；那份本身也是上游產生的）。")
    out.append("    ///")
    out.append("    /// ⚠️ **順序是資料的一部分**：`RuntimeExpressionGenerator.sampleFacs` 照 `BaseTendencyKeys` 的順序")
    out.append("    ///    抽亂數、`pickOne` 照 `VariantKeys` 的順序挑變體 ⇒ 順序換掉＝同一顆 seed 生出不同演出。")
    out.append("    ///    所以每一層都同時帶「有序鍵清單」與「查表」。")
    out.append("    /// </summary>")
    out.append("    public sealed class EmotionVariant")
    out.append("    {")
    out.append("        public readonly IReadOnlyList<string> RangeKeys;")
    out.append("        public readonly IReadOnlyDictionary<string, NumberRange> Ranges;")
    out.append("        public readonly IReadOnlyList<string> Tags;")
    out.append("")
    out.append("        public EmotionVariant(")
    out.append("            IReadOnlyList<string> rangeKeys,")
    out.append("            IReadOnlyDictionary<string, NumberRange> ranges,")
    out.append("            IReadOnlyList<string> tags)")
    out.append("        {")
    out.append("            RangeKeys = rangeKeys;")
    out.append("            Ranges = ranges;")
    out.append("            Tags = tags;")
    out.append("        }")
    out.append("    }")
    out.append("")
    out.append("    /// <summary>一個情緒的原型：基礎傾向 ＋ 若干變體。</summary>")
    out.append("    public sealed class EmotionArchetype")
    out.append("    {")
    out.append("        public readonly string Emotion;")
    out.append("        public readonly IReadOnlyList<string> BaseTendencyKeys;")
    out.append("        public readonly IReadOnlyDictionary<string, NumberRange> BaseTendency;")
    out.append("        public readonly IReadOnlyList<string> VariantKeys;")
    out.append("        public readonly IReadOnlyDictionary<string, EmotionVariant> Variants;")
    out.append("")
    out.append("        public EmotionArchetype(")
    out.append("            string emotion,")
    out.append("            IReadOnlyList<string> baseTendencyKeys,")
    out.append("            IReadOnlyDictionary<string, NumberRange> baseTendency,")
    out.append("            IReadOnlyList<string> variantKeys,")
    out.append("            IReadOnlyDictionary<string, EmotionVariant> variants)")
    out.append("        {")
    out.append("            Emotion = emotion;")
    out.append("            BaseTendencyKeys = baseTendencyKeys;")
    out.append("            BaseTendency = baseTendency;")
    out.append("            VariantKeys = variantKeys;")
    out.append("            Variants = variants;")
    out.append("        }")
    out.append("    }")
    out.append("")
    out.append("    public static class EmotionArchetypes")
    out.append("    {")
    out.append("        /// <summary>照上游的宣告順序（順序會影響 variant 挑選時的平手取捨）。</summary>")
    out.append("        public static readonly IReadOnlyList<KeyValuePair<string, EmotionArchetype>> List = new[]")
    out.append("        {")
    for a in archetypes:
        # ⚠️ 字典的集合初始器只能用 `{ key, value }`——`new KeyValuePair<...>(...)` 那份走的是
        #    `ICollection<KeyValuePair<,>>.Add`，而 Dictionary 是**顯式介面實作**（編譯器看不到）⇒ CS7036。
        base_items = ", ".join(
            f'{{ "{k}", new NumberRange({cs_num(lo)}, {cs_num(hi)}) }}'
            for (k, lo, hi) in a["base"]
        )
        base_keys = ", ".join(f'"{k}"' for (k, _, _) in a["base"])
        out.append(f'            new KeyValuePair<string, EmotionArchetype>("{a["emotion"]}", new EmotionArchetype(')
        out.append(f'                "{a["emotion"]}",')
        out.append(f"                new string[] {{ {base_keys} }},")
        out.append('                new Dictionary<string, NumberRange> { ' + base_items + " },")
        var_keys = ", ".join(f'"{v["name"]}"' for v in a["variants"])
        out.append(f"                new string[] {{ {var_keys} }},")
        out.append("                new Dictionary<string, EmotionVariant>")
        out.append("                {")
        for v in a["variants"]:
            items = ", ".join(
                f'{{ "{k}", new NumberRange({cs_num(lo)}, {cs_num(hi)}) }}'
                for (k, lo, hi) in v["ranges"]
            )
            vkeys = ", ".join(f'"{k}"' for (k, _, _) in v["ranges"])
            tags = ", ".join(f'"{t}"' for t in v["tags"])
            out.append(f'                    {{ "{v["name"]}", new EmotionVariant(')
            out.append(f"                        new string[] {{ {vkeys} }},")
            out.append('                        new Dictionary<string, NumberRange> { ' + items + " },")
            out.append(f"                        new string[] {{ {tags} }}) }},")
        out.append("                })),")
    out.append("        };")
    out.append("")
    out.append("        private static readonly Dictionary<string, EmotionArchetype> Map = BuildMap();")
    out.append("")
    out.append("        private static Dictionary<string, EmotionArchetype> BuildMap()")
    out.append("        {")
    out.append("            var outp = new Dictionary<string, EmotionArchetype>(List.Count);")
    out.append("            foreach (var kv in List) outp[kv.Key] = kv.Value;")
    out.append("            return outp;")
    out.append("        }")
    out.append("")
    out.append("        /// <summary>`getEmotionArchetype`：不認得的情緒名回 neutral（照上游行為）。</summary>")
    out.append("        public static EmotionArchetype Get(string emotion) =>")
    out.append('            Map.TryGetValue(emotion ?? "", out var a) ? a : Map["neutral"];')
    out.append("    }")
    out.append("}")
    out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比對產物是否為最新（不寫檔）")
    args = ap.parse_args()

    if not os.path.exists(SRC):
        raise SystemExit(f"找不到來源：{SRC}")
    with open(SRC, encoding="utf-8") as f:
        text = f.read()

    archetypes = parse(text)
    if len(archetypes) < 15:
        raise SystemExit(f"只解析到 {len(archetypes)} 個情緒（原始檔應該有 15 個）——解析規則要跟上格式")
    total_variants = sum(len(a["variants"]) for a in archetypes)
    cs = emit(archetypes)

    if args.check:
        with open(DST, encoding="utf-8") as f:
            cur = f.read()
        if cur != cs:
            raise SystemExit("產物不是最新的：重跑 `python3 tools/gen_emotion_archetypes_cs.py`")
        print(f"OK：{len(archetypes)} 個情緒／{total_variants} 個變體，產物是最新的")
        return

    with open(DST, "w", encoding="utf-8") as f:
        f.write(cs)
    print(f"寫入 {DST}：{len(archetypes)} 個情緒／{total_variants} 個變體")


if __name__ == "__main__":
    sys.exit(main())
