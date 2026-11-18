using System;
using System.Collections.Generic;
using System.Globalization;

namespace BlueAgent.Logic
{
    /// <summary>
    /// 話者ゲートにトリガ時に渡すもの——そして、なぜそれが独立したファイルなのか。
    ///
    /// `blueagent/live2d/android/kmp/src/commonMain/.../GateWindow.kt` の**逐行移植**（真値＝Kotlin 側）。
    /// 等価性は `fixtures/gate_window.json` を逐條對拍して判定する。
    ///
    /// ゲートは 1.5 秒のモデル：一つの窓を埋め込み、所有者の登録重心と比較する。**どの音声からその窓を
    /// 取るか**が、テレビが喋っている部屋での判定を決める。そしてその選択はインデックス上の純粋な
    /// 算術なので、ここに置く（JNI ハンドルから離れたところで単体テストできる）。
    ///
    /// 二つの規則：
    ///
    ///  * <see cref="RuleTail"/>（出荷済みの挙動）：**トリガで終わる**窓。名前がその約 0.8 秒で、
    ///    残りは部屋——埋め込みのどこにも「どの部分が呼びかけだったか」は書いていない。
    ///    2026-09-30 にテレビを点けた部屋で測定：名前一致のトリガ 114 件、全件 cos 0.40–0.75 で拒否。
    ///  * <see cref="RuleBest"/>：トリガ周辺の <see cref="SpanSamples"/> 内の**すべての窓**を、最良の
    ///    コサインで判定する。同じ測定で、テレビ自身の最良 1.5 秒は最大 0.660（40 区間中 0.689 超は 0 件）
    ///    ⇒ 広く見ることは基準を下げることではない。
    ///
    /// <see cref="SpanSamples"/> は、native のローリング PCM バッファに要求される大きさでもある。
    /// それが無いとバッファは 1.5 秒の窓に対して 0.985 秒しか持たず、不足分は**先頭**にゼロ埋め
    /// された——判定される窓の三分の一がデジタル無音で、それはモデルが学習・検証された条件ではない。
    ///
    /// このモジュールに**時計は無い**（入力はスパン長・窓・PCM だけ）⇒ `IClock`／`FakeClock` を必要としない。
    /// </summary>
    public static class GateWindow
    {
        public const int SampleRate = 16000;

        /// <summary>16 kHz で 1.5 秒：話者網が学習された窓であり、`Spk::cosine` が期待する長さ。</summary>
        public const int WindowSamples = 24000;

        /// <summary>3 秒のストリーム。ローリングバッファに持ち、[RuleBest] が見に行く場所を作る。</summary>
        public const int SpanSamples = 48000;

        /// <summary>候補窓の間隔は 0.25 秒。</summary>
        public const int StepSamples = 4000;

        /// <summary>保存する窓のリングサイズ（`cache/spk_win_&lt;i&gt;.wav`）。</summary>
        public const int Keep = 24;

        public const string RuleTail = "tail";
        public const string RuleBest = "best";

        /// <summary>
        /// 長さ <paramref name="spanSamples"/> のスパン内で採点すべき窓の開始インデックス。
        ///
        /// 常に少なくとも一つ、そして**スパンの末尾で終わる窓が必ず含まれる**：[RuleBest] の下では
        /// 出荷済みの窓は特別扱いではなく候補の一つなので、規則は受理するコサインを**上げることしか
        /// できない**。窓より短いスパンは開始 0 の一つを返し、[Window] が埋め草する——古い挙動で、
        /// マイクが開いた直後の数秒のために残してある。
        /// </summary>
        public static int[] Starts(int spanSamples, string rule)
        {
            // `(spanSamples - WINDOW_SAMPLES).coerceAtLeast(0)`
            var last = Math.Max(spanSamples - WindowSamples, 0);
            if (rule != RuleBest || last == 0) return new[] { last };
            var outp = new List<int>(last / StepSamples + 2);
            for (var s = 0; s <= last; s += StepSamples)
            {
                outp.Add(s);
            }
            if (outp[outp.Count - 1] != last) outp.Add(last);
            return outp.ToArray();
        }

        /// <summary>
        /// スパンから一つの窓を取り出す。**ちょうど <see cref="WindowSamples"/> の長さ**で、スパンが
        /// 短すぎるときは**前詰め**で埋め草する——`Kws::recent` と `Spk::cosine` が使うのと同じ慣習で、
        /// パディングの判断が下される唯一の場所。
        /// </summary>
        public static float[] Window(float[] span, int start)
        {
            var from = Clamp(start, 0, span.Length);
            var have = Clamp(span.Length - from, 0, WindowSamples);
            if (have == WindowSamples)
            {
                // `span.copyOfRange(from, from + WINDOW_SAMPLES)`——**コピー**（採点が元のスパンを汚せない）。
                var exact = new float[WindowSamples];
                Array.Copy(span, from, exact, 0, WindowSamples);
                return exact;
            }
            var outp = new float[WindowSamples];
            // `span.copyInto(out, WINDOW_SAMPLES - have, from, from + have)`——前詰め。
            if (have > 0) Array.Copy(span, from, outp, WindowSamples - have, have);
            return outp;
        }

        /// <summary>float サンプル → アプリが診断用に書く 16-bit PCM（**丸めではなく飽和**）。</summary>
        public static short[] Pcm16(float[] window)
        {
            var outp = new short[window.Length];
            for (var i = 0; i < window.Length; i++)
            {
                var v = window[i] * 32768f;
                // `.coerceIn(-32768f, 32767f)`（NaN はどちらにも当たらず素通りするが、入力に来ない）。
                if (v < -32768f) v = -32768f;
                else if (v > 32767f) v = 32767f;
                // Kotlin の `Float.toInt()`（0 方向に切り捨て・飽和）→ `Int.toShort()`（下位 16-bit に切る）。
                // ここは既に範囲内なので `(int)` は切り捨てと等価。
                outp[i] = unchecked((short)(int)v);
            }
            return outp;
        }

        /// <summary>
        /// 16-bit PCM の**最後の** <paramref name="samples"/> 個を [-1, 1] の float にする。短ければ**前詰め**。
        ///
        /// [Window] と native 側と同じ慣習なので、録音した発話から計算したコサインが、トリガ時に計算した
        /// ものと比較できる。
        ///
        /// **意図的に要素ごと**。歴史：以前はここが `System.arraycopy` で、それは型の合わない配列
        /// （`Incompatible types: src=short[], dst=float[]`）を拒んで例外を投げ、呼び手の `runCatching` が
        /// それを null コサインに変え、null コサインは**大げさに失敗しない——声紋検査を素通りする**。
        /// 2026-09-30 の測定：この一行でセッションの合併行列の声紋検査が全部効かなくなり、テレビの台詞
        /// 4 文が要求として送られ、ゲートはどれも健康に見えていた。
        /// （`commonMain` へ移した後はこの経路自体が成り立たない：`copyInto` に short→float の
        ///   オーバーロードは無く、型が違えばコンパイルが通らない。要素ごとの書き方は、それが同時に
        ///   「先頭にゼロを詰める」規則の実装でもあるので残す。）
        /// </summary>
        public static float[] FloatsOfPcm16(short[] pcm, int samples = WindowSamples)
        {
            var n = Math.Min(pcm.Length, samples);
            var outp = new float[samples];
            var from = pcm.Length - n;
            var pad = samples - n;
            for (var i = 0; i < n; i++) outp[pad + i] = pcm[from + i] / 32768f;
            return outp;
        }

        /// <summary>[index] 番目に保存された窓がキャッシュディレクトリで持つ名前。</summary>
        public static string KeptName(int index) =>
            "spk_win_" + index.ToString(CultureInfo.InvariantCulture) + ".wav";

        /// <summary>Kotlin の `coerceIn(lo, hi)`（ここでは lo &lt;= hi が常に成り立つ）。</summary>
        private static int Clamp(int v, int lo, int hi) => v < lo ? lo : (v > hi ? hi : v);
    }
}
