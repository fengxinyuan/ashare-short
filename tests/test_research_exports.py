"""研究缓存替换失败保留旧版本，恢复后可重复执行。"""

import json
from types import SimpleNamespace

import pytest

from ashare_decision_support.research.leaders import refresh_sector_leaders


def test_leader_refresh_preserves_the_last_good_cache_on_write_failure(tmp_path, monkeypatch):
    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    target = output_dir / "sector_leaders_latest.json"
    previous = '{"schema":"previous-good-report","sectors":[]}\n'
    target.write_text(previous, encoding="utf-8")
    taxonomy = SimpleNamespace(load=lambda: SimpleNamespace(profiles={}, notes=[]))
    adapter = SimpleNamespace(get_historical_bars=lambda *args: {})

    def fail_replace(*args):
        raise OSError("replacement failed")

    with monkeypatch.context() as patch:
        patch.setattr("ashare_decision_support.state.store.os.replace", fail_replace)
        with pytest.raises(OSError, match="replacement failed"):
            refresh_sector_leaders(
                output_dir=output_dir,
                levels=(),
                adapter=adapter,
                taxonomy_source=taxonomy,
            )
    assert target.read_text(encoding="utf-8") == previous
    assert list(output_dir.iterdir()) == [target]
    result = refresh_sector_leaders(
        output_dir=output_dir,
        levels=(),
        adapter=adapter,
        taxonomy_source=taxonomy,
    )
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == result["schema"]
    assert result["payload_path"] == str(target)
