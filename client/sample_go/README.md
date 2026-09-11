### Go サンプルの実行

- **前提**: `docker compose up -d` 済み、`docker exec -it client sh` で client に入る

1) 依存解決（初回のみ）
```bash
go version   # go version go1.23.1 linux/amd64 の想定
cd /workspace/sample_go
go mod tidy
```

2) 各サンプルを実行
```bash
# MongoDB
go run mongo.go

# Redis
go run redis.go

# DynamoDB Local
go run dynamodb.go

# Cassandra
go run cassandra.go

# CockroachDB
go run cockroach.go

# etcd
go run etcd.go

# ZooKeeper
go run zookeeper.go

# Consul
go run consul.go
```
