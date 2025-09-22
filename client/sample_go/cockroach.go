package main

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5"
)

func main() {
	ctx := context.Background()
	// CockroachDB is running insecure in compose
	conn, err := pgx.Connect(ctx, "postgresql://root@cockroachdb:26259/defaultdb?sslmode=disable")
	if err != nil {
		panic(err)
	}
	defer conn.Close(ctx)

	var now string
	if err := conn.QueryRow(ctx, "select now()::string").Scan(&now); err != nil {
		panic(err)
	}
	fmt.Println("Cockroach now:", now)
}
