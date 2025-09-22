from cassandra.cluster import Cluster

cluster = Cluster(["cassandra"], port=9042)
session = cluster.connect()

session.execute("CREATE KEYSPACE IF NOT EXISTS testks WITH replication = {'class':'SimpleStrategy', 'replication_factor':1};")
session.set_keyspace("testks")
session.execute("CREATE TABLE IF NOT EXISTS users (id UUID PRIMARY KEY, name text);")
session.execute("INSERT INTO users (id, name) VALUES (uuid(), 'Bob');")

rows = session.execute("SELECT * FROM users;")
for row in rows:
    print(row)
