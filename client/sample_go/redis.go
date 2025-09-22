package main

import (
	"context"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

func main() {
	ctx := context.Background()
	client := redis.NewClient(&redis.Options{Addr: "redis:6379"})
	defer client.Close()

	if err := client.Set(ctx, "hello", "world", time.Minute).Err(); err != nil {
		panic(err)
	}
	val, err := client.Get(ctx, "hello").Result()
	if err != nil {
		panic(err)
	}
	fmt.Println("Redis value:", val)
}
