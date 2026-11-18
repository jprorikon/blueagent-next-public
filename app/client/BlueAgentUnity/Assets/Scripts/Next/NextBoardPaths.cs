using UnityEngine.Networking;

namespace BlueAgent.Next
{
    /// <summary>
    /// 公共区的路徑只在這裡寫一次：直連 App Server 的 `/v1/feed`、`/v1/posts…`（2026-11-08，PENDING B.App.8）。
    /// 以前經 Agent 的 `/api/feed`／`/api/post` 兼容入口，Agent 掛了公共區就跟著掛；
    /// 契約見 `app/server/board_http.py`（與 MCP 同一實作，回包形狀不變）。
    /// </summary>
    public static class NextBoardPaths
    {
        public const string Posts = "/v1/posts";

        /// <summary>`since`＝秒；`before`＝上一頁的 `next_cursor`（空＝第一頁）。</summary>
        public static string Feed(int limit, long since, string before = "")
        {
            var path = "/v1/feed?limit=" + limit + "&since=" + since;
            return string.IsNullOrEmpty(before) ? path : path + "&before=" + UnityWebRequest.EscapeURL(before);
        }

        /// <summary>某一個人的帖子履歷（個人主頁；跨天、最新在前）。`author`＝帖子上的 `author_id`。</summary>
        public static string AuthorFeed(string author, int limit, string before = "")
        {
            var path = "/v1/feed?limit=" + limit + "&authors=" + UnityWebRequest.EscapeURL(author ?? "");
            return string.IsNullOrEmpty(before) ? path : path + "&before=" + UnityWebRequest.EscapeURL(before);
        }

        /// <summary>單篇（GET 詳情／DELETE 軟刪）。</summary>
        public static string Post(string id)
        {
            return Posts + "/" + UnityWebRequest.EscapeURL(id ?? "");
        }

        public static string Replies(string id)
        {
            return Post(id) + "/replies";
        }

        public static string Like(string id)
        {
            return Post(id) + "/like";
        }
    }
}
