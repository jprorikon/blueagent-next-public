using BlueAgent.Logic;
using BlueAgent.Screens;
using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// 端末的本機設定：**伺服器位址、token、上次的 role／entity**（PlayerPrefs；token 例外，見 <see cref="Token"/>）。
    ///
    /// token 2026-11-17 起改存系統安全儲存（Android Keystore／iOS 鑰匙圈），其餘照舊 PlayerPrefs。
    /// 契約 `accounts-and-clients.md` §2：第一次由使用者貼上，之後只讀這裡。
    /// </summary>
    public static class NextConfig
    {
        /// <summary>
        /// 伺服器位址＝**寫死的**（owner 2026-10-29：登入不需要手動輸入位址，設定裡也不准看到位址）。
        /// 現在指向 NAS 上那道 **Tailscale Funnel 公網入口**（Let's Encrypt 公簽憑證，Android 直接信）。
        /// 換伺服器＝改這裡重新打包，不走使用者輸入。
        /// </summary>
        public const string DefaultBaseUrl = "http://127.0.0.1:8788";

        /// <summary>
        /// 「對話紀錄」只看這一端的回合（`/api/turns?source=`）：owner 2026-10-27 定，
        /// App 與 Discord 的紀錄各自獨立，App 不顯示 DC 上說過的話。
        /// 兩個前綴都要：打字／語音是 `phone:owner`（`phone:guest-…`），
        /// **圖片那條是 `api:image`**（`/api/image` 自己標的）——只給 `phone` 會把圖片那輪濾掉。
        /// </summary>
        public const string HistorySource = "phone,api";

        private const string KeyToken = "ba_next_token";
        private const string KeyRole = "ba_next_role";
        private const string KeyEntity = "ba_next_entity";
        private const string KeyWantVoice = "ba_next_want_voice";
        private const string KeyLastTurn = "ba_next_last_turn";
        private const string KeyLiveTurn = "ba_next_live_";
        private const string KeyIdentityKeys = "ba_next_identity_keys";
        private const string KeyLanguage = "ba_next_language";
        private const string KeyUiLanguage = "ba_next_ui_language";
        private const string KeyVolume = "ba_next_volume";
        private const string KeyBackground = "ba_next_bg_";
        private const string KeyStage3D = "ba_next_stage3d";
        private const string KeyIdleMotion = "ba_next_idle_motion";
        private const string KeyIdleArHmm = "ba_next_idle_arhmm";
        private const string KeyConversations = "ba_next_conversations";
        private const string KeyChatList = "ba_next_chat_list";
        private const string KeyHomeIsFeed = "ba_next_home_is_feed";
        private const string KeyLocationSharing = "ba_next_location_sharing";
        private const string KeyLocationDeviceId = "ba_next_location_device_id";

        /// <summary>服务端确认后的位置共享状态；默认关闭，绝不作为开启传感器的本地预设。</summary>
        public static bool LocationSharing
        {
            get { return PlayerPrefs.GetInt(KeyLocationSharing, 0) != 0; }
            set { PlayerPrefs.SetInt(KeyLocationSharing, value ? 1 : 0); PlayerPrefs.Save(); }
        }

        /// <summary>每次安装随机生成；不发送设备名、硬件 ID 或广告标识。</summary>
        public static string LocationDeviceId
        {
            get
            {
                var value = PlayerPrefs.GetString(KeyLocationDeviceId, "");
                if (value.Length >= 8 && value.Length <= 64) return value;
                value = System.Guid.NewGuid().ToString("N");
                PlayerPrefs.SetString(KeyLocationDeviceId, value);
                PlayerPrefs.Save();
                return value;
            }
        }

        /// <summary>M1：先進會話列表；關掉時保持舊版「直接進上次角色聊天」的回退路徑。</summary>
        public static bool ChatList
        {
            get { return PlayerPrefs.GetInt(KeyChatList, 1) != 0; }
            set { PlayerPrefs.SetInt(KeyChatList, value ? 1 : 0); PlayerPrefs.Save(); }
        }

        /// <summary>M3 預設首頁是公共區；關掉後完整回到 M1 的列表／單頁聊天入口。</summary>
        public static bool HomeIsFeed
        {
            get { return PlayerPrefs.GetInt(KeyHomeIsFeed, 1) != 0; }
            set { PlayerPrefs.SetInt(KeyHomeIsFeed, value ? 1 : 0); PlayerPrefs.Save(); }
        }

        /// <summary>
        /// 伺服器位址＝**唯讀**（owner 2026-10-29：位址不給使用者輸入，也不准在設定頁看到）。
        /// 寫死在 `DefaultBaseUrl`；**不再存 PlayerPrefs** ⇒ 換位址＝改常數重新打包，
        /// 舊版存在本機的 `ba_next_base_url` 自然失效（不再讀）。
        /// </summary>
        public static string BaseUrl
        {
            get
            {
#if BA_SELFTEST
                // 検収包だけ：この端末・この App だけを loopback に切り、NAS／host の通信を止めずに実オフライン復旧を試す。
                // 本番ビルドにはキーも分岐も残らず、従来どおり DefaultBaseUrl 固定。
                var testOverride = PlayerPrefs.GetString("ba_next_test_base_url", "");
                if (!string.IsNullOrEmpty(testOverride)) return testOverride.TrimEnd('/');
#endif
                return DefaultBaseUrl;
            }
        }

        private static string _token;

        /// <summary>
        /// 登入 token：**有系統安全儲存就存那裡**（Android Keystore／iOS 鑰匙圈，`NativeShell.SecureToken*`；
        /// PENDING B.App.4，2026-11-17），PlayerPrefs 只剩「這台機器用不了安全儲存」時的退路。
        /// 舊版明文存在 PlayerPrefs 的那一把，第一次讀到時搬進安全儲存、**搬成功才刪**明文。
        /// 讀過一次就留在記憶體（每個請求都會讀，不能每次都跨 JNI／鑰匙圈）。
        /// </summary>
        public static string Token
        {
            get
            {
                if (_token != null) return _token;
                var plain = PlayerPrefs.GetString(KeyToken, "");
                if (!NativeShell.HasSecureStore) return _token = plain;
                var secure = NativeShell.SecureTokenGet();
                if (!string.IsNullOrEmpty(secure))
                {
                    if (!string.IsNullOrEmpty(plain)) { PlayerPrefs.DeleteKey(KeyToken); PlayerPrefs.Save(); }
                    return _token = secure;
                }
                if (!string.IsNullOrEmpty(plain) && NativeShell.SecureTokenPut(plain))
                {
                    PlayerPrefs.DeleteKey(KeyToken);
                    PlayerPrefs.Save();
                }
                return _token = plain;
            }
            set
            {
                var token = string.IsNullOrEmpty(value) ? "" : value.Trim();
                _token = token;
                var secured = NativeShell.HasSecureStore && NativeShell.SecureTokenPut(token);
                if (secured || token.Length == 0) PlayerPrefs.DeleteKey(KeyToken);
                else PlayerPrefs.SetString(KeyToken, token);   // 安全儲存用不了：退回明文，至少能登入
                PlayerPrefs.Save();
            }
        }

        public static bool HasToken
        {
            get { return !string.IsNullOrEmpty(Token); }
        }

        /// <summary>`master`／`guest`（只作顯示與「上次進哪一端」的線索；真正的判斷永遠用 `/api/me`）。</summary>
        public static string Role
        {
            get { return PlayerPrefs.GetString(KeyRole, ""); }
            set { PlayerPrefs.SetString(KeyRole, value ?? ""); PlayerPrefs.Save(); }
        }

        /// <summary>上次對談的 entity（`/api/turn` 的 `entity` 欄位）。</summary>
        public static string Entity
        {
            get { return PlayerPrefs.GetString(KeyEntity, ""); }
            set { PlayerPrefs.SetString(KeyEntity, value ?? ""); PlayerPrefs.Save(); }
        }

        /// <summary>立繪舞台要不要用 3D 模型（有模型的角色才有切換鈕；owner 2026-10-30）。</summary>
        public static bool Stage3D
        {
            get { return PlayerPrefs.GetInt(KeyStage3D, 0) != 0; }
            set { PlayerPrefs.SetInt(KeyStage3D, value ? 1 : 0); PlayerPrefs.Save(); }
        }

        private static int _idleMotion = -1;
        private static int _idleArHmm = -1;

        /// <summary>
        /// 3D 待機用 MAGIC 生成的微動作（`docs/PLAN-magic-motion-20261031.md`）；關掉退回固定的正弦擺動。
        /// 每幀都會讀 ⇒ 快取在記憶體，不每幀打 PlayerPrefs。
        /// </summary>
        public static bool IdleMotion
        {
            get
            {
                if (_idleMotion < 0) _idleMotion = PlayerPrefs.GetInt(KeyIdleMotion, 1);
                return _idleMotion != 0;
            }
            set
            {
                _idleMotion = value ? 1 : 0;
                PlayerPrefs.SetInt(KeyIdleMotion, _idleMotion);
                PlayerPrefs.Save();
            }
        }

        /// <summary>
        /// 待機用哪個 MAGIC 模型：false＝VAR（預設，平滑），true＝AR-HMM（實驗，會換姿勢但偶爾跳）。
        /// 只有 <see cref="IdleMotion"/> 開著時才有意義。
        /// </summary>
        public static bool IdleArHmm
        {
            get
            {
                if (_idleArHmm < 0) _idleArHmm = PlayerPrefs.GetInt(KeyIdleArHmm, 0);
                return _idleArHmm != 0;
            }
            set
            {
                _idleArHmm = value ? 1 : 0;
                PlayerPrefs.SetInt(KeyIdleArHmm, _idleArHmm);
                PlayerPrefs.Save();
            }
        }

        /// <summary>要不要她出聲（契約：只有 master 有效；guest 一律 false）。</summary>
        public static bool WantVoice
        {
            get { return PlayerPrefs.GetInt(KeyWantVoice, 1) != 0; }
            set { PlayerPrefs.SetInt(KeyWantVoice, value ? 1 : 0); PlayerPrefs.Save(); }
        }

        /// <summary>
        /// 契約 v2 的補拿游標：**最後一個完整收到 `done` 的 turn_id**。
        /// 存在本機 ⇒ 連 app 被殺掉再開也能只補「漏掉的那幾輪」。
        /// key 帶 principal ＋ entity（換身份／換角色時不能沿用別人的游標）。
        /// </summary>
        public static string LastTurn
        {
            get { return PlayerPrefs.GetString(KeyLastTurn, ""); }
            set
            {
                if (string.IsNullOrEmpty(value)) PlayerPrefs.DeleteKey(KeyLastTurn);
                else PlayerPrefs.SetString(KeyLastTurn, value);
                PlayerPrefs.Save();
            }
        }

        /// <summary>
        /// 補拿游標**按角色分開存**（B3）：多實體時每個角色各有自己的 outbox 進度，
        /// 共用一顆游標會讓她 A 的 turn_id 去 since B 的串（伺服器只能回「找不到就給最近 50 筆」）。
        /// </summary>
        public static string LastTurnFor(string entity)
        {
            if (string.IsNullOrEmpty(entity)) return LastTurn;
            var scoped = PlayerPrefs.GetString(KeyLastTurn + "_" + entity, "");
            return string.IsNullOrEmpty(scoped) ? LastTurn : scoped;   // 舊資料（單一游標）當起點
        }

        public static void SetLastTurnFor(string entity, string turn)
        {
            if (string.IsNullOrEmpty(entity)) { LastTurn = turn; return; }
            var key = KeyLastTurn + "_" + entity;
            if (string.IsNullOrEmpty(turn)) PlayerPrefs.DeleteKey(key);
            else
            {
                PlayerPrefs.SetString(key, turn);
                TrackIdentityKey(key);
            }
            PlayerPrefs.Save();
        }

        /// <summary>
        /// **只**回這個角色自己的游標（不回退到舊的單一游標）——換角色時用。
        /// 回退會讓「剛切過去的角色」拿到上一個角色的游標，`since` 對不上 ⇒ 伺服器退回
        /// 「最近 50 筆」，等於把整段歷史再倒一次（2026-10-27 換角色 bug 的同源問題）。
        /// </summary>
        public static string OwnTurnFor(string entity)
        {
            if (string.IsNullOrEmpty(entity)) return LastTurn;
            return PlayerPrefs.GetString(KeyLastTurn + "_" + entity, "");
        }

        /// <summary>
        /// 正在串流的那一輪「已經收到的分句」按角色存一份（owner 2026-11-04：
        /// 「前台收到分句就落檔這一句」）。App 被殺／重啟時，畫面至少還原得了她已經說出口的部分；
        /// 收尾（`done`）之後就清掉——那時 outbox 已經是權威。
        /// </summary>
        public static string LiveTurnFor(string entity)
        {
            if (string.IsNullOrEmpty(entity)) return "";
            return PlayerPrefs.GetString(KeyLiveTurn + entity, "");
        }

        public static void SetLiveTurnFor(string entity, string json)
        {
            if (string.IsNullOrEmpty(entity)) return;
            var key = KeyLiveTurn + entity;
            if (string.IsNullOrEmpty(json)) PlayerPrefs.DeleteKey(key);
            else
            {
                PlayerPrefs.SetString(key, json);
                TrackIdentityKey(key);
            }
            PlayerPrefs.Save();
        }

        /// <summary>
        /// 送語音簡訊時告訴伺服器的語言（`X-Lang`，契約 `/api/audio` 的選填 header）。
        /// 預設 `ja`——她的世界是日語；之後若要跟系統語言就改這裡。
        /// </summary>
        public static string Language
        {
            get
            {
                var value = PlayerPrefs.GetString(KeyLanguage, "ja");
                return string.IsNullOrEmpty(value) ? "ja" : value;
            }
            set { PlayerPrefs.SetString(KeyLanguage, string.IsNullOrEmpty(value) ? "ja" : value); PlayerPrefs.Save(); }
        }

        /// <summary>
        /// 界面語言（ja／zh-Hans／zh-Hant）：只影響 Next 自己排的字。
        /// 沒選過 ⇒ 跟系統語言（`NextText.FromSystemLanguage`；中文分簡繁，其他一律日語），選過就照選的。
        /// </summary>
        public static string UiLanguage
        {
            get
            {
                var value = PlayerPrefs.GetString(KeyUiLanguage, "");
                return string.IsNullOrEmpty(value)
                    ? NextText.FromSystemLanguage(Application.systemLanguage.ToString())
                    : NextText.Normalize(value);
            }
            set { PlayerPrefs.SetString(KeyUiLanguage, NextText.Normalize(value)); PlayerPrefs.Save(); }
        }

        /// <summary>她說話的音量（0..1），套在 `SpeechPlayer.Volume`。</summary>
        public static float Volume
        {
            get { return PlayerPrefs.GetFloat(KeyVolume, 1f); }
            set { PlayerPrefs.SetFloat(KeyVolume, value < 0f ? 0f : value > 1f ? 1f : value); PlayerPrefs.Save(); }
        }

        /// <summary>某個角色用的場景包 id（空字串＝なし／純底色）。</summary>
        public static string BackgroundFor(string entity)
        {
            return PlayerPrefs.GetString(KeyBackground + (entity ?? ""), "");
        }

        public static void SetBackgroundFor(string entity, string id)
        {
            var key = KeyBackground + (entity ?? "");
            if (string.IsNullOrEmpty(id)) PlayerPrefs.DeleteKey(key);
            else PlayerPrefs.SetString(key, id);
            PlayerPrefs.Save();
        }

        /// <summary>有界未讀窗口的本機快照；完整歷史仍以伺服器 outbox 為準。</summary>
        public static ConversationStoreSnapshot LoadConversationStore()
        {
            var raw = PlayerPrefs.GetString(KeyConversations, "");
            if (string.IsNullOrEmpty(raw)) return new ConversationStoreSnapshot();
            try
            {
                return JsonUtility.FromJson<ConversationStoreSnapshot>(raw)
                       ?? new ConversationStoreSnapshot();
            }
            catch (System.Exception error)
            {
                Debug.LogWarning("[next] 未讀快照讀取失敗，改從伺服器歷史補齊：" + error.Message);
                return new ConversationStoreSnapshot();
            }
        }

        public static void SaveConversationStore(ConversationStoreSnapshot snapshot)
        {
            PlayerPrefs.SetString(KeyConversations,
                JsonUtility.ToJson(snapshot ?? new ConversationStoreSnapshot()));
            TrackIdentityKey(KeyConversations);
            PlayerPrefs.Save();
        }

        /// <summary>
        /// PlayerPrefs 不能列舉 key，所以身份快取在寫入時登記；登出照索引清，新增角色不必再改角色名單。
        /// 索引本身由呼叫方最後的 `Save()` 一起落盤，不多做一次同步寫。
        /// </summary>
        private static void TrackIdentityKey(string key)
        {
            var raw = PlayerPrefs.GetString(KeyIdentityKeys, "");
            foreach (var existing in raw.Split('|'))
                if (existing == key) return;
            PlayerPrefs.SetString(KeyIdentityKeys, string.IsNullOrEmpty(raw) ? key : raw + "|" + key);
        }

        /// <summary>
        /// 401／登出 ⇒ 清 token（契約 §4：清掉本機 token，回登入頁）。
        /// 只清**跟身份走**的東西；界面語言、音量是這台裝置的偏好，換身份不重設（unity-review 2026-10-28：
        /// 以前一起清 ⇒ master ↔ guest 換手一次，語言就跳回日語、音量回預設）。
        /// 事件游標按「伺服器＋身份」各存一份（`EventCursors`），這裡不動。
        /// </summary>
        public static void ClearToken()
        {
            Token = "";                                // 安全儲存與 PlayerPrefs 兩邊一起清
            PlayerPrefs.DeleteKey(KeyToken);
            PlayerPrefs.DeleteKey(KeyRole);
            PlayerPrefs.DeleteKey(KeyLastTurn);
            PlayerPrefs.DeleteKey(KeyLocationSharing); // 服务端授权保留；下个登录会话须重新确认。
            var tracked = PlayerPrefs.GetString(KeyIdentityKeys, "");
            foreach (var key in tracked.Split('|'))
                if (!string.IsNullOrEmpty(key)) PlayerPrefs.DeleteKey(key);
            // 舊資料還沒進索引；把現役四個角色的三類殘留清一次。新資料全靠上面的索引。
            foreach (var entity in new[] { "mira", "aster", "nori", "lumi" })
            {
                PlayerPrefs.DeleteKey(KeyLastTurn + "_" + entity);
                PlayerPrefs.DeleteKey("ba_next_mood_" + entity); // 清除舊版私有快照；平台心情已改存 SQLite。
                PlayerPrefs.DeleteKey(KeyLiveTurn + entity);
            }
            PlayerPrefs.DeleteKey(KeyIdentityKeys);
            PlayerPrefs.Save();
        }
    }

    /// <summary>共用層 `IPrefs` 的 PlayerPrefs 版（`EventCursors` 用）。</summary>
    public sealed class NextPrefs : IPrefs
    {
        public string Get(string key) { return PlayerPrefs.GetString(key, ""); }

        public void Set(string key, string value)
        {
            PlayerPrefs.SetString(key, value ?? "");
            PlayerPrefs.Save();
        }

        public void Delete(string key) { PlayerPrefs.DeleteKey(key); }
    }
}
