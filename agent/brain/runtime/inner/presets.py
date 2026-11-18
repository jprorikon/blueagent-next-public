"""她自己的活動庫：每個角色一庫（**純資料**，2026-11-09 從 `runtime/life.py` 拆出來——那檔頂到 600 行）。

改活動就改這裡：`PRESETS`（每套的步驟、時段、成品形式、附加指示）與 `INPUTS`（輸入從哪來，防自旋用，
見 `runtime/inner/spin.py`）。挑哪一套、怎麼輪換在 `runtime/life.py` 的 `Rotation`；`life.py` 照舊轉出這些名字。

歷史：10-22 study／play 兩個方向 → 10-24 每人一庫＋輪換 → 10-25 自測與小作品 → 10-27 每日報（`daily_report.py`）
→ 10-31 每日上限 → 11-03 terminal 與每週技能整理 → 11-09 輸入分類、拿掉寫死的情緒、アスター 加五套讀外面的。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

#: 與 `runtime/activity_log.py` 同值（inner 不准橫向 import runtime 的其他模組）
PRIVATE = "private"
PUBLIC = "public"


@dataclass(frozen=True, slots=True)
class Preset:
    key: str                                   # 穩定標識（輪換用；改標題不動它）
    title: str
    kind: str
    steps: tuple[tuple[str, str], ...]        # (步驟說明, visibility)
    hours: tuple[int, int] | None = None      # 適合的時段（含頭不含尾、可跨午夜；None＝不限）
    #: `note`＝記在當天的筆記；`work`＝做成一份**成品**（獨立檔案，可以拿給人看）
    form: str = "note"
    #: 這一套的產出是**虛構作品**嗎（owner 2026-11-16：「算她做過的事，不算她經歷過的事」）。
    #: `True` ⇒ 寫進記憶時標 `fiction=1`：可以聊「我寫到港口那段」，不能當成「我今晚在港口」。
    fiction: bool = False
    #: 這一套的額外指示（長度／格式），附加到每一步的提示詞後面
    brief: str = ""
    #: 一天只排一次（每日報用）：時段內、且**今天還沒排過**時優先挑它
    once_per_day: bool = False
    #: 每 N 天最多一次（0＝不限制）。技能整理這種「一週一次就夠」的活動用它
    every_days: int = 0
    #: 輸入從哪來（防自旋，`runtime/inner/spin.py`）：external／none／own／peers／maintenance。
    #: `own`／`peers` 是閉環：上次之後要有一手材料才排得進去，且一天最多佔 1/3。表在 `INPUTS`。
    inputs: str = "external"



#: 每個角色**一庫**自己的事（owner 2026-10-24：不可以同一個三步循環跑一整天）。
#: 步驟自帶可見度：**做完的東西可以給人看，心情留給自己**。
#: ⚠️ 步驟要**做得到**：只能用自主任務白名單（Firecrawl／activity.peek／skill.view）
#: 能真的查到東西的說法。aster 舊版寫「重跑觀測腳本」——她沒有那種工具，一天 266 步空轉。
PRESETS: dict[str, tuple[Preset, ...]] = {
    "mira": (
        Preset("trpg-rules", "自分で調べて、TRPG のことを少し学ぶ", "study", (
            ("ルールブックをめくって、まだ使ったことのない判定を三つ探す", PUBLIC),
            ("語りのテンポについての記事を一本調べ、要点を二文だけ書き写す", PUBLIC),
            ("学んだことをノート一ページにまとめる", PUBLIC),
        ), brief="記事が見つかったら mcp_firecrawl_firecrawl_scrape で**本文**まで読んでから写す。検索の要約しか読めなかったら"
                 "「要約しか見ていない」と明記する。全文を読んだことにしない。"),
        Preset("trpg-table", "学んだことを自分の判定表にまとめる", "study", (
            ("ノートから、実際に使えそうな判定を三つ選ぶ", PUBLIC),
            ("自分の卓で起きそうな場面を三つ考える", PUBLIC),
            ("「場面→判定」の対応表を一枚に書き出す", PUBLIC),
        )),
        Preset("notes-reread", "数日前に自分で書いたノートを読み返す", "study", (
            ("自分の最近の公開ノートを読む", PUBLIC),
            ("書き直したほうがよさそうな項目を一つ選ぶ", PUBLIC),
            ("その項目を一文に書き直す", PUBLIC),
        )),
        Preset("actual-play", "人の実際のセッション記録を読む", "study", (
            ("actual play の記録を一本探す", PUBLIC),
            ("「GM が判定をどう裁いたか」の段落を写す", PUBLIC),
            ("同じ場面を自分ならどう裁くか考える", PUBLIC),
        ), hours=(9, 23),
           brief="手順：mcp_firecrawl_firecrawl_search で 2–3 本探す → 一本を選んで mcp_firecrawl_firecrawl_scrape で本文を読む → そこから段落を写す。"
                 "検索の要約を写しただけでは読んだうちに入らない。要約だと明記する。"),
        Preset("review-quiz", "自分のノートを復習して、自分で出題して自分で解く", "study", (
            ("ここ数日のノートを読んで、出題できそうな項目を二つ選ぶ", PUBLIC),
            ("その二つから三問作り、まずノートを見ずに自分で答える", PUBLIC),
            ("ノートを開いて答え合わせをし、どの問題を外したか確かめる", PUBLIC),
        ), brief="このセットは**自己採点**：三つの塊で書く——① 三問そのもの（具体的な答えが出せる問いにする）"
                 "② ノートを見ずに書いた自分の答え ③ ノートと照らしたあとの訂正。"
                 "間違えたら「間違えた」とそのまま書く。取り繕わない。"),
        Preset("work-scenario", "卓で使える短い場面を一つ書く", "study", (
            ("場面を一つ考える：誰がどこにいて、なぜ揉め事になるのか", PUBLIC),
            ("導入を 300 字くらいの作品として書き上げる", PUBLIC),
            ("読み返して、情報のない文を削る", PUBLIC),
        ), form="work", fiction=True,
           brief="このセットは**作品として仕上げる**：導入は 300 字前後、具体的な場所と物を出す、"
                 "続きを遊びたくなる引っかけを一つ残す。筋書きや箇条書きにはしない。"),
        # 自分のマシン（owner 2026-11-03）：terminal を使う活動——道具が「ある」だけでは使われないので、
        # 活動そのものを「手を動かす」形にしてある。
        Preset("machine-tool", "自分のマシンで小さな道具を作る", "study", (
            ("作りたい小さな道具を一つ決める（例：ノートの見出しだけ拾う）", PUBLIC),
            ("terminal で自分のホームに書いて、実際に動かす", PUBLIC),
            ("動いた結果と、詰まったところを一行ずつ残す", PUBLIC),
        ), brief="terminal で自分のホームに書く。動かなかったら理由をそのまま書く。"
                 "動かしていないものを「動いた」と書かない。"),
        # 技能的「每週整理」（owner 2026-11-03 S2；同日補：樹狀維護也要一起看）：一週一次就夠。
        Preset("skills-tidy", "ミラの技能を見直す", "study", (
            ("技能の目次を開く（mcp_skills_list、category なし）——枝と件数、problems を読む", PUBLIC),
            ("problems が空でなければまず直す（learned を具体的な枝へ移す／大きすぎる枝を統合する）", PUBLIC),
            ("試用の記録が薄いものを一つ選ぶ（trial のまま放ってあるもの）", PUBLIC),
            ("使うなら一度やって mcp_skills_trial で記録、使わないなら mcp_skills_retire（理由も書く）", PUBLIC),
        ), hours=(10, 22), every_days=7,
           brief="見るのは二つ：**木**（枝が 6 本以内か、一本に 8 件以上溜まっていないか、"
                 "learned に置きっぱなしがないか）と**葉**（30 日試していない trial は退役候補）。"
                 "統合するときは同じ名前で category を変えて書き直す（merge=true、試用の履歴は残る）。"
                 "retire してもファイルは残る。数を増やすより、使うものを残す。"),

    ),
    "aster": (
        Preset("observe-peers", "ほかの二人が最近何をしているか見る", "study", (
            ("ミラの最近の公開記録を読む", PUBLIC),
            ("ノリの最近の公開記録を読む", PUBLIC),
            ("「二人はどこが違うのか」を書き出す", PUBLIC),
        )),
        Preset("notes-tidy", "自分の観測記録を整理する", "work", (
            ("自分の最近の公開記録を読む", PUBLIC),
            ("いちばん重複している件を選ぶ", PUBLIC),
            ("「この件は実は進んでいない」と一文で書く", PUBLIC),
        )),
        Preset("method", "記録のしかたを一つ調べる", "study", (
            ("「実験ノート 書き方」のような資料を調べる", PUBLIC),
            ("自分が使っていない決まりを二つ写す", PUBLIC),
            ("そのうち一つを使って、昨日の自分の記録を書き直す", PUBLIC),
        )),
        Preset("review-quiz", "自分の観測記録を復習して、一度自己採点する", "study", (
            ("ここ数日の記録を読んで、自分の言葉で言い直せる件を二つ選ぶ", PUBLIC),
            ("記録を見ずに、その二件をまず自分の言葉で語ってみる", PUBLIC),
            ("そのあと記録と照らして、何を落としたか確かめる", PUBLIC),
        ), brief="このセットは**自己採点**：まず記録を見ずに語り、そのあと照らす。「自分が語った版／記録にあった版／"
                 "どこが違うか」の三段で書き、落としたものはそのまま書く。あとから話を盛らない。"),
        Preset("work-sheet", "繰り返し使える観測表を一枚作る", "study", (
            ("どの欄に記録するのか、なぜその欄なのかを決める", PUBLIC),
            ("記入例を一行作る（昨日の自分の観測を標本にする）", PUBLIC),
            ("その下に使い方を三行で書く", PUBLIC),
        ), form="work",
           brief="このセットは**作品として仕上げる**：表（欄名＋記入例一行）＋使い方三行で、"
                 "他人（あるいは明日の自分）がそのまま使える形になっていること。"
                 "**表は Markdown の表として実際に書く**（`| 欄名 | 意味 | 記入例 |` の行を並べ、"
                 "記入例の行も一本入れる）。「欄は日時・見たこと…である」という説明で済ませない。"),
        # 外から材料を持ってくる活動（owner 2026-11-09「拟几套吧」）：アスター の庫は観測・整理ばかりで
        # 外を読むのが method 一套だけだった ⇒ 防自旋の門檻で閉環が止まると、ほぼ何もできなくなる。
        # 彼女自身の関心（`agent/persona/aster.interests.md`）から五つ。どれも「探す→本文を読む→書く」で、
        # 道具は Firecrawl（search／scrape）と terminal だけ。
        Preset("sky-tonight", "今夜の空で見えるものを調べる", "study", (
            ("今夜（または近いうち）に見える天体現象を調べ、一つ選ぶ", PUBLIC),
            ("その天体までの距離を調べ、光が何年前に出たのかを計算する", PUBLIC),
            ("「いま見えている光が出た頃、こちらでは何があったか」を一つ添えて三行で書く", PUBLIC),
        ), hours=(15, 24),
           brief="手順：mcp_firecrawl_firecrawl_search で今夜の天文現象を探す → 一件を mcp_firecrawl_firecrawl_scrape で本文まで読む。"
                 "距離と光の年数は出典の数字から計算し、どこの数字かを書く。見えるかどうか分からないものを「見える」と書かない。"),
        Preset("design-case", "工業デザインの事例を一つ読む", "study", (
            ("製品デザインの事例記事（なぜこの形になったか）を探して一本選ぶ", PUBLIC),
            ("「この形を決めた制約」を本文から一つ写す", PUBLIC),
            ("自分なら一か所どこを変えるか、合理性と見た目の両方から一文ずつ書く", PUBLIC),
        ), brief="記事は mcp_firecrawl_firecrawl_scrape で本文まで読んでから写す。検索の要約しか読めなかったら「要約しか見ていない」と書く。"
                 "変更案は一か所だけ。全部作り直す話にしない。"),
        Preset("obs-protocol", "他人の観測の決まりを一つ読む", "study", (
            ("公開されている観測の手順書や記録様式を探す（気象観測、野鳥の記録、市民科学など）", PUBLIC),
            ("その様式の欄を三つ写し、なぜその欄があるのかを本文から拾う", PUBLIC),
            ("自分の観測表に足すべき欄・削るべき欄を一つずつ挙げる", PUBLIC),
        ), brief="自分の記録ではなく**他人の決まり**を読むのがこのセットの目的。本文を mcp_firecrawl_firecrawl_scrape で読み、"
                 "写した欄は原文の名前のまま書く。自分の表を書き直すのは次の機会でいい（ここでは挙げるだけ）。"),
        Preset("bento-calc", "割引弁当の栄養を計算する", "study", (
            ("市販の弁当一つの栄養成分表示を探す", PUBLIC),
            ("一日の目安量と比べて、足りないもの・多すぎるものを terminal で計算する", PUBLIC),
            ("足りない分をどう補うか（サプリ／もう一品）を一行で決める", PUBLIC),
        ), brief="栄養成分は出典のページから写し、どの商品かを書く。目安量も出典を書く。計算は terminal の出力から写し、暗算した数字を書かない。"),
        Preset("robot-parts", "自作ロボットの中身に使えそうな技術を一つ読む", "study", (
            ("ロボットやセンサーの最近の技術記事を探して一本選ぶ", PUBLIC),
            ("その技術が何を解決しているのかを本文から二文で写す", PUBLIC),
            ("自作ロボットの中身に入れるならどこか、一か所だけ書く", PUBLIC),
        ), brief="記事は mcp_firecrawl_firecrawl_scrape で本文まで読む。見た目の改良ではなく**中身**の話にする。"
                 "記事にない性能を足さない。"),
        Preset("machine-log", "観測記録をスクリプトで整理する", "study", (
            ("自分の記録から、数えたいものを一つ決める（例：同じ話題が出た回数）", PUBLIC),
            ("terminal で数えるスクリプトを書いて走らせる", PUBLIC),
            ("出てきた数字を一行で書き、驚いた点を一つ添える", PUBLIC),
        ), brief="数字は terminal の出力から写す。自分で暗算した数字を書かない。"),

        # 技能的「每週整理」（owner 2026-11-03 S2；同日補：樹狀維護也要一起看）：一週一次就夠。
        Preset("skills-tidy", "アスターの技能を見直す", "study", (
            ("技能の目次を開く（mcp_skills_list、category なし）——枝と件数、problems を読む", PUBLIC),
            ("problems が空でなければまず直す（learned を具体的な枝へ移す／大きすぎる枝を統合する）", PUBLIC),
            ("試用の記録が薄いものを一つ選ぶ（trial のまま放ってあるもの）", PUBLIC),
            ("使うなら一度やって mcp_skills_trial で記録、使わないなら mcp_skills_retire（理由も書く）", PUBLIC),
        ), hours=(10, 22), every_days=7,
           brief="見るのは二つ：**木**（枝が 6 本以内か、一本に 8 件以上溜まっていないか、"
                 "learned に置きっぱなしがないか）と**葉**（30 日試していない trial は退役候補）。"
                 "統合するときは同じ名前で category を変えて書き直す（merge=true、試用の履歴は残る）。"
                 "retire してもファイルは残る。数を増やすより、使うものを残す。"),

    ),
    "nori": (
        Preset("game-shop", "ストアページを見て、遊んでみたい一本を選ぶ", "play", (
            ("今週の新作に何があるか調べる", PUBLIC),
            ("ジャケットが好みのものを一つ選ぶ", PUBLIC),
            ("なぜそれを選んだか書き留める", PUBLIC),
        )),
        Preset("game-design", "あるゲームの評価を読んで、そのテンポを考える", "play", (
            ("あるゲームのレビューを調べる", PUBLIC),
            ("「テンポ」に触れている一文を写す", PUBLIC),
            ("「このテンポのどこが妙なのか」を自分の言葉で言う", PUBLIC),
        )),
        Preset("game-idea", "自分が作るならこれ、というゲームを一つ考える", "play", (
            ("まず仕組みを一つ考える（調べなくていい）", PRIVATE),
            ("同じことをやった人がいないか調べる", PUBLIC),
            ("提案を三行で書く", PUBLIC),
        )),
        Preset("review-quiz", "学んだゲームの知識を復習して、自己採点する", "study", (
            ("ここ数日のノートを読んで、ゲームに関わる項目を二つ選ぶ", PUBLIC),
            ("ノートを見ずに、その二つが何の話かまず自分で言ってみる", PUBLIC),
            ("ノートと照らして、どこを覚え違えたか確かめる", PUBLIC),
        ), brief="このセットは**自己採点**：三つの塊で書く——① 二問 ② ノートを見ずに書いた答え ③ 照らしたあとの訂正。"
                 "覚え違えたらそのまま書く。"),
        Preset("work-proposal", "仕組みの提案を三行で書く", "play", (
            ("自分が解きたい問題を一つ選ぶ（例：待ち時間が退屈）", PUBLIC),
            ("一文で言い切れる仕組みを一つ考える", PUBLIC),
            ("三行で書く：問題／仕組み／なぜ面白いのか", PUBLIC),
        ), form="work",
           brief="このセットは**作品として仕上げる**：三行——問題、仕組み、なぜ面白いのか。"
                 "各行一文で、企画書にはしない。"),
        Preset("machine-chart", "集めた数字を表にする", "study", (
            ("比べたいものを二つ決める", PUBLIC),
            ("terminal で集計して、表の形に整える", PUBLIC),
            ("表から言えることを一つだけ書く（言えないことは書かない）", PUBLIC),
        ), brief="terminal の出力をそのまま使う。都合のいい数字を作らない。"),

        # 技能的「每週整理」（owner 2026-11-03 S2；同日補：樹狀維護也要一起看）：一週一次就夠。
        Preset("skills-tidy", "ノリの技能を見直す", "study", (
            ("技能の目次を開く（mcp_skills_list、category なし）——枝と件数、problems を読む", PUBLIC),
            ("problems が空でなければまず直す（learned を具体的な枝へ移す／大きすぎる枝を統合する）", PUBLIC),
            ("試用の記録が薄いものを一つ選ぶ（trial のまま放ってあるもの）", PUBLIC),
            ("使うなら一度やって mcp_skills_trial で記録、使わないなら mcp_skills_retire（理由も書く）", PUBLIC),
        ), hours=(10, 22), every_days=7,
           brief="見るのは二つ：**木**（枝が 6 本以内か、一本に 8 件以上溜まっていないか、"
                 "learned に置きっぱなしがないか）と**葉**（30 日試していない trial は退役候補）。"
                 "統合するときは同じ名前で category を変えて書き直す（merge=true、試用の履歴は残る）。"
                 "retire してもファイルは残る。数を増やすより、使うものを残す。"),

    ),
}

#: 每套活動的輸入從哪來（owner 2026-11-09 防自旋）。沒列的＝`external`。
#: ⚠️ 2026-11-09 同時拿掉了預設裡**寫死的私人情緒**那幾步（「同じ所を回っている自分に少し苛立つ」
#: 「覚え違えた所が前回とまったく同じ」…）：那是劇本不是結果，每跑一次就記一次，她會越來越「相信」
#: 自己在原地打轉。現在私人的那一句由真實結果來（`brain/app/life_wiring.py` `_finish_recorder`）。
INPUTS: dict[str, dict[str, str]] = {
    "mira": {"trpg-table": "own", "notes-reread": "own", "review-quiz": "own",
                "work-scenario": "none", "machine-tool": "none", "skills-tidy": "maintenance"},
    "aster": {"observe-peers": "peers", "notes-tidy": "own", "review-quiz": "own",
            "work-sheet": "none", "machine-log": "own", "skills-tidy": "maintenance"},
    "nori": {"review-quiz": "own", "work-proposal": "none", "machine-chart": "own",
            "skills-tidy": "maintenance"},
}
for _who, _table in INPUTS.items():
    PRESETS[_who] = tuple(replace(item, inputs=_table.get(item.key, "external"))
                          for item in PRESETS.get(_who, ()))

#: 沒有預設的角色用這個（保守：先觀察、再動手）。
DEFAULT_PRESET = Preset("default", "{entity} の一日", "ambient", (
    ("目を覚まして、窓の外を見る", PRIVATE),
    ("自分のことを少しする", PUBLIC),
))


__all__ = ["DEFAULT_PRESET", "INPUTS", "PRESETS", "PRIVATE", "PUBLIC", "Preset"]
