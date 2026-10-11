def test_audit_log_records(tmp_store):
    tmp_store.audit(
        actor="ingest",
        action="create_opportunity",
        target_kind="opportunity",
        target_id="abc",
        payload={"company": "Acme"},
    )
    with tmp_store.connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["action"] == "create_opportunity"


def test_idempotency_first_claim_succeeds_second_blocked(tmp_store):
    assert tmp_store.claim_idempotency("ingest_tick", "abc", ttl_seconds=60)
    assert not tmp_store.claim_idempotency("ingest_tick", "abc", ttl_seconds=60)


def test_embeddings_round_trip(tmp_store):
    vec = [0.1, 0.2, 0.3, -0.4]
    tmp_store.store_embedding(
        namespace="answer_question", key="p1", model="m", vector=vec, text_preview="q"
    )
    rows = tmp_store.load_embeddings(namespace="answer_question", model="m")
    assert len(rows) == 1
    key, loaded = rows[0]
    assert key == "p1"
    assert len(loaded) == len(vec)
    for a, b in zip(loaded, vec, strict=True):
        assert abs(a - b) < 1e-6


def test_opportunity_cache_upsert(tmp_store):
    tmp_store.upsert_opportunity_cache(
        fingerprint="fp1",
        source="Greenhouse",
        source_adapter="greenhouse",
        external_job_id="1",
        company="Acme",
        role_title="SWE",
        url="https://x.com/1",
        description_hash="h1",
    )
    row = tmp_store.find_opportunity_by_fingerprint("fp1")
    assert row is not None
    assert row["company"] == "Acme"


def test_prune_opportunity_cache_removes_noncanonical_rows(tmp_store):
    for fp in ("keep", "remove"):
        tmp_store.upsert_opportunity_cache(
            fingerprint=fp,
            notion_page_id=f"page-{fp}",
            source="Test",
            source_adapter="test",
            external_job_id=fp,
            company="Acme",
            role_title="Engineer",
            url=f"https://example.com/{fp}",
            description_hash=fp,
        )

    tmp_store.prune_opportunity_cache({"keep"})

    assert tmp_store.find_opportunity_by_fingerprint("keep") is not None
    assert tmp_store.find_opportunity_by_fingerprint("remove") is None


def test_observability_tables_round_trip(tmp_store):
    source_id = tmp_store.record_source_run(
        source_key="rss",
        adapter_key="rss",
        target="https://example.com/feed",
        status="ok",
        configured=True,
        postings_seen=3,
        http_status=200,
        payload={"feed": "example"},
    )
    assert source_id > 0
    assert tmp_store.recent_source_runs(1)[0]["postings_seen"] == 3

    llm_id = tmp_store.record_llm_call(
        stage="score",
        role="primary",
        task="fit",
        model="m",
        prompt_hash="abc",
        prompt_preview="prompt",
        response_preview="{}",
        json_valid=True,
        latency_ms=10.0,
    )
    assert llm_id > 0
    assert tmp_store.recent_llm_calls(1)[0]["json_valid"] == 1

    tmp_store.record_score_component(
        opportunity_page_id="opp1",
        criterion_key="fit",
        criterion_name="Fit",
        category="Skills",
        weight=1.0,
        direction="Positive",
        evaluation_method="LLM",
        score=0.75,
        weighted_effect=0.25,
        reason="good match",
        triggered_hard_exclusion=False,
        triggered_human_review=False,
        lane_override="",
        fallback_used=False,
        error="",
        llm_call_id=llm_id,
        dry_run=True,
    )
    rows = tmp_store.latest_score_components("opp1")
    assert len(rows) == 1
    assert rows[0]["criterion_key"] == "fit"
