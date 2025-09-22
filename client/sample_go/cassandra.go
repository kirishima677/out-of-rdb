package main

import (
	"fmt"

	"github.com/gocql/gocql"
)

func main() {
	cluster := gocql.NewCluster("cassandra")
	cluster.Keyspace = "system"
	session, err := cluster.CreateSession()
	if err != nil {
		panic(err)
	}
	defer session.Close()

	var clusterName string
	if err := session.Query("SELECT cluster_name FROM local").Scan(&clusterName); err != nil {
		panic(err)
	}
	fmt.Println("Cassandra cluster:", clusterName)
}
