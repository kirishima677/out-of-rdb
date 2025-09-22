package main

import (
	"fmt"

	consul "github.com/hashicorp/consul/api"
)

func main() {
	cfg := consul.DefaultConfig()
	cfg.Address = "http://consul:8500"
	client, err := consul.NewClient(cfg)
	if err != nil {
		panic(err)
	}

	kv := client.KV()
	p := &consul.KVPair{Key: "hello", Value: []byte("world")}
	if _, err := kv.Put(p, nil); err != nil {
		panic(err)
	}
	pair, _, err := kv.Get("hello", nil)
	if err != nil {
		panic(err)
	}
	fmt.Printf("%s=%s\n", pair.Key, string(pair.Value))
}
