"""Schedule calculations and shared duty selection; cloud operations live elsewhere."""
from __future__ import annotations
import calendar
import datetime as dt
import logging
import time
from typing import Any
from . import messages, repo
log = logging.getLogger('cdt-management.schedule')
_TZ_WARNED = False
_TZ_CACHE: dict[str, Any] = {'at': 0.0, 'name': ''}

def _hhmm_to_minutes(value: str) -> int:
    """'06:00' → 360。非法输入返回 -1。"""
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        return -1
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return -1
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return -1
    return hour * 60 + minute


def keepalive_window(item: dict[str, Any], now: dt.datetime) -> tuple[bool, str]:
    """单机保活的运行窗口：(此刻能不能跑, 人话摘要)。

    两个维度取交集：时段（每天几点到几点，**不跨零点**，结束写 00:00 表示到 24:00）
    与日期（每月几号到几号，**不跨月**）。都为空 = 全天全月，等价于没设窗口。

    日期超过当月天数时按月末截断：2 月填「1–31 日」就是 1 号到 28/29 号，
    否则那个月一天都匹配不上，机器整个月不干活。

    配置读不懂（手改库写坏的半截配置）时按「不限」处理并照常保活 —— 宁可多跑，
    不能因为一行坏配置把用户的机器钉死在停机状态。
    """
    start_raw = str(item.get("start_time") or "").strip()
    end_raw = str(item.get("end_time") or "").strip()
    start = _hhmm_to_minutes(start_raw) if start_raw else -1
    end = _hhmm_to_minutes(end_raw) if end_raw else -1
    # 结束 00:00 = 当天到午夜（24:00），和联合换班是同一套写法：
    # 「08:00–00:00」表示整段跑到午夜，不是零长度。
    if end == 0 and start > 0:
        end = 24 * 60
    timed = start >= 0 and end > start

    # 手改库写坏的半截配置按「不限」处理：int() 遇到文本会抛，不能让一行坏配置
    # 把整轮巡检掀翻（注释承诺的是「照常保活」，不是让机器全停）。
    try:
        day_start = int(item.get("start_day") or 0)
        day_end = int(item.get("end_day") or 0)
    except (TypeError, ValueError):
        day_start = day_end = 0
    dated = 1 <= day_start <= day_end <= 31

    parts: list[str] = []
    if timed:
        # 摘要按面板习惯写 24:00：写「每天 08:00–00:00」会被读成零长度
        end_label = "24:00" if end == 24 * 60 else end_raw
        parts.append(f"每天 {start_raw}–{end_label}")
    if dated:
        parts.append(f"{day_start}–{day_end} 日")
    summary = " · ".join(parts)

    if timed:
        minute = now.hour * 60 + now.minute
        if not (start <= minute < end):
            return False, summary
    if dated:
        month_days = calendar.monthrange(now.year, now.month)[1]
        if not (min(day_start, month_days) <= now.day <= min(day_end, month_days)):
            return False, summary
    return True, summary


def _window_bracket(window_text: str) -> str:
    """事件进度里紧跟「运行窗口」的括号说明；没配窗口时给空串，别留一对空括号。"""
    return f"（{window_text}）" if window_text else ""


def _is_timetable_group(group: dict[str, Any]) -> bool:
    """组内所有启用成员都填了合法起止时刻，才走「固定时刻表」模式。"""
    members = [m for m in group.get("members") or [] if m.get("enabled")]
    if len(members) < 2:
        return False
    return all(
        _hhmm_to_minutes(m.get("start_time")) >= 0 and _hhmm_to_minutes(m.get("end_time")) >= 0
        for m in members
    )


def _duty_at_timetable(group: dict[str, Any], now_ts: float) -> tuple[dict[str, Any], int]:
    """固定时刻表模式：按每天真实时钟判定当班。

    每个成员占据一天内的 [start, end) 分钟区间，全组刚好把 0–24 点分完；
    end == 0（保存时的 00:00 / 面板上的 24:00）表示这一段到午夜为止。
    真正的跨天配置已在保存时拒绝，这里保留 [start, 1440) ∪ [0, end) 的写法
    只为兼容历史数据。

    返回 (当班成员, 该班次开始时间戳)。开始时间戳用于 slot_key 去重与通知时间段显示。
    """
    members = active_members(group)
    tz_name = _group_timezone(group)
    now = dt.datetime.fromtimestamp(now_ts, _timezone(tz_name))
    minute_of_day = now.hour * 60 + now.minute

    # 逐个成员判断当前分钟落在谁家。跨天成员优先处理边界。
    for member in members:
        start = _hhmm_to_minutes(member.get("start_time"))
        end = _hhmm_to_minutes(member.get("end_time"))
        if start < 0 or end < 0:
            continue
        if end > start:
            # 普通区间 [start, end)
            if start <= minute_of_day < end:
                return member, _shift_start_ts(now, start)
        else:
            # 跨天区间 [start, 1440) ∪ [0, end)
            if minute_of_day >= start or minute_of_day < end:
                return member, _shift_start_ts(now, start)

    # 没命中（理论上不该发生），退回第一台
    first = members[0]
    return first, _shift_start_ts(now, _hhmm_to_minutes(first.get("start_time")))


def _shift_start_ts(now: dt.datetime, start_minutes: int) -> int:
    """今天 start_minutes 时刻的时间戳。

    班次都在一天 0–24 点内分完（结束 00:00 表示到午夜），命中的一定是今天开的班，
    不需要回退；只有历史遗留的跨天配置（18:00–06:00）才走下面这层兜底。
    """
    start_minutes = max(0, start_minutes)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidate = today_start + dt.timedelta(minutes=start_minutes)
    if candidate > now:
        candidate -= dt.timedelta(days=1)
    return int(candidate.timestamp())


def is_month_cycle(group: dict[str, Any]) -> bool:
    """排班周期是不是「一月一轮」（组员按天轮换）。

    默认 day：一天一轮，按小时累加或按固定时刻表 —— 两种都在 24 小时内换完。
    """
    return str(group.get("cycle") or "day") == "month"


def active_members(group: dict[str, Any]) -> list[dict[str, Any]]:
    """启用的成员，按登记顺序。

    一月一轮看天数，一天一轮看工时：两个字段各自代表「这台能上班」的凭据，
    混着判会让月模式的成员（hours 可能是 0）整组消失。
    """
    month = is_month_cycle(group)
    members = [
        member
        for member in group.get("members") or []
        if member.get("enabled")
        and (float(member.get("days") or 0) > 0 if month else float(member.get("hours") or 0) > 0)
    ]
    return sorted(members, key=lambda item: int(item.get("position") or 0))


def _month_layout(
    group: dict[str, Any], year: int, month: int
) -> list[tuple[dict[str, Any], int, int]]:
    """当月排班表：[(成员, 起始日, 结束日)]，日号从 1 开始、含首尾。

    前面的人各上自己填的天数，最后一位一直排到月末 —— 28/29/30/31 天的月份
    因此能自动伸缩，不用每月改配置。前面的人把整月排完了，后面的人这段就是
    空的（不返回），历史数据填了超额天数时才可能出现。
    """
    members = active_members(group)
    days_in_month = calendar.monthrange(year, month)[1]
    layout: list[tuple[dict[str, Any], int, int]] = []
    cursor = 1
    for index, member in enumerate(members):
        remaining = days_in_month - cursor + 1
        if remaining <= 0:
            break
        span = remaining if index == len(members) - 1 else int(member.get("days") or 0)
        span = min(max(span, 0), remaining)
        if span <= 0:
            continue
        layout.append((member, cursor, cursor + span - 1))
        cursor += span
    return layout


def _duty_at_month(group: dict[str, Any], now_ts: float) -> tuple[dict[str, Any], int] | None:
    """一月一轮：按天排，从当月 1 号 0 点起依次铺开，不看小时边界。

    slot 起点用「某人这段的第一天 0 点」，跨月自然产生新 slot，run 表不用改。
    """
    members = active_members(group)
    if not members:
        return None
    now = dt.datetime.fromtimestamp(now_ts, _timezone(_group_timezone(group)))
    layout = _month_layout(group, now.year, now.month)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for member, start_day, end_day in layout:
        if start_day <= now.day <= end_day:
            return member, int((month_start + dt.timedelta(days=start_day - 1)).timestamp())
    # 排班表没盖住今天（前面的人把整月排完、最后一位没位置）：让第一位兜底，
    # 至少保证有一台在跑，不要出现「谁都不当班」
    if not layout:
        return None
    member, start_day, _ = layout[0]
    return member, int((month_start + dt.timedelta(days=start_day - 1)).timestamp())


def duty_at(group: dict[str, Any], now_ts: float) -> tuple[dict[str, Any], int] | None:
    """返回 (当班成员, 该班次开始时间戳)。按 position 顺序循环轮换。"""
    members = active_members(group)
    if len(members) < 2:
        return None

    if is_month_cycle(group):
        return _duty_at_month(group, now_ts)

    if _is_timetable_group(group):
        return _duty_at_timetable(group, now_ts)

    total = sum(float(m["hours"]) for m in members) * 3600
    anchor = float(group.get("anchor_at") or group.get("created_at") or now_ts)
    elapsed = max(0.0, now_ts - anchor)
    cycle_index = int(elapsed // total)
    offset = elapsed - cycle_index * total

    accumulated = 0.0
    for member in members:
        span = float(member["hours"]) * 3600
        if offset < accumulated + span:
            return member, int(anchor + cycle_index * total + accumulated)
        accumulated += span
    last = members[-1]
    return last, int(anchor + cycle_index * total + accumulated - float(last["hours"]) * 3600)


def actual_duty(group: dict[str, Any], now_ts: float) -> tuple[dict[str, Any], int] | None:
    """此刻**真正**当班的是谁：返回 (当班成员, 该班次开始时间戳)。

    时钟只是默认答案。本班次里如果发生过故障转移（run.failover）或者人工点过
    立即换班（run.manual），实际当班是 run.duty_server_id 覆盖出来的那台。

    这个判定以前散在四个地方各写一遍，其中界面、DNS 两处只认 failover、忘了
    manual，于是手动换完班：引擎在跑 A，界面和域名却还认为当班是 B。
    统一收到这里，谁要问「现在谁当班」都必须走这个函数。
    """
    picked = duty_at(group, now_ts)
    if picked is None:
        return None
    member, shift_start = picked
    run = repo.get_run(int(group["id"]), str(shift_start)) or {}
    if not (run.get("failover") or run.get("manual")):
        return member, shift_start
    override = int(run.get("duty_server_id") or 0)
    if not override:
        return member, shift_start
    for candidate in active_members(group):
        if int(candidate["server_id"]) == override:
            return candidate, shift_start
    # 被指定的那台已经被移出组 / 删掉了：按时钟算，别拿一个不存在的当班
    return member, shift_start


def groups_of_server(server_id: int) -> list[int]:
    """这台机器被哪些**启用中**的换班组收着（DNS 巡检判断换班是否进行中用）。"""
    groups: list[int] = []
    for group in repo.list_groups():
        if not group.get("enabled"):
            continue
        for member in group.get("members") or []:
            if member.get("enabled") and int(member.get("server_id") or 0) == int(server_id):
                groups.append(int(group["id"]))
                break
    return groups


def next_switch_at(group: dict[str, Any], now_ts: float) -> float | None:
    members = active_members(group)
    if len(members) < 2:
        return None

    if is_month_cycle(group):
        return _next_switch_month(group, now_ts)

    if _is_timetable_group(group):
        return _next_switch_timetable(group, now_ts)

    total = sum(float(m["hours"]) for m in members) * 3600
    anchor = float(group.get("anchor_at") or group.get("created_at") or now_ts)
    elapsed = max(0.0, now_ts - anchor)
    cycle_index = int(elapsed // total)
    offset = elapsed - cycle_index * total
    accumulated = 0.0
    for member in members:
        accumulated += float(member["hours"]) * 3600
        if offset < accumulated:
            return anchor + cycle_index * total + accumulated
    return anchor + (cycle_index + 1) * total


def _next_switch_month(group: dict[str, Any], now_ts: float) -> float:
    """一月一轮：下一班开始 = 当前这段的最后一天结束（次日 0 点）。"""
    tz_name = _group_timezone(group)
    now = dt.datetime.fromtimestamp(now_ts, _timezone(tz_name))
    layout = _month_layout(group, now.year, now.month)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _member, start_day, end_day in layout:
        if start_day <= now.day <= end_day:
            # end_day 是月末时，加一天正好落到下月 1 号 0 点
            return int((month_start + dt.timedelta(days=end_day)).timestamp())
    # 兜底：下个月 1 号 0 点
    return int(
        (month_start + dt.timedelta(days=calendar.monthrange(now.year, now.month)[1])).timestamp()
    )


def _next_switch_timetable(group: dict[str, Any], now_ts: float) -> float:
    """固定时刻表模式：下一班开始时刻 = 当前班次的结束时刻。"""
    members = active_members(group)
    tz_name = _group_timezone(group)
    now = dt.datetime.fromtimestamp(now_ts, _timezone(tz_name))
    minute_of_day = now.hour * 60 + now.minute

    for member in members:
        start = _hhmm_to_minutes(member.get("start_time"))
        end = _hhmm_to_minutes(member.get("end_time"))
        if start < 0 or end < 0:
            continue
        if end > start:
            if start <= minute_of_day < end:
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                return int((today_start + dt.timedelta(minutes=end)).timestamp())
        else:
            if minute_of_day >= start or minute_of_day < end:
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                candidate = today_start + dt.timedelta(minutes=end)
                # 跨天班次结束在今天（比如现在 20:00 属于 18:00–06:00，结束在明天 06:00）
                if candidate <= now:
                    candidate += dt.timedelta(days=1)
                return int(candidate.timestamp())
    # 兜底
    return now_ts + 3600


def current_member_index(group: dict[str, Any], now_ts: float) -> int:
    members = active_members(group)
    picked = actual_duty(group, now_ts)
    if picked is None:
        return -1
    target = int(picked[0].get("server_id") or 0)
    for index, member in enumerate(members):
        if int(member.get("server_id") or 0) == target:
            return index
    return -1


def _timezone(name: str) -> dt.tzinfo:
    """时区名 → tzinfo。取不到就回落 +08:00，并大声警告一次。

    回落静默是最坑的：容器里没装 tzdata 时 ZoneInfo 会抛 ZoneInfoNotFoundError，
    看着一切正常，实际所有时刻都按北京时间算。
    """
    global _TZ_WARNED
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        if not _TZ_WARNED:
            _TZ_WARNED = True
            log.warning("时区 %s 不可用（缺 tzdata？），临时按 UTC+08:00 计算", name)
        return dt.timezone(dt.timedelta(hours=8))


def timezone_name() -> str:
    """系统设置里的时区。duty_at 被界面 / DNS / 手动换班频繁调用，这里做 5 秒缓存。"""
    now = time.time()
    if _TZ_CACHE["name"] and now - float(_TZ_CACHE["at"]) < 5:
        return str(_TZ_CACHE["name"])
    try:
        name = str(repo.get_setting("timezone") or "").strip() or "Asia/Shanghai"
    except Exception:  # noqa: BLE001 - 读设置失败不该让轮换计算崩掉
        name = "Asia/Shanghai"
    _TZ_CACHE["at"] = now
    _TZ_CACHE["name"] = name
    return name


def _group_timezone(group: dict[str, Any]) -> str:
    """组上显式带的时区优先（引擎 reconcile 时塞的），否则读系统设置。

    以前只有引擎塞了 `_timezone`，界面当班高亮、DNS 指向判定、手动换班拿到的
    都是裸字典，一律回落到硬编码的 +08:00 —— 时区配成别的就和引擎错位。
    """
    return str(group.get("_timezone") or "").strip() or timezone_name()


def _hours_of(group: dict[str, Any], server: dict[str, Any]) -> float:
    """这台机器在组里设的班次时长；读不到就按 12 小时，别让文案变成问号。"""
    for member in group.get("members") or []:
        if int(member.get("server_id") or 0) == int(server.get("id") or 0):
            return float(member.get("hours") or 12)
    return 12.0


def _member_of(group: dict[str, Any], server_id: int) -> dict[str, Any]:
    """按 server_id 找组成员；找不到返回空 dict。"""
    for member in group.get("members") or []:
        if int(member.get("server_id") or 0) == int(server_id):
            return member
    return {}


def _shift_span(
    group: dict[str, Any],
    duty: dict[str, Any],
    config: dict[str, str],
    now_ts: float | None = None,
) -> str:
    """当前班次文案：一月一轮写日期区间，一天一轮优先「班次名称（起–止）」。

    一月一轮按「今天是哪一段」写，不看当班是谁：手动换班 / 故障转移会把别的
    成员顶到这一段来，按当班成员去找他自己的排班段，10 月 5 号就会报出
    「10 月 16–31 日」，与今天的日期自相矛盾。当班是谁正文里已经写清楚了。
    """
    member = _member_of(group, int(duty.get("id") or 0))
    slot_name = member.get("slot_name") or ""
    if is_month_cycle(group):
        # 按月轮换没有小时边界，只报本段覆盖哪几天
        stamp = int(now_ts if now_ts is not None else time.time())
        now = dt.datetime.fromtimestamp(stamp, _timezone(_group_timezone(group)))
        for item, start_day, end_day in _month_layout(group, now.year, now.month):
            if start_day <= now.day <= end_day:
                # 段的名字属于「这一段」，不是当班那个人
                name = str(item.get("slot_name") or "").strip()
                span = f"{now.month} 月 {start_day}–{end_day} 日"
                return f"{name}（{span}）" if name else span
        return slot_name
    start = member.get("start_time") or ""
    end = member.get("end_time") or ""
    if slot_name or start or end:
        return messages.shift_period(slot_name, start, end)
    # 老 hours 累加模式：没有时刻表，退回「机名（N 小时）」式表达
    hours = float(member.get("hours") or _hours_of(group, duty))
    label = str(duty.get("label") or "")
    return f"{label}（{hours:g} 小时）"


def member_span_label(group: dict[str, Any], member: dict[str, Any], now_ts: float) -> str:
    """成员「这一班」在人眼里的写法，给界面用，不带班次名称。

    - 一月一轮 → `1–15 日`（本月这一段覆盖哪几天）
    - 固定时刻表 → `08:00–20:00`
    - 其余（老 hours 累加）→ 空串，界面自己退回「N 小时」

    界面三处（换班页成员行、轮换顺序、概览组卡片）都读这一个字段，
    免得前端各算一套，跟引擎排出来的段对不上。
    """
    if is_month_cycle(group):
        now = dt.datetime.fromtimestamp(now_ts, _timezone(_group_timezone(group)))
        target = int(member.get("server_id") or 0)
        for item, start_day, end_day in _month_layout(group, now.year, now.month):
            if int(item.get("server_id") or 0) == target:
                return f"{start_day}–{end_day} 日"
        return ""
    start = str(member.get("start_time") or "")
    end = str(member.get("end_time") or "")
    if start and end:
        # 复用通知那套写法：结束 00:00 显示成 24:00，不写「次日」
        return messages.shift_period("", start, end)
    return ""
