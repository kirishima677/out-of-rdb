import etcd3

etcd = etcd3.client(host="etcd", port=2379)
etcd.put("foo", "bar")
print("GET foo:", etcd.get("foo")[0].decode())