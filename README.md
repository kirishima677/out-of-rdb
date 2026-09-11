
### out-of-rdb-lab

## Supported Databases

| Category | Database | Python | Go |
|----------|----------|:------:|:--:|
| Document | MongoDB | ✅ | ✅ |
| Key-Value / Document | DynamoDB Local | ✅ | ✅ |
| Key-Value | Redis | ✅ | ✅ |
| Wide Column | Cassandra | ✅ | ✅ |
| Column-Oriented | ClickHouse | ✅ | ✅ |
| Graph | Neo4j | ✅ | ✅ |
| NewSQL | CockroachDB | ✅ | ✅ |
| Distributed KV | etcd | ✅ | ✅ |
| Service Discovery | Consul | ✅ | ✅ |
| Coordination | ZooKeeper | ✅ | ✅ |

**Features**

- Docker Compose によるワンコマンド起動
- Python / Go の接続サンプルを同梱
- 同一データモデルを複数のデータベースで比較可能
- Docker ネットワーク内・ホスト環境の両方から接続可能
- データベース学習・検証・比較を目的とした実験環境

---

複数の NoSQL / NewSQL を Docker Compose で立ち上げ、1つの `client` コンテナから接続検証できる実験用リポジトリです。Python / Go の接続サンプルを同梱しています。

---

### 構成
- 共有ネットワーク: `labnet`
- クライアント: `client`（Python3, Go 1.23, ビルド環境・CA 証明書入り）
- ミドルウェア（独立コンテナ）
  - MongoDB (`mongodb:27017`, 認証: root/example)
  - DynamoDB Local (`dynamodb:8000`, ホスト: `localhost:8000`)
  - Redis（コンテナ: `redis:6379` / ホスト: `localhost:6381`）
  - Cassandra (`cassandra:9042`)
  - ClickHouse (`clickhouse:9000` Native, `clickhouse:8123` HTTP)
  - Neo4j (`neo4j:7687` Bolt, Browser: `localhost:7474`)
  - CockroachDB (`cockroachdb:26259` コンテナ内、ホスト 26258→26259 マッピング, Admin UI: host 8081)
  - etcd (`etcd:2379`)
  - ZooKeeper (`zookeeper:2181`)
  - Consul (`consul:8500` HTTP/UI, `8600/udp` DNS)

> 注意: CockroachDB は開発用途の `--insecure` 構成です。ホストからは `localhost:26258` で接続してください。

---

### 使い方（起動）
```bash
cd /Users/nishiokahiroki/code/out-of-rdb
# 初回や更新時
docker compose build client
# 起動
docker compose up -d
# 状態確認
docker compose ps
```

### client に入る
```bash
docker exec -it client sh
```

### Python（仮想環境 + 依存）
```bash
python3 -m venv /workspace/.venv
. /workspace/.venv/bin/activate
pip install -r /workspace/requirements.txt
```

- Python サンプル: `/workspace/sample`
  - 例: `python /workspace/sample/mongo_db_sample.py`
  - 例: `python /workspace/sample/cassandra_sample.py`
  - 例: `python /workspace/sample/consul_sample.py`
  - 例: `python /workspace/sample/clickhouse_sample.py`
  - 例: `python /workspace/sample/neo4j_sample.py`
  - 例: `python /workspace/sample/dynamodb_sample.py`

### Go（モジュール準備）
```bash
cd /workspace/sample_go
go mod tidy
```
- Go サンプル: `/workspace/sample_go`
  - 例: `go run mongo.go`
  - 例: `go run redis.go`
  - 例: `go run cassandra.go`
  - 例: `go run cockroach.go`
  - 例: `go run etcd.go`
  - 例: `go run zookeeper.go`
  - 例: `go run consul.go`
  - 例: `go run clickhouse.go`
  - 例: `go run neo4j.go`
  - 例: `go run dynamodb.go`

---

### 主な接続情報（client からの接続先）
- MongoDB: `mongodb:27017`（URI 例: `mongodb://root:example@mongodb:27017/?authSource=admin`）
- DynamoDB Local: `http://dynamodb:8000`（ホストからは `http://localhost:8000`、AWS認証情報は任意のダミー値で可）
- Redis: `redis:6379`（ホストからは `localhost:6381`）
- Cassandra: `cassandra:9042`
- ClickHouse: Native `clickhouse:9000` / HTTP `http://clickhouse:8123`
- Neo4j: Bolt `neo4j:7687`（Browser: `http://localhost:7474`）
- CockroachDB: `cockroachdb:26259`（ホストからは `localhost:26258`）
- etcd: `http://etcd:2379`
- ZooKeeper: `zookeeper:2181`
- Consul: `http://consul:8500`

---

### よくあるトラブルと対処
- ポート衝突（ホスト）: 既にローカルで使っている場合は Compose の `ports` を変更するか、公開を外し `client` から内部名で接続してください。
- Python の PEP 668: システム Python は触らず、仮想環境（venv）にインストールしてください。
- Cassandra Python ドライバ: `python3-dev`, `libev-dev` などが必要で、イメージに同梱済み。ビルドし直した場合は venv 内で `cassandra-driver` を入れ直してください。
- Go モジュール解決: `go mod tidy` 実行。CA 証明書・`GOPROXY` は client で設定済み。

---

### フォルダ構成
- `docker-compose.yaml`: 全サービスの起動定義
- `client/docker/Dockerfile`: client イメージのビルド定義
- `client/requirements.txt`: Python 依存
- `client/Readme.md`: client での詳細な使い方
- `client/sample`: Python サンプル
- `client/sample_go`: Go サンプル（`go.mod`/`go.sum`）

---

### ライセンス
実験用途を想定。商用利用・セキュリティ要件に合わせる場合は各ミドルウェアの設定を強化してください。
