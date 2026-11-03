from tests.test_docker import (  # noqa: F401  (fixtures used by name)
    PASSWORD,
    connect_worker,
    fake_docker,
    live,
)


def graph_of(client, headers, node_id):
    return client.get(f"/api/v1/nodes/{node_id}/relations", headers=headers).json()


def test_map_links_container_network_volume_image_project_router_domain(live):  # noqa: F811
    client, headers, port, tmp_path = live
    node_id, stop = connect_worker(client, headers, port, tmp_path, "docker")
    graph = graph_of(client, headers, node_id)
    kinds = {(o["type"], o["label"]) for o in graph["objects"]}
    assert {
        ("container", "web"),
        ("image", "nginx:1.27"),
        ("project", "demo"),
        ("network", "demo_default"),
        ("volume", "data"),
        ("router", "web@docker"),
        ("domain", "app.example.com"),
        ("middleware", "auth@docker"),
    } <= kinds
    links = {(k["source"]["type"], k["kind"], k["target"]["type"]) for k in graph["links"]}
    assert {
        ("container", "runs", "image"),
        ("container", "part_of", "project"),
        ("container", "attached_to", "network"),
        ("container", "mounts", "volume"),
        ("router", "routes_to", "container"),
        ("router", "serves", "domain"),
        ("router", "uses", "middleware"),
    } <= links
    # A bind mount is not a volume object, and no secret label reached the graph.
    assert not any(o["label"] == "/srv/site" for o in graph["objects"])
    assert "apr1" not in str(graph)
    stop.set()


def test_panel_shows_links_in_both_directions(live):  # noqa: F811
    client, headers, port, tmp_path = live
    node_id, stop = connect_worker(client, headers, port, tmp_path, "docker")
    base = f"/api/v1/nodes/{node_id}/relations"
    container_id = next(
        o["id"] for o in graph_of(client, headers, node_id)["objects"] if o["type"] == "container"
    )
    panel = client.get(f"{base}/container/{container_id}", headers=headers).json()
    assert {(k["direction"], k["kind"], k["object"]["type"]) for k in panel["links"]} >= {
        ("outgoing", "runs", "image"),
        ("incoming", "routes_to", "router"),  # the router points at the container
    }
    # From the other end, the same link is visible too (container <- router <- domain).
    router = client.get(f"{base}/router/web@docker", headers=headers).json()
    assert {(k["direction"], k["kind"]) for k in router["links"]} >= {
        ("outgoing", "routes_to"),
        ("outgoing", "serves"),
    }
    domain = client.get(f"{base}/domain/app.example.com", headers=headers).json()
    assert domain["links"] == [
        {
            "direction": "incoming",
            "kind": "serves",
            "object": {"type": "router", "id": "web@docker", "label": "web@docker"},
        }
    ]
    assert client.get(f"{base}/container/nope", headers=headers).status_code == 404
    stop.set()


def test_viewer_without_docker_right_sees_no_docker_objects_and_hidden_nodes_are_404(live):  # noqa: F811
    client, headers, port, tmp_path = live
    node_id, stop = connect_worker(client, headers, port, tmp_path, "docker")
    uid = client.post(
        "/api/v1/users", headers=headers, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    bob_token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {bob_token}"}
    url = f"/api/v1/nodes/{node_id}/relations"
    assert client.get(url, headers=bob).status_code == 404
    client.post(
        f"/api/v1/users/{uid}/grants",
        headers=headers,
        json={"right": "nodes.read", "scope": f"node:{node_id}"},
    )
    empty = client.get(url, headers=bob).json()
    assert empty["objects"] == [] and empty["links"] == []
    stop.set()


def test_offline_worker_is_reported_not_fatal(live):  # noqa: F811
    client, headers, port, tmp_path = live
    node_id = client.post(
        "/api/v1/nodes", headers=headers, json={"name": "n", "type": "docker"}
    ).json()["id"]
    graph = graph_of(client, headers, node_id)
    assert graph["objects"] == [] and graph["unavailable"] == {"docker": "worker.not_connected"}
