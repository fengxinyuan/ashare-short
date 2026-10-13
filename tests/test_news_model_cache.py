"""模型新闻缓存必须匹配语料、持仓、板块清单和模型配置。"""

from __future__ import annotations

import json
from datetime import date

import pytest

from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.news.cache import NewsCache
from ashare_decision_support.news.daily_read import DailyNewsReader
from ashare_decision_support.news.dedup import dedup_key
from ashare_decision_support.news.llm_classifier import LlmNewsClassifier
from tests.test_daily_read import FakeClient

DAY = date(2026, 9, 13)
FIRST = "央行下调存款准备金率"
SECOND = "交易所披露风险警示公告"


def _read_reply(title, symbol=None):
    row = {"text": title, "evidence": [title]}
    if symbol:
        row["symbol"] = symbol
    return json.dumps({"holdings" if symbol else "mainlines": [row]}, ensure_ascii=False)


def _label_reply(title, sector):
    return json.dumps(
        [{"t": title, "sectors": [sector], "direction": "bullish", "why": "流动性改善"}],
        ensure_ascii=False,
    )


def test_unchanged_news_input_reuses_the_verified_read(tmp_path):
    client = FakeClient(_read_reply(FIRST))
    reader = DailyNewsReader(client, NewsCache(tmp_path))
    docs = [NewsDoc(title=FIRST)]

    first = reader.read(docs, DAY)
    second = reader.read(docs, DAY)

    assert first.ok and second.ok
    assert len(client.prompts) == 1
    assert second.mainlines == first.mainlines


def test_changed_news_input_cannot_reuse_an_earlier_story(tmp_path):
    client = FakeClient(_read_reply(FIRST))
    reader = DailyNewsReader(client, NewsCache(tmp_path))
    assert reader.read([NewsDoc(title=FIRST)], DAY).ok
    client.reply = _read_reply(SECOND)

    second = reader.read([NewsDoc(title=SECOND)], DAY)

    assert len(client.prompts) == 2
    assert second.ok
    assert second.mainlines[0].evidence == (SECOND,)


def test_changed_holdings_cannot_reuse_another_stocks_read(tmp_path):
    client = FakeClient(_read_reply(FIRST, "600519.SH"))
    reader = DailyNewsReader(client, NewsCache(tmp_path))
    docs = [NewsDoc(title=FIRST)]
    first = reader.read(docs, DAY, symbol_docs={"600519.SH": docs})
    assert first.holdings[0].symbol == "600519.SH"
    client.reply = _read_reply(FIRST, "000001.SZ")

    second = reader.read(docs, DAY, symbol_docs={"000001.SZ": docs})

    assert len(client.prompts) == 2
    assert [item.symbol for item in second.holdings] == ["000001.SZ"]


def test_changing_the_selected_model_invalidates_a_cached_read(tmp_path):
    client = FakeClient(_read_reply(FIRST))
    client.model = "model-a"
    reader = DailyNewsReader(client, NewsCache(tmp_path))
    docs = [NewsDoc(title=FIRST)]
    assert reader.read(docs, DAY).ok
    client.model = "model-b"

    assert reader.read(docs, DAY).ok
    assert len(client.prompts) == 2


def test_cached_read_must_still_pass_the_evidence_gate(tmp_path):
    client = FakeClient(_read_reply(FIRST))
    cache = NewsCache(tmp_path)
    reader = DailyNewsReader(client, cache)
    docs = [NewsDoc(title=FIRST)]
    assert reader.read(docs, DAY).ok
    path = next(cache.root.rglob("*.json"))
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["data"]["mainlines"] = [{"text": SECOND, "evidence": [SECOND]}]
    path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")

    restored = reader.read(docs, DAY)

    assert restored.ok
    assert len(client.prompts) == 2
    assert restored.mainlines[0].evidence == (FIRST,)


@pytest.mark.parametrize("change", ["docs", "sectors", "model"])
def test_classifier_cache_is_bound_to_its_inputs(tmp_path, change):
    client = FakeClient(_label_reply(FIRST, "银行"))
    client.model = "model-a"
    classifier = LlmNewsClassifier(client, cache=NewsCache(tmp_path))
    docs, sectors = [NewsDoc(title=FIRST)], ["银行"]
    assert classifier.classify(docs, sectors, DAY).ok
    assert classifier.classify(docs, sectors, DAY).ok
    assert len(client.prompts) == 1
    if change == "docs":
        docs = [NewsDoc(title=SECOND)]
    elif change == "sectors":
        sectors = ["非银金融"]
    else:
        client.model = "model-b"
    client.reply = _label_reply(docs[0].title, sectors[0])

    result = classifier.classify(docs, sectors, DAY)

    assert len(client.prompts) == 2
    assert result.labels[dedup_key(docs[0])].sectors == tuple(sectors)


def test_incomplete_classification_is_retried_when_the_model_recovers(tmp_path):
    class Client(FakeClient):
        fail_second = True

        def chat(self, messages):
            prompt = messages[-1]["content"]
            self.prompts.append(prompt)
            if SECOND in prompt and self.fail_second:
                raise OSError("temporary model failure")
            return _label_reply(SECOND if SECOND in prompt else FIRST, "银行")

    client = Client()
    classifier = LlmNewsClassifier(client, cache=NewsCache(tmp_path), batch_size=1, max_workers=1)
    docs = [NewsDoc(title=FIRST), NewsDoc(title=SECOND)]
    first = classifier.classify(docs, ["银行"], DAY)
    assert len(first.labels) == 1
    client.fail_second = False

    recovered = classifier.classify(docs, ["银行"], DAY)

    assert len(recovered.labels) == 2
    assert len(client.prompts) == 4
