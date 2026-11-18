using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.XR.Hands;

namespace BlueAgent.MR
{
    /// <summary>
    /// 捏手指（手部追蹤；PLAN-mr-quest3 M3，owner 2026-11-01）：拇指尖與食指尖的距離，帶遲滯——
    /// 小於 <see cref="PinchOn"/> 算捏住、大於 <see cref="PinchOff"/> 才算放開（中間不來回跳）。左右手各算各的。
    ///
    /// 只回報「哪隻手、在世界的哪一點捏下／放開」與「現在捏在哪」；捏兩下說話、捏她拿起來這些意思由 `MrShell` 決定。
    /// owner：「VR 對手勢識別不是很精確」⇒ 只用最穩的一種手勢（捏），不認別的手形。
    /// </summary>
    public sealed class MrPinch : MonoBehaviour
    {
        /// <summary>拇指尖—食指尖小於這個（公尺）＝捏住。</summary>
        private const float PinchOn = 0.015f;
        /// <summary>大於這個才算放開（遲滯：手抖不會一下捏一下放）。</summary>
        private const float PinchOff = 0.03f;
        /// <summary>
        /// 放開要**持續**這麼久（秒）才算數；中途又捏上（或手只是丟了一兩幀又找回來）＝同一下還沒放。
        /// 2026-11-01 實機：一下捏常被拆成「放開 0.01–0.10 秒又捏上」⇒ 被當成雙擊，通話自己開關、角色連換。
        /// 有意的雙擊間隔實測 0.33–0.75 秒，不受影響。
        /// </summary>
        private const float ReleaseHold = 0.12f;

        /// <summary>某隻手捏下（0＝左、1＝右；世界座標的捏點）。</summary>
        public event Action<int, Vector3> Down;
        /// <summary>某隻手放開（放開前最後的捏點）。</summary>
        public event Action<int, Vector3> Up;

        /// <summary>手部追蹤有沒有在跑（沒有＝頭顯的「手部追蹤」沒開、或權限沒給）。</summary>
        public bool Running => _hands != null && _hands.running;

        private Transform _space;
        private XRHandSubsystem _hands;
        private readonly bool[] _pinched = new bool[2];
        private readonly Vector3[] _point = new Vector3[2];
        /// <summary>這隻手從什麼時候開始看起來是放開的（-1＝沒有在放開中）。</summary>
        private readonly float[] _releaseFrom = { -1f, -1f };
        private readonly List<XRHandSubsystem> _found = new List<XRHandSubsystem>();

        /// <summary><paramref name="trackingSpace"/>：手的姿勢是相對它的（XR Origin 的 Camera Offset，跟頭顯姿勢同一個座標系）。</summary>
        public static MrPinch Create(Transform trackingSpace)
        {
            var pinch = new GameObject("MR Pinch").AddComponent<MrPinch>();
            pinch._space = trackingSpace;
            return pinch;
        }

        /// <summary>這隻手現在捏著的那一點（沒捏 ⇒ false）。</summary>
        public bool Holding(int hand, out Vector3 point)
        {
            point = _point[hand];
            return _pinched[hand];
        }

        private void Update()
        {
            if (_hands == null || !_hands.running)
            {
                SubsystemManager.GetSubsystems(_found);
                _hands = null;
                foreach (var subsystem in _found)
                {
                    if (subsystem.running) { _hands = subsystem; break; }
                }
                if (_hands == null) return;
            }
            Track(0, _hands.leftHand);
            Track(1, _hands.rightHand);
        }

        private void Track(int index, XRHand hand)
        {
            if (!hand.isTracked
                || !hand.GetJoint(XRHandJointID.ThumbTip).TryGetPose(out var thumb)
                || !hand.GetJoint(XRHandJointID.IndexTip).TryGetPose(out var tip))
            {
                // 手不見了（出視野、換回手柄）：捏著的當作放開（免得一直抓著她）——但跟手指張開一樣要持續 ReleaseHold
                if (_pinched[index]) Releasing(index);
                return;
            }
            var distance = Vector3.Distance(thumb.position, tip.position);
            var middle = (thumb.position + tip.position) * 0.5f;
            _point[index] = _space != null ? _space.TransformPoint(middle) : middle;
            if (!_pinched[index] && distance < PinchOn)
            {
                _pinched[index] = true;
                _releaseFrom[index] = -1f;
                Down?.Invoke(index, _point[index]);
            }
            else if (_pinched[index] && distance > PinchOff) Releasing(index);
            else if (_pinched[index]) _releaseFrom[index] = -1f;          // 又捏回來了：剛才那一下張開不算
        }

        /// <summary>看起來放開了：開始計時，持續 <see cref="ReleaseHold"/> 才真的送 `Up`。</summary>
        private void Releasing(int index)
        {
            var now = Time.unscaledTime;
            if (_releaseFrom[index] < 0f)
            {
                _releaseFrom[index] = now;
                return;
            }
            if (now - _releaseFrom[index] < ReleaseHold) return;
            _pinched[index] = false;
            _releaseFrom[index] = -1f;
            Up?.Invoke(index, _point[index]);
        }
    }
}
