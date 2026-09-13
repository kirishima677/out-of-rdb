### 使い方

- **前提**: ルートで実行 `/Users/nishiokahiroki/code/out-of-rdb`

1) **ビルドと起動**
```bash
docker compose build client
docker compose up -d
```

2) **client コンテナへ入る**
```bash
docker exec -it client sh
```

3) **Python 仮想環境を作成し、依存をインストール**
```bash
python3 -m venv /workspace/.venv
. /workspace/.venv/bin/activate
pip install -r /workspace/requirements.txt
```

4) **動作確認**
```bash
python3 --version
pip --version
go version
```

5) **サンプル実行**
```bash
# MongoDB
python /workspace/sample/mongo_db_sample.py

# Redis
python /workspace/sample/redis_connect_sample.py

# DynamoDB Local
python /workspace/sample/dynamodb_sample.py

# DynamoDB Local: メール重複検知の履歴を表示
python /workspace/sample/show_mail_duplicate_events.py

# 指定した EmailID の履歴だけを表示
python /workspace/sample/show_mail_duplicate_events.py --email-id 900001

# Cassandra
python /workspace/sample/cassandra_sample.py

# CockroachDB (psycopg2)
python /workspace/sample/cockroach_db_sample.py

# etcd
python /workspace/sample/etcd_sample.py

# ZooKeeper (kazoo)
python /workspace/sample/zoo_keeper_sample.py

# Consul
python /workspace/sample/consul_sample.py
```

- **Go のサンプル**（例: `/workspace/sample/main.go` がある場合）
```bash
cd /workspace/sample
go run main.go
```

6) **終了/クリーンアップ**
```bash
docker compose down
# ボリュームも削除する場合
# docker compose down -v
```

- 補足:
  - Python は PEP 668 回避のため仮想環境でライブラリを導入します。
  - 追加ライブラリは `requirements.txt` に追記し、再インストールしてください。
