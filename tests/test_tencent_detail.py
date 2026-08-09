from __future__ import annotations

from ashare_decision_support.adapters.tencent_detail import TencentDetailAdapter


class StubHttp:
    def __init__(self, pages: dict[int, str]) -> None:
        self.pages = pages

    def __call__(self, request, timeout=None):
        url = request.full_url
        page = int(url.rsplit("=", 1)[-1])
        body = self.pages.get(page, 'v_detail_data_sh600519=[0,""];')

        class Response:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def read(self) -> bytes:
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Response(body.encode("gbk", errors="replace"))


def test_tencent_detail_summarises_active_and_large_flow() -> None:
    adapter = TencentDetailAdapter(
        opener=StubHttp(
            {
                # 页号从 0 起：第 0 页是集合竞价与开盘后的头几分钟，适配器必须抓它。
                0: (
                    'v_detail_data_sh600519=[2,"'
                    "1/14:29:00/10.00/+0.10/100/100000/B|"
                    "2/14:31:00/10.01/+0.01/50/50000/S|"
                    '3/14:55:00/10.02/+0.01/80/80000/B"];'
                ),
                1: (
                    'v_detail_data_sh600519=[1,"'
                    "4/10:00:00/9.90/-0.10/20/20000/M|"
                    '5/10:05:00/9.91/+0.01/300/300000/S"];'
                ),
            }
        )
    )

    summary = adapter.fetch_summary("600519.SH")

    assert summary is not None and summary.ok
    assert summary.prints_count == 5
    assert summary.buy_amount == 180000.0
    assert summary.sell_amount == 350000.0
    assert summary.neutral_amount == 20000.0
    assert summary.net_amount == -170000.0
    assert summary.buy_ratio == 0.3396
    assert summary.large_threshold_amount == 300000.0
    assert summary.large_net_amount == -300000.0
    assert summary.tail_net_amount == 30000.0
    assert summary.tail_ratio is None
    assert any("非正" in note for note in summary.notes)


def test_tencent_detail_defines_tail_ratio_only_on_positive_day_flow() -> None:
    adapter = TencentDetailAdapter(
        opener=StubHttp(
            {
                0: (
                    'v_detail_data_sh600519=[1,"'
                    "1/14:35:00/10.00/+0.10/100/100000/B|"
                    "2/14:50:00/10.01/+0.01/50/50000/B|"
                    '3/10:05:00/9.98/-0.02/20/20000/S"];'
                ),
            }
        )
    )

    summary = adapter.fetch_summary("600519.SH")

    assert summary is not None and summary.ok
    assert summary.net_amount == 130000.0
    assert summary.tail_net_amount == 150000.0
    assert summary.tail_ratio == 1.1538
