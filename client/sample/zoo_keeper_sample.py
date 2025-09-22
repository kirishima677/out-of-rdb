from kazoo.client import KazooClient

zk = KazooClient(hosts="zookeeper:2181")
zk.start()

zk.ensure_path("/myapp")
zk.set("/myapp", b"Hello ZooKeeper")
print("GET /myapp:", zk.get("/myapp")[0].decode())

zk.stop()