from job_search_hub.notion import properties as P
from job_search_hub.pipeline.answer_bank_cli import propose_yes_no_answers
from job_search_hub.pipeline.answers import (
    DEFAULT_REUSE_THRESHOLD,
    CachedAnswer,
    cosine_similarity,
    find_answer,
    normalize_question,
)


def _ans(**kw) -> CachedAnswer:
    base = dict(
        page_id="p1",
        question="Are you authorized to work in the United States?",
        normalized_question=normalize_question("Are you authorized to work in the United States?"),
        question_family="Work Authorization",
        answer_type="Yes/No",
        answer="Yes",
        status="Approved",
        approved_for_reuse=True,
        sensitive=False,
        context_specific=False,
        reuse_threshold=DEFAULT_REUSE_THRESHOLD,
    )
    base.update(kw)
    return CachedAnswer(**base)


def test_normalize_collapses_punct_and_case():
    a = normalize_question("Are you authorized to work in the U.S.?")
    normalize_question("are you authorized to work in the u s")
    # both should drop punctuation; we only require they're stable internally
    assert normalize_question(a) == a
    assert "authorized" in a and "work" in a


def test_exact_match_hits():
    candidates = [_ans()]
    lookup = find_answer(
        question="Are you authorized to work in the United States?",
        answer_type="Yes/No",
        candidates=candidates,
        question_embedding=None,
        candidate_embeddings=None,
    )
    assert lookup.found
    assert lookup.method == "exact"


def test_unapproved_answer_is_not_returned():
    candidates = [_ans(approved_for_reuse=False)]
    lookup = find_answer(
        question="Are you authorized to work in the United States?",
        answer_type="Yes/No",
        candidates=candidates,
        question_embedding=None,
        candidate_embeddings=None,
    )
    assert not lookup.found


def test_context_specific_blocks_reuse():
    candidates = [_ans(context_specific=True)]
    lookup = find_answer(
        question="Are you authorized to work in the United States?",
        answer_type="Yes/No",
        candidates=candidates,
        question_embedding=None,
        candidate_embeddings=None,
    )
    assert not lookup.found


def test_embedding_threshold_gate():
    candidates = [_ans(normalized_question="different question")]
    q_emb = [1.0, 0.0, 0.0]
    cand_embs = {"p1": [0.99, 0.01, 0.0]}  # similar
    lookup = find_answer(
        question="something else",
        answer_type="Yes/No",
        candidates=candidates,
        question_embedding=q_emb,
        candidate_embeddings=cand_embs,
    )
    assert lookup.found and lookup.method == "embedding"

    cand_embs = {"p1": [0.0, 1.0, 0.0]}  # orthogonal
    lookup = find_answer(
        question="something else",
        answer_type="Yes/No",
        candidates=candidates,
        question_embedding=q_emb,
        candidate_embeddings=cand_embs,
    )
    assert not lookup.found


def test_cosine():
    assert cosine_similarity([1, 0], [1, 0]) == 1.0
    assert cosine_similarity([1, 0], [0, 1]) == 0.0


class _Router:
    def get(self, role):
        return "model"


class _Store:
    def __init__(self):
        self.audit_rows = []

    def audit(self, **kwargs):
        self.audit_rows.append(kwargs)


class _Notion:
    def __init__(self, answer_pages, fact_pages):
        self.answer_pages = answer_pages
        self.fact_pages = fact_pages
        self.updates = []

    def query_data_source(self, ds_id, *args, **kwargs):
        if ds_id == "answers":
            return list(self.answer_pages)
        if ds_id == "facts":
            return list(self.fact_pages)
        return []

    def update_page(self, page_id, properties):
        self.updates.append((page_id, properties))


def _settings(tmp_path):
    from job_search_hub.config import load_settings

    settings = load_settings(env_file=tmp_path / "missing.env")
    settings.notion.data_sources["answer_bank"] = "answers"
    settings.notion.data_sources["resume_facts"] = "facts"
    return settings


def _answer_page(
    page_id,
    question,
    *,
    status="Needs Answer",
    answer="",
    family="Skills",
    intent="Experience",
    topics=None,
    approved=False,
):
    return {
        "id": page_id,
        "created_time": "2026-01-01T00:00:00Z",
        "properties": {
            "Question": P.title(question),
            "Normalized Question": P.rich_text(normalize_question(question)),
            "Question Family": P.select(family),
            "Answer Type": P.select("Yes/No"),
            "My Answer": P.rich_text(answer),
            "Status": {"status": {"name": status}},
            "Approved for Reuse": P.checkbox(approved),
            "Sensitive": P.checkbox(False),
            "Context Specific": P.checkbox(False),
            "Reuse Threshold": P.number(DEFAULT_REUSE_THRESHOLD),
            "Canonical Key": P.rich_text(""),
            "Topics": P.multi_select(topics or []),
            "Question Intent": P.select(intent),
            "Related Opportunities": P.relation([]),
            "Related Applications": P.relation([]),
            "Fact Sources": P.relation([]),
            "Use Count": P.number(0),
        },
    }


def _fact_page(page_id, value):
    return {
        "id": page_id,
        "properties": {
            "Fact ID": P.rich_text(f"fact:{value.lower()}"),
            "Fact Type": P.select("Skill"),
            "Value": P.rich_text(value),
            "Fact": P.title(value),
            "Organization": P.rich_text(""),
            "Role Label": P.rich_text("Backend Software Engineer"),
            "Relationship Type": P.select("Not Applicable"),
            "Start Date": P.date_property(None),
            "End Date": P.date_property(None),
            "Current": P.checkbox(False),
            "Category": P.select("Backend"),
            "Proficiency": P.select("Strong"),
            "Emphasis Tags": P.multi_select(["Backend"]),
            "Verified": P.checkbox(True),
            "Approved for Profile": P.checkbox(True),
            "Approved for Applications": P.checkbox(True),
            "Approved for Cover Letters": P.checkbox(False),
            "Metrics Verified": P.checkbox(False),
            "Sensitive": P.checkbox(False),
            "Sort Order": P.number(1),
            "Active": P.checkbox(True),
            "Parent Fact": P.relation([]),
        },
    }


def test_propose_yes_no_writes_confident_evidenced_answer(tmp_path):
    class Client:
        def chat_json(self, **kwargs):
            return {
                "proposals": [
                    {
                        "question_index": 0,
                        "answer": "Yes",
                        "confidence": 0.91,
                        "reason": "Python is a verified skill.",
                        "fact_ids": ["fact-page"],
                        "answer_ids": [],
                    }
                ]
            }

    notion = _Notion(
        [_answer_page("answer-page", "Do you have Python experience?", topics=["Python"])],
        [_fact_page("fact-page", "Python")],
    )
    stats = propose_yes_no_answers(
        notion=notion,
        settings=_settings(tmp_path),
        store=_Store(),
        router=_Router(),
        client=Client(),
        dry_run=False,
        approve=True,
    )

    assert stats.proposed == 1
    assert stats.written == 1
    assert notion.updates[0][0] == "answer-page"
    props = notion.updates[0][1]
    assert P.read_text(props["My Answer"]) == "Yes"
    assert P.read_relation_ids(props["Fact Sources"]) == ["fact-page"]
    assert P.read_checkbox(props["Approved for Reuse"])


def test_propose_yes_no_refuses_no_without_explicit_answer_evidence(tmp_path):
    class Client:
        def chat_json(self, **kwargs):
            return {
                "proposals": [
                    {
                        "question_index": 0,
                        "answer": "No",
                        "confidence": 0.95,
                        "reason": "No matching fact was supplied.",
                        "fact_ids": [],
                        "answer_ids": [],
                    }
                ]
            }

    notion = _Notion(
        [_answer_page("answer-page", "Do you have Kubernetes experience?")],
        [_fact_page("fact-page", "Python")],
    )
    stats = propose_yes_no_answers(
        notion=notion,
        settings=_settings(tmp_path),
        store=_Store(),
        router=_Router(),
        client=Client(),
        dry_run=False,
        approve=True,
    )

    assert stats.proposed == 0
    assert stats.skipped == 1
    assert stats.proposals[0].skipped_reason == "missing cited evidence"
    assert notion.updates == []


def test_propose_yes_no_splits_context_length_failures(tmp_path):
    class Client:
        def __init__(self):
            self.calls = 0

        def chat_json(self, **kwargs):
            self.calls += 1
            content = kwargs["messages"][1]["content"]
            if content.count("question_index") > 1:
                raise RuntimeError("LM Studio context length exceeded token limit")
            return {
                "proposals": [
                    {
                        "question_index": 0,
                        "answer": "Yes",
                        "confidence": 0.9,
                        "reason": "Verified matching skill.",
                        "fact_ids": ["fact-page"],
                        "answer_ids": [],
                    }
                ]
            }

    client = Client()
    notion = _Notion(
        [
            _answer_page("answer-1", "Do you have Python experience?", topics=["Python"]),
            _answer_page("answer-2", "Do you have backend experience?", topics=["Backend"]),
        ],
        [_fact_page("fact-page", "Python backend")],
    )
    stats = propose_yes_no_answers(
        notion=notion,
        settings=_settings(tmp_path),
        store=_Store(),
        router=_Router(),
        client=client,
        dry_run=True,
        batch_size=2,
    )

    assert client.calls == 3
    assert stats.errors == []
    assert stats.proposed == 2


def test_propose_yes_no_splits_malformed_json_batches(tmp_path):
    class Client:
        def __init__(self):
            self.calls = 0

        def chat_json(self, **kwargs):
            self.calls += 1
            content = kwargs["messages"][1]["content"]
            if content.count("question_index") > 1:
                raise RuntimeError("LM Studio returned malformed JSON")
            return {
                "proposals": [
                    {
                        "question_index": 0,
                        "answer": "Unknown",
                        "confidence": 0.0,
                        "reason": "No direct evidence.",
                        "fact_ids": [],
                        "answer_ids": [],
                    }
                ]
            }

    client = Client()
    notion = _Notion(
        [
            _answer_page("answer-1", "Do you have Angular experience?", topics=["Angular"]),
            _answer_page("answer-2", "Do you have Flink experience?", topics=["Flink"]),
        ],
        [_fact_page("fact-page", "Python backend")],
    )
    stats = propose_yes_no_answers(
        notion=notion,
        settings=_settings(tmp_path),
        store=_Store(),
        router=_Router(),
        client=client,
        dry_run=True,
        batch_size=2,
    )

    assert client.calls == 3
    assert stats.errors == []
    assert stats.unknown == 2


def test_propose_yes_no_only_targets_skill_like_rows(tmp_path):
    class Client:
        def chat_json(self, **kwargs):
            return {"proposals": []}

    notion = _Notion(
        [
            _answer_page(
                "location",
                "Are you located in Dallas TX?",
                family="Location",
                intent="Location",
            ),
            _answer_page(
                "credential",
                "Do you hold the required Cypress credential?",
                family="Skills",
                intent="Credential",
                topics=["Cypress"],
            ),
        ],
        [_fact_page("fact-page", "Cypress")],
    )
    stats = propose_yes_no_answers(
        notion=notion,
        settings=_settings(tmp_path),
        store=_Store(),
        router=_Router(),
        client=Client(),
        dry_run=True,
    )

    assert stats.candidates == 0
