package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"strconv"

	"github.com/neo4j/neo4j-go-driver/v5/neo4j"
)

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	ctx := context.Background()

	host := getenv("NEO4J_HOST", "localhost")
	port := getenv("NEO4J_PORT", "7687")
	user := getenv("NEO4J_USER", "neo4j")
	pass := getenv("NEO4J_PASSWORD", "password")

	uri := fmt.Sprintf("bolt://%s:%s", host, port)

	driver, err := neo4j.NewDriverWithContext(uri, neo4j.BasicAuth(user, pass, ""))
	if err != nil {
		log.Fatal(err)
	}
	defer driver.Close(ctx)

	if err := driver.VerifyConnectivity(ctx); err != nil {
		log.Fatal(err)
	}

	fmt.Printf("Connected to Neo4j: %s\n", uri)

	session := driver.NewSession(ctx, neo4j.SessionConfig{})
	defer session.Close(ctx)

	_, err = session.ExecuteWrite(ctx, func(tx neo4j.ManagedTransaction) (any, error) {
		_, err := tx.Run(ctx, "MATCH (n) DETACH DELETE n", nil)
		return nil, err
	})
	if err != nil {
		log.Fatal(err)
	}

	_, err = session.ExecuteWrite(ctx, func(tx neo4j.ManagedTransaction) (any, error) {
		_, err := tx.Run(ctx, `
			CREATE
			  (alice:Person {name:'Alice', age:25}),
			  (bob:Person {name:'Bob', age:31}),
			  (charlie:Person {name:'Charlie', age:28}),
			  (alice)-[:FRIEND]->(bob),
			  (bob)-[:FRIEND]->(charlie),
			  (alice)-[:FRIEND]->(charlie)
		`, nil)
		return nil, err
	})
	if err != nil {
		log.Fatal(err)
	}

	fmt.Println("\nFriend relationships")
	fmt.Println("--------------------")

	_, err = session.ExecuteRead(ctx, func(tx neo4j.ManagedTransaction) (any, error) {
		result, err := tx.Run(ctx, `
			MATCH (a:Person)-[:FRIEND]->(b:Person)
			RETURN a.name AS from_person, b.name AS to_person
			ORDER BY from_person, to_person
		`, nil)
		if err != nil {
			return nil, err
		}

		for result.Next(ctx) {
			record := result.Record()
			fromPerson, _ := record.Get("from_person")
			toPerson, _ := record.Get("to_person")
			fmt.Printf("%s -> %s\n", fromPerson, toPerson)
		}

		return nil, result.Err()
	})
	if err != nil {
		log.Fatal(err)
	}

	total, err := session.ExecuteRead(ctx, func(tx neo4j.ManagedTransaction) (any, error) {
		result, err := tx.Run(ctx, "MATCH (p:Person) RETURN count(p) AS total", nil)
		if err != nil {
			return nil, err
		}

		if result.Next(ctx) {
			record := result.Record()
			value, _ := record.Get("total")
			return value, nil
		}

		return int64(0), result.Err()
	})
	if err != nil {
		log.Fatal(err)
	}

	count, err := strconv.ParseInt(fmt.Sprint(total), 10, 64)
	if err != nil {
		log.Fatal(err)
	}

	fmt.Printf("\nTotal persons: %d\n", count)
}
