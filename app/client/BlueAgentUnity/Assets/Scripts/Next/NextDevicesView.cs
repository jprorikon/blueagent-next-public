using System;
using BlueAgent.Logic;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.UI;
using Object = UnityEngine.Object;

namespace BlueAgent.Next
{
    /// <summary>
    /// 端末管理（owner 2026-11-16：多設備配對，二維碼＋6 桁兩種都要；客人也可以多設備）。主人與客人都看得到。
    ///
    /// · 一覧：`GET /api/devices`——這個帳號的每一台端末，「この端末」標出自己；一台一台解除
    ///   （解除自己這台＝登出這台）。
    /// · 追加：`POST /api/pair/start` ⇒ 二維碼（伺服器畫好的 PNG）＋ 6 桁、5 分鐘有效、一次限り。
    ///   新端末用系統相機掃（開落地頁 → 一顆鈕跳回 App），或在登入頁輸入 6 桁。
    /// 配對碼只活在這一次進頁（換頁就不再畫）。
    /// </summary>
    public static class NextDevicesView
    {
        private static RectTransform _pane;
        private static NextShell _shell;
        private static string _code = "";
        private static string _qr = "";
        private static string _note = "";
        private static Texture2D _qrTexture;

        public static void Build(RectTransform pane, NextShell shell)
        {
            _pane = UiBuilder.Column("DevPane", pane, 0f, null, true, TextAnchor.UpperLeft);
            UiBuilder.Weight(_pane, 1f);
            _shell = shell;
            _code = _qr = _note = "";
            Reload();
        }

        private static void Clear()
        {
            if (_pane == null) return;
            for (var i = _pane.childCount - 1; i >= 0; i--) Object.Destroy(_pane.GetChild(i).gameObject);
        }

        private static void Reload()
        {
            if (_shell == null) return;
            _shell.Api.Get("/api/devices", reply =>
            {
                if (_shell.HandleAuth(reply)) return;
                if (!reply.Ok)
                {
                    Render(null, NextStrings.F("dev_list_fail", "reason", reply.UserMessage()));
                    return;
                }
                Render(reply.Json.OptJArray("devices"), _note);
            });
        }

        private static void Render(JArray devices, string note)
        {
            Clear();
            if (_pane == null) return;
            var column = UiBuilder.Column("DevBody", _pane, 7f, new RectOffset(10, 10, 4, 10), true, TextAnchor.UpperLeft);
            UiBuilder.Weight(column, 1f);

            if (!string.IsNullOrEmpty(_code)) BuildPairing(column);

            var listCard = NextUi.Card("DevListCard", column, 11f, 6f);
            UiBuilder.Weight(listCard, 0f);
            NextUi.Section("DevListHead", listCard, NextStrings.Get("dev_list_title"), "phone");
            for (var i = 0; devices != null && i < devices.Length(); i++)
            {
                var device = devices.OptJObject(i);
                if (device == null || device.OptBoolean("revoked")) continue;
                BuildRow(listCard, device, i, devices);
            }

            var addCard = NextUi.Card("DevAddCard", column, 11f, 5f);
            UiBuilder.Weight(addCard, 0f);
            NextUi.Section("DevAddHead", addCard, NextStrings.Get("dev_add_head"), "plus");
            NextUi.Note("DevAddHint", addCard, NextStrings.Get("dev_add_hint"));
            var add = NextUi.Button("DevAdd", addCard, NextStrings.Get("dev_add"), NextUi.Style.Accent, 36f, 0f, "plus");
            add.onClick.AddListener(() => StartPairing(devices));

            if (!string.IsNullOrEmpty(note)) NextUi.Note("DevNote", column, note, NextUi.Danger);
            UiBuilder.Settle((RectTransform)_pane);
        }

        private static void BuildPairing(RectTransform column)
        {
            var card = NextUi.Card("DevPairCard", column, 12f, 6f);
            UiBuilder.Weight(card, 0f);
            NextUi.Section("DevPairHead", card, NextStrings.Get("dev_pair_ready"), "check");
            var texture = QrTexture(_qr);
            if (texture != null)
            {
                var holder = UiBuilder.Row("DevPairQrRow", card, 0f, null, TextAnchor.MiddleCenter);
                UiBuilder.FixedHeight(holder, 216f);
                var go = new GameObject("DevPairQr", typeof(RectTransform), typeof(RawImage));
                var rect = (RectTransform)go.transform;
                rect.SetParent(holder, false);
                go.GetComponent<RawImage>().texture = texture;
                go.GetComponent<RawImage>().raycastTarget = false;
                UiBuilder.FixedWidth(rect, 208f);
                UiBuilder.FixedHeight(rect, 208f);
                rect.sizeDelta = new Vector2(208f, 208f);
            }
            var code = UiBuilder.Text("DevPairCode", card, _code.Length == 6 ? _code.Substring(0, 3) + " " + _code.Substring(3) : _code,
                                      34f, NextTheme.Ink, TextAlignmentOptions.Center, noWrap: true, bold: true);
            UiBuilder.FixedHeight((RectTransform)code.transform, 46f);
            NextUi.Note("DevPairNote", card, NextStrings.Get("dev_pair_note"));
        }

        private static Texture2D QrTexture(string dataUri)
        {
            const string prefix = "data:image/png;base64,";
            if (string.IsNullOrEmpty(dataUri) || !dataUri.StartsWith(prefix, StringComparison.Ordinal)) return null;
            try
            {
                var bytes = Convert.FromBase64String(dataUri.Substring(prefix.Length));
                if (_qrTexture != null) Object.Destroy(_qrTexture);
                _qrTexture = new Texture2D(2, 2, TextureFormat.RGBA32, false) { filterMode = FilterMode.Point };
                return _qrTexture.LoadImage(bytes) ? _qrTexture : null;
            }
            catch (Exception error)
            {
                Debug.LogWarning("[devices] 二維碼解不開：" + error.Message);
                return null;
            }
        }

        private static void StartPairing(JArray devices)
        {
            var body = new JObject().Put("base_url", _shell.Api.BaseUrl);
            _shell.Api.Post("/api/pair/start", body, reply =>
            {
                if (_shell.HandleAuth(reply)) return;
                if (!reply.Ok || reply.Json == null)
                {
                    _note = NextStrings.F("dev_add_fail", "reason", reply.UserMessage());
                    Render(devices, _note);
                    return;
                }
                _code = reply.Json.OptString("code");
                _qr = reply.Json.OptString("qr");
                _note = "";
                Render(devices, _note);
            });
        }

        private static void BuildRow(RectTransform card, JObject device, int index, JArray devices)
        {
            var id = device.OptString("device_id");
            var current = device.OptBoolean("current");
            var row = UiBuilder.Column("DevRow" + index, card, 3f, new RectOffset(8, 8, 6, 6), true, TextAnchor.UpperLeft);
            UiBuilder.Backdrop("DevRowBg" + index, row, NextTheme.Field, radius: 6f);
            var top = UiBuilder.Row("DevRowTop" + index, row, 6f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(top, 18f);
            var name = id == "primary" ? NextStrings.Get("dev_primary") : device.OptString("name");
            UiBuilder.Text("DevRowName" + index, top, name, 12.5f, NextTheme.Ink,
                           TextAlignmentOptions.MidlineLeft, weight: 1f, noWrap: true, bold: true);
            if (current) NextUi.Badge("DevRowCurrent" + index, top, NextStrings.Get("dev_current"),
                                      NextTheme.SkyPale, NextTheme.SkyDeep);
            NextUi.KeyValue("DevRowSeen" + index, row, NextStrings.Get("dev_last_seen"),
                            When(device.OptDouble("last_seen_at")));

            var actions = UiBuilder.Row("DevRowActions" + index, row, 6f, null, TextAnchor.MiddleRight);
            UiBuilder.FixedHeight(actions, 30f);
            var revoke = NextUi.Button("DevRowRevoke" + index, actions, NextStrings.Get("dev_revoke"), NextUi.Style.Danger,
                                       28f, 0f, "close", true, 11f);
            revoke.onClick.AddListener(() =>
            {
                _shell.Api.Post("/api/devices/revoke", new JObject().Put("device_id", id), reply =>
                {
                    if (_shell.HandleAuth(reply)) return;
                    if (!reply.Ok)
                    {
                        _note = NextStrings.F("dev_revoke_fail", "reason", reply.UserMessage());
                        Render(devices, _note);
                        return;
                    }
                    if (current) { _shell.Logout(); return; }   // 解除自己這台＝登出這台
                    _note = "";
                    Reload();
                });
            });
        }

        private static string When(double unix)
        {
            if (unix <= 0) return "—";
            return DateTimeOffset.FromUnixTimeSeconds((long)unix).ToLocalTime().ToString("yyyy-MM-dd HH:mm");
        }
    }
}
