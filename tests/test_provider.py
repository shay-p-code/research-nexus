from types import SimpleNamespace as Obj

import pytest

from nexus.providers import OpenAIProvider


class Responses:
    def __init__(self, response):
        self.response, self.request = response, None

    def create(self, **kwargs):
        self.request = kwargs
        return self.response


def provider(settings, status="completed", annotations=None):
    settings = settings.model_copy(update={"openai_api_key": "fake-test-key"})
    p = OpenAIProvider(settings)
    responses = Responses(Obj(status=status, id="response-1", output_text="A cited finding.", usage=None,
        output=[Obj(type="message", content=[Obj(type="output_text", annotations=annotations or [])])]))
    p.client = Obj(responses=responses)
    return p, responses


def test_research_requires_real_provider_citation_annotations(rig):
    p, _ = provider(rig[3])
    with pytest.raises(ValueError, match="no provider-backed citations"):
        p.research("Question", "Missing evidence", [])


def test_incomplete_provider_response_is_not_saved_as_evidence(rig):
    p, _ = provider(rig[3], status="incomplete")
    with pytest.raises(ValueError, match="did not complete"):
        p.research("Question", "Missing evidence", [])


def test_research_extracts_citations_and_sets_work_limits(rig):
    p, responses = provider(rig[3], annotations=[Obj(type="url_citation", url="https://example.org/study",
        title="Study", start_index=2, end_index=10)])
    result = p.research("Question", "Missing evidence", [])
    assert result["citations"][0]["url"] == "https://example.org/study"
    assert responses.request["max_tool_calls"] == 3
    assert responses.request["store"] is False
    assert responses.request["tools"] == [{"type": "web_search", "search_context_size": "medium"}]
