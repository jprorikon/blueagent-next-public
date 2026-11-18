using System;
using System.Collections;
using BlueAgent.Logic;
using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// 公共區 Outbox worker：按建立順序一次送一筆；成功／失敗都先落 SQLite，UI 不直接拿網路回包當真值。
    /// 投稿與回覆重用 client_mutation_id；點讚與刪除沿用服務端自然冪等語意。
    /// </summary>
    public sealed class NextSocialSync
    {
        /// <summary>一筆變更送出後的結局。</summary>
        public enum Outcome { Accepted, Rejected, Deferred }

        /// <summary>
        /// 每筆變更有結果就通知一次（2026-11-08）：以前 worker 只寫 SQLite、不告訴畫面 ⇒
        /// 服務端早已收下，帖子／回覆仍掛著「送信待ち」直到手動刷新；投稿提示也只能一律說「聯網後發送」。
        /// `Deferred`＝網路／5xx／429，留在佇列等下一次 Kick；`code` 是服務端錯誤碼（成功時空字串）。
        /// </summary>
        public event Action<SocialMutation, Outcome, string> Settled;

        private readonly NextApi _api;
        private bool _draining;
        private Coroutine _routine;

        public NextSocialSync(NextApi api) { _api = api; }

        public void Kick()
        {
            if (_draining || _api.Social == null) return;
            _routine = _api.StartCoroutine(Drain());
        }

        /// <summary>換帳號／銷毀 API 時停掉舊 Repository 的等待；舊 HTTP 回包不再能寫新資料庫。</summary>
        public void Cancel()
        {
            if (_routine != null) _api.StopCoroutine(_routine);
            _routine = null;
            _draining = false;
        }

        private IEnumerator Drain()
        {
            _draining = true;
            try
            {
                while (_api.Social != null)
                {
                    var pending = _api.Social.PendingMutations();
                    if (pending.Count == 0) yield break;
                    var mutation = pending[0];
                    var now = Now();
                    _api.Social.MarkAttempt(mutation.Id, now);
                    NextApi.Reply reply = null;
                    yield return UploadPendingMedia(mutation, value => reply = value);
                    if (_api.Social == null) yield break;
                    if (reply == null)
                    {
                        mutation = _api.Social.Mutation(mutation.Id) ?? mutation;   // 佔位已換成伺服器 id
                        yield return Send(mutation, value => reply = value);
                    }
                    if (_api.Social == null) yield break;
                    if (reply != null && (reply.Ok || (reply.Status == 404 && IsDelete(mutation))))
                    {
                        // 刪除重試時 404 代表目標已不存在，與成功後的終態一致；其他 404 不可當成功。
                        _api.Social.MarkAccepted(mutation.Id,
                            reply.Json ?? new JObject().Put("ok", true).Put("id", mutation.Payload.OptString("id")), Now());
                        Notify(mutation, Outcome.Accepted, "");
                        continue;
                    }
                    var transport = reply == null || reply.Status == 0 || reply.Status == 429 || reply.Status >= 500;
                    var code = reply == null ? "no_response" : reply.Code;
                    _api.Social.MarkFailed(mutation.Id, code, transport, Now());
                    Notify(mutation, transport ? Outcome.Deferred : Outcome.Rejected, code);
                    if (transport) yield break;       // 保留順序；下一次 Kick 從同一筆繼續
                }
            }
            finally
            {
                _routine = null;
                _draining = false;
            }
        }

        private IEnumerator Send(SocialMutation mutation, Action<NextApi.Reply> done)
        {
            var payload = mutation.Payload;
            var path = "";
            var delete = false;
            switch (mutation.Kind)
            {
                case "post.create":
                    path = NextBoardPaths.Posts;
                    payload.Put("client_mutation_id", mutation.Id);
                    break;
                case "reply.create":
                    path = NextBoardPaths.Replies(payload.OptString("id"));
                    payload.Put("client_mutation_id", mutation.Id);
                    break;
                case "post.like":
                    path = NextBoardPaths.Like(payload.OptString("id"));
                    break;
                case "post.delete":
                case "reply.delete":
                    path = NextBoardPaths.Post(payload.OptString("id"));
                    delete = true;
                    break;
                default:
                    done(new NextApi.Reply { Status = 400,
                        Json = new JObject().Put("error", "unknown_mutation") });
                    yield break;
            }
            var completed = false;
            if (delete) _api.Delete(path, reply => { completed = true; done(reply); });
            else _api.Post(path, payload, reply => { completed = true; done(reply); });
            while (!completed) yield return null;
        }

        /// <summary>
        /// 投稿／回覆裡的離線佔位圖先上傳（NextOutboxMedia，2026-11-17）：一張成功就換成伺服器 id 落庫、刪本機檔，
        /// 重送時不會再傳第二次。上傳失敗 ⇒ 把那個失敗當成這筆的結果交回（連不上＝Deferred 留在佇列、拒收＝Rejected）；
        /// 全部傳完 ⇒ 不交結果（呼叫端接著發帖）。本機檔已不見（被系統清掉）⇒ 拿掉那張照發，不讓整筆卡死。
        /// </summary>
        private IEnumerator UploadPendingMedia(SocialMutation mutation, Action<NextApi.Reply> failed)
        {
            if (mutation.Kind != "post.create" && mutation.Kind != "reply.create") yield break;
            var media = mutation.Payload.OptJArray("media");
            if (media == null) yield break;
            var repository = _api.Social;
            var ids = new System.Collections.Generic.List<string>();
            for (var i = 0; i < media.Length(); i++)
            {
                var id = media.OptString(i);
                if (SocialRepository.IsPendingMedia(id)) ids.Add(id);
            }
            foreach (var pending in ids)
            {
                var bytes = NextOutboxMedia.Read(pending);
                if (bytes == null)
                {
                    Debug.LogWarning("[outbox-media] 本機圖已不在，這張不送：" + pending);
                    repository.ResolveMedia(mutation.Id, pending, "");
                    continue;
                }
                var completed = false;
                var remote = "";
                var status = 0;
                var reason = "";
                _api.UploadMedia(bytes, "", json =>
                {
                    remote = json != null ? json.OptString("id") : "";
                    completed = true;
                }, (code, message) =>
                {
                    status = code;
                    reason = message;
                    completed = true;
                });
                // 換帳號時上傳的回呼會被 Session 擋掉、永遠不來 ⇒ 庫換了就別再等
                while (!completed && ReferenceEquals(repository, _api.Social)) yield return null;
                if (!ReferenceEquals(repository, _api.Social)) yield break;
                if (string.IsNullOrEmpty(remote))
                {
                    failed(new NextApi.Reply { Status = status,
                        Json = new JObject().Put("error", status == 0 ? "media_offline" : "media_rejected")
                                            .Put("detail", reason ?? "") });
                    yield break;
                }
                repository.ResolveMedia(mutation.Id, pending, remote);
                NextOutboxMedia.Delete(pending);
            }
        }

        private void Notify(SocialMutation mutation, Outcome outcome, string code)
        {
            // 畫面重畫出錯不能卡住佇列：吞掉並留日誌，下一筆照送。
            try { Settled?.Invoke(mutation, outcome, code ?? ""); }
            catch (Exception error) { Debug.LogException(error); }
        }

        private static bool IsDelete(SocialMutation mutation)
        {
            return mutation.Kind == "post.delete" || mutation.Kind == "reply.delete";
        }

        private static double Now() { return DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0; }
    }
}
