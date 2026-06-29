

"""Simple Neo4j sample.

Requires:
    pip install neo4j

Run from host:
    python3 client/sample/neo4j_sample.py

Run from the client container:
    NEO4J_HOST=neo4j python /workspace/sample/neo4j_sample.py
"""

import os
from neo4j import GraphDatabase

host = os.getenv("NEO4J_HOST", "localhost")
port = int(os.getenv("NEO4J_PORT", "7687"))
user = os.getenv("NEO4J_USER", "neo4j")
password = os.getenv("NEO4J_PASSWORD", "password")

uri = f"bolt://{host}:{port}"

driver = GraphDatabase.driver(uri, auth=(user, password))

with driver.session() as session:
    print(f"Connected to Neo4j: {uri}")

    session.run("MATCH (n) DETACH DELETE n")

    session.run(
        """
        CREATE
          (alice:Person {name:'Alice', age:25}),
          (bob:Person {name:'Bob', age:31}),
          (charlie:Person {name:'Charlie', age:28}),
          (alice)-[:FRIEND]->(bob),
          (bob)-[:FRIEND]->(charlie),
          (alice)-[:FRIEND]->(charlie)
        """
    )

    print("\nFriend relationships")
    print("--------------------")

    result = session.run(
        """
        MATCH (a:Person)-[:FRIEND]->(b:Person)
        RETURN a.name AS from_person, b.name AS to_person
        ORDER BY from_person, to_person
        """
    )

    for record in result:
        print(f"{record['from_person']} -> {record['to_person']}")

    count = session.run("MATCH (p:Person) RETURN count(p) AS total").single()["total"]
    print(f"\nTotal persons: {count}")

driver.close()