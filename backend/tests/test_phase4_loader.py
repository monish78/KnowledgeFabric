"""Phase 4/7: building graphs from an approved schema, add-data, and KB isolation: against the real
Neo4j and the difficult dataset. Expected numbers come from data/expected/manifest.json."""

import pytest
from neo4j.exceptions import ClientError

from app import graph_schema as gs
from app import loader
from app.graphstore import GraphStore, UnsafeQueryError
from app.tabular import read_table_file

from .fixtures import SAMPLES, finance_schema, manifest, retail_schema

REASONS = {
    "missing customer_id": "missing_customer_id",
    "unknown Product": "unknown_sku",
    "unknown Warehouse": "unknown_warehouse",
    "missing order_id": "missing_order_id",
    "unknown Supplier": "supplier_given_as_name",
    "unknown Customer": "unknown_customer_id",
    "missing Order ID": "missing_order_id",
    "missing sku": "missing_sku",
}


def _by_reason(plan, sheet=None):
    out = {}
    for r in plan.rejected:
        if sheet and r["sheet"] != sheet:
            continue
        key = REASONS.get(r["reason"].split(":")[0], r["reason"].split(":")[0])
        out[key] = out.get(key, 0) + 1
    return out


def _load(store, schema, path, **kw):
    plan = loader.plan_load(schema, read_table_file(path), loader.existing_keys_for(store, schema), **kw)
    totals = loader.execute_plan(store, schema, plan)
    return plan, totals


@pytest.fixture(scope="module")
def retail():
    store = GraphStore("t_retail", mode="single")
    store.drop()
    schema = gs.clean(retail_schema())
    plan, totals = _load(store, schema, SAMPLES / "supplier_orders.xlsx")
    yield store, schema, plan, totals
    store.drop()


@pytest.fixture(scope="module")
def finance():
    store = GraphStore("t_finance", mode="single")
    store.drop()
    schema = gs.clean(finance_schema())
    plan, totals = _load(store, schema, SAMPLES / "finance_ledger.csv")
    yield store, schema, plan, totals
    store.drop()


def q1(store, cypher, **params):
    rows = store.read_internal(cypher, **params)
    return rows[0] if rows else None


# ------------------------------------------------------------------ initial build
def test_node_counts_match_manifest(retail):
    store, *_ = retail
    expected = manifest()["files"]["supplier_orders.xlsx"]["expected_nodes"]
    counts = store.counts()["nodes"]
    assert counts == {
        "Supplier": expected["Suppliers.Supplier ID"],
        "Product": expected["Products.SKU"],
        "Warehouse": expected["Warehouses.WH Code"],
        "Customer": expected["Customers.customer_id"],
        "Order": expected["Orders.order_id"],
    }


def test_relationship_counts_match_manifest(retail):
    store, *_ = retail
    e = manifest()["files"]["supplier_orders.xlsx"]["expected_relationships"]
    assert store.counts()["relationships"] == {
        "SUPPLIES": e["supplier->product"],
        "SUBSTITUTE_OF": e["product->substitute_product"],
        "STORED_IN": e["product->warehouse (stock)"],
        "PLACED": e["customer->order"],
        "CONTAINS": e["order->product (line)"],
        "SHIPPED_FROM": e["order->warehouse"],
    }


def test_rejections_match_manifest(retail):
    _, _, plan, _ = retail
    e = manifest()["files"]["supplier_orders.xlsx"]["expected_rejections"]
    assert _by_reason(plan, "Products") == e["Products"]
    assert _by_reason(plan, "Orders") == e["Orders"]
    assert len(plan.rejected) == sum(e["Products"].values()) + sum(e["Orders"].values())
    assert all(r["row"] > 1 for r in plan.rejected)  # file row numbers for the report


def test_messy_keys_are_merged_and_spelled_consistently(retail):
    store, *_ = retail
    # " sup-011" / "SUP-011 " duplicates collapse to one node, stored in canonical form
    assert q1(
        store,
        f"MATCH (s:{store.label('Supplier')}) WHERE toUpper(s.supplier_id) = 'SUP-011' "
        "RETURN count(s) AS n, collect(s.supplier_id) AS ids",
    ) == {"n": 1, "ids": ["SUP-011"]}
    bad = q1(
        store,
        f"MATCH (n:{store.label('Customer')}) WHERE n.customer_id <> trim(n.customer_id) "
        "OR n.customer_id <> toUpper(n.customer_id) RETURN count(n) AS n",
    )
    assert bad["n"] == 0


def test_values_are_typed(retail):
    store, *_ = retail
    row = q1(store, f"MATCH (w:{store.label('Warehouse')} {{warehouse_id: 'WH-MUM'}}) RETURN w.capacity AS c")
    assert row["c"] == 25000  # "25,000" -> integer
    row = q1(
        store,
        f"MATCH (p:{store.label('Product')}) WHERE p.unit_price IS NOT NULL "
        "RETURN count(p) AS n, min(p.unit_price) > 0 AS positive",
    )
    assert row == {"n": 307, "positive": True}  # "₹1,250.00", "INR 1250" -> float
    row = q1(
        store,
        f"MATCH (s:{store.label('Supplier')}) RETURN count(s.rating) AS rated, "
        "sum(CASE WHEN s.onboarded IS :: DATE THEN 1 ELSE 0 END) AS dates",
    )
    assert row == {"rated": 45, "dates": 48}  # 3 "N/A" ratings -> null; four date formats -> DATE
    row = q1(
        store,
        f"MATCH (p:{store.label('Product')}) RETURN count(DISTINCT valueType(p.discontinued)) AS t, "
        "collect(DISTINCT p.discontinued) AS vals",
    )
    assert sorted(row["vals"]) == [False, True]


def test_forward_reference_and_optional_relationship(retail):
    store, *_ = retail
    row = q1(store, f"MATCH (:{store.label('Product')} {{sku: 'SKU-10007'}})-[:SUBSTITUTE_OF]->(s) RETURN s.sku AS sku")
    assert row["sku"] == "SKU-10213"


def test_last_row_wins_for_repeated_relationship_rows(retail):
    store, *_ = retail
    expected = next(q for q in manifest()["questions"] if "SKU-10001" in q["q"])["answer"]
    row = q1(
        store, f"MATCH (:{store.label('Product')} {{sku: 'SKU-10001'}})-[r:STORED_IN]->() RETURN sum(r.stock_qty) AS s"
    )
    assert row["s"] == expected


def test_line_level_properties_on_relationship(retail):
    store, *_ = retail
    row = q1(
        store,
        f"MATCH (o:{store.label('Order')})-[c:CONTAINS]->() RETURN count(c) AS lines, "
        "count(DISTINCT o) AS orders, min(c.qty) AS min_qty",
    )
    assert row["lines"] > row["orders"] and row["min_qty"] >= 1


@pytest.mark.parametrize(
    "question_start",
    [
        "How many suppliers",
        "Which suppliers deliver",
        "Which of those",
        "Which customer ordered",
        "How many orders were cancelled",
        "Which warehouse has",
        "How many products does",
    ],
)
def test_graph_supports_the_expected_answers(retail, question_start):
    """Reference Cypher over the loaded graph reproduces the manifest answers (so chat can too)."""
    store, *_ = retail
    L = store.label
    q = next(
        q for q in manifest()["questions"] if q["q"].startswith(question_start) and not q.get("after_add_data_only")
    )
    ref = {
        "How many suppliers": f"MATCH (s:{L('Supplier')}) RETURN count(s) AS a",
        "Which suppliers deliver": f"MATCH (s:{L('Supplier')})-[:SUPPLIES]->(:{L('Product')})-[:STORED_IN]->"
        f"(:{L('Warehouse')} {{city: 'Chennai'}}) RETURN collect(DISTINCT s.name) AS a",
        "Which of those": f"MATCH (s:{L('Supplier')})-[:SUPPLIES]->(:{L('Product')})-[:STORED_IN]->"
        f"(:{L('Warehouse')} {{city: 'Chennai'}}) WHERE s.rating < 4 RETURN collect(DISTINCT s.name) AS a",
        "Which customer ordered": f"MATCH (c:{L('Customer')})-[:PLACED]->(:{L('Order')})-[r:CONTAINS]->() "
        "WITH c, sum(r.qty) AS q RETURN c.name AS a ORDER BY q DESC LIMIT 1",
        "How many orders were cancelled": f"MATCH (o:{L('Order')} {{status: 'Cancelled'}}) RETURN count(o) AS a",
        "Which warehouse has": f"MATCH (w:{L('Warehouse')}) RETURN w.city AS a ORDER BY w.capacity DESC LIMIT 1",
        "How many products does": f"MATCH (:{L('Supplier')} {{name: 'Bluepeak Textiles'}})-[:SUPPLIES]->(p) "
        "RETURN count(p) AS a",
    }[question_start]
    got = q1(store, ref)["a"]
    assert (sorted(got) if isinstance(got, list) else got) == q["answer"]


# ------------------------------------------------------------------ add data
def test_add_data_october_orders(retail):
    store, schema, *_ = retail
    e = manifest()["files"]["supplier_orders_october.xlsx"]["expected"]
    before = store.counts()
    plan, totals = _load(store, schema, SAMPLES / "supplier_orders_october.xlsx")
    after = store.counts()
    assert [m.schema_sheet for m in plan.matches] == ["Orders"]  # renamed/reordered headers still match
    assert _by_reason(plan) == e["rejected"]
    assert after["nodes"]["Order"] - before["nodes"]["Order"] == e["new_orders"]
    assert after["relationships"]["CONTAINS"] - before["relationships"]["CONTAINS"] == e["new_order_lines"]
    assert after["nodes"]["Customer"] == before["nodes"]["Customer"]  # unknown customers are not invented
    cancelled = next(q for q in manifest()["questions"] if q.get("after_add_data_only"))["answer"]
    assert q1(store, f"MATCH (o:{store.label('Order')} {{status: 'Cancelled'}}) RETURN count(o) AS n")["n"] == cancelled


def test_add_data_inventory_csv(retail):
    store, schema, *_ = retail
    e = manifest()["files"]["warehouse_update.csv"]["expected"]
    before = store.counts()["relationships"]["STORED_IN"]
    plan, _ = _load(store, schema, SAMPLES / "warehouse_update.csv")
    assert [m.schema_sheet for m in plan.matches] == ["Inventory"]  # BOM + ';' + CRLF
    assert _by_reason(plan) == e["rejected"]
    assert store.counts()["relationships"]["STORED_IN"] - before == e["new_pairs"]
    expected = next(q for q in manifest()["questions"] if "SKU-10001" in q["q"])["after_add_data"]
    assert (
        q1(
            store,
            f"MATCH (:{store.label('Product')} {{sku: 'SKU-10001'}})-[r:STORED_IN]->() " "RETURN sum(r.stock_qty) AS s",
        )["s"]
        == expected
    )


def test_merge_off_rejects_rows_for_existing_keys(retail):
    store, schema, *_ = retail
    plan = loader.plan_load(
        schema,
        read_table_file(SAMPLES / "supplier_orders.xlsx"),
        loader.existing_keys_for(store, schema),
        merge_existing=False,
    )
    assert plan.rows_loaded == 0 or all(
        r["reason"].startswith(("already exists", "unknown", "missing")) for r in plan.rejected
    )
    assert any(r["reason"].startswith("already exists") for r in plan.rejected)


def test_file_that_matches_nothing_is_refused(retail):
    store, schema, *_ = retail
    with pytest.raises(loader.LoadError):
        loader.plan_load(schema, read_table_file(SAMPLES / "finance_ledger.csv"), {})


# ------------------------------------------------------------------ isolation (single-database mode)
def test_second_kb_in_same_database_is_isolated(retail, finance):
    r_store, *_ = retail
    f_store, _, f_plan, _ = finance
    f_counts = f_store.counts()
    assert f_counts["nodes"]["LedgerEntry"] == 480 and not f_plan.rejected
    assert set(f_counts["nodes"]) == {"LedgerEntry", "Account", "CostCentre", "Vendor", "Approver"}
    # the same question in each KB only sees that KB
    _, rows = f_store.run_readonly("MATCH (n) RETURN count(n) AS n")
    assert rows[0]["n"] == sum(f_counts["nodes"].values())
    _, rows = r_store.run_readonly("MATCH (n) RETURN count(n) AS n")
    assert rows[0]["n"] == sum(r_store.counts()["nodes"].values())
    _, rows = f_store.run_readonly("MATCH (w:Warehouse) RETURN count(w) AS n")
    assert rows[0]["n"] == 0
    # 'Bluepeak Textiles' is a Supplier in retail and a Vendor in finance; neither leaks
    _, rows = r_store.run_readonly("MATCH (n) WHERE n.name = 'Bluepeak Textiles' RETURN labels(n) AS l")
    assert [sorted(x for x in r["l"] if not x.startswith("KB_")) for r in rows] == [["Supplier"]]
    _, rows = f_store.run_readonly("MATCH ()-[r]->() RETURN count(r) AS n")
    assert rows[0]["n"] == sum(f_counts["relationships"].values())


def test_finance_answers(finance):
    store, *_ = finance
    it = next(q for q in manifest()["questions"] if "CC-IT" in q["q"])
    row = q1(
        store,
        f"MATCH (e:{store.label('LedgerEntry')})-[:CHARGED_TO]->(:{store.label('CostCentre')} {{code: 'CC-IT'}}) "
        "RETURN round(sum(e.amount), 2) AS s",
    )
    assert abs(row["s"] - it["answer"]) <= 0.01  # accounting "(1,200.00)" parsed as negative
    top = next(q for q in manifest()["questions"] if "highest total spend" in q["q"])
    row = q1(
        store,
        f"MATCH (e:{store.label('LedgerEntry')})-[:PAID_TO]->(v) WITH v, sum(e.amount) AS s "
        "RETURN v.name AS v ORDER BY s DESC LIMIT 1",
    )
    assert row["v"] == top["answer"]


def test_chat_queries_are_read_only_twice_over(retail):
    store, *_ = retail
    with pytest.raises(UnsafeQueryError):
        store.run_readonly("MATCH (n) DETACH DELETE n")
    # even without the text check, the read transaction refuses writes
    with store.driver.session(database=store.database, default_access_mode="READ") as s, pytest.raises(ClientError):
        s.execute_read(lambda tx: tx.run("CREATE (:Hack)").consume())
    assert store.counts()["nodes"]["Supplier"] == 48
