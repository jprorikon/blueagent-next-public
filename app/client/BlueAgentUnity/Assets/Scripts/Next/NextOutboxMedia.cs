using System;
using System.IO;
using BlueAgent.Logic;
using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// 離線選的圖先存在端末（PENDING B.App.5「離線時不能上傳媒體」，2026-11-17）。
    ///
    /// 以前：選圖當下就 `POST /v1/media`，連不上就失敗 ⇒ 帶圖的帖子離線時根本組不起來（文字帖早就能進 Outbox）。
    /// 現在：上傳因為**連不上**（0／429／5xx）失敗時，原圖寫進 `persistentDataPath/outbox-media/`，
    /// 帖子裡先放佔位 id（<see cref="SocialRepository.PendingMediaPrefix"/>）；Outbox 送那一筆之前先上傳、
    /// 換成伺服器 id（<see cref="SocialRepository.ResolveMedia"/>）再發帖，上傳成功才刪本機檔。
    /// 被伺服器拒絕（4xx，例如太大／格式不收）不走這裡——那是當場就該告訴使用者的錯。
    /// </summary>
    public static class NextOutboxMedia
    {
        private static string Folder => Path.Combine(Application.persistentDataPath, "outbox-media");

        public static string Save(byte[] bytes)
        {
            if (bytes == null || bytes.Length == 0) return "";
            Directory.CreateDirectory(Folder);
            var id = SocialRepository.PendingMediaPrefix + Guid.NewGuid().ToString("N");
            File.WriteAllBytes(PathOf(id), bytes);
            return id;
        }

        /// <summary>讀回原圖；不是佔位 id、或檔案已不在 ⇒ null。</summary>
        public static byte[] Read(string id)
        {
            if (!SocialRepository.IsPendingMedia(id)) return null;
            try
            {
                var path = PathOf(id);
                return File.Exists(path) ? File.ReadAllBytes(path) : null;
            }
            catch (Exception error)
            {
                Debug.LogWarning("[outbox-media] 讀不到 " + id + "：" + error.Message);
                return null;
            }
        }

        public static void Delete(string id)
        {
            if (!SocialRepository.IsPendingMedia(id)) return;
            try { File.Delete(PathOf(id)); }
            catch (Exception error) { Debug.LogWarning("[outbox-media] 刪不掉 " + id + "：" + error.Message); }
        }

        /// <summary>上傳失敗是不是「連不上」（之後重送有機會成功）；4xx 是伺服器明確拒收。</summary>
        public static bool IsTransport(int status)
        {
            return status == 0 || status == 429 || status >= 500;
        }

        // 佔位 id 只含前綴＋32 位 hex（Save 產的）；別的字串一律不當路徑用
        private static string PathOf(string id)
        {
            var name = id.Substring(SocialRepository.PendingMediaPrefix.Length);
            foreach (var ch in name)
            {
                if (!Uri.IsHexDigit(ch)) throw new ArgumentException("bad pending media id");
            }
            return Path.Combine(Folder, name + ".img");
        }
    }
}
