using System;

namespace BlueAgent.Logic
{
    /// <summary>
    /// 16-bit PCM → WAV 檔的位元組（**純算式**，沒有平台）——`blueagent/live2d/android/kmp/src/
    /// commonMain/.../WavBytes.kt` 的**逐行移植**（真值＝Kotlin 側）。
    /// 等價性由 `fixtures/wav_bytes.json` 逐條對拍（每一條都是「輸入 PCM／取樣率 ⇒ 位元組」），
    /// 不靠「看起來差不多」。
    ///
    /// 為什麼在共用層：`WakeKws` 的聲紋除錯窗（`cache/spk_win_*.wav`）要落盤，而安卓原本的
    /// `Wav.writePcm16`（在 `VoiceTurn.kt`）吃 `java.io.File` ⇒ 搬不過去。把「44 byte 標頭 ＋
    /// little-endian PCM」抽成這裡，安卓的 `Wav.writePcm16` 改成呼叫它（單一真相），共用層則配
    /// `Fs.writeBytes` 落盤。
    ///
    /// 輸出必須與原本的 `Wav.writePcm16` **一個位元組都不差**——檔頭格式是 whisper／VAD 都要吃的。
    /// </summary>
    public static class WavBytes
    {
        /// <summary>16 kHz 單聲道 16-bit PCM 的 RIFF/WAVE 位元組。</summary>
        public static byte[] Encode(short[] pcm, int sampleRate)
        {
            var dataBytes = pcm.Length * 2;
            var outp = new byte[44 + dataBytes];
            Ascii(outp, 0, "RIFF");
            Int32(outp, 4, 36 + dataBytes);
            Ascii(outp, 8, "WAVE");
            Ascii(outp, 12, "fmt ");
            Int32(outp, 16, 16);
            Int16(outp, 20, 1);                       // PCM
            Int16(outp, 22, 1);                       // mono
            Int32(outp, 24, sampleRate);
            Int32(outp, 28, sampleRate * 2);          // byte rate
            Int16(outp, 32, 2);                       // block align
            Int16(outp, 34, 16);                      // bits per sample
            Ascii(outp, 36, "data");
            Int32(outp, 40, dataBytes);
            for (var i = 0; i < pcm.Length; i++)
            {
                // ⚠️ Kotlin 的 `Short.toInt()` 是**符號擴張**（-2 ⇒ -2，不是 65534）；
                // C# 的 `(int)short` 相同 ⇒ 直接轉。下兩行的 `>> 8` 對負值是**算術位移**，
                // 兩邊一致。
                var v = (int)pcm[i];
                outp[44 + i * 2] = (byte)(v & 0xFF);
                outp[44 + i * 2 + 1] = (byte)((v >> 8) & 0xFF);
            }
            return outp;
        }

        /// <summary>`dst[at + i] = s[i].code.toByte()`（只有 ASCII 會被傳進來）。</summary>
        private static void Ascii(byte[] dst, int at, string s)
        {
            for (var i = 0; i < s.Length; i++) dst[at + i] = (byte)s[i];
        }

        /// <summary>little-endian 32-bit（照 Kotlin 逐位元組寫；**不要**換成 `BitConverter`——它跟機器位元序有關）。</summary>
        private static void Int32(byte[] dst, int at, int value)
        {
            dst[at] = (byte)(value & 0xFF);
            dst[at + 1] = (byte)((value >> 8) & 0xFF);
            dst[at + 2] = (byte)((value >> 16) & 0xFF);
            dst[at + 3] = (byte)((value >> 24) & 0xFF);
        }

        /// <summary>little-endian 16-bit。</summary>
        private static void Int16(byte[] dst, int at, int value)
        {
            dst[at] = (byte)(value & 0xFF);
            dst[at + 1] = (byte)((value >> 8) & 0xFF);
        }
    }
}
