using System;
using System.Collections;
using System.Collections.Generic;
using BlueAgent.Logic;
using UnityEngine;
using UnityEngine.Networking;

namespace BlueAgent.Next
{
    /// <summary>
    /// S3 聊天同步器：Outbox 一次 drain 一筆；回包、分頁與 SSE 全部只寫 ChatRepository。
    /// 初始同步先向後補齊歷史，再記最新 seq；之後只用 after 向前增量，避免漏掉中間頁。
    /// </summary>
    public sealed class NextChatSync
    {
        private readonly NextApi _api;
        private readonly HashSet<string> _syncing = new HashSet<string>(StringComparer.Ordinal);
        private Coroutine _drainRoutine;
        private bool _draining;
        private int _epoch;
        private readonly HashSet<string> _readPosted = new HashSet<string>(StringComparer.Ordinal);

        public NextChatSync(NextApi api) { _api = api; }
        /// <summary>本地 id 已被服務端 id 取代；UI 依 mutation 收掉對應的暫存泡泡。</summary>
        public event Action<string, string> Accepted;

        public void Cancel()
        {
            _epoch++;
            _syncing.Clear();
            _readPosted.Clear();
            if (_drainRoutine != null) _api.StopCoroutine(_drainRoutine);
            _drainRoutine = null;
            _draining = false;
        }

        public void Kick()
        {
            if (_draining || _api.Chat == null) return;
            _drainRoutine = _api.StartCoroutine(Drain(_api.Chat, _epoch));
        }

        private bool Alive(ChatRepository repo, int epoch)
        {
            return epoch == _epoch && ReferenceEquals(repo, _api.Chat);
        }

        private IEnumerator Drain(ChatRepository repo, int epoch)
        {
            _draining = true;
            try
            {
                while (Alive(repo, epoch))
                {
                    var pending = repo.Pending();
                    if (pending.Count == 0) yield break;
                    var mutation = pending[0];
                    repo.MarkAttempt(mutation.Id, Now());
                    var body = new JObject().Put("parts", mutation.Parts)
                        .Put("client_mutation_id", mutation.Id);
                    var path = "/v1/conversations/" + UnityWebRequest.EscapeURL(mutation.ConversationId) + "/messages";
                    NextApi.Reply reply = null;
                    var completed = false;
                    _api.Post(path, body, value => { reply = value; completed = true; });
                    while (!completed && Alive(repo, epoch)) yield return null;
                    if (!Alive(repo, epoch)) yield break;
                    if (reply != null && reply.Ok)
                    {
                        repo.MarkAccepted(mutation.Id, reply.Json, Now());
                        Accepted?.Invoke(mutation.ConversationId, mutation.Id);
                        continue;
                    }
                    var transport = reply == null || reply.Status == 0 || reply.Status == 429 || reply.Status >= 500;
                    repo.MarkFailed(mutation.Id, reply == null ? "no_response" : reply.Code, transport, Now());
                    if (transport) yield break;       // 保留順序；重連後 Kick 從同一 mutation 繼續。
                }
            }
            finally
            {
                if (epoch == _epoch)
                {
                    _drainRoutine = null;
                    _draining = false;
                }
            }
        }

        public void SyncConversations(Action<bool, string> done = null)
        {
            var repo = _api.Chat;
            var epoch = _epoch;
            if (repo == null) { done?.Invoke(false, "not_bound"); return; }
            _api.Get("/v1/conversations", reply =>
            {
                if (!Alive(repo, epoch)) return;
                if (!reply.Ok) { done?.Invoke(false, reply.Code); return; }
                repo.MergeConversations(reply.Json.OptJArray("conversations"), _api.ChatAccountId);
                done?.Invoke(true, "");
            });
        }

        /// <summary>owner 打开固定 Agent 房；服务端幂等建群，客人不调用这个入口。</summary>
        public void SyncAgentRoom(Action<bool, string> done = null)
        {
            var repo = _api.Chat;
            var epoch = _epoch;
            if (repo == null) { done?.Invoke(false, "not_bound"); return; }
            _api.Post("/v1/conversations/agent-room", new JObject(), reply =>
            {
                if (!Alive(repo, epoch)) return;
                if (!reply.Ok) { done?.Invoke(false, reply.Code); return; }
                repo.MergeConversation(reply.Json.OptJObject("conversation"), _api.ChatAccountId);
                done?.Invoke(true, "");
            });
        }

        public void SyncConversation(string entity, Action<bool, string> done = null)
        {
            var repo = _api.Chat;
            var epoch = _epoch;
            if (repo == null || string.IsNullOrEmpty(entity)) { done?.Invoke(false, "not_bound"); return; }
            if (_syncing.Contains(entity)) return;
            _syncing.Add(entity);
            Action<string> syncKnown = id =>
            {
                long cursor;
                if (long.TryParse(repo.Cursor(id), out cursor) && cursor > 0)
                    Forward(repo, epoch, entity, id, cursor, done);
                else
                    Initial(repo, epoch, entity, id, "", 0L, done);
            };
            var existing = repo.Conversation(entity);
            if (existing?.OptString("kind") == "group")
            {
                var id = existing.OptString("id");
                if (string.IsNullOrEmpty(id)) { Finish(entity, false, "conversation_not_found", done); return; }
                syncKnown(id);                 // 群会话已经由列表／agent-room 打开，绝不误入 direct 接口。
                return;
            }
            _api.Post("/v1/conversations/direct", new JObject().Put("account_id", entity), reply =>
            {
                if (!Alive(repo, epoch)) return;
                if (!reply.Ok) { Finish(entity, false, reply.Code, done); return; }
                var conversation = reply.Json.OptJObject("conversation");
                repo.MergeConversation(conversation, _api.ChatAccountId);
                syncKnown(conversation?.OptString("id") ?? ChatRepository.DirectId(_api.ChatAccountId, entity));
            });
        }

        private void Initial(ChatRepository repo, int epoch, string entity, string conversationId,
                             string before, long newest, Action<bool, string> done)
        {
            var path = "/v1/conversations/" + UnityWebRequest.EscapeURL(conversationId) + "/messages?limit=200";
            if (!string.IsNullOrEmpty(before)) path += "&before=" + UnityWebRequest.EscapeURL(before);
            _api.Get(path, reply =>
            {
                if (!Alive(repo, epoch)) return;
                if (!reply.Ok) { Finish(entity, false, reply.Code, done); return; }
                var messages = reply.Json.OptJArray("messages");
                repo.MergeMessages(messages);
                for (var i = 0; messages != null && i < messages.Length(); i++)
                    newest = Math.Max(newest, (long)messages.OptJObject(i).OptDouble("seq"));
                var next = reply.Json.OptString("next_before");
                if (reply.Json.OptBoolean("has_more") && !string.IsNullOrEmpty(next))
                {
                    Initial(repo, epoch, entity, conversationId, next, newest, done);
                    return;
                }
                if (newest > 0) repo.SetCursor(conversationId, newest.ToString());
                Finish(entity, true, "", done);
            });
        }

        private void Forward(ChatRepository repo, int epoch, string entity, string conversationId,
                             long cursor, Action<bool, string> done)
        {
            var path = "/v1/conversations/" + UnityWebRequest.EscapeURL(conversationId)
                       + "/messages?limit=200&after=" + cursor;
            _api.Get(path, reply =>
            {
                if (!Alive(repo, epoch)) return;
                if (!reply.Ok) { Finish(entity, false, reply.Code, done); return; }
                repo.MergeMessages(reply.Json.OptJArray("messages"));
                long next;
                if (!long.TryParse(reply.Json.OptString("next_after"), out next)) next = cursor;
                if (next > cursor) repo.SetCursor(conversationId, next.ToString());
                if (reply.Json.OptBoolean("has_more") && next > cursor)
                {
                    Forward(repo, epoch, entity, conversationId, next, done);
                    return;
                }
                Finish(entity, true, "", done);
            });
        }

        private void Finish(string entity, bool ok, string error, Action<bool, string> done)
        {
            _syncing.Remove(entity);
            done?.Invoke(ok, error ?? "");
        }

        public void MarkRead(string conversationId, string messageId)
        {
            if (_api.Chat == null || string.IsNullOrEmpty(conversationId) ||
                string.IsNullOrEmpty(messageId) || messageId.StartsWith("local_", StringComparison.Ordinal)) return;
            var repo = _api.Chat;
            var epoch = _epoch;
            repo.MarkRead(conversationId, messageId);       // 离线打开也立即清本地 unread。
            var key = conversationId + "|" + messageId;
            if (_readPosted.Contains(key)) return;
            _api.Post("/v1/conversations/" + UnityWebRequest.EscapeURL(conversationId) + "/read",
                new JObject().Put("message_id", messageId), reply =>
                {
                    // 失败不记 posted；下次 Paint／重启会重新提交同一已读位置。
                    if (Alive(repo, epoch) && reply.Ok) _readPosted.Add(key);
                });
        }

        private static double Now() { return DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0; }
    }
}
