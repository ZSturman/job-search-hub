"""A disposable Notion API model; no account, credentials, sockets, or PDFs."""

from copy import deepcopy
from uuid import UUID

from job_search_hub.notion import properties as P
from job_search_hub.storage import READ_ONLY

ROOT = str(UUID(int=1))


class HubNotion:
    def __init__(self):
        self.counter = 100
        self.blocks = {ROOT: []}
        self.databases = {}
        self.schemas = {}
        self.rows = {}
        self.views = {}
        self.pages = {}
        self.writes = []
        self.requests = []
        self.fail_relation_once = False

    def identity(self):
        self.counter += 1
        return str(UUID(int=self.counter))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def record(self, method, path, payload):
        if READ_ONLY.get():
            raise AssertionError("A preview attempted a remote write")
        self.writes.append((method, path, deepcopy(payload)))

    def _request(self, method, path, **kw):
        self.requests.append((method, path, deepcopy(kw)))
        if method == "GET" and path.endswith("/children"):
            parent = path.split("/")[2]
            return {"results": deepcopy(self.blocks.get(parent, [])), "has_more": False}
        if method == "GET" and path.startswith("/databases/"):
            return deepcopy(self.databases[path.rsplit("/", 1)[1]])
        if method == "GET" and path == "/views":
            return {
                "results": deepcopy(self.views.get(kw["params"]["database_id"], [])),
                "has_more": False,
            }
        payload = kw["json"]
        self.record(method, path, payload)
        if method == "PATCH" and path.endswith("/children"):
            parent = path.split("/")[2]
            for block in deepcopy(payload["children"]):
                block["id"] = self.identity()
                self.blocks.setdefault(parent, []).append(block)
            return {"results": []}
        if method == "PATCH" and path.startswith("/blocks/"):
            identity = path.rsplit("/", 1)[1]
            for blocks in self.blocks.values():
                for b in blocks:
                    if b["id"] == identity:
                        b.update(payload)
            return {}
        if path == "/pages":
            identity = self.identity()
            parent = payload["parent"]["page_id"]
            title = P.read_title(payload["properties"]["title"])
            self.blocks.setdefault(parent, []).append(
                {"id": identity, "type": "child_page", "child_page": {"title": title}}
            )
            self.blocks[identity] = deepcopy(payload.get("children", []))
            for b in self.blocks[identity]:
                b["id"] = self.identity()
            return {"id": identity}
        if path == "/databases":
            identity = self.identity()
            ds = self.identity()
            title = payload["title"][0]["text"]["content"]
            parent = payload["parent"]["page_id"]
            self.blocks.setdefault(parent, []).append(
                {"id": identity, "type": "child_database", "child_database": {"title": title}}
            )
            self.databases[identity] = {
                "id": identity,
                "data_sources": [{"id": ds}],
                "parent": {"page_id": parent},
            }
            self.schemas[ds] = {"id": ds, "properties": {}, "parent": {"database_id": identity}}
            self.rows[ds] = []
            self.update_data_source(ds, properties=payload["initial_data_source"]["properties"])
            return self.databases[identity]
        if path == "/views":
            identity = self.identity()
            view = self.identity()
            parent = payload["create_database"]["parent"]["page_id"]
            self.blocks[parent].append(
                {
                    "id": identity,
                    "type": "child_database",
                    "child_database": {"title": "Linked workflow view"},
                }
            )
            self.views[identity] = [dict(payload, id=view)]
            return {"id": view, "parent": {"database_id": identity}}
        raise AssertionError((method, path))

    def get_data_source(self, ds):
        return deepcopy(self.schemas[ds])

    def update_data_source(self, ds, *, properties):
        self.record("PATCH", f"/data_sources/{ds}", properties)
        for name, prop in deepcopy(properties).items():
            typ = next(iter(prop))
            prop.update(
                id=self.schemas[ds]["properties"].get(name, {}).get("id", self.identity()),
                type=typ,
                name=name,
            )
            self.schemas[ds]["properties"][name] = prop
        return self.get_data_source(ds)

    def query_data_source(self, ds, **kw):
        return deepcopy([p for p in self.rows.get(ds, []) if matches(p, kw.get("filter_") or {})])

    def create_page(self, *, parent, properties, **kw):
        self.record("POST", "/pages", {"parent": parent, "properties": properties})
        ds = parent["data_source_id"]
        identity = self.identity()
        page = {
            "id": identity,
            "parent": {"type": "data_source_id", "data_source_id": ds},
            "properties": deepcopy(properties),
            "url": f"https://notion.so/{identity}",
        }
        self.rows.setdefault(ds, []).append(page)
        self.pages[identity] = page
        return deepcopy(page)

    def get_page(self, identity):
        return deepcopy(self.pages[identity])

    def update_page(self, identity, properties):
        if self.fail_relation_once and "Application" in properties:
            self.fail_relation_once = False
            raise RuntimeError("interrupted relation write")
        self.record("PATCH", f"/pages/{identity}", properties)
        self.pages[identity]["properties"].update(deepcopy(properties))
        return self.get_page(identity)

    def archive_page(self, identity):
        self.record("PATCH", f"/pages/{identity}", {"in_trash": True})
        self.pages[identity]["in_trash"] = True


def matches(page, condition):
    if not condition:
        return True
    if "and" in condition:
        return all(matches(page, c) for c in condition["and"])
    if "or" in condition:
        return any(matches(page, c) for c in condition["or"])
    name = condition.get("property")
    prop = page["properties"].get(name, {})
    for typ, clause in condition.items():
        if typ == "property":
            continue
        value = {
            "title": P.read_title,
            "rich_text": P.read_text,
            "select": P.read_select,
            "checkbox": P.read_checkbox,
            "relation": P.read_relation_ids,
            "number": P.read_number,
            "date": P.read_date,
        }.get(typ, lambda x: None)(prop)
        if "equals" in clause:
            return value == clause["equals"]
        if "does_not_equal" in clause:
            return value != clause["does_not_equal"]
        if "is_empty" in clause:
            return not value
        if "is_not_empty" in clause:
            return bool(value)
    return True
