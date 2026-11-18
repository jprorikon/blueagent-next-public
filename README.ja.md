# BlueAgent Next — Public Demo

**Language**: [English](README.md) | 日本語 | [中文](README.zh.md)

エンティティファースト（Entity-First）なマルチキャラクター AI ランタイムの公開デモです。イベント取り込み、ルーティング、記憶開示制御、ペルソナ組み立てから Web / App クライアント配信まで、障害復旧とテストが可能な一連のパイプラインを示します。

> 本リポジトリは転職・ポートフォリオ向けに整理した公開版です。デフォルトでオフライン Mock モードで動作し、実ユーザーデータ、アクセストークン、内部サービスアドレス、開発用署名鍵、第三者キャラクター素材は一切含まれていません。

## 主な特徴

- **明示的なセッション管理が不要**：`(scope, entity, scene, purpose)` からスレッドを自動導出し、コンテキストを自動スライシングします。
- **マルチエンティティ＆マルチアイデンティティ**：記憶、関係性、スタンス、音声、注意状態はすべて `CharacterEntity` に帰属します。
- **データ境界での記憶開示制御**：現在その場にいる参加者がすべて記憶の `audience` に含まれる場合のみ記憶を想起します（プロンプト任せにしないデータ境界認可）。
- **差し替え可能なモデル層**：デフォルトのオフライン Mock のほか、任意の OpenAI 互換エンドポイントに接続可能です。
- **永続化とリカバリ**：スレッド、ファクト、エピソード、アクティビティ、リース、Outbox をディスクに永続化し、SSE 切断時のカーソル再接続に対応しています。
- **マルチクライアント配信**：Owner 用 Web デバッグコンソール、独立 App Server、Unity モバイルクライアント、Quest 3 MR プロジェクトを含みます。
- **エンジニアリングガードレール**：レイヤー依存、API 契約、権限制御、記憶隔離、ストリーミング配信、永続化を検証する自動テスト群を整備しています。

## クイックスタート

Python 3.11 以上が必要です。コアの Mock デモは Python 標準ライブラリのみで動作します。

```bash
cd blueagent-next-public
BLUEAGENT_BRAIN=mock python3 agent/run.py
```

初回起動時にローカルで Owner トークンが生成されます。ターミナルに表示される Owner URL にアクセスしてください（トークンはローカル実行ディレクトリのみに保存され、Git には含まれません）。

```text
http://127.0.0.1:8787/
```

## オフラインテスト

```bash
python3 agent/tests/run_all.py
```

音声関連テストの一部には追加ライブラリが必要ですが、コアデモは外部モデルやネットワークなしで動作します。

## 独自モデルへの接続

サンプル設定ファイルをリポジトリ外へコピーするか、`.env` をローカルのみに保持した上で明示的に指定してください。

```bash
cp .env.example .env
# .env を編集し、OpenAI 互換エンドポイントと API キーを設定
BLUEAGENT_ENV_FILE=.env python3 agent/run.py
```

`.env`、実トークン、実行データは絶対にコミットしないでください。

## Docker Compose での起動

App Server はアカウント初期化をバイパスしません。先にローカルで Agent を起動して `data/accounts.json` を生成し、その後 App Server 用の架空ローカルアカウントとエンティティトークンを発行します。

```bash
# 初回起動後 Ctrl+C で停止（ローカルに data/accounts.json が生成されます）
BLUEAGENT_BRAIN=mock python3 agent/run.py

python3 agent/tools/provision_app_server.py \
  --agent-accounts data/accounts.json \
  --app-accounts app-data/accounts.json \
  --secret-file app-secrets/agent.env

BLUEAGENT_BRAIN=mock docker compose up --build
```

`data/`、`app-data/`、`app-secrets/` はすべて `.gitignore` 対象です。Compose はデフォルトで Agent と App Server を起動します（gateway は `gateway` profile を明示的に有効化した場合のみ起動し、プライベートインフラには接続しません）。

## ディレクトリ構成

```text
agent/
  brain/        ドメインモデル、ルーティング、記憶、ペルソナ、ランタイム
  adapter/      Discord、TRPG、App、音声などの外部アダプター
  server/       Agent HTTP / SSE および管理 API
  client/       Owner 用 Web デバッグコンソール
  persona/      オリジナル架空キャラクターと共通ペルソナルール
  tests/        Agent オフライン受け入れテスト
app/
  server/       独立アカウント、セッション、パブリックエリア、メディアサービス
  client/       Unity クライアント、共有 C# ロジック、自動生成ジオメトリ素材
  tests/        App Server およびクライアント契約テスト
mr/             Quest 3 用 MR プロジェクト
docs/           アーキテクチャ、API 契約、用語集、生成ドキュメント
```

## 公開版の境界仕様

- デフォルトの3キャラクター **Mira、Aster、Nori** および追加サンプル **Lumi** はオリジナルの架空キャラクターです。
- 立ち絵、アイコン、背景、スタンプはすべてプログラムで生成された抽象ジオメトリックグラフィックです。
- 第三者のキャラクターモデルは含まれていません（3D ロード機構は残しつつ、未配置時は 2D にフォールバックします）。
- ビルド済みネイティブライブラリは含まれていません。モバイルビルド時は各依存環境をローカルでご用意ください。
- Android ビルドには開発者ローカルの署名鍵を使用するため、本リポジトリに keystore は含まれません。
- プロジェクト全体のオープンソースライセンスは設定していません。公開閲覧は再配布権の付与を意味しません。サードパーティ製コンポーネントのライセンスは `THIRD_PARTY_NOTICES.md` をご参照ください。

## 設計ドキュメント

- `DESIGN.md`：アーキテクチャおよびセキュリティ境界サマリー
- `docs/APP-API-SSE.md`：App / Agent 間の API・SSE 契約仕様
- `docs/accounts-and-clients.md`：アカウントおよびクライアント認証モデル
- `docs/FILE-TREE.md`：ファイル構成一覧
- `docs/ENVIRONMENT.md`：環境変数リファレンス
