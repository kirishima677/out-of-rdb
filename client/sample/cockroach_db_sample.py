import psycopg2

conn = psycopg2.connect("postgresql://root@cockroachdb:26259/defaultdb?sslmode=disable")
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS accounts (id SERIAL PRIMARY KEY, balance INT);")
cur.execute("INSERT INTO accounts (balance) VALUES (100), (200);")
conn.commit()

cur.execute("SELECT * FROM accounts;")
print(cur.fetchall())

cur.close()
conn.close()