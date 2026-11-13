"""社区随机分配的推广效果分析（仅评价服务流程，不涉及个体病情与医疗建议）。

入口：
    python3 -m colorectal_cascade.outreach_trial --input <trial_json> \
        --database <cascade_db> --output <effects_json>

分析原则：
  * 主要结论按原随机分配（意向性，ITT）：未响应者仍属于原分配组。
  * 随机单位是社区而不是个人，推断在社区级进行。
  * 观察窗口未关闭者与等待结果者记为待定，不计入分母也不计为失败。
  * 截止时刻（as_of）之后才可用的事实暂不采用。
  * 参与者亚群中的比例只作描述，不冒充随机化效果。
  * 跨社区共用服务点容量视为适用性限制，不事后删除社区。
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from colorectal_cascade.runtime import DomainError, fingerprint

METHOD_VERSION = "outreach-trial-v1"
STAGE_ORDER = ("attended", "sample", "referral")
STAGE_LABELS = {"attended": "到场", "sample": "样本完成", "referral": "转介完成"}
ENDPOINT_ALIASES = {
    "attendance": "attended",
    "attended": "attended",
    "sample": "sample",
    "sample_completion": "sample",
    "referral": "referral",
    "referral_completion": "referral",
    "referral_completion_per_invitee": "referral",
}
ALPHA = 0.05
# 可枚举的重随机化分配总数上限；超过则改用固定种子蒙特卡洛。
EXACT_ENUMERATION_LIMIT = 131072
MONTE_CARLO_DRAWS = 20000
MONTE_CARLO_SEED = 20261113
# 每个分配组至少需要的可评估社区数，否则认为证据不足。
MIN_COMMUNITIES_PER_ARM = 2
TOL = 1e-9


# ---------------------------------------------------------------------------
# 输入读取与校验
# ---------------------------------------------------------------------------

def parse_instant(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"{field} 缺失或不是字符串")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise DomainError(f"{field} 不是合法 ISO 8601 时间: {value}") from None
    if moment.tzinfo is None:
        raise DomainError(f"{field} 缺少时区: {value}")
    return moment.astimezone(timezone.utc)


def iso_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class TrialData:
    """校验后的分析输入。只保留服务流程字段，不含任何健康信息。"""

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.as_of_text = raw["as_of"]
        self.as_of = parse_instant(raw["as_of"], "as_of")
        study = raw.get("study") or {}
        horizon = study.get("horizon_days", 30)
        if not isinstance(horizon, (int, float)) or isinstance(horizon, bool) or horizon <= 0:
            raise DomainError("study.horizon_days 必须是正数")
        self.horizon = timedelta(days=float(horizon))
        self.horizon_days = horizon
        primary_raw = str(study.get("primary_endpoint", "attendance"))
        self.primary_stage = ENDPOINT_ALIASES.get(primary_raw, primary_raw)
        if self.primary_stage not in STAGE_ORDER:
            raise DomainError(f"无法识别的主要终点: {primary_raw}")
        self.operational_outcome = str(
            study.get("operational_outcome", "referral_completion_per_invitee"))
        self.randomization_unit = str(study.get("randomization_unit", "community"))
        self.baseline_case_id = raw.get("baseline_case_id")
        self.source_versions = raw.get("source_versions") or {}

        # 社区与分配
        self.communities: dict[str, dict[str, Any]] = {}
        for entry in raw["communities"]:
            cid = entry.get("community_id")
            if not cid or cid in self.communities:
                raise DomainError(f"community_id 缺失或重复: {cid}")
            arm = entry.get("assigned_arm")
            if not isinstance(arm, str) or not arm:
                raise DomainError(f"社区 {cid} 缺少 assigned_arm")
            prob = entry.get("inclusion_probability", 0.5)
            if not isinstance(prob, (int, float)) or isinstance(prob, bool) or not 0 < prob <= 1:
                raise DomainError(f"社区 {cid} 的 inclusion_probability 非法")
            self.communities[cid] = {
                "arm": arm,
                "block": entry.get("randomization_block") or "block:default",
                "center": entry.get("shared_center"),
                "inclusion_probability": float(prob),
                "probability_missing": "inclusion_probability" not in entry,
            }
        arms = sorted({c["arm"] for c in self.communities.values()})
        if len(arms) != 2:
            raise DomainError(f"需要恰好两个分配组，实际为: {arms}")
        self.control_arm = "usual" if "usual" in arms else arms[0]
        self.treat_arm = "extra" if "extra" in arms else (
            arms[1] if arms[0] == self.control_arm else arms[0])
        self.blocks: dict[str, list[str]] = {}
        self.centers: dict[str, list[str]] = {}
        for cid, meta in self.communities.items():
            self.blocks.setdefault(meta["block"], []).append(cid)
            if meta["center"]:
                self.centers.setdefault(meta["center"], []).append(cid)

        # 受邀名单
        self.invitees: dict[str, dict[str, Any]] = {}
        for entry in raw["invitees"]:
            pid = entry.get("person_id")
            if not pid or pid in self.invitees:
                raise DomainError(f"person_id 缺失或重复: {pid}")
            cid = entry.get("community_id")
            if cid not in self.communities:
                raise DomainError(f"受邀者 {pid} 指向未知社区: {cid}")
            self.invitees[pid] = {
                "community": cid,
                "invited_at": parse_instant(entry.get("invited_at"), f"{pid}.invited_at"),
                "invited_at_text": entry.get("invited_at"),
                "reminder_received": bool(entry.get("reminder_received")),
            }

        # 环节记录：截止后才可用（或发生）的事实先隔离，不进入本次分析。
        self.events: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self.deferred_facts: list[dict[str, Any]] = []
        self.ignored_stages: set[str] = set()
        for entry in raw.get("stages", []):
            pid = entry.get("person_id")
            if pid not in self.invitees:
                raise DomainError(f"环节记录指向未知受邀者: {pid}")
            stage = entry.get("stage")
            if stage not in STAGE_ORDER:
                self.ignored_stages.add(str(stage))
                continue
            occurred = parse_instant(entry.get("occurred_at"), f"{pid}.{stage}.occurred_at")
            available = parse_instant(entry.get("available_at"), f"{pid}.{stage}.available_at")
            record = {
                "person_id": pid,
                "stage": stage,
                "occurred_at": occurred,
                "available_at": available,
                "occurred_at_text": entry.get("occurred_at"),
                "available_at_text": entry.get("available_at"),
                "outcome": str(entry.get("outcome", "")).strip().lower(),
            }
            if available > self.as_of or occurred > self.as_of:
                self.deferred_facts.append(record)
            else:
                self.events.setdefault(pid, {}).setdefault(stage, []).append(record)

        # 转介资格：只对确有后续资料者有定义；晚到的资格记录同样隔离。
        self.eligibility: dict[str, bool] = {}
        self.eligibility_deferred = 0
        for entry in raw.get("referral_eligibility", []):
            pid = entry.get("person_id")
            if pid not in self.invitees:
                raise DomainError(f"转介资格记录指向未知受邀者: {pid}")
            available = parse_instant(entry.get("available_at"), f"{pid}.eligibility.available_at")
            if available > self.as_of:
                self.eligibility_deferred += 1
                continue
            self.eligibility[pid] = bool(entry.get("eligible"))


def load_trial_input(path: str | Path) -> TrialData:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DomainError(f"输入文件不存在: {path}") from None
    except json.JSONDecodeError as exc:
        raise DomainError(f"输入文件不是合法 JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise DomainError("输入必须是 JSON 对象")
    if raw.get("schema_version") != 1:
        raise DomainError("schema_version 必须为 1")
    for key in ("as_of", "communities", "invitees"):
        if key not in raw:
            raise DomainError(f"输入缺少字段: {key}")
    if not raw["communities"]:
        raise DomainError("communities 不能为空")
    if not raw["invitees"]:
        raise DomainError("invitees 不能为空")
    return TrialData(raw)


# ---------------------------------------------------------------------------
# 随访成熟度分类：成功 / 失败 / 待定（待定绝不是失败）
# ---------------------------------------------------------------------------

def classify_person_stage(data: TrialData, pid: str, stage: str) -> str:
    invited = data.invitees[pid]["invited_at"]
    deadline = invited + data.horizon
    recorded = [ev for ev in data.events.get(pid, {}).get(stage, [])
                if ev["occurred_at"] <= deadline]
    if any(ev["outcome"] == "complete" for ev in recorded):
        return "success"
    if recorded:
        return "failure"  # 窗口内已有明确的非完成结果
    if deadline <= data.as_of:
        return "failure"  # 窗口已关闭仍无记录
    return "pending"      # 窗口未关闭，等待中


def classify_all(data: TrialData) -> dict[str, dict[str, str]]:
    return {stage: {pid: classify_person_stage(data, pid, stage) for pid in data.invitees}
            for stage in STAGE_ORDER}


# ---------------------------------------------------------------------------
# 重随机化推断（组级）：区组内保持分配数不变，可枚举时精确枚举
# ---------------------------------------------------------------------------

def build_block_schemes(data: TrialData) -> list[dict[str, Any]]:
    """每个区组列出所有“处理组社区集合”的候选及其设计权重。"""
    schemes = []
    for block_id in sorted(data.blocks):
        members = sorted(data.blocks[block_id])
        k = sum(1 for cid in members
                if data.communities[cid]["arm"] == data.treat_arm)
        combos = []
        for chosen in itertools.combinations(members, k):
            chosen_set = frozenset(chosen)
            weight = 1.0
            for cid in members:
                prob = data.communities[cid]["inclusion_probability"]
                weight *= prob if cid in chosen_set else (1.0 - prob)
            combos.append((chosen_set, weight))
        schemes.append({"block": block_id, "combos": combos, "varies": len(combos) > 1})
    return schemes


def enumerate_assignments(schemes: list[dict[str, Any]]
                          ) -> tuple[list[tuple[frozenset, float]], str, int]:
    total = 1
    for scheme in schemes:
        total *= len(scheme["combos"])
    if total <= EXACT_ENUMERATION_LIMIT:
        perms = []
        for picks in itertools.product(*(s["combos"] for s in schemes)):
            extra = frozenset().union(*(pick[0] for pick in picks))
            weight = 1.0
            for pick in picks:
                weight *= pick[1]
            perms.append((extra, weight))
        return perms, "exact_block_rerandomization", total
    rng = random.Random(MONTE_CARLO_SEED)
    perms = []
    for _ in range(MONTE_CARLO_DRAWS):
        extra: set[str] = set()
        for scheme in schemes:
            weights = [w for _, w in scheme["combos"]]
            picked = rng.choices(scheme["combos"], weights=weights, k=1)[0]
            extra |= picked[0]
        perms.append((frozenset(extra), 1.0))
    return perms, "monte_carlo_block_rerandomization", MONTE_CARLO_DRAWS


class Rerandomization:
    """对“社区比例的两组均值差”做重随机化检验与区间反演。

    统计量：T = 处理组社区比例均值 − 对照组社区比例均值。
    原假设下，区组内哪些社区分到处理组是随机的（保持每组分配数不变），
    据此得到 T 的精确（或蒙特卡洛）分布；不假设正态，零事件无需校正。
    区间通过对“比例平移 δ”的检验反演得到。
    """

    def __init__(self, proportions: dict[str, float], arms: dict[str, str],
                 treat_arm: str, perms: list[tuple[frozenset, float]],
                 exact: bool):
        self.cids = sorted(proportions)
        self.props = [proportions[c] for c in self.cids]
        self.treat_flags = [arms[c] == treat_arm for c in self.cids]
        self.perms = perms
        self.exact = exact
        self.n = len(self.cids)
        self.total_sum = sum(self.props)
        self.total_treated = sum(self.treat_flags)
        self.t_obs = self._statistic(self.treat_flags)
        # 预计算每种分配下的分子，供检验与区间反演复用
        self.precomputed = []
        for extra_set, weight in perms:
            flags = [cid in extra_set for cid in self.cids]
            n_e = sum(flags)
            if n_e == 0 or n_e == self.n:
                continue  # 某一组为空时统计量无定义，跳过该分配
            sum_e = sum(p for p, f in zip(self.props, flags) if f)
            z_e = sum(1 for f, t in zip(flags, self.treat_flags) if f and t)
            self.precomputed.append((n_e, sum_e, z_e, weight))
        self.skipped = len(perms) - len(self.precomputed)

    def _statistic(self, flags: list[bool]) -> float | None:
        n_e = sum(flags)
        if n_e == 0 or n_e == len(flags):
            return None
        sum_e = sum(p for p, f in zip(self.props, flags) if f)
        sum_c = self.total_sum - sum_e
        return sum_e / n_e - sum_c / (len(flags) - n_e)

    def _stat_at(self, n_e: int, sum_e: float, z_e: int, delta: float) -> float:
        """平移 δ 后、某一重随机化分配下的统计量。"""
        n_c = self.n - n_e
        adj_e = sum_e - delta * z_e
        adj_c = (self.total_sum - sum_e) - delta * (self.total_treated - z_e)
        return adj_e / n_e - adj_c / n_c

    def p_value(self, delta: float = 0.0) -> float:
        """检验“处理使社区比例平移 delta”的双侧 p 值。"""
        if self.t_obs is None or not self.precomputed:
            return 1.0
        t_shifted = self.t_obs - delta
        exceed = 0.0
        weight = 0.0
        for n_e, sum_e, z_e, w in self.precomputed:
            t_star = self._stat_at(n_e, sum_e, z_e, delta)
            if abs(t_star) >= abs(t_shifted) - TOL:
                exceed += w
            weight += w
        if self.exact:
            return exceed / weight if weight else 1.0
        return (1.0 + exceed) / (1.0 + len(self.precomputed))

    def confidence_interval(self, alpha: float = ALPHA) -> tuple[float, float, bool]:
        """反演检验得到 (1-alpha) 区间；返回 (下界, 上界, 是否触及搜索边界)。"""
        if self.t_obs is None or not self.precomputed:
            return (0.0, 0.0, True)
        lo_bound, hi_bound = -1.0, 1.0

        def accepted(delta: float) -> bool:
            return self.p_value(delta) > alpha

        def find_edge(start: float, end: float) -> float:
            lo, hi = start, end
            for _ in range(60):
                mid = (lo + hi) / 2.0
                if accepted(mid):
                    lo = mid
                else:
                    hi = mid
            return lo

        touches = False
        if accepted(lo_bound):
            lower, touches = lo_bound, True
        else:
            lower = find_edge(self.t_obs, lo_bound) if accepted(self.t_obs) else lo_bound
        if accepted(hi_bound):
            upper, touches = hi_bound, True
        else:
            upper = find_edge(self.t_obs, hi_bound) if accepted(self.t_obs) else hi_bound
        return (lower, upper, touches)


# ---------------------------------------------------------------------------
# 多终点：固定顺序检验（按研究声明的主要终点）+ Holm 敏感性对照
# ---------------------------------------------------------------------------

def holm_adjust(raw_p: dict[str, float]) -> dict[str, float]:
    ordered = sorted(raw_p.items(), key=lambda kv: kv[1])
    adjusted: dict[str, float] = {}
    running = 0.0
    m = len(ordered)
    for index, (stage, p) in enumerate(ordered):
        running = max(running, min(1.0, (m - index) * p))
        adjusted[stage] = running
    return adjusted


def gatekeeping_sequence(data: TrialData) -> list[str]:
    return [data.primary_stage] + [s for s in STAGE_ORDER if s != data.primary_stage]


# ---------------------------------------------------------------------------
# 描述性（非随机化）对比：只对特定人群有定义
# ---------------------------------------------------------------------------

def conditional_descriptives(data: TrialData,
                             classes: dict[str, dict[str, str]]) -> dict[str, Any]:
    def arm_of(pid: str) -> str:
        return data.communities[data.invitees[pid]["community"]]["arm"]

    # 转介资格只对确有后续资料者有定义 → 条件完成率仅作描述
    eligible_eval: dict[str, list[str]] = {data.treat_arm: [], data.control_arm: []}
    eligible_pending: dict[str, int] = {data.treat_arm: 0, data.control_arm: 0}
    for pid, flag in data.eligibility.items():
        if not flag:
            continue
        arm = arm_of(pid)
        status = classes["referral"][pid]
        if status == "pending":
            eligible_pending[arm] += 1
        else:
            eligible_eval[arm].append(status)
    referral_among_eligible = {
        arm: {
            "eligible_evaluable": len(states),
            "completed": sum(1 for s in states if s == "success"),
            "rate": round(sum(1 for s in states if s == "success") / len(states), 6)
                    if states else None,
            "pending": eligible_pending[arm],
        }
        for arm, states in eligible_eval.items()
    }

    # 到场者中的样本完成（只对到场者有定义）
    sample_among_attended: dict[str, Any] = {}
    for arm in (data.treat_arm, data.control_arm):
        states = [classes["sample"][pid] for pid in data.invitees
                  if arm_of(pid) == arm and classes["attended"][pid] == "success"
                  and classes["sample"][pid] != "pending"]
        done = sum(1 for s in states if s == "success")
        sample_among_attended[arm] = {
            "attended_evaluable": len(states),
            "completed": done,
            "rate": round(done / len(states), 6) if states else None,
        }

    # 按实际是否收到提醒分组（与分配无关的观察对比）
    by_receipt: dict[str, Any] = {}
    for received in (True, False):
        states = [classes["attended"][pid] for pid, inv in data.invitees.items()
                  if inv["reminder_received"] == received
                  and classes["attended"][pid] != "pending"]
        done = sum(1 for s in states if s == "success")
        by_receipt["reminded" if received else "not_reminded"] = {
            "evaluable": len(states),
            "attended": done,
            "rate": round(done / len(states), 6) if states else None,
        }

    return {
        "note": "以下比例只在特定人群上有定义，属于观察性描述，不能解释为随机分配的效果。",
        "referral_completion_among_eligible": {
            "defined_for": "有后续资料且转介资格已知的受邀者",
            "randomized": False,
            "arms": referral_among_eligible,
        },
        "sample_completion_among_attended": {
            "defined_for": "已到场且样本环节可判定的受邀者",
            "randomized": False,
            "arms": sample_among_attended,
        },
        "attendance_by_actual_reminder_receipt": {
            "defined_for": "全部受邀者（按实际收到提醒分组，与分配无关）",
            "randomized": False,
            "groups": by_receipt,
        },
    }


# ---------------------------------------------------------------------------
# 数据库对照：与既有 cohort / referral 记录核对
# ---------------------------------------------------------------------------

def crosscheck_database(db_path: str | Path, baseline_case_id: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "database": str(db_path),
        "baseline_case_id": baseline_case_id,
    }
    if not baseline_case_id:
        result.update(status="no_baseline_case_id",
                      note="输入未提供 baseline_case_id，跳过与既有级联运行的对照。")
        return result
    if not Path(db_path).exists():
        result.update(status="database_unavailable",
                      note="级联数据库不存在，无法对照既有 cohort/referral 记录；"
                           "分析仅基于本次输入。")
        return result
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        result.update(status="database_unavailable", note=f"无法打开数据库: {exc}")
        return result
    try:
        tables = {row[0] for row in
                  con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"runs", "events"} <= tables:
            result.update(status="missing_cascade_tables",
                          note="数据库中没有级联运行表，无法对照。")
            return result
        runs = con.execute(
            "SELECT run_id, state, revision FROM runs WHERE case_id=? "
            "ORDER BY updated_at DESC", (str(baseline_case_id),)).fetchall()
        if not runs:
            result.update(status="baseline_not_found",
                          note=f"数据库中找不到 baseline_case_id={baseline_case_id} 的运行。")
            return result
        run_id, state, revision = runs[0]
        events = con.execute(
            "SELECT stage, kind, COUNT(*) FROM events WHERE run_id=? GROUP BY stage, kind",
            (run_id,)).fetchall()
        stage_kinds = {(stage, kind): count for stage, kind, count in events}
        cohort_done = stage_kinds.get(("cohort_invite", "stage_completed"), 0) > 0
        referral_done = stage_kinds.get(("referral_loop", "stage_completed"), 0) > 0
        cohort_events = sum(c for (s, _), c in stage_kinds.items() if s == "cohort_invite")
        referral_events = sum(c for (s, _), c in stage_kinds.items() if s == "referral_loop")
        consistent = state == "completed" and cohort_done and referral_done
        result.update(
            status="consistent" if consistent else "incomplete",
            run_state=state,
            run_revision=revision,
            runs_for_case=len(runs),
            cohort_stage_completed=cohort_done,
            referral_stage_completed=referral_done,
            cohort_event_count=cohort_events,
            referral_event_count=referral_events,
            note=("既有运行的 cohort_invite 与 referral_loop 阶段均已完成，"
                  "与分析输入的环节记录口径一致；基线运行与分析人群粒度不同，"
                  "不做数值合并。" if consistent else
                  "既有运行未完整走完 cohort_invite / referral_loop 阶段，"
                  "请确认级联流程状态后再解读分析结果。"),
        )
        return result
    except sqlite3.Error as exc:
        result.update(status="database_unavailable", note=f"读取数据库失败: {exc}")
        return result
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 报告组装
# ---------------------------------------------------------------------------

def r6(value: float | None) -> float | None:
    if value is None:
        return None
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0 else rounded


def pct(value: float) -> str:
    rounded = round(value * 100, 1)
    return f"{0.0 if rounded == 0 else rounded:.1f}"


def assignment_checks(data: TrialData, crosscheck: dict[str, Any]) -> dict[str, Any]:
    sizes = {}
    for inv in data.invitees.values():
        sizes[inv["community"]] = sizes.get(inv["community"], 0) + 1
    per_community = []
    extra_no_reminder, usual_with_reminder, extra_partial = [], [], []
    for cid in sorted(data.communities):
        members = [inv for inv in data.invitees.values() if inv["community"] == cid]
        reminded = sum(1 for inv in members if inv["reminder_received"])
        share = reminded / len(members) if members else 0.0
        arm = data.communities[cid]["arm"]
        if arm == data.treat_arm and share == 0:
            extra_no_reminder.append(cid)
        elif arm == data.treat_arm and share < 1:
            extra_partial.append(cid)
        if arm == data.control_arm and share > 0:
            usual_with_reminder.append(cid)
        per_community.append({
            "community_id": cid,
            "assigned_arm": arm,
            "invited": len(members),
            "reminded": reminded,
            "reminder_share": r6(share),
        })
    block_detail = []
    non_varying = []
    for block_id in sorted(data.blocks):
        members = sorted(data.blocks[block_id])
        arms_here = [data.communities[c]["arm"] for c in members]
        detail = {"block_id": block_id, "communities": len(members)}
        for arm in sorted(set(arms_here)):
            detail[arm] = arms_here.count(arm)
        if len(set(arms_here)) < 2:
            non_varying.append(block_id)
        block_detail.append(detail)
    probs = [c["inclusion_probability"] for c in data.communities.values()]
    counts = {arm: sum(1 for c in data.communities.values() if c["arm"] == arm)
              for arm in (data.control_arm, data.treat_arm)}
    return {
        "randomization_unit": data.randomization_unit,
        "community_count": len(data.communities),
        "arms": {"control": data.control_arm, "treat": data.treat_arm,
                 "community_counts": counts},
        "blocks": {
            "count": len(data.blocks),
            "all_blocks_have_both_arms": not non_varying,
            "non_varying_blocks": non_varying,
            "detail": block_detail,
        },
        "inclusion_probability": {
            "constant": len(set(probs)) == 1,
            "values": sorted(set(probs)),
            "note": "各社区纳入概率相同，重随机化各候选分配等权。"
                    if len(set(probs)) == 1 else
                    "纳入概率不同，重随机化按设计概率加权。",
        },
        "invitees": {
            "total": len(data.invitees),
            "per_community_min": min(sizes.values()),
            "per_community_max": max(sizes.values()),
            "unequal_sizes": len(set(sizes.values())) > 1,
        },
        "delivery_fidelity": {
            "by_community": per_community,
            "assigned_extra_without_any_reminder": extra_no_reminder,
            "assigned_extra_partial_reminder": extra_partial,
            "assigned_usual_with_any_reminder": usual_with_reminder,
            "note": "实际收到提醒与随机分配不完全一致。主要结论按原分配（意向性）："
                    "未收到或未响应提醒的人仍计入原分配组；按实际收到的对比仅作描述，"
                    "不能解释为随机化效果。",
        },
        "baseline_crosscheck": crosscheck,
    }


def followup_support(data: TrialData,
                     classes: dict[str, dict[str, str]]) -> dict[str, Any]:
    stage_support: dict[str, Any] = {}
    immature: dict[str, list[str]] = {}
    for stage in STAGE_ORDER:
        counts = {"success": 0, "failure": 0, "pending": 0}
        by_arm: dict[str, dict[str, int]] = {
            arm: {"evaluable": 0, "success": 0, "failure": 0, "pending": 0}
            for arm in (data.control_arm, data.treat_arm)}
        waves: dict[str, dict[str, Any]] = {}
        pending_communities: set[str] = set()
        for pid, status in classes[stage].items():
            counts[status] += 1
            arm = data.communities[data.invitees[pid]["community"]]["arm"]
            by_arm[arm][status] += 1
            if status != "pending":
                by_arm[arm]["evaluable"] += 1
            else:
                pending_communities.add(data.invitees[pid]["community"])
                invited_text = data.invitees[pid]["invited_at_text"]
                wave = waves.setdefault(invited_text, {
                    "invited_at": invited_text,
                    "pending": 0,
                    "matures_at": iso_text(data.invitees[pid]["invited_at"] + data.horizon),
                })
                wave["pending"] += 1
        deferred = [f for f in data.deferred_facts if f["stage"] == stage]
        wave_list = sorted(waves.values(), key=lambda w: w["invited_at"])
        stage_support[stage] = {
            "evaluable": counts["success"] + counts["failure"],
            "success": counts["success"],
            "failure": counts["failure"],
            "pending": counts["pending"],
            "deferred_facts": len(deferred),
            "by_arm": by_arm,
            "pending_by_wave": wave_list,
            "next_maturity": wave_list[0]["matures_at"] if wave_list else None,
        }
        immature[stage] = sorted(pending_communities)
    deferred_facts = [{
        "person_id": f["person_id"],
        "stage": f["stage"],
        "occurred_at": f["occurred_at_text"],
        "available_at": f["available_at_text"],
    } for f in sorted(data.deferred_facts,
                      key=lambda f: (f["person_id"], f["stage"]))]
    notes = ["观察窗口未关闭的受邀者记为待定：不计入分母，也不计为失败。"]
    if data.deferred_facts:
        people = sorted({f["person_id"] for f in data.deferred_facts})
        notes.append(
            f"{len(data.deferred_facts)} 条环节记录（涉及 {len(people)} 人）在截止时刻"
            f" {data.as_of_text} 之后才可用，本次未采用；后续采用时结论可能更新。")
    if data.eligibility_deferred:
        notes.append(f"{data.eligibility_deferred} 条转介资格记录在截止后才可用，本次未采用。")
    referral_pending = stage_support["referral"]["pending"]
    if referral_pending:
        notes.append(
            f"转介完成尚有 {referral_pending} 人在观察窗口内（最近批次 "
            f"{stage_support['referral']['next_maturity']} 成熟），"
            "这些社区的转介结论暂不能判断，当前估计仅基于随访已成熟的受邀者。")
    return {
        "as_of": data.as_of_text,
        "horizon_days": data.horizon_days,
        "stage_support": stage_support,
        "immature_communities": immature,
        "deferred_facts": deferred_facts,
        "notes": notes,
    }


def interference_section(data: TrialData) -> dict[str, Any]:
    centers = []
    mixed = 0
    for center_id in sorted(data.centers):
        members = sorted(data.centers[center_id])
        arms = sorted({data.communities[c]["arm"] for c in members})
        mixes = len(arms) > 1
        mixed += 1 if mixes else 0
        centers.append({
            "center_id": center_id,
            "communities": [{"community_id": c,
                             "assigned_arm": data.communities[c]["arm"]}
                            for c in members],
            "mixes_arms": mixes,
        })
    if mixed:
        assessment = (f"{mixed}/{len(centers)} 个共享服务点同时服务两个分配组，"
                      "容量共享可能带来组间干扰（如排队与资源挤占）；"
                      "估计反映共享容量现状下的效果，外推到独立容量场景需谨慎。")
    else:
        assessment = "未发现同时服务两个分配组的共享服务点。"
    return {
        "shared_centers": centers,
        "centers_total": len(centers),
        "centers_mixing_arms": mixed,
        "assessment": assessment,
        "handling": "作为适用性限制报告；未因共用容量而删除任何社区。",
        "communities_retained": len(data.communities),
    }


def interpret_effect(label: str, estimate: float, ci: tuple[float, float],
                 verdict: str, raw_p: float | None) -> str:
    if estimate > 0:
        diff = f"比对照组高 {pct(estimate)} 个百分点"
    elif estimate < 0:
        diff = f"比对照组低 {pct(-estimate)} 个百分点"
    else:
        diff = "与对照组相差 0.0 个百分点"
    base = (f"按原随机分配比较，提醒组社区平均{label}率{diff}"
            f"（95% 区间 [{pct(ci[0])}, {pct(ci[1])}] 个百分点"
            + (f"，重随机化双侧 p={r6(raw_p)}" if raw_p is not None else "")
            + "）。")
    tail = {
        "increase_detected": "按固定顺序检验达到 0.05 水准，方向为上升；"
                             "社区数量少，结论仍受设计规模限制。",
        "decrease_detected": "按固定顺序检验达到 0.05 水准，方向为下降；"
                             "社区数量少，结论仍受设计规模限制。",
        "inconclusive": "现有数据不能排除偶然差异，建议等待更多社区或更长随访。",
        "insufficient_data": "可评估社区不足，暂不能判断。",
        "not_reached_in_sequence": "前序终点未通过固定顺序检验，本终点只报告估计与区间，"
                                   "不作显著性结论。",
    }[verdict]
    return base + tail


def stage_effects_section(data: TrialData,
                          classes: dict[str, dict[str, str]],
                          perms: list[tuple[frozenset, float]],
                          method_name: str,
                          perm_total: int) -> dict[str, Any]:
    arms = {cid: meta["arm"] for cid, meta in data.communities.items()}
    sequence = gatekeeping_sequence(data)
    any_varying_block = any(s["varies"] for s in build_block_schemes(data))
    raw_p: dict[str, float] = {}
    per_stage: dict[str, Any] = {}
    stage_stats: dict[str, dict[str, Any]] = {}

    for stage in STAGE_ORDER:
        table: dict[str, dict[str, Any]] = {}
        for cid in sorted(data.communities):
            members = [pid for pid, inv in data.invitees.items()
                       if inv["community"] == cid]
            states = [classes[stage][pid] for pid in members]
            success = sum(1 for s in states if s == "success")
            failure = sum(1 for s in states if s == "failure")
            pending = sum(1 for s in states if s == "pending")
            evaluable = success + failure
            table[cid] = {
                "evaluable": evaluable,
                "events": success,
                "pending": pending,
                "proportion": success / evaluable if evaluable else None,
            }
        usable = {cid: row["proportion"] for cid, row in table.items()
                  if row["proportion"] is not None}
        excluded = sorted(cid for cid, row in table.items()
                          if row["proportion"] is None)
        per_arm_communities = {
            arm: sum(1 for cid in usable if arms[cid] == arm)
            for arm in (data.control_arm, data.treat_arm)}
        enough = all(n >= MIN_COMMUNITIES_PER_ARM
                     for n in per_arm_communities.values())

        estimate = ci_lo = ci_hi = p_value = None
        ci_boundary = False
        if usable and enough and any_varying_block:
            inference = Rerandomization(usable, arms, data.treat_arm, perms,
                                        exact=method_name.startswith("exact"))
            estimate = inference.t_obs
            if estimate is not None:
                p_value = inference.p_value(0.0)
                ci_lo, ci_hi, ci_boundary = inference.confidence_interval(ALPHA)
                raw_p[stage] = p_value
        stage_stats[stage] = {
            "table": table, "excluded": excluded, "estimate": estimate,
            "ci": (ci_lo, ci_hi), "ci_boundary": ci_boundary, "p": p_value,
            "per_arm_communities": per_arm_communities, "enough": enough,
        }

    holm = holm_adjust(raw_p) if raw_p else {}
    # 固定顺序检验：主要终点先行，未通过则后续终点不作显著性结论
    gate_results: dict[str, dict[str, Any]] = {}
    stopped_after = None
    stopped = False
    for stage in sequence:
        stats = stage_stats[stage]
        if stopped:
            gate_results[stage] = {"tested": False, "reason": "前序终点未通过固定顺序检验"}
            continue
        if stats["p"] is None:
            gate_results[stage] = {"tested": False, "reason": "可评估数据不足"}
            stopped = True
            stopped_after = stage
            continue
        significant = stats["p"] <= ALPHA
        gate_results[stage] = {"tested": True, "raw_p": r6(stats["p"]),
                               "significant_at_0.05": significant}
        if not significant:
            stopped = True
            stopped_after = stage

    for stage in STAGE_ORDER:
        stats = stage_stats[stage]
        gate = gate_results[stage]
        if stats["estimate"] is None:
            verdict = "insufficient_data"
        elif not gate.get("tested"):
            verdict = "insufficient_data" if stats["p"] is None else "not_reached_in_sequence"
        elif gate["significant_at_0.05"]:
            verdict = "increase_detected" if stats["estimate"] > 0 else "decrease_detected"
        else:
            verdict = "inconclusive"
        arm_summary = {}
        for arm in (data.control_arm, data.treat_arm):
            cids = [cid for cid in stats["table"] if arms[cid] == arm]
            evaluable = sum(stats["table"][c]["evaluable"] for c in cids)
            events = sum(stats["table"][c]["events"] for c in cids)
            pending = sum(stats["table"][c]["pending"] for c in cids)
            arm_summary[arm] = {
                "communities": stats["per_arm_communities"][arm],
                "evaluable_invitees": evaluable,
                "events": events,
                "pending": pending,
                "pooled_rate": r6(events / evaluable) if evaluable else None,
            }
        community_props = {
            arm: [{"community_id": cid,
                   "evaluable": stats["table"][cid]["evaluable"],
                   "events": stats["table"][cid]["events"],
                   "proportion": r6(stats["table"][cid]["proportion"])}
                  for cid in sorted(stats["table"]) if arms[cid] == arm]
            for arm in (data.control_arm, data.treat_arm)}
        pending_total = sum(a["pending"] for a in arm_summary.values())
        deferred = sum(1 for f in data.deferred_facts if f["stage"] == stage)
        effect_block = None
        if stats["estimate"] is not None:
            effect_block = {
                "estimate": r6(stats["estimate"]),
                "ci95": [r6(stats["ci"][0]), r6(stats["ci"][1])],
                "ci_reaches_boundary": stats["ci_boundary"],
                "raw_p": r6(stats["p"]),
                "holm_adjusted_p": r6(holm.get(stage)),
                "gatekeeping": gate,
            }
        notes = []
        if pending_total:
            notes.append(f"另有 {pending_total} 名受邀者随访窗口未关闭，"
                         "未计入分母也不计为失败。")
        if deferred:
            notes.append(f"{deferred} 条本环节记录在截止后才可用，本次未采用。")
        zero_event_arms = [arm for arm, a in arm_summary.items()
                           if a["evaluable_invitees"] and a["events"] == 0]
        if zero_event_arms:
            notes.append(f"{'、'.join(zero_event_arms)} 组本环节为零事件，"
                         "精确方法可直接处理，未做连续性校正。")
        if stats["excluded"]:
            notes.append(f"社区 {stats['excluded']} 本环节无可评估受邀者，未纳入比较。")
        interpretation = (
            interpret_effect(STAGE_LABELS[stage], stats["estimate"],
                             (stats["ci"][0], stats["ci"][1]), verdict, stats["p"])
            if stats["estimate"] is not None else
            f"{STAGE_LABELS[stage]}：可评估社区不足或区组内无分配变异，暂不能判断。")
        per_stage[stage] = {
            "stage": stage,
            "label": STAGE_LABELS[stage],
            "endpoint": f"{stage}_within_{data.horizon_days}d_per_invitee",
            "defined_for": "all_invitees",
            "estimand": "intent_to_treat_by_original_assignment",
            "is_primary_endpoint": stage == data.primary_stage,
            "is_operational_outcome": (
                ENDPOINT_ALIASES.get(data.operational_outcome) == stage),
            "treat_arm": data.treat_arm,
            "control_arm": data.control_arm,
            "arms": arm_summary,
            "community_proportions": community_props,
            "excluded_communities": stats["excluded"],
            "effect": effect_block,
            "verdict": verdict,
            "interpretation": interpretation,
            "notes": notes,
        }

    min_resolution = (f"1/{perm_total}≈{1 / perm_total:.4f}" if perm_total else "未知")
    per_stage["multiplicity"] = {
        "primary_endpoint": data.primary_stage,
        "procedure": "fixed_sequence_then_holm_sensitivity",
        "sequence": sequence,
        "stopped_after": stopped_after,
        "holm_adjusted_p": {stage: r6(p) for stage, p in holm.items()},
        "note": "固定顺序检验按研究声明的主要终点顺序执行；Holm 校正 p 值作为敏感性对照。"
                f"重随机化共 {perm_total} 种分配，p 值最小分辨约 {min_resolution}，"
                "社区少时即使差异很大也难以通过多重校正，解读请以区间与方向为主。",
    }
    per_stage["descriptive_only"] = conditional_descriptives(data, classes)
    per_stage["inference_method"] = {
        "method": method_name,
        "permutations": perm_total,
        "statistic": "社区比例的两组均值差（区组内保持分配数不变的重随机化）",
        "interval": "由重随机化检验反演得到的 95% 区间",
        "monte_carlo_seed": MONTE_CARLO_SEED if "monte_carlo" in method_name else None,
    }
    return per_stage


def limitations_section(data: TrialData,
                        stage_effects: dict[str, Any],
                        interference: dict[str, Any],
                        perm_total: int) -> list[str]:
    limits = [
        "主要结论按原随机分配（意向性）；参与者亚群中的比例只是描述，"
        "不能解释为随机化效果。",
        "观察窗口未关闭者与等待结果者记为待定，未计入分母也不计为失败；"
        "近期批次的下游环节（尤其转介）暂不能判断。",
        "本分析只评价服务流程（邀请、到场、采样、转介），不判断个体病情，"
        "不构成医疗建议；输入为本地合成数据。",
    ]
    if interference["centers_mixing_arms"]:
        limits.append("跨社区共用服务点容量可能带来组间干扰，结论外推到独立容量"
                      "场景需谨慎；该问题作为适用性限制保留，未删除任何社区。")
    limits.append(
        f"社区数量少（{len(data.communities)} 个社区、{len(data.blocks)} 个区组、"
        f"{perm_total} 种重随机化分配），区间较宽、多重校正后难以显著，"
        "解读以区间与方向为主。")
    if data.deferred_facts:
        limits.append(f"{len(data.deferred_facts)} 条记录在截止后才可用，本次未采用；"
                      "采用后结论可能更新。")
    sizes = {len([p for p in data.invitees.values()
                  if p["community"] == cid]) for cid in data.communities}
    if len(sizes) > 1:
        limits.append("社区规模不等：社区级比例等权进入主要估计，"
                      "个体合并比例仅作描述。")
    for stage in STAGE_ORDER:
        effect = stage_effects[stage].get("effect")
        if effect and effect.get("ci_reaches_boundary"):
            limits.append(f"{STAGE_LABELS[stage]}的 95% 区间触及比例差的自然边界"
                          "[-1, 1]，反映小样本设计的分辨极限。")
    if any(c["probability_missing"] for c in data.communities.values()):
        limits.append("部分社区缺少 inclusion_probability，已按 0.5 处理。")
    return limits


def run_analysis(input_path: str | Path, database_path: str | Path) -> dict[str, Any]:
    data = load_trial_input(input_path)
    classes = classify_all(data)
    schemes = build_block_schemes(data)
    perms, method_name, perm_total = enumerate_assignments(schemes)
    crosscheck = crosscheck_database(database_path, data.baseline_case_id)
    stage_effects = stage_effects_section(data, classes, perms, method_name, perm_total)
    followup = followup_support(data, classes)
    interference = interference_section(data)
    report = {
        "trial_analysis_id": "trial-" + fingerprint(
            {"method": METHOD_VERSION, "input": data.raw})[:20],
        "method_version": METHOD_VERSION,
        "analysis_scope": "仅评价服务流程（邀请、到场、采样、转介）的推广效果；"
                          "不判断个体病情，不生成医疗建议。",
        "as_of": data.as_of_text,
        "source_versions": data.source_versions,
        "methods": {
            "design": "社区级区组随机；主要结论按原随机分配（意向性），"
                      "未响应者仍属于原分配组。",
            "inference": "区组内保持分配数不变的重随机化检验：可枚举时精确枚举全部"
                         "分配，否则用固定种子蒙特卡洛；区间由检验反演得到，"
                         "不假设正态分布，零事件无需校正。",
            "multiplicity": "按研究声明的主要终点顺序做固定顺序检验，"
                            "并报告 Holm 校正 p 值作为敏感性对照。",
            "maturity": f"受邀后 {data.horizon_days} 天窗口未关闭者为待定，"
                        "不计入分母也不计为失败；截止后才可用的事实暂不采用。",
            "interference": "跨社区共用容量作为适用性限制报告，不事后删除社区。",
        },
        "assignment_checks": assignment_checks(data, crosscheck),
        "stage_effects": stage_effects,
        "followup_support": followup,
        "interference": interference,
        "limitations": limitations_section(data, stage_effects,
                                           interference, perm_total),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="colorectal_cascade.outreach_trial",
        description="社区随机分配的推广效果分析（仅评价服务流程）")
    parser.add_argument("--input", required=True, help="试验输入 JSON")
    parser.add_argument("--database", required=True, help="级联运行数据库（只读对照）")
    parser.add_argument("--output", required=True, help="分析结果输出 JSON")
    args = parser.parse_args(argv)
    try:
        report = run_analysis(args.input, args.database)
    except DomainError as exc:
        print(f"分析输入错误: {exc}", file=sys.stderr)
        return 2
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(json.dumps({"trial_analysis_id": report["trial_analysis_id"],
                      "output": args.output}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
