from ashare_decision_support.sectors.clusters import cluster_ranked_sectors


def test_overlapping_technology_sectors_are_one_risk_cluster() -> None:
    result = cluster_ranked_sectors(
        [
            {"name": "芯片概念", "level": "CONCEPT", "score": 82},
            {"name": "电子", "level": "THS_INDUSTRY", "score": 81},
            {"name": "通信", "level": "THS_INDUSTRY", "score": 80},
            {"name": "共封装光学(CPO)", "level": "CONCEPT", "score": 79},
            {"name": "农林牧渔", "level": "THS_INDUSTRY", "score": 70},
        ]
    )

    technology = next(item for item in result["clusters"] if item["name"] == "科技硬件")
    assert technology["seats"] == 4
    assert technology["seat_share"] == 0.8
    assert result["crowded"] == [technology]
    assert "独立确认" in result["interpretation"]
