from pymongo import MongoClient

client = MongoClient("mongodb://root:example@mongodb:27017/")
db = client["testdb"]
collection = db["testcol"]

collection.insert_one({"name": "Alice", "age": 30})
print("Find one:", collection.find_one({"name": "Alice"}))