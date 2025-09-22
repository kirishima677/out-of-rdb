import consul

c = consul.Consul(host="consul", port=8500)
c.kv.put("config/theme", "dark")
index, data = c.kv.get("config/theme")
print("GET config/theme:", data["Value"].decode())
