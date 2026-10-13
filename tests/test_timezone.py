"""时钟只有一个来源，而且它算的是北京，不是碰巧装在哪台机器上。

这几条守的是同一件事：`datetime.now()` / `date.today()` / `fromtimestamp()` 在
UTC 容器里会全部偏 8 小时，且**两种错法都不报错**——盘前 08:30 的跑批按前一天
算，当天的快讯被时效折减整批判成超龄。所以每条用例都刻意把系统时区拨走，看这
几个函数是不是还给出北京时间。
"""

from datetime import UTC, datetime, timedelta, timezone

from ashare_decision_support.schedule.timezone import (
    BEIJING_TZ,
    beijing_from_epoch,
    beijing_naive_now,
    beijing_now,
    beijing_today,
    to_beijing_naive,
)


def test_beijing_clock_is_explicit_and_date_matches_the_clock() -> None:
    now = beijing_now()

    assert now.tzinfo == BEIJING_TZ
    assert beijing_today() == now.date()
    assert now.utcoffset() == datetime(2026, 1, 1, tzinfo=BEIJING_TZ).utcoffset()


def test_the_naive_clock_is_beijing_wall_time_not_host_wall_time() -> None:
    """本机装的是 Asia/Shanghai，所以这条只有把差值算出来才有意义。"""
    naive = beijing_naive_now()

    assert naive.tzinfo is None
    # 和带时区的那只表对齐（允许两次取值之间的秒级漂移）。
    assert abs(naive - beijing_now().replace(tzinfo=None)) < timedelta(seconds=5)
    # 与 UTC 差 8 小时——这正是宿主机时区一旦不是上海就会丢掉的那 8 小时。
    assert abs((naive - datetime.now(UTC).replace(tzinfo=None)) - timedelta(hours=8)) < timedelta(
        seconds=5
    )


def test_epoch_is_expanded_on_the_beijing_clock() -> None:
    """行情源给的是毫秒时间戳；按宿主机时区展开会让「最后一根 K 线」挪到午盘。"""
    # 2026-08-09 21:30:00 +08:00
    stamp = datetime(2026, 8, 9, 21, 30, tzinfo=BEIJING_TZ).timestamp()

    assert beijing_from_epoch(stamp) == datetime(2026, 8, 9, 21, 30)


def test_utc_feed_timestamps_land_on_the_same_ruler() -> None:
    """RSS 与外部裁决包给的是 UTC，本地源给的是北京裸串，两者要能相减。"""
    assert to_beijing_naive(datetime(2026, 8, 9, 1, 30, tzinfo=UTC)) == datetime(2026, 8, 9, 9, 30)
    assert to_beijing_naive(
        datetime(2026, 8, 9, 10, 30, tzinfo=timezone(timedelta(hours=9)))
    ) == datetime(2026, 8, 9, 9, 30)


def test_a_naive_timestamp_is_already_beijing_and_is_left_alone() -> None:
    """财联社、东财给的裸串按约定就是北京时间。

    对它调 `astimezone` 会拿**宿主机时区**去解释它，正好制造出这个模块想消灭的
    那个错——而且在本机（Asia/Shanghai）上永远看不出来。
    """
    naive = datetime(2026, 8, 9, 9, 30)

    assert to_beijing_naive(naive) == naive


# 交易时段边界只有一份


def test_downstream_modules_share_the_one_session_close() -> None:
    """收盘时刻曾经在仓库里有四份 `time(15, 0)`，而规范的那份没人用。

    2026-08-10 实测：`schedule/clock.py:AFTERNOON_CLOSE`（模块名就叫时钟）零引用，
    而 `adapters/freshness.py`、`signals/intraday.py`、`ask/context.py` 各写了一份
    `SESSION_CLOSE = time(15, 0)`。交易所哪天调整收盘时间，要改四处，**漏一处不会
    有任何东西报错**——四个模块各自继续自洽地算错。

    钉的是**同一个对象**而不是「值等于 15:00」：后者在每处各写一遍字面量时同样
    会通过，正好放过要防的那件事。
    """
    from ashare_decision_support.adapters import freshness
    from ashare_decision_support.ask import context
    from ashare_decision_support.schedule import clock

    assert freshness.AFTERNOON_CLOSE is clock.AFTERNOON_CLOSE
    assert context.SESSION_CLOSE is clock.AFTERNOON_CLOSE
    # 集合竞价起算的那一刻同理：`ask/context.py` 与 `schedule/calendar.py` 共用一份。
    assert context.SESSION_OPEN is clock.AUCTION_OPEN


def test_no_module_outside_clock_writes_a_session_boundary_literal() -> None:
    """边界字面量只准出现在 `schedule/clock.py` 里。

    上面那条只覆盖今天已知的三个下游；这条挡的是**明天新写的那个模块**——
    它才是四份定义当初的来源。`ask/context.py` 的 05:00 不在此列：那是本层特有的
    时段口径，不是交易时段边界。
    """
    import ast
    import pathlib

    from ashare_decision_support.schedule import clock

    boundaries = {(9, 15), (9, 30), (11, 30), (13, 0), (15, 0)}
    package = pathlib.Path(clock.__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "clock.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in {"time", "clock_time"} or len(node.args) != 2:
                continue
            args = [a.value for a in node.args if isinstance(a, ast.Constant)]
            if len(args) == 2 and tuple(args) in boundaries:
                offenders.append(f"{path.relative_to(package)}:{node.lineno} time{tuple(args)}")

    assert offenders == [], "交易时段边界要从 schedule/clock.py 取：" + "、".join(offenders)


def test_the_session_length_is_not_stored_twice() -> None:
    """`signals/intraday.py` 已经从 clock 拿了 `minutes_since_open`，上界却曾经
    自己又存了一份 240。

    这里不能用 `is`：240 会被 CPython 驻留，重新写一遍字面量照样通过。所以查的是
    源码——除 clock 之外，没有模块可以把裸的 240 赋给 `*SESSION_MINUTES`。
    """
    import ast
    import pathlib

    from ashare_decision_support.schedule import clock
    from ashare_decision_support.signals import intraday

    assert intraday.FULL_SESSION_MINUTES == clock.SESSION_MINUTES
    assert clock.SESSION_MINUTES == clock.MORNING_MINUTES * 2

    package = pathlib.Path(clock.__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "clock.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)):
                continue
            if node.value.value != clock.SESSION_MINUTES:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and "SESSION_MINUTES" in target.id:
                    offenders.append(f"{path.relative_to(package)}:{node.lineno} {target.id}")

    assert offenders == [], "交易分钟数要从 schedule/clock.py 取：" + "、".join(offenders)


def test_no_module_hands_out_a_host_clock_as_a_value() -> None:
    """没有模块可以把 `datetime.now` / `date.today` **不带括号**地传出去。

    上面几条查的都是「调用」；这条查的是**引用**，因为那正是 ruff 看不见的
    那一种，而它恰好是注入时钟的默认参数最自然的写法：

        now_fn: Callable[[], datetime] = datetime.now      # DTZ 沉默
        moment = datetime.now()                            # DTZ005

    `monitor/loop.py` 与 `schedule/daemon.py` 的 `now_fn` 默认值都曾是前者，
    从 DTZ 整族通电起两道闸一次都没响过；测试也照不到，因为测试全都显式传
    `now_fn=`，默认值那条路从没跑过。后果不是崩，是 UTC 容器里收盘后
    `in_session()` 返回 `True`，告警照着收盘数据发出去。

    所以这道闸只能由测试来把——ruff 结构上就看不到这个形状。
    """
    import ast
    import pathlib

    from ashare_decision_support.schedule import clock

    banned = {
        ("datetime", "now"),
        ("datetime", "utcnow"),
        ("date", "today"),
        ("datetime", "fromtimestamp"),
    }
    package = pathlib.Path(clock.__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        called: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called.add(id(node.func))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or id(node) in called:
                continue
            owner = getattr(node.value, "id", None)
            if (owner, node.attr) in banned:
                offenders.append(f"{path.relative_to(package)}:{node.lineno} {owner}.{node.attr}")

    assert offenders == [], (
        "宿主机时钟不能当值传出去，用 schedule/timezone.py 的 beijing_* 系列："
        + "、".join(offenders)
    )
