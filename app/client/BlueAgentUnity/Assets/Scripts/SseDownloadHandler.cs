using System;
using System.Collections.Generic;
using System.Text;
using BlueAgent.Logic;
using UnityEngine.Networking;

namespace BlueAgent.Spike
{
    /// <summary>
    /// 收位元組 → 一行一行切 → 一幀一幀交出去。**切割規則在共用邏輯層**（<see cref="SseStream"/>，
    /// 與手機那兩端同一份），這裡只是把 Unity 給的塊餵進去；收到的原始文字也留一份（失敗時要能
    /// 把橋回的錯誤內容寫進狀態列）。
    ///
    /// 為什麼抽成獨立一支（2026-10-16）：橋的**兩條**串流都要它——`POST /chat`（一輪的
    /// started…done）與 `GET /v1/ears/live`（耳朵那條常駐訂閱）。先前它是 `BridgeClient` 的
    /// 私有內嵌類別 ⇒ 第二條路只能複製一份，而複製的那一份一定會漂。
    /// </summary>
    public sealed class SseDownloadHandler : DownloadHandlerScript
    {
        private readonly SseStream _stream = new SseStream();
        private readonly Action<List<SseStream.Wire>> _sink;
        private readonly StringBuilder _text = new StringBuilder();

        public SseDownloadHandler(Action<List<SseStream.Wire>> sink) : base(new byte[8192])
        {
            _sink = sink;
        }

        /// <summary>網路上收過幾塊（診斷用；一塊 ≠ 一幀）。</summary>
        public int ChunkCount { get; private set; }

        /// <summary>收過幾個位元組（診斷用）。</summary>
        public int Bytes { get; private set; }

        /// <summary>收到的原始文字前 4 KB（HTTP 非 200 時橋回的是 JSON 而不是串流 ⇒ 要留著）。</summary>
        public string Text => _text.ToString();

        protected override bool ReceiveData(byte[] data, int dataLength)
        {
            if (data == null || dataLength <= 0) return false;
            ChunkCount++;
            Bytes += dataLength;
            if (_text.Length < 4096)
            {
                var take = Math.Min(dataLength, 4096 - _text.Length);
                _text.Append(Encoding.UTF8.GetString(data, 0, take));
            }
            var wires = _stream.Push(data, 0, dataLength);
            if (wires.Count > 0) _sink(wires);
            return true;
        }

        protected override void CompleteContent()
        {
            var tail = _stream.Flush();
            if (tail.Count > 0) _sink(tail);
        }
    }
}
