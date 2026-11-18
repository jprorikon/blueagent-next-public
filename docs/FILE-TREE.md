# 檔案樹

> 由 `agent/tools/gen_file_tree.py` 產生；每一行的說明取自那個檔自己的 docstring（C# 取 `/// <summary>`，
> 文件取第一個標題）。**要改說明就改原檔，再重產**：
>
> ```bash
> python3 agent/tools/gen_file_tree.py
> ```
>
> `agent/tests/test_file_tree.py` 只比「列了哪些路徑」：新增／刪除／搬檔沒重產就會紅；改 docstring 不會。
> 分層規則與單檔上限見 `README.md`「目录」一節。

<!-- tree:begin（agent/tools/gen_file_tree.py 產生，不要手改） -->
```text
blueagent-next/
  .dockerignore
  .env.example
  .gitignore
  DESIGN.md                                  # BlueAgent Next 设计摘要
  README.md                                  # BlueAgent Next — Public Demo
  THIRD_PARTY_NOTICES.md                     # Third-Party Notices
  docker-compose.yml
  agent/                                     # Agent 本體（包名 agent.*）：腦、Agent HTTP、外部渠道、人格、維運腳本
    Dockerfile
    env.deepseek.example
    requirements.txt
    run.py                                   # 啟動 Agent：python3 agent/run.py（或在倉庫根 python3 -m agent.run）
    brain/                                   # 主控與記憶核心（依賴只准由外往內，見 test_package_layering）
      compose/                               # 應用層（組合根）：`bootstrap` 接線、`state` 對外的應用狀態、對話管線的各階段（`stages`）。
        account_service.py                   # 帳號生命週期的應用服務；HTTP 層不直接依賴存儲實作。
        activity_api.py                      # 活動控制：開始/暫停/繼續/重置/模擬重啟。**按角色**（每條 LifeLoop 各自一條）。
        affect_wiring.py                     # 情感整理的接線（docs/PLAN-affect-reorg-20261109.md §3-6／§3-7／§3-8）。
        app_root.py                          # 組合根 `App` 的型別（架構審查 E3，2026-11-17 從 `bootstrap.py` 拆出）。
        boot_wiring.py                       # 開機接線的兩件小事（owner 2026-11-03 抓到的兩條）。
        bootstrap.py                         # 新一代 BlueAgent 的組裝根：只接線，不放業務邏輯。
        diary.py                             # 她自己的日記與工作成果（master 端「+」→ 日記；owner 2026-10-31）。
        entities.py                          # 實體、人、關係圖的**生產初值**：只建資料，不接線。
        episode_api.py                       # 手動封片（owner 調試用；平常由閒置/輪數/字數自動觸發）。
        followups.py                         # 語音回合「先出聲、再幹活」：短回覆立刻說，真正的工作排進主人指令佇列，做完再交付。
        frames.py                            # 請求級的幀收集器：一次 send 一個 sink。
        group_opener.py                      # 三人房安静时，她拿起手机后要不要自己开个话题（GROUP-SELF-CHAT §2 ①「定时敲门」的后半段）。
        guest_api.py                         # Guest 加入、關係聲明、主人拍板、白名單的應用層。
        heartbeat_compose.py                 # 心跳台詞：她自己組織（不是模板挑一句），接地在她真的在做／聊過／想過的事上。
        identity.py                          # 核心帳號在腦端，外部帳號都綁在它身上（owner 2026-11-09）。
        inner_life.py                        # 她腦內的背景活動：子代理（回合內）、閒時預想、每日日記、記憶效用記帳。
        inner_service.py                     # 情緒與自主任務的應用服務。
        interruptibility.py                  # 主人此刻能不能被打擾（common-ground 第 8 步，docs/PLAN-common-ground-20261109…
        life_wiring.py                       # 她自己的生活：每一步真的做事（帶工具的自主任務）、活動輪換、每個角色一條 LifeLoop。
        memory_service.py                    # 記憶履歷與窗口摘要的唯讀應用服務。
        motion.py                            # 每輪挑一個動作（`docs/PLAN-motion-kimodo-20261031.md` §5.2）：從回覆文字挑 tag，…
        pipeline.py                          # 管線組裝：名稱 → stage 實例；與 bootstrap 解耦。
        ports.py                             # 應用層的 Ports：把記憶與腦包成管線協議。
        proactive_echo.py                    # 把「她自己发出去的主动消息」写回她的 transcript（owner 2026-11-03）。
        self_work_start.py                   # 「開一件她自己的事」的兩個入口——從 `compose/bootstrap.py` 搬出來（守 600 行上限）。
        snapshot.py                          # 把運行時狀態投影成 UI 快照；不改變任何狀態。
        stages/                              # 對話管線的階段：normalize → recall → persona → state → brain → voice → …
          brain.py                           # 大腦階段：串流、工具子回合（最多三輪）、首句先合成、文字先發。
          context.py                         # 階段之間的資料契約（review.md 2026-10-25 P2：`env.meta` 是隱式字串契約）。
          egress.py                          # 出口：語音、產物、最後的回覆幀。
          intake.py                          # 進線：文字正規化、看圖（圖 → 文字描述）。
          memory_write.py                    # 記憶寫入與效用記帳：這一輪留下什麼、撈出來的有沒有真的用到。
          persona.py                         # 人格段：人格檔、身分、立場、user.md、委任權限的放行。
          recall.py                          # 回想：被動召回（披露規則過濾）與子代理（腦內分工）。
          speech_text.py                     # 句子切分與「還剩哪段沒唸」：大腦串流與語音出口共用。
          state_elicit.py                    # 狀態引出（SPIN 第②段）：這一輪的心情／狀態 → 情緒量表。
        stance_seed.py                       # 立场定义与播种：语义按角色写，机制共用。
        state.py                             # 應用狀態：對外的一個入口（HTTP／Discord／TRPG 都打這裡），把工作交給窄服務。
        streaming.py                         # 串流交付：一輪的幀按到達順序推出去，最後給一個 result；整輪結束寫進 outbox（斷線可補）。
        summary.py                           # 片段摘要（逐句退路）：沒有提煉窗口摘要時，用前幾句拼一段承接。
        trpg_service.py                      # TRPG hub 的應用服務；HTTP 層不直接認識 adapter。
        turns.py                             # 回合執行：一次輸入 → 事件 → 跑管線並等它 → 決策／幀／封片／遙測／情緒。
        voice_api.py                         # 語音控制：App 的單人通話（撥／心跳／打斷／掛），與掛斷。她自己撥打／說話仍由 brain 的 tool_call 觸發。
        wiring.py                            # 組合根的分段接線：記憶後端、旁路 LLM、語意檢索、工具面、客人還原。
      config/                                # 不依賴內部套件的組態與模型設定。
        llm.py                               # LLM 角色設定：**一個檔決定每個角色用什麼模型**（owner 2026-10-23「列出來再配置」）。
      domain/                                # 領域層：不依賴 HTTP、不依賴 `deps`、不依賴任何業務模組的純模型。
        activity.py                          # 她自己的「正在做的事」：串行、可暂停、可检查点、可恢复。
        attention.py                         # 群聊注意力：焦點租約、四級動作、評分參數。
        auth.py                              # 认证后的调用者：**principal 是谁、以什么角色在说话**。
        client_end.py                        # App 的端末：手機（`phone`）與 Quest 3 的 MR 頭顯（`mr`）——**各走各的管道**（owner 20…
        decision.py                          # 路由決策：**一次分流的全部結果**，唯一能被日誌／面板解釋的東西。
        entity.py                            # 角色實體：整座橋的**第一性對象**。
        episode.py                           # 自動切片：把一條線切成 `Episode`，**不需要使用者按「新會話」**。
        event.py                             # 輸入事件與輸出幀：**源只說「發生了什麼」，不說「該怎麼處理」**。
        guest.py                             # 從屬客戶端（Guest）的加入申請與關係聲明。
        ids.py                               # 領域標識：**一個角色 ＝ 一個實體（entity），不是一個 session**。
        pending.py                           # YIELD 待辦：她決定「先禮讓一下」時，把要做的事寫成可恢復的記錄。
        principal.py                         # Principal：**說話的人是誰**。
        social.py                            # 關係認知：人與人之間的邊、圈子，以及記憶的可見域。
        social_condition.py                  # 「他是在問我事實，還是在群裡公開表態」——SPIN 的 informational / normative 區分。
        stance.py                            # 立场（stance）：**角色对某个人**的慢变量，不是人跟人的关系图。
        thread.py                            # 實體的「線」與記憶作用域。
        time.py                              # 可獨立於應用狀態使用的時間文字規則。
        voice.py                             # 語音動作的狀態對象：一次通話 / 一段語音輸出。
      factory.py                             # 按环境变量选择 BrainPort。
      games/                                 # 把舊橋的五子棋規則層／引擎搬進新倉（`ba/game/gomoku_board.py` ＋ `gomoku_engine.py…
        gomoku_board.py                      # 五子棋的 **規則層**：棋盤狀態＋勝負判定（`Board`＝唯一規則權威）。
        gomoku_engine.py                     # 五子棋的 **棋力引擎**（確定性、非語言模型）——難度旋鈕＋落子選擇。
        session.py                           # 五子棋的「一局」：**用完即棄、與會話無關**。
      markup_guard.py                        # 文字形式的工具呼叫攔截器（owner 2026-11-03 實機 bug）。
      media/                                 # `brain.media`：圖片的存放與「看圖」那一支。
        app_audio.py                         # 送給 App 的語音瘦身（owner 2026-10-30「網路傳輸優化」）。
        digest.py                            # 內容雜湊（owner 2026-10-29：「圖片、表情包和聊天記錄採用 app 側快取，對比線上的雜湊決定要不要重新同步」）。
        files.py                             # 她送出去的文件（不是圖）：`data/files/` 一顆檔案 ＋ 一行索引。
        motion.py                            # 兼容转发：动作片段格式的 canonical 归属在 ``app.client.assets``。
        stickers.py                          # 兼容转发：贴纸目录与素材的 canonical 归属在 ``app.client.assets``。
        store.py                             # 媒體（圖片）存放：`data/media/` 一顆檔案 ＋ 一行索引。
        thumbs.py                            # 縮圖（owner 2026-10-30「網路傳輸優化」）：相冊牆、表情盤不必抓原圖。
        vision.py                            # 看圖：**圖 → 本地 VL → 一段文字 → 腦**（照舊橋 `ba/media/vision.py` 的做法搬過來）。
      memory/                                # 記憶核心：事實模型與披露規則（`facts`）、原文落盤（`fact_store`）、
        fact_rules.py                        # 一轮对话 → 该沉成哪些事实。**纯规则**，不碰存储。
        fact_store.py                        # 事实的 JSONL 持久化：一行一条，只追加，不改写。
        facts.py                             # 記憶的基本型：分層、可見域、匹配用的 gram、取代關係的門牌（哈希指針）。
        in_memory.py
        supersede.py                         # 取代關係（GLOSSARY.md「取代」）的落庫動作：**兩側各一個哈希門牌**（owner 2026-11-01）。
      messages.py                            # 把 BrainRequest 组装成 OpenAI 兼容的 messages。
      messages_continuity.py                 # 本輪段的「承接」兩塊：`[[before]]`（這條線上一段的摘要）與 `[[elsewhere]]`（別條線剛剛聊的）。
      messages_form.py                       # 返事の「形」段（`[[form]]`）：渠道ごとの長さ・書式の縛り。
      mock.py                                # Mock 腦（只在明寫 `BLUEAGENT_BRAIN=mock` 時用：離線開發／測試）：不接外部模型，但把人格、對象、記…
      open_retry.py                          # 送出請求時的「還沒出字就重試」（owner 2026-10-30 起；2026-11-03 從 `openai_compat`…
      openai_compat.py                       # OpenAI 兼容的流式 BrainPort（DeepSeek / Hermes / 本地 vLLM 都可）。
      persona/                               # 人格層：人格檔（soul）的讀取與組裝。不碰大腦、不碰記憶、不碰傳輸。
        core.py                              # 人格編譯（SPIN 第 ① 段的縮小版）：把散在 prose 裡的人格，壓成一張**結構化 core**。
        interests.py                         # 她的関心（興趣播種）：把「她自己的興趣」與「對方的領域」分成兩桶。
        soul.py                              # 人格檔（soul）：**照抄現役橋的形狀**，只換誰把它貼上去。
        state_elicit.py                      # 狀態引出（SPIN 第 ② 段）：用**人格 core × 當前情境**推出這一刻的心理狀態。
        userfile.py                          # 每個 user 一份 `user.md`：**兩段**——`identity`（手寫，不被程式改）＋ `recent`（自動累…
      pipelines/                             # 管線層：`Stage` 是插件，`Pipeline` 是宣告，`blueprints` 是形狀表。
        base.py                              # 管線協議：一輪 ＝ 一條 `Stage` 序列，**階段是插件，管線是宣告**。
        blueprints.py                        # 管線藍圖：**宣告**一條管線由哪些階段組成；實作由插件提供。
      ports.py                               # Ports：新架構與舊世界的**唯一接縫**。
      router/                                # 路由層：唯一決定「哪個實體、哪條線、哪條管線、什麼模式、什麼優先級」的地方。
        policy.py                            # Router：把一個 `IngressEvent` 變成一個 `RouteDecision`。
        rules.py                             # 路由規則：**能算的先用算的**，LLM 只在必要時、且在非實時路徑上才進來。
      runtime/                               # 執行層：線的存放、自動切片、調度、發言權仲裁、實體狀態、入站主幹。
        activity_log.py                      # 她的行動日誌：**private log / public log**（owner 2026-10-22）。
        activity_store.py                    # 活動檢查點的落盤：每實體一個 JSON，原子寫。
        addressee.py                         # 群聊裡「這句話在對誰說」——收件人狀態機（2026-10-27）。
        attention.py                         # AttentionGate：確定性特徵 → 四級動作；租約持久化；shadow 記錄。
        attention_history.py                 # 注意力决策历史：從 JSONL 聚合出按日/按群/按動作的看板數據。
        budget.py                            # 行動預算：**她自己的精力額度**（owner 2026-10-22）。
        carry_backfill.py                    # 回填**承接段**的門牌：把舊封片留下的 `Thread.carry_summary` 補上指針。
        daily_report.py                      # 每日報（引擎內）：一天一次，她自己整理「今天」——不是外部 cron 替她報。
        dedupe.py                            # 幂等窗口：**同一件事只处理一次**。
        delegate.py                          # 委任本體（work 模式真的去幹活）—— owner 2026-10-21 的 M4。
        delivery/                            # `runtime/delivery/`：出站：事件匯流排、回合結果的發件箱（未送達的回覆／分段）、按信道投遞（App／Disc…
          bus.py                             # 推送匯流排：讓「不是這一輪回覆」的東西也能到 App（SSE 長連）。
          delivery.py                        # 她主动说话时**走哪条信道**（owner 2026-10-22 问「选择信道上你怎么计划的」）。
          outbox.py                          # 回合交付的「出匣」（outbox）：**斷線不該讓她的回答消失**。
        distill.py                           # 背景提煉：session 過濾 → LLM 抽事實 → 按門檻入檔 → **每次判定都留履歷**。
        doctor.py                            # 體檢器（哨兵）：一層一項，逐項先說「它在哪找證據」。
        entity_store.py                      # 實體的「自身狀態」：在場、注意力、情緒。
        episodes.py                          # 自動切片／歸檔的執行端：**沒有任何「新會話」入口**。
        expression.py                        # 「她可不可以用語音」——由 AEM 決定（owner 2026-10-22 第④條）。
        fileperm.py                          # 檔案擁有者對齊：容器以 root 跑，但 `data/` 是主機側（986）要讀寫的共用目錄。
        game_context.py                      # 「遊戲中的那段上下文」——**獨立一段，不吃對話的滾動預算**。
        heartbeat.py                         # 心跳：**每個角色一條，各自獨立**（owner 2026-10-22）。
        identity/                            # `runtime/identity/`：身分：帳號表（平台帳號 id ↔ 大腦 principal）、owner／guest …
          accounts.py                        # 帳號層：**主人（master）與客人（guest）用同一套帳號物件**，token 只存雜湊。
          auth.py                            # Token → AuthContext：owner token、guest token、角色、限流与审计。
          devices.py                         # 一個帳號、多台端末（owner 2026-11-16 拍板「多設備配對；客人也可以多設備」）。
          guest_store.py                     # Guest 加入申請、關係聲明、主人審核、Discord 白名單的持久化。
          pairing.py                         # 配對碼：已登入的端末幫同一個帳號簽一台新端末（owner 2026-11-16：二維碼＋6 位數字兩種都要）。
        ingress.py                           # 入站執行：**源 → 路由 → 自動切片 → 調度 → 管線 → 源**。
        ingress_support.py                   # IngressRuntime 的內部雜務：記錄、反應幀、在場、切片。
        inner/                               # 時間感、心跳、提醒與情緒等內在狀態。
          activity_affect.py                 # 她「自己的事」也會改變心情（owner 2026-10-22：「她的個人活動也應該影響心情變化」）。
          affect.py                          # 情緒狀態的**適配層**：核心是 vendored（`brain/vendor/aem_v2.py`，未改）。
          affect_repair.py                   # 情緒層的「保養」與「換尺」——不改 vendored 核心，只在適配層動手（owner 2026-11-09 稽核）。
          beats.py                           # 心跳留痕（2026-10-27）：每一跳寫一行，讓她（和我們）能回指「第 N 跳」。
          call_pickup.py                     # 接不接電話：主人在 App 撥過來時，先由她決定要不要接（owner 2026-11-17）。
          circadian.py                       # 各自的生物钟：她们各有各的作息（owner 2026-11-08）。
          clocks.py                          # 兩個時鐘：「她的時間」與「對方的時間」（owner 2026-10-22）。
          dedupe.py                          # 跨源同句的合併索引：**30 秒內、非同源、同一句話 ⇒ 算一件**（owner 2026-11-01 定）。
          drive.py                           # 內驅：把「能做而不做」換成「有帳沒結、有假設沒驗」（owner 2026-11-09）。
          failure.py                         # 失敗的三分法：**先把「失敗」分類，再決定怎麼辦**（owner 2026-11-03 拍板 ①）。
          heartbeat_set.py                   # 一組心跳（每個角色一條）——從 `runtime/heartbeat.py` 搬出來（本檔已到 600 行上限）。
          intervals.py                       # 每個角色自己的心跳節拍（秒）——從 `runtime/heartbeat.py` 搬出來（本檔已到 600 行上限）。
          monologue.py                       # 內心獨白（推演層）：**她每幾分鐘自己想一遍**，寫一句話＋一個動作（owner 2026-11-01「走下一步」）。
          mood_cause.py                      # 心情的「主要原因」，以及**主人看不看得見**它（情感整理 P4，docs/PLAN-affect-reorg-2026110…
          mood_events.py                     # 把情緒變更送到 App 的常駐 SSE；聊天交付與狀態事件共用傳輸、分開處理。
          mood_series.py                     # 心情的「曲線」與「統計」——都只讀，不改狀態（`AffectService.series()`／`stats()` 的實作）。
          mood_words.py                      # 心情 → 話（情感整理 P7：以前散在 `affect._TONE`、心跳 `_TEMPLATES`、`heartbeat_c…
          moodline.py                        # 心情定位（mood line）：把 AEM 的**數字**翻成一句人話（owner 2026-11-01 指定要做）。
          pending_delivery.py                # 主人要她交付、她還沒交的東西（2026-11-08）。
          premise.py                         # 出口閘：她**先開口**的那一句，主人只憑他看得到的東西能不能看懂（common-ground 第 5、6 步）。
          presets.py                         # 她自己的活動庫：每個角色一庫（**純資料**，2026-11-09 從 `runtime/life.py` 拆出來——那檔頂到…
          reminders.py                       # 用「跳」記時間的預約（2026-10-27）。
          rooms.py                           # 房間：**世界變化與分層記憶共用的那一個鍵**（owner 2026-11-03「語義對齊」）。
          rp_ledger.py                       # 存摺（RP 帳本）：**只記帳，不擋人**（owner 2026-11-01 定）。
          self_facts.py                      # 獨白／自主任務要用的兩塊「關於她自己」的事實（2026-11-16）。
          self_model.py                      # 自傳式記憶（提案 ⑤，owner 2026-11-16「帮我做了」）。
          sleep_decay.py                     # 睡着的時間流速（owner 2026-11-16「睡眠中衰减率应该有所…」）。
          sleep_gate.py                      # 睡眠闸（owner 2026-11-16「睡眠时间还有内心活动和心情变化，不太合理」）＋让路逻辑。
          spin.py                            # 自我活動不自旋（owner 2026-11-09「我应该如何防止它们自旋」→ 做 1／2／3／4／6）。
          task_ledger.py                     # 她的任務台帳（owner 2026-11-03）：**先查自己手上還有什麼，再決定做什麼**。
          timeframe.py                       # 時間感：給她的 prompt 一隻鐘（2026-10-27）。
          willing.py                         # 開下一件自己的事之前，**她可以選擇不做**（owner 2026-11-09「心情不好，就不做了」）。
          work_output.py                     # 作品檔的合併（生活回路的產物）：**一件作品一個檔**，每一步是檔裡的一節。
          world_delta.py                     # 「世界はどう変わったか」——兩層（owner 2026-11-03 拍板）。
          world_events.py                    # 一次性世界事件：共享环境与限定范围的房间事件分开投递。
          world_shared.py                    # 共享世界層：對誰都一樣的事實（時刻／主人在哪個端／天氣／位置的 keyed 摘要）。
        lane.py                              # 執行槽的兩個基本型：取消令牌與線的執行狀態。
        lease_store.py                       # FocusLease 的持久化：重啟後 A 的追問仍然接得上。
        life.py                              # 她自己的活動：步驟執行器與預設事項（owner 2026-10-22 擴充）。
        life_loop.py                         # 串行生活迴圈：**一次只推進一步；人一來就停在邊界；斷點落盤；之後接著做。**
        life_step.py                         # 她的「自己做事」：每一步怎麼寫（從 `life.py` 拆出來，2026-10-27）。
        locations.py                         # 位置（座標）倉：**按端末**存，天氣查詢取「最近上報的那台」。
        memory/                              # 記憶檢索、門牌解析、窗口摘要與披露邊界。
          attention_field.py                 # 記憶注意力場（提案，owner 2026-10-31 提出、2026-11-16「帮我做了」）。
          disclosure.py                      # 披露層的唯一規則：`在場 ⊆ 房間`（可說）；以及「她自己那層永遠想得起來」（可讀）。
          distill_known.py                   # 提煉時給模型看「已經記得、而且跟這段對話有關的事」（PENDING B.Agent.3：多值事實的同義碎片，2026-11-1…
          distill_support.py                 # 蒸餾的輔助函式：**遊戲窗口的標記**、窗口指紋與候選事實落庫（把 distill.py 壓在 600 行棘輪下）。
          fact_history.py                    # 記憶入檔履歷（`facts_history`）的唯一讀寫入口＋「記憶庫在哪個檔」的唯一判法。
          life_index.py                      # 生活流的主題索引（owner 2026-11-03；2026-11-03 從 `memory_search` 拆出來壓行數）。
          memory_open.py                     # `memory.open`：按門牌把**逐字原文**取回來（不轉述、不猜鄰行）。
          memory_search.py                   # `memory.search`（P3）：她自己在回合裡查過去，而不是開頭被動塞。
          past_topic.py                      # 籠統的「以前」＋話題檢索（owner 2026-11-03）。
          pointer.py                         # 門牌（pointer）：摘要 → 原文的可回取指針。
          rerank.py                          # 檢索重排（rerank）：向量只管「像不像」，最後由 LLM 判「到底是不是同一件事」。
          time_intent.py                     # 問句裡的時間意圖：`memory.search` 要不要按某個時間點查（as-of），還是只是在問「以前」。
          usage_feedback.py                  # 記憶使用量回饋：哪一輪真的用到了哪條記憶（owner 2026-11-16）。
          vector_store.py                    # 向量倉：把事實／窗口摘要／原文的向量存在本地 SQLite（可重建、可離線）。
          window_repeat.py                   # 窗口摘要的「復讀」判定（owner 2026-11-03；同日從 `distill` 拆出來壓行數）。
          window_store.py                    # 提煉窗口摘要（`window_summaries`）的**唯一**讀寫入口。
        ops/                                 # `runtime/ops/`：維運：每日資料快照、日誌輪替與每日護欄、回合遙測、告警。
          alerts.py                          # 告警：**她出問題時要有人知道**（`turns.jsonl` ＋ `/api/metrics` 只是紀錄，沒人盯）。
          backup.py                          # 每日資料快照（owner 2026-10-23 第 1 項）：她的記憶現在只有一份，先加保險。
          rotation.py                        # 日誌／資料輪轉：**append-only 的檔案要有上限**（owner 2026-10-22 指定的第二件事）。
          telemetry.py                       # 每輪的結構化紀錄（`data/logs/turns.jsonl`）＋ `/api/metrics`。
          thread_reaper.py                   # 線的回收器（E4，2026-11-17）：定期把不會再用的線關掉，片段尾巴補封片。
        owner_queue.py                       # 主人指令的佇列：**一次只跑一條**，每條有編號、狀態、結果摘要；落盤、可取消、有上限。
        pending_store.py                     # YIELD 待辦的落盤：重啟後仍知道「她原本打算什麼時候補位」。
        playground.py                        # 遊樂場：**她自己的** ＋ **大家共用的**（owner 2026-10-22）。
        prethink.py                          # 提前想（idle pre-think）：她一個人的時候先想，等下一輪直接用。
        proactive.py                         # 主動消息的閘：**什麼時候她可以自己開口**（owner 2026-10-22「直接開工」的第 1 項）。
        rawlog.py                            # Raw 原文倉：**一個角色一個倉**，append-only，保留 12 個月（owner 2026-10-23）。
        reflection.py                        # 她的每日自我總結（owner 2026-10-24：「四個都做」的第 C 件）。
        reply_split.py                       # 回覆的分段（server 側唯一一份）——owner 2026-10-23：「为什么不做在server侧」。
        scheduler.py                         # 調度器：**實時源並行、非實時源排隊**（使用者 2026-10-21 拍板）。
        self_tasks.py                        # 她的「自己的事」什麼時候做——由 AEM 決定（owner 2026-10-22 第②條）。
        self_work.py                         # 她自己的事也要**真的去做**（owner 2026-10-24）。
        skills.py                            # 工作技能（skills）：**人格不進這裡**，只放「一件事怎麼做」。
        source_backfill.py                   # 歷史來源清洗：把「本該是 Discord 私訊」的老回合標回 `discord:dm:`。
        stance.py                            # 立场的应用层：读、写、按 principal 渲染成 prompt 里的一小段。
        stance_store.py                      # 立场的落盘：**每实体一个 JSON**，里面按 principal 分槽。
        subagents.py                         # 子代理（內在並行思考）——**單行動主線不變**。
        text_clean.py                        # 回覆文字的清洗（2026-10-29 owner：回信带 # 数字、TTS 念数字、假贴图）
        threadstore.py                       # 線的存放區：**只負責「哪條線是活的」**，不碰記憶、不碰執行。
        tools/                               # 她可以自己調用的動作（工具）。每一類一個模組；這裡把名字匯總出來（舊的 import 照用）。
          base.py                            # 工具的共同型別：呼叫情境、結果、協議與註冊表（按模式／身分／能力／渠道收窄；分層見 `channels.py`）。
          channels.py                        # 工具的「渠道」：同一個她，在 App、電話、Discord、TRPG 桌上能用的工具不一樣。
          delegate.py                        # 委任：把出圖交給外部的委任先（只有主人、只在 work）。
          file_send.py                       # `file.send`：把她自己的檔案當附件送給主人（owner 2026-11-08「加上送文件到 discord 私聊的功…
          memory.py                          # `memory.search`：她在回合裡自己翻過去（三層，各守披露規則）。
          playground.py                      # 遊樂場：她自己的小筆記本（每個角色一份＋共用一份）。
          records.py                         # 她自己的紀錄：筆記正文（`notes.read`）。
          remind.py                          # `remind.in`：她替自己排「幾跳之後／幾分鐘後／幾點」要提某件事（2026-10-27）。
          shell.py                           # `terminal`：在她**自己的 Ubuntu 虚拟机**里执行命令（owner 2026-11-03）。
          skills.py                          # `skill.view`：取一支工作技能的全文。
          sticker.py                         # `sticker.send`：她自己挑一張表情包（スタンプ）回過去（owner 2026-10-29「雙向」）。
          voice.py                           # 語音動作：撥號／這一輪出聲／掛斷（她自己決定，不是 UI 開關）。
        trpg_reports.py                      # TRPG 匯報的落點（owner 2026-10-22：「trpg 还是不要往 discord 里面投好一点」）。
        voice/                               # 語音子包（2026-11-17，NEXT-STEPS WP-D／架構審核 E5）：通話服務、發言權仲裁、語音房在場、通話尾句扣…
          arbiter.py                         # 發言權仲裁：**一個實體只有一張嘴**。
          outbound.py                        # 她自己打電話給主人（owner 2026-11-17）：什麼時候可以打。
          service.py                         # 語音服務：通話 session、粘性綁定、假的 speech 幀。
          speech_hold.py                     # 通話裡「還沒講完」的那句先扣住：主人接著講 ⇒ 兩句併成一句再送，不各起一輪。
          vc_presence.py                     # 她在語音房的**在場決策**：什麼時候自己進去、什麼時候自己退、什麼時候先問一句。
      skills_brain.py                        # 技能整理／學習專用腦（owner 2026-11-03）：長上下文、沒有回話延遲壓力 ⇒ 走雲端 DeepSeek。
      sources/                               # 源層：每個入口一個適配器，只做「傳輸 ↔ 事件」，不做人格／記憶／大腦。
        base.py                              # 源（Source）協議：**只做傳輸 ↔ 事件，不做人格、不做記憶、不做大腦**。
      stream_guard.py                        # 串流的兩個守門員：**首幀預算**與**讀取超時放寬**（owner 2026-11-02 重排拆出來的）。
      turn_trace.py                          # 每輪的「為什麼慢」取證（owner 2026-11-03：「先量為什麼慢」）。
      vendor/
        aem_v2.py                            # Python 3.10+, POSIX local filesystem, standard library only.
    adapter/                                 # 外部渠道 canonical 實作；無 blueagent_next 兼容命名空間
      app_adapter_store.py                   # App adapter 的旁挂档：事件失败计数／dead-letter／未提交的回复。
      app_assets.py                          # Agent 与客户端资产的窄边界：远程读取贴纸语义，旧端点按需取单张图。
      app_board.py                           # 旧公共区 API／stdio MCP 的远程兼容壳；凭证按调用者解析，绝不共享改写。
      app_board_events.py                    # 公共区事件回流：有人回她的帖／赞她的帖 ⇒ 她知道（感知，不开 LLM 回合）。
      app_board_publish.py                   # Agent 公开活动 → App Server 公共区的显式发布器。
      app_call_decline.py                    # 她沒接主人的電話之後（owner 2026-11-17）：留痕，必要時改發一則私聊。
      app_call_out.py                        # 她打給主人：經 App Server 讓主人的 App 響鈴，等結果，寫進她的私人行動日誌（owner 2026-11-17）。
      app_dm_sleep.py                        # 私聊也可以「先不回」：她睡著的時候，私聊先放著，醒來再讀（owner 2026-11-09）。
      app_group.py                           # App 三人房：每人自己「拿起手机」＋共享场账本（群聊自聊 M5a／M5b，`docs/GROUP-SELF-CHAT-202…
      app_group_delay.py                     # 群聊「拿起手机」延迟队列：把等待与事件拉取解耦（架构审核 E1／WP8，2026-11-15）。
      app_group_knock.py                     # 三人房的定时敲门（GROUP-SELF-CHAT §2 ①，2026-11-08 后台报告「没人说话时不会自己醒来敲门」）。
      app_identity.py                        # App 邊界的身分翻譯：App 帳號 id → 腦端 principal（owner 2026-11-09「核心账号层在脑端」…
      app_locations.py                       # 按角色凭证读取 App Server 授权位置；旧 MR 位置仍走原 Agent 端点。
      app_mentions.py                        # 群聊点名判定：主人这句话是不是在叫某个角色（PLAN-chat-list §3.3「點名優先」）。
      app_proactive.py                       # 主動消息走 App Server 的會話（docs/PLAN-common-ground-20261109.md §7.4，o…
      app_protocol.py                        # Agent 侧平台线协议；只含远程调用所需类型，不导入 App Server 实现。
      app_read_tools.py                      # 她從 App 取聊天前後文與帖子的工具（owner 2026-11-08「app 聊天的前後文更倾向于从 app 工具中去取，…
      app_server.py                          # App adapter：消费平台会话事件，感知与回复分离，最终回复只经 MCP 提交。
      app_voice.py                           # App Server 实时语音 pipe：完整 utterance → STT／对话／TTS → 平台事件。
      discord_source/                        # Discord 源：bot 在频道里听、把话送进同一条管道、把回复（和语音）发回去。
        bots.py                              # 多顆 Discord bot：一顆 bot ↔ 一個實體（owner 2026-10-27「做 1 到 4」）。
        config.py                            # Discord 源的設定（`DISCORD_*`／`BLUEAGENT_DISCORD_*`）與她的叫名。
        routing.py                           # 訊息映射：誰在說話（Discord id → principal）、對誰說（mention／叫名 → entity）、要不要回。
        source.py                            # 源本體：一則 Discord 訊息怎麼變成她的一輪（含客人、旁聽、回覆判定）。
        speech.py                            # 語音與音檔：VC 的輸入、speech 幀的播放、WAV 合併／解碼、語音頻道指令。
        transport.py                         # 傳輸與連線生命週期：discord.py client、送出（頻道／私聊）、後續交付、啟動與停止。
      discord_vc_receiver.py                 # Discord 語音接收（**照搬現役 discordvc 那套已跑通的**，owner 2026-10-22 指正）。
      discord_voice/                         # Discord 語音頻道：進頻道聽、斷句、STT、回話（聲音也回進頻道）。
        audio.py                             # 音訊：48k 立體聲 PCM ↔ 16k WAV、能量（RMS）、按靜音把一段段話切出來（Utterance）。
        config.py                            # 語音頻道的設定（`BLUEAGENT_DISCORD_VC*`）與取樣率常數。
        playback.py                          # 播放與發佈：PCM 播放佇列、打斷時停播、音檔發佈與文字貼文、`say`。
        presence.py                          # 在場與進出頻道：join／leave／bootstrap、在場輪詢（vc_presence 的決策落地）、人在不在。
        receive.py                           # 接收與打斷：音訊塊 → 斷句 → 一句話；主人一開口就打斷她；噪音不進管線。
        session.py                           # 語音工作階段本體：一個頻道一個 session（由在場、接收、播放三個 mixin 組成）＋ build_voice。
      embedder.py                            # 向量（語意）嵌入：**本機跑，不依賴外部服務**（owner 2026-10-23「可以 b」）。
      end_judge.py                           # 「說完了沒」：一句話停下來之後，讓本地 qwen 看轉寫判斷它是不是講完了。
      fact_audience_index.py                 # `fact_audience` 索引：把 `facts.audience`（逗號串接 TEXT）與 `facts.princi…
      hermes_delegate.py                     # 委任的傳輸層：把一件工作交給 Hermes（`/v1/runs` 轉播），與現役橋同一條路。
      mcp_client.py                          # 最小 MCP 客戶端（P5）：**外部能力走協議接進來**，不寫死 API。
      mcp_tools.py                           # 把 MCP server 的工具註冊成她的工具面（P5）。
      memory_hindsight.py                    # 記憶：**和現役 blueagent 同一個框架**（Hindsight bank）＋本地原文/ACL 層。
      memory_sqlite.py                       # 記憶的索引後端：SQLite（FTS5 trigram）＋ JSONL 原文。
      memory_sqlite_schema.py                # SQLite 記憶索引的建表結構與欄位遷移（2026-11-15 從 `memory_sqlite.py` 拆出，維持單檔 6…
      netguard.py                            # Firecrawl 起始 URL 的公網閘：模型給的網址不能直接碰家裡的內網。
      silero_cut.py                          # 切句：**silero-vad**（照搬現役 `ears.StreamCutter`，owner 2026-10-22：「ST…
      stt_local.py                           # 語音轉文字：**沿用現役 blue agent 那套**（owner 2026-10-22：「STT 復用之前 blue ag…
      trpg/                                  # 把她放進 TRPG 桌：**桌端 kick 叫醒她 → 她看牌面 → 說一句＋宣告一個合法動作**。
        config.py                            # TRPG 席位的設定（`BLUEAGENT_TRPG_*`）。
        decide.py                            # 動作決策：牌面渲染成文字、從她說的話裡挑一個合法動作（確定性規則，不強迫她吐 JSON）。
        hub.py                               # 通往 TRPG hub 的客戶端（**伺服器側的代理**）。
        mcp.py                               # 桌子的 MCP 協議：看牌面／出手／說話（`table_view`／`table_act`／`table_say`）。
        mood.py                              # 遊戲情緒：遊戲事件 → 她的心情（記在桌上，不記在主人頭上）；心情太差就不想玩。
        prompt.py                            # 每一手的 prompt：把牌面、可做的動作、她的席位寫成她看得懂的一段。
        report.py                            # 匯報：她在桌上的近況（多久報一次、情緒門檻、報給誰）。
        seat.py                              # 席位本體：kick 入口、入座／離席、每一手的流程、狀態與健康。
      tts_bridge.py                          # 真聲音的嘴：**直接包現役橋的 `ba/voice`**（不另寫一套合成客戶端）。
      weather.py                             # 天氣：座標（或地名）→ 一句日語。**沿用現役 BlueAgent 的實作**（owner 2026-10-23）。
      weather_tool.py                        # 天氣工具 `weather.get`（包 Open-Meteo 等外部服務，所以住 adapters；由組合根註冊）。
    server/                                  # Agent 自己的 HTTP／SSE（/api、/admin）；App Server 是 app/server
      api/                                   # HTTP 路由背後的處理函式（回 `(status, payload)`）。按路由群分模組；這裡把名字匯總出來，
        accounts.py                          # 帳號與身分：/api/me、/api/accounts*。
        admin.py                             # 腦端管理頁的資料（owner 2026-11-09「把脑端的管理页面做出来」）：`/admin`。
        app_kick.py                          # `POST /api/app/kick`：App Server 有新事件時叫醒她的 App 適配器（規格 `docs/PLAN…
        board.py                             # 旧 `/api/media/<id>` 的公共区授权判定：这张图能不能给这个调用者看，问 App Server。
        common.py                            # 共用：401／403、owner 閘、請求體裡的 id／參與者。
        diary.py                             # 她的日記與工作成果（master 端「+」→ 日記；owner 2026-10-31）：`GET /api/diary`、`/…
        dispatch.py                          # POST 分派：路徑 → 處理函式（含 owner-only 前綴的統一閘）。
        games.py                             # TRPG 桌與小遊戲：/api/trpg/*、/api/game、/api/self/next。
        guests.py                            # 訪客：加入、狀態、目錄、owner 審批、Discord 白名單。
        items.py                             # 延時聚合的送出內容（owner 2026-11-04：「對於用戶的前台文字／圖片／表情包發送，設計延時聚合發送」）。
        media.py                             # 圖片輸入與媒體庫：/api/image、/api/media。
        memory.py                            # 記憶的唯讀檢視：提煉履歷（trace）與窗口摘要。
        motion.py                            # 動作片段（`bamotion/1`）：`GET /api/motion/<id>`、`GET /api/motion?tag=…
        owner.py                             # 主人的控制面：活動、語音、封片、立場、指令佇列、手動主動消息。
        pairing.py                           # 多設備配對與端末管理（owner 2026-11-16：二維碼＋6 位數字都要；客人也可以多設備）。
        sessions.py                          # 會話列表：只讀聚合現有 entity 與 outbox，不建立第二份聊天真值。
        system.py                            # 狀態與營運：/api/health、/api/state、/api/mood、/api/metrics、/api/reset。
        tasks.py                             # 她自己的任務台帳（master 端「看板＋日曆」；owner 2026-11-03）：
        turns.py                             # 對話回合：送出（文字／音檔）、outbox 補拿、位置。
      app_voice_http.py                      # App Server → brain 内部语音 HTTP 边界。
      http.py                                # HTTP/靜態層：只解析請求、寫回應；業務在 `api.py`。客戶端在 `client/`。
      http_encoding.py                       # HTTP 內容協商的小函式（保持 Handler 只負責寫回應）。
      http_responses.py                      # 可快取的 JSON／音檔 HTTP 表示；由 `server.http.Handler` 的小包裝呼叫。
      http_routes.py                         # 避免主 HTTP 文件膨胀的薄路由；不承载业务规则。
      integrations/                          # 外部整合的啟動裝配（Discord、TRPG 席位、告警、主動發言信道）。
        alerts.py                            # 告警：TTS 失敗率／p95 超閾值就私聊主人（紀錄有人看才算可觀測）。
        delivery.py                          # 主動發言的信道：Discord 上線後才註冊（DM 先、群後；私事不進群；語音在寂寞度高時優先）。
        discord.py                           # Discord 源（文字＋可選的語音頻道）：有 token 才連。
        trpg.py                              # TRPG 席位：桌端 kick 叫她 → 她看牌面 → 說話＋宣告一個合法動作。
      main.py                                # 啟動入口：參數、環境檔、建應用、起 HTTP、接外部整合、收尾。
      sse_names.py                           # SSE 事件名的兩條規則（從 `server/http.py` 抽出來，那個檔有 600 行棘輪）。
      stream_profile.py                      # App 流式回應的可選精簡表示；未協商的客戶端永遠拿原始契約。
    tools/                                   # 維運／遷移／診斷腳本（不被生產碼 import）
      aem_reproject.py                       # 換 AEM 核心後的一次性重投影（owner 2026-11-01 升 1,215 行那顆）。
      agent_container_entrypoint.py          # Load mounted Docker env files, then exec the Agent without logg…
      alert_calibration.py                   # 告警門檻標定：拿**真數據**決定 p95 / 失敗率門檻，不要再拍數字。
      backfill_call_history.py               # 回填：把**已存在的**通話回合從 App 的對話紀錄裡拿掉，只留一條標記。
      backfill_episode_ids.py                # 改正舊的封片 id（補上線後綴）：python3 agent/tools/backfill_episode_ids.py --…
      backfill_episodes.py                   # 把已封存段落（`data/episodes.jsonl`）回填進原文倉（owner 2026-10-23「補吧」）。
      backfill_raw.py                        # 把歷史對話回填進 raw 原文倉（`data/raw/<entity>/<YYYY-MM>.jsonl`）。
      backfill_raw_from_threads.py           # 把 `threads.json` 裡**還在窗口內、但沒落進 raw** 的輪次補回去（2026-11-01）。
      backfill_streams.py                    # 回填 `stream`／`topic`（所有者 2026-11-03：記憶分流第一步，**不改行為**）。
      backfill_window_ptrs.py                # 回填窗口摘要的門牌：python3 agent/tools/backfill_window_ptrs.py --data da…
      bench_threads_persist.py               # E4 先量再遷（架構審查 2026-11-15 §3.2-4）：`ThreadStore.persist()` 每次要多久、寫…
      cache_probe.py                         # 用**真的 prompt**量前綴快取：現在這一版 vs 舊排法（每回合變的東西放 system 頭）。
      call_report.py                         # 讀最近的通話：outbox 的 kind=call 列 ➜ 對到 turns.jsonl 的同一輪 ➜ 印分段耗時。
      compact_memory.py                      # 清理向量倉孤兒並壓縮兩個庫（owner 2026-11-03「繼續清」的收尾）。
      compile_persona_core.py                # 編譯某個角色的人格 core（SPIN 第 ① 段，P0）。
      context_probe.py                       # 36K 預算下滾動段實際能放幾條（用真實長度的句子估，只看組裝結果）。
      distill_now.py                         # 立刻跑一輪背景提煉（不用等 worker 的 10 分鐘）。
      doctor.py                              # 體檢器 CLI：python3 agent/tools/doctor.py --data data --entity mira
      gen_env_table.py                       # 產生 `docs/ENVIRONMENT.md`：環境變數／預設值／讀取位置總表。
      gen_file_tree.py                       # 產生 `docs/FILE-TREE.md` 的檔案樹（每個 Python 模組帶它 docstring 的第一行）。
      group_stats.py                         # 三人房的六个指标（GROUP-SELF-CHAT §10，M5d）：群聊「像不像人在聊」用分布判，不用形容词。
      mcp_board_server.py                    # 公共区与公开心情 stdio MCP；平台模式下全部经 App Server 远程写入。
      mcp_firecrawl_server.py                # Firecrawl 的 MCP 伺服器（stdio）——把 Mac mini 上的自架 Firecrawl 當「外部能力」接給…
      mcp_skills_server.py                   # 技能台帳的 MCP 伺服器（stdio）——她們各自能「學」：寫、試用、退役。
      mcp_tasks_server.py                    # 任務台帳的 MCP 伺服器（stdio）——把「看板＋日曆」做成她可以自己操作的外部能力。
      measure_stt_latency.py                 # 量測通話一句的實際分段耗時（STT）。
      migrate_app_identities.py              # 一次性遷移：把 11-07 之後**以 App 帳號 id 存下的身分**改回腦端 principal（owner 2026-…
      migrations/
        archive_noise_facts.py               # 把「系統提示／桌端叫醒」這類誤入事實層的記憶歸檔（不刪，只標成失效）。
        audit_game_scene_20261115.py         # 只讀盤點（2026-11-15 WP2a）：TRPG／現實場景隔離修復前的兩類歷史髒資料。
        backfill_fact_audience_20261116.py   # 把 `facts.audience`／`principal` 回填进 `fact_audience` 索引（owner 202…
        backfill_fiction_20261116.py         # 存量分「作品 vs 經歷」（owner 2026-11-16 選的第 3 件）。
        backfill_supersedes_outbound.py      # 把「只有 inbound、沒有 outbound」的舊取代關係補成雙向（只補空的那一側）。
        classify_facts.py                    # 事實分類回填：給既有事實補 `kind` / `slot`（只補欄位，不改文本）。
        converge_slots_20261116.py           # 存量回溯：單值槽位收成一條現值（owner 2026-11-16「同一槽位保持一條，按時間關係刷新」）。
        fix_raw_and_facts_20261024.py        # 一次性資料修補（2026-10-24）：① raw 原文倉去重 ② 行動日誌事實回填 entity／有效期。
        migrate_entity_partition.py          # 按角色實體分倉：把 `facts` / `facts.jsonl` 補上 `entity` 欄並填值（owner 2026-1…
        purge_seed_facts.py                  # 清掉生產庫裡的 demo 種子事實（`source='seed'`）——owner 2026-10-23「先做 1」。
        rebuild_shingles.py                  # 重建字面索引（shingles）——修好「假名被濾掉」之後的補救。
        split_norm_unique.py                 # 把 `facts.norm` 的全域唯一改成「同一倉唯一」（`UNIQUE (norm, entity)`）。
      peek_affect.py                         # 看她現在的情緒數值（用來標定 TRPG 匯報門檻）。
      peek_threads.py                        # 印最近幾輪對話（所有 thread 的 transcript 按時間排序），看「混亂」長什麼樣。
      provision_app_server.py                # Provision App Server accounts without exposing owner or entity …
      prune_memory.py                        # 記憶庫清理（owner 2026-11-03「繼續清」）。
      rotate_owner_token.py                  # 把 owner token 輪替掉（舊的立刻失效，新的只印這一次）。
      rp_daily.py                            # 存摺的日結（給人看的報表；**不進她的 prompt**——aster 2026-11-01 的提醒）。
      rp_status.py                           # 存摺的讀取口：**唯一權威是流水**，這個工具只負責印，不算第二份。
      seed_initial_memories.py               # 前史（初期記憶）を記憶庫に入れる——**一度だけ**走らせる道具（owner 2026-11-03）。
      tts_failure_probe.py                   # 查「為什麼合成失敗」：池的健康、單句延遲、並發時會不會拿不到實例。
      vm_web_cli.py                          # 她的「看网页」工具（跑在自己的 VM 裡）：`web text|shot|links|eval`
    client/                                  # 無構建的 owner 調試台＋管理台（Web，/static、/admin）；14 檔
    persona/                                 # 人格檔（SOUL.md、各角色 .md／.interests.md）；7 檔
    motion_service/                          # GPU 機上的動作生成服務（獨立部署）；6 檔
    tests/                                   # 離線驗收（run_all.py 連 app/tests 一起跑） 276 支
      arch_common.py
      demo_common.py                         # Demo 验收的共享工具（纯 stdlib、离线、不碰旧 agent）。
      demo_people.py                         # 測試用的假人物與種子記憶（2026-10-25 從生產搬出來）。
      platform_server.py                     # 启动真实 App Server 并给旧 Agent 入口注入远程凭证的测试夹具。
      prompt_lang.py                         # 「進 prompt 的文字必須是日語」的共用判據（給靜態掃描與渲染級測試共用）。
      run_all.py                             # 跑全部離線驗收（Agent ＋ App）：python3 agent/tests/run_all.py
  app/                                       # 獨立 App 產品邊界（App Server＋Unity 手機端）；不依賴 Agent
    Dockerfile
    client/                                  # 客户端拥有的素材、语义目录与播放实现；不包含 Agent 运行时。
      assets/                                # 平台客户端资产：贴纸内容包与动作片段格式。
        motion.py                            # 動作片段庫（`bamotion/1`）：`$BLUEAGENT_DATA_DIR/motion/` 下的 `<id>.json…
        stickers.py                          # 客户端贴纸目录：每角色一包，共用包在 ``_shared/``。
      tools/
        bake_icons.py                        # 把真相的 SVG 圖示（`index.html` 的 `<symbol id="i-*">`）烤成 PNG ⇒ `Assets…
        bake_next_icons.py                   # 烤 Next 端新增的圖示與裝飾貼圖（2026-10-29 原創幾何風格改版）⇒ `Assets/Resources/icon…
        button_audit.py                      # **每一屏、每一顆鈕**：把「畫面上做得出名字的節點」與「`AppShell` 有接線的分支」對一次。
        capture_bridge_sse.py                # 把橋（本機 :8900）真實吐出來的 SSE 位元組原樣抓下來，當作 C# 側 SSE 解析器的**線上形狀**真值。
        check_next_strings.py                # 閘門：Next 端的界面文字（`Resources/i18n/next_{locale}.json` ＋ `Scripts/N…
        color_probe.py                       # 像素抽查（M3 的**第二路**判據）：畫上去的**顏色／形狀**用像素量測驗收。
        corner_probe.py                      # **圓角量測**：量「那個元素的四個角到底有沒有被切掉、切掉多少」。
        device_beat.py                       # 收真機心跳（UDP）——真機沒有 logcat，這是唯一可靠的診斷通道。
        diff_preview.py                      # 機械比對：同一場景、只換嘴型值的兩張預覽圖差在哪裡。
        diff_regions.py                      # 真相 vs Unity 的**差異熱點盤點**：把兩張圖縮到同一格、算逐點色差，回報「差在哪裡」。
        gen_emotion_archetypes_cs.py         # 情緒原型表：Kotlin → C#（**產生器**，不要手改產物）。
        ink_bbox.py                          # **墨跡 bbox 量測**（第三路判據：文字 A/B 之外、`rect_audit` 之外）。
        ios_mouth_diff.py                    # 比較 iOS 模擬器連續截圖：嘴部區域有沒有在動（機械判據，不靠眼睛）。
        magic_idle.py                        # MAGIC 待機的訓練資料（`magic-train/1`）——`docs/PLAN-magic-motion-2026103…
        make_launcher_icon.py                # 把 `tools/icon_src/blueagent_logo.webp`（owner 2026-10-20 给的标志）烤成…
        make_replay_fixture.py               # 把**真橋**抓下來的幀序列（`capture_bridge_sse.py` 的 `.frames.jsonl`）變成真機回放…
        motion_clip.py                       # `bamotion/1` 動作片段：參考編碼器、驗證器、合成示範（owner 2026-10-31：Kimodo 驅動 3D …
        overlay_base_probe.py                # **真相側「疊層底下是誰」的判準工具**：證明那幾屏是「底屏留著、只壓一層暗」。
        row_scan.py                          # **同一格設計座標的豎掃**：在一條（或幾條）x 上，把真相與 Unity 的每一行顏色並排印出來。
        slice_probe.py                       # **沿一條線掃色塊**（M3 的像素判據之二）：在同一組**設計座標**上掃真相／Unity 兩張圖，
        subset_fonts.py                      # 把 Noto Sans JP／SC／TC 裁成常用字子集（2026-10-31：三支全字型 23.5 MB 全進包，裁完 7.…
        sync_stage3d_models.py               # 把 BA 原生 3D 模型匯出物搬進 Unity（聊天頁立繪那一格的 3D 切換，owner 2026-10-30）。
        unpack_unitypackage.py               # 把 `TMP Essential Resources.unitypackage` 解到 Unity 專案的 Assets/。
      README.md                              # BlueAgent Next — Unity 客户端
      shared/BlueAgent.Logic/                # 共用邏輯層 C#（53 檔，不碰平台 API）
      shared/BlueAgent.Logic.Tests/          # NUnit 對拍測試（33 檔）
      fixtures/                              # 上游產生的真值 JSON（20 檔）
      tools/                                 # 建置／部署／對拍腳本（56 檔）
      BlueAgentUnity/Assets/Scripts/         # 手機與共享執行期 C#（52 檔）
        Next/                                # blueagent-next 的 master／guest 端（Next.unity）
          NextAccountsView.cs                # 帳號管理頁（**只給 master**）：列表／簽發 guest／輪替／撤銷。
          NextAlbumView.cs                   # 送過的照片（P4）：`GET /v1/media` 的縮圖牆，點一張看全圖。從設定頁進來。
          NextApi.cs                         # blueagent-next 的 HTTP 客戶端（端末唯一一條連線）。
          NextBoardPaths.cs                  # 公共区的路徑只在這裡寫一次：直連 App Server 的 `/v1/feed`、`/v1/posts…`（2026-11-0…
          NextBoardUi.cs                     # 公共區共用的帖子零件；聊天頁的泡泡、輸入列與會話狀態不在這裡。
          NextCallView.cs                    # **單人通話**（owner 2026-10-29：「實時和 agent 對話」、只給 master、可以打斷她）。
          NextChatListView.cs                # 会话列表只渲染 App Server／SQLite 的会话摘要，不在端侧另存最后一句。
          NextChatSync.cs                    # S3 聊天同步器：Outbox 一次 drain 一筆；回包、分頁與 SSE 全部只寫 ChatRepository。
          NextChatView.cs                    # 對話（master／guest 共用同一份畫面）。
          NextCommandsView.cs                # 主人指令的收件箱（PENDING B.Agent.0：「Unity 收件箱 UI：看佇列、點取消」，2026-11-17）。只…
          NextComposeView.cs                 # 獨立投稿頁。選圖先走既有 UploadMedia，正式投稿只提交 media id。
          NextConfig.cs                      # 端末的本機設定：**伺服器位址、token、上次的 role／entity**（PlayerPrefs；token 例外，見 …
          NextDevicesView.cs                 # 端末管理（owner 2026-11-16：多設備配對，二維碼＋6 桁兩種都要；客人也可以多設備）。主人與客人都看得到。
          NextFeedView.cs                    # M3 首頁：今天的公共動態與聊天入口。不分角色、不掛舞台；這一級沒有聊天輸入列。
          NextFonts.cs                       # 按語言挑字型（2026-10-28）：界面字跟界面語言走 Noto Sans JP／SC／TC，**她說的話與角色名一律日語字…
          NextFx.cs                          # 平行四邊形：把圖形的頂點按高度水平錯開（上緣往右、下緣往左）。原創幾何風格按鈕／徽章／頁籤的底板都靠它。
          NextIncomingCall.cs                # **她打來的電話**（owner 2026-11-17）：全屏來電畫面——頭像、名字、「応答」「拒否」、鈴聲。
          NextLayout.cs                      # 大螢幕適配（owner 2026-10-30「iPhone Duo 的展開屏幕適配」）。
          NextLocation.cs                    # 位置推送（C-14）：服务端明确授权后，把粗化座標推給 App Server（`POST /v1/me/location`）。
          NextLoginView.cs                   # 登入頁（兩端同一頁）：貼 token → `GET /api/me`。
          NextMoodBadge.cs                   # 立繪左上角的公開心情徽章。資料只來自 App Server 的 `/v1/moods` 與
          NextMotion.cs                      # Next 的動態（2026-10-29 原創幾何風格改版）：一支常駐的補間器＋幾個小元件。
          NextNotices.cs                     # 人類帖主的通知（owner 2026-11-16 定 B.App.8：**App 內提醒條＋系統推送，先只做 Android*…
          NextOutboxMedia.cs                 # 離線選的圖先存在端末（PENDING B.App.5「離線時不能上傳媒體」，2026-11-17）。
          NextOwnedSprite.cs                 # 一顆 `Image` 自己擁有的執行期 Sprite（＋可選的 Texture2D）：物件銷毀或換圖時一起釋放
          NextPager.cs                       # 橫向分頁（owner 2026-11-03「＋ 號選單做成兩排、可以橫向滑動換頁」）：一頁＝可視區那麼寬，
          NextPostView.cs                    # 帖子全文、點讚與扁平一層回覆；這裡沒有也不應長出私聊入口。
          NextProfileView.cs                 # 個人主頁（owner 2026-11-08）：從公共区／帖子詳情點頭像進來。
          NextSettingsView.cs                # 設定（P2）：四頁——音声／背景／サービス／診断，下面固定「帳號管理」「登出」兩列。
          NextShell.cs                       # **blueagent-next 的端末殼**：開機 → 讀 token → `GET /api/me` → 依 `role`…
          NextSocialSync.cs                  # 公共區 Outbox worker：按建立順序一次送一筆；成功／失敗都先落 SQLite，UI 不直接拿網路回包當真值。
          NextStage3D.cs                     # 立繪舞台的 **3D 模式**（owner 2026-10-30：「先用現在的模型……在現在顯示立繪的位置切換顯示 3D 模型…
          NextStageView.cs                   # 聊天頁上方的**立繪舞台**（B2）＋**場景背景**（P3）。
          NextStatusView.cs                  # 状態頁（master）：`/api/health` 的 counts ＋ 這一端自己看到的連線資訊。
          NextStickers.cs                    # 表情包：聊天頁打字列旁那顆笑臉打開的**表情盤**，以及氣泡裡的那張圖。
          NextStrings.cs                     # Next 的界面文字（2026-10-28 完整 i18n）：**畫面上每一個我們自己寫的字都從這裡拿**。
          NextTheme.cs                       # Next 的設計記號（2026-10-29 改版）：參考 an original geometric UI 的**設計語言**…
          NextToast.cs                       # 一次性的提示（owner 2026-10-30「底部狀態列完全可以不顯示」）：以前「她離席了」「進桌失敗」
          NextUi.cs                          # Next 兩端共用的畫面元件（2026-10-29 改成 原創幾何風格的設計系統，顏色見 <see cref="NextThe…
          NextUiDump.cs                      # 驗收鉤子（只在 `BA_SELFTEST=1` 的包裡）：把每一頁、每一種界面語言畫面上的字倒成文字檔。
          NextVoice.cs                       # 語音簡訊的錄音器（B1）：按一下開始、再按一下停止 ⇒ 回一段 **16 kHz／單聲道／16-bit WAV**。
          Stage3DMotion.cs                   # 一段 `bamotion/1` 動作片段（後端 Kimodo 生成；規格 `docs/PLAN-motion-kimodo-2…
          Stage3DSpring.cs                   # 3D 立繪的**頭髮／袖子物理**（owner 2026-10-31：「頭髮和衣服大袖口能不能加點物理演算」）。
    server/                                  # App Server：账号、社交数据与事件契约的独立服务端边界。
      __main__.py                            # 启动独立 App Server：python3 -m app.server。
      accounts.py                            # App Server 平台服务账号：文件只存 token 哈希，认证结果映射为 Actor。
      board_http.py                          # 公共区的客户端 HTTP 面：`/v1/feed`、`/v1/posts…`，与 MCP 的 feed/get/post/re…
      chat/                                  # App Server 持有的会话与消息；不依赖 Agent。
        migration.py                         # legacy outbox 的一次性只读导入；默认 dry-run，不产生社交事件。
        service.py                           # 1:1 聊天用例：认证身份、参与者权限、输入规范化与幂等写入。
        store.py                             # 会话、消息、分段与已读游标的 SQLite 持久化。
      config.py                              # 独立 App Server 配置；数据与账号文件默认独立于 Agent data/。
      device_auth.py                         # 人類帳號的 token 以 Agent 為準（多設備配對，owner 2026-11-16）。
      events.py                              # App Server 持久事件日志：业务库同事务追加，按序号游标同步与重连。
      http.py                                # 独立 App Server HTTP 入口：健康检查、账号、MCP、事件同步与 SSE。
      kicker.py                              # App Server → Agent 的「推送叫醒」（規格：`docs/PLAN-app-kick-20261108.md`）。
      location.py                            # 明确授权的 coarse 设备位置；独立保存，不导入 Agent 或社交消息库。
      mcp.py                                 # App Server 远程 MCP：公共区、聊天、媒体与公开心情，身份只取认证账号。
      media.py                               # App Server 自有图片库：SQLite 元数据、私有二进制、内容授权与缩图。
      media_migration.py                     # legacy data/media 的一次性迁移；默认 dry-run，账号歧义与内容冲突绝不猜测。
      mood.py                                # 平台公开展示心情：独立社交状态，不读取或复制 Agent 私有 affect。
      services.py                            # App Server 运行时装配：只组合独立模块，不 import Agent。
      social/                                # 不依赖 Agent 的公共区社交核心。
        feed.py                              # 公共区通用摘要、稳定游标与分页；不读取 Agent 活动或作品。
        migration.py                         # 旧 Agent 公共区的一次性迁移；默认 dry-run，冲突时绝不写入或回放事件。
        models.py                            # 平台公共区模型：统一表示手动投稿、回复与外部内容的只读投影。
        service.py                           # 公共区独立业务核：身份、可见域、限流、投稿、回复、点赞、删除与分页。
        store.py                             # 公共区 SQLite 索引与互动状态；手动帖／回复正文另存按月 JSONL 原文。
      stickers.py                            # App Server 贴纸目录与认证内容端点；素材属于客户端，不暴露文件路径。
      voice.py                               # 平台实时通话：PCM 小帧接入、服务端切句、brain pipe 与会话内事件流。
      voice_outgoing.py                      # 她打給主人（owner 2026-11-17）：「響鈴中」的來電、接聽／拒接／逾時。
    tests/                                   # App 的離線驗收（app/tests/run_all.py 單跑） 21 支
      app_paths.py                           # App 测试的导入路径：仓库根进 sys.path（`app.*`），不碰 Agent。
      run_all.py                             # 只跑 App（App Server＋Unity 文案／设计）的離線驗收：python3 app/tests/run_all.py
  mr/                                        # Quest 專有工程與構建；共享實作由 app/client 唯一持有
    README.md                                # BlueAgent MR — Quest 3 客户端
    BlueAgentUnity/Assets/Scripts/           # Quest 專有執行期
      MR/                                    # OpenXR／空間／手勢／通話（MR.unity）
        MrBridge.cs                          # MR 端接**同一條橋**（PLAN-mr-quest3 M3；§4-2：不新增協議、服務端一行不改）。
        MrBubble.cs                          # 她頭頂的對話氣泡（owner 2026-11-01：「顯示成她頭頂的氣泡」→「風格參照手機 app」）。
        MrCall.cs                            # MR 端的**通話模式**（owner 2026-11-01「接近 app 的通話模式」）：
        MrCallBadge.cs                       # 通話中她頭頂的聽筒圖示（owner 2026-11-01：「非通話時什麼都不顯示，通話狀態時角色頭頂顯示聽筒圖標」）。
        MrGroundShadow.cs                    # 她腳下的接地陰影（PLAN-mr-quest3 M1）：一片貼地的橢圓，跟著她的腳與朝向走，高度釘在量到的地面上。
        MrPinch.cs                           # 捏手指（手部追蹤；PLAN-mr-quest3 M3，owner 2026-11-01）：拇指尖與食指尖的距離，帶遲滯——
        MrProbe.cs                           # M0 的驗收儀表（PLAN-mr-quest3 §3 M0 的 ②③④，**量出來**而不是看起來）：
        MrShell.cs                           # Quest 3 混合現實端的殼（`Assets/Scenes/MR.unity` 裡唯一的東西；PLAN-mr-quest3）。
        MrToast.cs                           # 視野下方的事件通知（owner 2026-11-01：「debug 用的顯示轉移到 UI 上，事件通知放在視野下方，超時消失」…
    tools/
      sync_workspace.py                      # 組合 Quest／MR 的 Unity 構建工作區，共享執行期仍只由 app/client 持有。
  docs/                                      # 契約與專題設計（現行）
    ADR-app-social-platform-20261105.md      # ADR — App 社交平台与 Agent 解耦
    APP-API-SSE.md                           # App ⇄ BlueAgent Next：**SSE 契約**（v1 + v2 扩展，2026-10-22 定；2026-10…
    ENVIRONMENT.md                           # 環境變數總表
    FILE-TREE.md                             # 檔案樹
    GLOSSARY.md                              # BlueAgent Next 術語表
    accounts-and-clients.md                  # 帳號層與兩種端（master／guest）— 給 Unity 改造用
```
<!-- tree:end -->
