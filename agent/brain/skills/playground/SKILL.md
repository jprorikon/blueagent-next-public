---
name: playground
description: 遊び場で読み書きしたいときに使う（自分だけの場所、またはみんなの共有；道具 playground）
scope: chat
requires: [tool:playground]
owner_only: false
category: self
---

# 遊び場（chat 技能）

## いつ使う

- 相手が「遊び場に書いて」「共有のところに何がある？」と言ったとき
- あとの自分や他の子に向けて何か残しておきたいとき

## 手順

1. `playground`：まず `action=list` で何があるか見る。`read` で一つ読み、`write` で書く。
2. `scope=own` は自分だけが見える。`scope=shared` はみんなの共有（名前と時刻が自動で付く）。

## 守ること

- shared に書いたものはみんなに見える：主人の私的なことは書かない。
- 読めなければ読めないと言う。中身を作らない。
