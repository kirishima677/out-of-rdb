package main

import (
	"fmt"
	"time"

	"github.com/go-zookeeper/zk"
)

func main() {
	conn, _, err := zk.Connect([]string{"zookeeper:2181"}, 5*time.Second)
	if err != nil {
		panic(err)
	}
	defer conn.Close()

	path := "/hello"
	_, err = conn.Create(path, []byte("world"), 0, zk.WorldACL(zk.PermAll))
	if err != nil && err != zk.ErrNodeExists {
		panic(err)
	}
	data, _, err := conn.Get(path)
	if err != nil {
		panic(err)
	}
	fmt.Printf("%s=%s\n", path, string(data))
}
