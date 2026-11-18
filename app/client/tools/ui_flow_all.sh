#!/usr/bin/env bash
# **全部頁面跑一遍**的自驅流程（owner 2026-10-14：「這次優先把 UI 全部跑通」）。
#
# 這一支只做一件事：把「每一屏 × 每一顆按鈕」串成一條流程丟給 `ui_flow.sh`
#（＝裝一次包、約 30–40 秒跑完；判據全在 logcat，不看畫面）。
#
# 兩段：
#   ① 主線（每一步都有期待值 ⇒ 任何一條斷了當場紅）
#   ② 收尾（把畫面收回主頁，免得下一次接手的人看到一堆疊層開著）
#
# 用法：bash tools/ui_flow_all.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

# ⚠️ 節點名是**看出來的**（`ShellRouter` 會把沒接線的節點名＋祖先鏈印出來），不是猜的。
#   索引規則：選單第一層 `Tile0_history`／`Tile1_newSession`／`Tile2_games`／`Tile3_settings`；
#   第二層 `Tile0_game`（五子棋）／`Tile1_trpg`；對話清單 `Row<i>`（i＝投影 `rows` 的索引）。
STEPS="\
NavButton;\
MenuDim;\
NavButton;Tile3_settings;\
Tab_character;\
Card_character_aster;\
Tab_background;\
Card_background_school;\
Tab_audio;\
Row_outputVolume;\
Row_inputLanguage;\
Tab_language;\
Row_uiLocale;\
SettingsClose;\
NavButton;Tile0_history;\
SortLabel;\
Row2;\
ChatBack;\
TalkClose;\
NavButton;Tile0_history;\
TalkHelp;\
Close;\
NavButton;Tile2_games;\
MenuBack;\
NavButton;Tile1_newSession;\
NavButton;Tile2_games;Tile0_game;\
GameClose;\
No;\
End;\
No;\
End;\
Yes;\
NavButton;Tile2_games;Tile1_trpg;\
MenuBack;\
MenuBack;\
MenuDim"

bash "$HERE/ui_flow.sh" "$STEPS" \
  --expect "疊層：menu" \
  --expect "疊層：settings" \
  --expect "設定分頁 ⇒ character" \
  --expect "角色 ⇒ aster" \
  --expect "背景 ⇒ school" \
  --expect "滑桿 outputVolume" \
  --expect "下拉 inputLanguage" \
  --expect "介面語言" \
  --expect "疊層：talk" \
  --expect "對話排序" \
  --expect "開對話 ⇒ " \
  --expect "對話紀錄 ⇒ 回清單" \
  --expect "疊層：hand" \
  --expect "換屏：game" \
  --expect "疊層：confirm" \
  --expect "結束對局" \
  --expect "第三層（選桌）" \
  --expect "疊層：關"
