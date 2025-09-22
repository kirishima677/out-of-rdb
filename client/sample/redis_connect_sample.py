# client/redis_example.py
import redis

r = redis.Redis(host="redis", port=6379)
r.set("hello", "world")
print("GET hello:", r.get("hello").decode())