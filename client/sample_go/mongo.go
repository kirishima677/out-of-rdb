package main

import (
	"context"
	"fmt"
	"time"

	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	// Add authSource=admin to match root user created by init
	uri := "mongodb://root:example@mongodb:27017/?authSource=admin"
	client, err := mongo.Connect(ctx, options.Client().ApplyURI(uri))
	if err != nil {
		panic(err)
	}
	defer client.Disconnect(context.Background())

	// Retry ping for a short window to allow service readiness
	var lastErr error
	for i := 0; i < 10; i++ {
		pingCtx, cancelPing := context.WithTimeout(context.Background(), 2*time.Second)
		lastErr = client.Ping(pingCtx, nil)
		cancelPing()
		if lastErr == nil {
			fmt.Println("MongoDB ping OK")
			return
		}
		time.Sleep(1 * time.Second)
	}
	panic(lastErr)
}
