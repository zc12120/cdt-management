"""Pure decisions over explicit observations; no network, storage, or clock reads."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StartDecision:
    allowed: bool
    reason: str = ""


def traffic_unavailable(server: dict, now: float, max_age: int) -> bool:
    """Cost protection applies to running instances as well as new starts.

    Successful first-empty readings remain valid under the existing resolver.
    An API error never renews the last successful reading's grace period.
    """
    if not server.get("traffic_error") or server.get("traffic_policy") == "availability":
        return False
    checked = int(server.get("traffic_checked_at") or 0)
    return server.get("traffic_used") is None or not 0 <= now - checked <= max_age


def may_start(server: dict, now: float, max_age: int) -> StartDecision:
    used = server.get("traffic_used")
    threshold = float(server.get("threshold_gb") or 0)
    if used is not None and threshold > 0 and float(used) >= threshold:
        return StartDecision(False, "流量达到阈值，启动被暂停")
    if not server.get("traffic_error"):
        return StartDecision(True)
    if server.get("traffic_policy") == "availability":
        return StartDecision(True, "流量未知，按可用性优先策略继续")
    if traffic_unavailable(server, now, max_age):
        return StartDecision(False, "流量未知或读数过期，费用保护策略暂停启动")
    return StartDecision(True, "流量接口异常，暂用有效期内的已知读数")
