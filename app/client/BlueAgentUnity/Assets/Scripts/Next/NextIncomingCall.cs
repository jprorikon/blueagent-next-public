using System;
using BlueAgent.Logic;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// **她打來的電話**（owner 2026-11-17）：全屏來電畫面——頭像、名字、「応答」「拒否」、鈴聲。
    ///
    /// 來源：App Server 的 `call.incoming`（只給主人，見 `app/server/voice_outgoing.py`）。事件只當「去問一下」的訊號，
    /// 真值一律是 `GET /v1/calls/incoming`：重連重放舊事件、App 從背景回來、點系統通知進來，都走同一個
    /// <see cref="Check"/>——還在響才顯示，響完的不補畫。
    /// · 応答 ⇒ `POST /v1/calls/{id}/accept`（伺服器先叫她那頭掛上通話）成功 ⇒ 殼直接進現有的通話畫面（不再撥號）；
    /// · 拒否 ⇒ `POST /v1/calls/{id}/decline`；響鈴中每秒問一次 `/state`，她掛掉或 30 秒逾時就收起來。
    /// Android 背景由 Java `NoticeListen` 讀同一條 SSE 彈系統通知；**iOS 背景響不了**（沒有 APNs／PushKit／CallKit），
    /// 回到前景時若還在響才會看到這個畫面（`docs/PENDING.md` 記為能力邊界）。
    /// </summary>
    public sealed class NextIncomingCall : MonoBehaviour
    {
        private const float PollSeconds = 1f;
        private const float VibrateSeconds = 1.6f;

        private static NextIncomingCall _showing;
        /// <summary>主人已經處理過（接／拒／收起）的那一通：重放的事件不再彈。</summary>
        private static string _handled = "";

        private NextShell _shell;
        private string _callId = "";
        private string _entity = "";
        private string _display = "";
        private bool _busy;
        private float _nextPoll;
        private float _nextVibrate;
        private AudioSource _ring;
        private Component _status;

        /// <summary>事件流上的 `call.incoming`／`call.updated`。</summary>
        public static void OnEvent(NextShell shell, string eventName, JObject payload)
        {
            if (shell == null || !shell.IsMaster) return;
            if (eventName == "call.updated")
            {
                var id = payload != null ? payload.OptString("call_id") : "";
                if (_showing != null && _showing._callId == id && payload.OptString("state") != "ringing")
                    _showing.Gone(payload.OptString("state") == "missed");
                return;
            }
            Check(shell);
        }

        /// <summary>問伺服器：現在有沒有她打來、還在響的那一通（回前景／重連／收到事件都叫這裡）。</summary>
        public static void Check(NextShell shell)
        {
            if (shell == null || !shell.IsMaster || shell.Api == null) return;
            shell.Api.Get("/v1/calls/incoming", reply =>
            {
                if (!reply.Ok || reply.Json == null || shell == null) return;
                var session = reply.Json.OptJObject("session");
                if (session == null)
                {
                    if (_showing != null && !_showing._busy) _showing.Gone(false);
                    return;
                }
                var id = session.OptString("id");
                if (string.IsNullOrEmpty(id) || id == _handled) return;
                if (_showing != null && _showing._callId == id) return;
                Show(shell, id, session.OptString("entity"));
            });
        }

        private static void Show(NextShell shell, string callId, string entity)
        {
            var root = shell.OverlayRoot;
            if (root == null) return;
            if (_showing != null) Destroy(_showing.gameObject);
            var overlay = UiBuilder.Overlay("IncomingCall", root);
            var bg = overlay.gameObject.AddComponent<Image>();
            bg.color = new Color(NextTheme.Navy.r, NextTheme.Navy.g, NextTheme.Navy.b, 0.97f);
            bg.raycastTarget = true;                         // 蓋住底下的畫面，點不穿
            // 防點穿（2026-11-17 模擬器實測：有一次「応答」的點擊落到底下公共區的帖子上，事後重現不出來）：
            // 來電畫面自己一張最上層的畫布＋射線器，響鈴期間把底下那張畫布的射線器關掉——
            // 不管原因是什麼，點擊都只可能落在來電畫面上。收起時（Close／OnDestroy）恢復。
            var ownCanvas = overlay.gameObject.AddComponent<Canvas>();
            ownCanvas.overrideSorting = true;
            ownCanvas.sortingOrder = 1000;
            overlay.gameObject.AddComponent<GraphicRaycaster>();
            var view = overlay.gameObject.AddComponent<NextIncomingCall>();
            view.BlockBelow(root);
            view._shell = shell;
            view._callId = callId;
            view._entity = entity ?? "";
            view._display = shell.DisplayNameOf(view._entity);
            view.BuildUi(overlay);
            view.StartRinging();
            overlay.SetAsLastSibling();
            _showing = view;
            Debug.Log("[incoming] 她打來了：" + callId + " " + entity);
            UiBuilder.Settle((RectTransform)root);
        }

        /// <summary>響鈴期間關掉的底層射線器（收起時恢復）。</summary>
        private GraphicRaycaster _blocked;

        private void BlockBelow(Transform root)
        {
            var below = root != null ? root.GetComponent<GraphicRaycaster>() : null;
            if (below == null || !below.enabled) return;
            below.enabled = false;
            _blocked = below;
        }

        private void UnblockBelow()
        {
            if (_blocked != null) _blocked.enabled = true;
            _blocked = null;
        }

        private void BuildUi(RectTransform overlay)
        {
            var column = UiBuilder.Column("IncomingBody", overlay, 14f, new RectOffset(24, 24, 72, 56), true,
                                          TextAnchor.UpperCenter);
            UiBuilder.Text("IncomingTitle", column, NextStrings.Get("incoming_title"), NextTheme.LabelSize,
                           NextTheme.SkyPale, TextAlignmentOptions.Center, noWrap: true, bold: true);
            var faceRow = UiBuilder.Row("IncomingFaceRow", column, 0f, null, TextAnchor.MiddleCenter);
            UiBuilder.FixedHeight(faceRow, 132f);
            NextUi.Avatar("IncomingFace", faceRow, _entity, 120f, NextTheme.Sky);
            var name = UiBuilder.Text("IncomingName", column, _display, NextTheme.HeadSize, NextTheme.OnColor,
                                      TextAlignmentOptions.Center, noWrap: true, bold: true);
            NextUi.Italic(name);
            _status = UiBuilder.Text("IncomingStatus", column, NextStrings.F("incoming_ringing", "name", _display),
                                     NextTheme.BodySize, NextTheme.SkyPale, TextAlignmentOptions.Center);
            var spacer = UiBuilder.NewRect("IncomingSpacer", column);
            UiBuilder.Weight(spacer, 1f);
            var controls = UiBuilder.Row("IncomingControls", column, 56f, null, TextAnchor.MiddleCenter);
            UiBuilder.FixedHeight(controls, 104f);
            Choice(controls, "IncomingDecline", "phone-off", NextUi.Style.Danger, NextStrings.Get("incoming_decline"),
                   Decline);
            Choice(controls, "IncomingAccept", "phone", NextUi.Style.Primary, NextStrings.Get("incoming_accept"),
                   Accept);
        }

        private static void Choice(RectTransform parent, string name, string icon, NextUi.Style style, string label,
                                   Action click)
        {
            var cell = UiBuilder.Column(name + "Cell", parent, 8f, null, false, TextAnchor.UpperCenter);
            UiBuilder.FixedWidth(cell, 72f);                  // 跟圓鈕同寬：欄會把子節點撐滿寬，寬了就變橢圓
            var button = NextUi.IconButton(name, cell, icon, style, 72f, round: true);
            button.onClick.AddListener(() => click());
            UiBuilder.Text(name + "Label", cell, label, NextTheme.NoteSize, NextTheme.OnColor,
                           TextAlignmentOptions.Center, noWrap: true);
        }

        // ── 鈴聲 ────────────────────────────────────────────────────────────────

        private void StartRinging()
        {
            _ring = gameObject.AddComponent<AudioSource>();
            _ring.clip = RingClip();
            _ring.loop = true;
            _ring.volume = 0.7f;
            _ring.playOnAwake = false;
            _ring.Play();
            _nextPoll = Time.realtimeSinceStartup + PollSeconds;
            _nextVibrate = Time.realtimeSinceStartup;
        }

        private static AudioClip _clip;

        /// <summary>程式產生的鈴聲（不帶音檔）：兩聲「嘟嚕」＋停頓，3 秒一輪。</summary>
        private static AudioClip RingClip()
        {
            if (_clip != null) return _clip;
            const int rate = 22050;
            var samples = new float[rate * 3];
            for (var i = 0; i < samples.Length; i++)
            {
                var t = (float)i / rate;
                var on = t < 0.4f || (t >= 0.6f && t < 1.0f);
                if (!on) continue;
                var local = t < 0.4f ? t : t - 0.6f;
                var envelope = Mathf.Min(1f, local / 0.02f) * Mathf.Min(1f, (0.4f - local) / 0.02f);
                samples[i] = 0.25f * envelope * (Mathf.Sin(2f * Mathf.PI * 440f * t) + Mathf.Sin(2f * Mathf.PI * 480f * t));
            }
            _clip = AudioClip.Create("incoming-ring", samples.Length, 1, rate, false);
            _clip.SetData(samples, 0);
            return _clip;
        }

        private void StopRinging()
        {
            if (_ring != null && _ring.isPlaying) _ring.Stop();
        }

        // ── 接／拒／收起 ────────────────────────────────────────────────────────

        private void Accept()
        {
            if (_busy) return;
            _busy = true;                                     // 先擋連按；_handled 等伺服器確認才標（審核 OC-1）
            StopRinging();
            NextUi.SetText(_status, NextStrings.Get("incoming_connecting"));
            var shell = _shell;
            var id = _callId;
            var entity = _entity;
            shell.Api.Call("accept", entity, id, reply =>
            {
                switch (IncomingCallReply.Classify(reply.Ok, reply.Status))
                {
                    case IncomingCallReply.Kind.Done:
                        _handled = id;
                        Close();
                        if (shell != null) shell.AnswerCall(id, entity);
                        return;
                    case IncomingCallReply.Kind.Gone:
                        _handled = id;
                        Close();
                        if (shell != null) NextToast.Show(shell.ToastHost, NextStrings.Get("incoming_gone"));
                        return;
                    default:
                        Debug.Log("[incoming] 接聽沒成功，可以再按：" + reply.Describe());
                        Retry(reply);
                        return;
                }
            });
        }

        private void Decline()
        {
            if (_busy) return;
            _busy = true;
            StopRinging();
            var id = _callId;
            // 等伺服器確認才收起（審核 OC-1：以前不等回應就關，送不到時伺服器還在響、這一通卻再也叫不出來）
            _shell.Api.Call("decline", _entity, id, reply =>
            {
                if (IncomingCallReply.Classify(reply.Ok, reply.Status) == IncomingCallReply.Kind.Retry)
                {
                    Debug.Log("[incoming] 拒接沒送到，可以再按：" + reply.Describe());
                    Retry(reply);
                    return;
                }
                _handled = id;
                Close();
            });
        }

        /// <summary>接聽／拒接沒送到（斷網、逾時、5xx）：畫面留著、說明原因、鈴聲繼續，按鈕恢復可再按。</summary>
        private void Retry(NextApi.Reply reply)
        {
            if (this == null) return;                         // 畫面已被收掉（例如她先掛了）
            _busy = false;
            NextUi.SetText(_status, NextStrings.F("call_failed", "reason", reply.UserMessage()));
            if (_ring != null && !_ring.isPlaying) _ring.Play();
            _nextPoll = Time.realtimeSinceStartup + PollSeconds;
        }

        /// <summary>她掛掉了／沒接到（逾時）：收起來，留一句提示。</summary>
        private void Gone(bool missed)
        {
            _handled = _callId;
            StopRinging();
            var shell = _shell;
            var text = missed ? NextStrings.F("incoming_missed", "name", _display) : NextStrings.Get("incoming_gone");
            Close();
            if (shell != null) NextToast.Show(shell.ToastHost, text);
        }

        private void Close()
        {
            UnblockBelow();                                   // 先放開：接聽後要進的通話畫面、提示都得點得到
            if (_showing == this) _showing = null;
            if (this != null) Destroy(gameObject);
        }

        private void Update()
        {
            if (_busy || _shell == null) return;
            var now = Time.realtimeSinceStartup;
#if UNITY_ANDROID || UNITY_IOS
            if (now >= _nextVibrate)
            {
                _nextVibrate = now + VibrateSeconds;
                Handheld.Vibrate();
            }
#endif
            if (now < _nextPoll) return;
            _nextPoll = now + PollSeconds;
            var id = _callId;
            _shell.Api.Call("state", _entity, id, reply =>
            {
                if (this == null || _busy || id != _callId) return;
                if (reply.Status == 404) { Gone(false); return; }
                if (!reply.Ok || reply.Json == null) return;
                var state = reply.Json.OptString("state");
                if (state != "ringing") Gone(state == "missed");
            });
        }

        private void OnDestroy()
        {
            UnblockBelow();
            if (_showing == this) _showing = null;
            StopRinging();
        }
    }
}
