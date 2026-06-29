package main

import (
	"context"
	"fmt"
	"log"
	"os"

	"github.com/ClickHouse/clickhouse-go/v2"
)

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	ctx := context.Background()

	host := getenv("CLICKHOUSE_HOST", "localhost")
	port := getenv("CLICKHOUSE_PORT", "9000")
	user := getenv("CLICKHOUSE_USER", "default")
	pass := getenv("CLICKHOUSE_PASSWORD", "")
	db := getenv("CLICKHOUSE_DATABASE", "default")

	conn, err := clickhouse.Open(&clickhouse.Options{
		Addr: []string{fmt.Sprintf("%s:%s", host, port)},
		Auth: clickhouse.Auth{
			Database: db,
			Username: user,
			Password: pass,
		},
	})
	if err != nil {
		log.Fatal(err)
	}

	if err := conn.Ping(ctx); err != nil {
		log.Fatal(err)
	}

	fmt.Printf("Connected to ClickHouse: %s:%s\n", host, port)

	conn.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS users (
			id UInt32,
			name String,
			age UInt8
		)
		ENGINE = MergeTree
		ORDER BY id
	`)

	conn.Exec(ctx, "TRUNCATE TABLE users")

	batch, err := conn.PrepareBatch(ctx, "INSERT INTO users")
	if err != nil {
		log.Fatal(err)
	}

	batch.Append(uint32(1), "Alice", uint8(25))
	batch.Append(uint32(2), "Bob", uint8(31))
	batch.Append(uint32(3), "Charlie", uint8(28))

	if err := batch.Send(); err != nil {
		log.Fatal(err)
	}

	fmt.Println("\nUsers")
	fmt.Println("-----")

	rows, err := conn.Query(ctx, "SELECT id, name, age FROM users ORDER BY id")
	if err != nil {
		log.Fatal(err)
	}
	defer rows.Close()

	for rows.Next() {
		var id uint32
		var name string
		var age uint8
		if err := rows.Scan(&id, &name, &age); err != nil {
			log.Fatal(err)
		}
		fmt.Printf("(%d, %s, %d)\n", id, name, age)
	}

	var count uint64
	if err := conn.QueryRow(ctx, "SELECT count() FROM users").Scan(&count); err != nil {
		log.Fatal(err)
	}

	fmt.Printf("\nTotal users: %d\n", count)
}
