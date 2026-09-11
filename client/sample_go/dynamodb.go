package main

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
)

const tableName = "users"

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	fmt.Println("Connecting to DynamoDB Local at http://dynamodb:8000...")
	cfg, err := config.LoadDefaultConfig(
		ctx,
		config.WithRegion("us-east-1"),
		config.WithCredentialsProvider(credentials.NewStaticCredentialsProvider("local", "local", "")),
		config.WithBaseEndpoint("http://dynamodb:8000"),
	)
	if err != nil {
		panic(err)
	}

	client := dynamodb.NewFromConfig(cfg)
	fmt.Printf("Creating or reusing table: %s...\n", tableName)
	_, err = client.DescribeTable(ctx, &dynamodb.DescribeTableInput{TableName: aws.String(tableName)})
	if err == nil {
		fmt.Println("Using existing table.")
	} else {
		var notFound *types.ResourceNotFoundException
		if !errors.As(err, &notFound) {
			panic(fmt.Errorf("describe table: %w", err))
		}

		_, err = client.CreateTable(ctx, &dynamodb.CreateTableInput{
			TableName: aws.String(tableName),
			AttributeDefinitions: []types.AttributeDefinition{{
				AttributeName: aws.String("user_id"),
				AttributeType: types.ScalarAttributeTypeS,
			}},
			KeySchema: []types.KeySchemaElement{{
				AttributeName: aws.String("user_id"),
				KeyType:       types.KeyTypeHash,
			}},
			BillingMode: types.BillingModePayPerRequest,
		})
		if err != nil {
			panic(fmt.Errorf("create table: %w", err))
		}
		fmt.Println("Created table.")
	}

	fmt.Println("Writing item...")
	_, err = client.PutItem(ctx, &dynamodb.PutItemInput{
		TableName: aws.String(tableName),
		Item: map[string]types.AttributeValue{
			"user_id": &types.AttributeValueMemberS{Value: "alice"},
			"name":    &types.AttributeValueMemberS{Value: "Alice"},
			"age":     &types.AttributeValueMemberN{Value: "30"},
		},
	})
	if err != nil {
		panic(fmt.Errorf("put item: %w", err))
	}

	fmt.Println("Reading item...")
	result, err := client.GetItem(ctx, &dynamodb.GetItemInput{
		TableName: aws.String(tableName),
		Key: map[string]types.AttributeValue{
			"user_id": &types.AttributeValueMemberS{Value: "alice"},
		},
	})
	if err != nil {
		panic(fmt.Errorf("get item: %w", err))
	}
	userID := result.Item["user_id"].(*types.AttributeValueMemberS).Value
	name := result.Item["name"].(*types.AttributeValueMemberS).Value
	age := result.Item["age"].(*types.AttributeValueMemberN).Value
	fmt.Printf("Get item: user_id=%s, name=%s, age=%s\n", userID, name, age)
}
