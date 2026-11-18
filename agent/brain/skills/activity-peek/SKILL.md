---
name: activity-peek
description: 誰か（または自分）が最近何をしているか聞かれたときに使う（公開の行動記録；道具 activity.peek）
scope: chat
requires: [tool:activity.peek]
owner_only: false
category: self
---

# 最近の行動を調べる（chat 技能）

## いつ使う

主人や客人に「アスターは最近何してる？」「ここ数日何をしてた？」のように**近況**を聞かれたとき。

## 手順

1. `activity.peek` を呼ぶ。`entity` は id を一つだけ（mira／aster／nori）。自分のことなら自分の id。
2. 返ってきた記録で近況を話す。口調はいつもの雑談のまま。

## 守ること

- **聞かれたときだけ使う**。自分から報告するためには使わない。
- 調べられるのは公開の記録だけ。private のことは分からないし、推測もしない。
- 見つからなければ「分からない」と言う。
