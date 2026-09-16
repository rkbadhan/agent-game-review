"""Fleet read-model over recovery episodes across ALL runs (item 30, 2026-09-08).

A "fleet view" answers: across every run in the store, which failures repeat,
how often, and how much they cost — grouped by whatever dimensions the
caller asks for (the failed call's ``tool``, its ``error_signature``, or
both). This is a pure re-read of already-persisted derived records
(``recoveries.json`` per run, item 29's enrichment); nothing new is computed
or inferred about the episodes themselves — only aggregated and sorted.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from . import read
from .execution_quality import execution_quality_record
from .queue import OUTCOME_BUCKETS, outcome_bucket
from .recovery import episode_limits
from .store import Store

# Accepted --group-by tokens -> the RecoveryEpisode field each one reads.
# "error"/"error_signature" are the same dimension under two spellings.
GROUP_DIMENSIONS = {"tool": "tool", "error": "error_signature", "error_signature": "error_signature"}

DEFAULT_GROUP_BY = ("tool", "error_signature")

# How many sample episodes each group carries for drilling into a run.
_MAX_ANCHORS = 3


def _resolve_dims(group_by: Optional[list[str]]) -> list[str]:
    requested = group_by if group_by else list(DEFAULT_GROUP_BY)
    dims: list[str] = []
    for token in requested:
        dim = GROUP_DIMENSIONS.get(token)
        if dim is None:
            raise ValueError(
                f"unknown --group-by dimension {token!r}; choose from "
                f"{sorted(set(GROUP_DIMENSIONS))}"
            )
        if dim not in dims:
            dims.append(dim)
    return dims


@dataclass
class EpisodeGroup:
    """One (tool, error_signature, ...) bucket of recovery episodes across
    every run in the store. Every field is a plain aggregate — a sum, a
    count, an average — over the ``RecoveryEpisode`` rows in the group;
    nothing here reclassifies or reinterprets a single episode.
    """

    key: tuple
    group_by: list[str]
    count: int
    # §12.1: distinct tasks behind the group — "6 runs" cannot distinguish one
    # duplicated task from six when the task count is left implicit.
    task_count: int = 0
    run_ids: list[str] = field(default_factory=list)
    unrecovered_count: int = 0
    # AGR-04: confirmed (good_recovery / retry_succeeded_without_strategy_
    # change) and plausible (plausibly_resolved) episodes are counted
    # separately from each other and from unrecovered_count — count ==
    # confirmed_count + plausible_count + unrecovered_count always. Nothing
    # here silently folds a plausible resolution into either "confirmed" or
    # "unresolved".
    confirmed_count: int = 0
    plausible_count: int = 0
    avg_turns_to_resolve: Optional[float] = None
    # AGR-06: the UNION of underlying usage records (event_id -> tokens)
    # across every episode in this group, keyed by (run_id, event_id) so an
    # event two episodes' windows both cover (e.g. an unresolved episode's
    # window running into the next failure's) is counted once, not once per
    # episode. total_tokens is this union's sum, never a plain sum of each
    # episode's episode_window_tokens — see overlapping_usage_events.
    total_tokens: int = 0
    # How many distinct (run, event) pairs were covered by MORE than one
    # episode in this group. >0 means total_tokens is NOT the same number a
    # naive sum of episode_window_tokens would give — the group's usage is
    # non-additive with respect to its own episode list, by construction
    # (shared events are real, not a bug to eliminate).
    overlapping_usage_events: int = 0
    # Plain sum of each episode's own initiating_attempt_tokens — the cost of
    # the failed attempt itself (the failing call and the turns that led to
    # it), unlike total_tokens which for an unrecovered episode includes
    # everything through the end of the run, whether or not it was spent
    # trying to fix this failure. Unlike total_tokens this is a genuine sum,
    # not a union: an attempt's own lead-in tokens are never double-counted
    # across two episodes since each episode has its own single failing call.
    attempt_tokens_total: int = 0
    total_wall_ms: int = 0
    # AGR-05/06 (review 82cc113): how many of this group's episodes have
    # usage_completeness == "unavailable" — no cost was ever recorded in
    # their window at all. A group where every episode is unavailable would
    # otherwise render as a measured "0 token(s)", indistinguishable from a
    # group that genuinely cost nothing.
    usage_unavailable_count: int = 0
    # Follow-up (review of commit 5782f1b): how many of this group's
    # episodes have their OWN usage_completeness == "partial" (some but not
    # all of that episode's window was instrumented). Distinct from
    # usage_unavailable_count — an episode can be individually "partial"
    # without the group containing any fully "unavailable" episode, and the
    # prior usage_availability property (unavailable-count-only) silently
    # read such a group as "measured", losing exactly the qualifier this
    # field exists to carry.
    usage_partial_count: int = 0
    # AGR-07 (review 82cc113): how many of this group's episodes selected
    # their error_signature via the opaque "fallback_last_nonempty" tier —
    # no traceback, no recognised diagnostic marker anywhere in the failure
    # text, so the signature is just whatever line happened to be last (the
    # groups like bare "---"/"}"/"===" the review calls out). Distinct from a
    # confident traceback/diagnostic-derived signature, which the UI should
    # not present identically.
    fallback_basis_count: int = 0
    # P3-B: how this group's AFFECTED runs ended, keyed by the shared outcome
    # classifier (§4.3.2) so Patterns can never disagree with the Runs chips.
    # A behaviour seen in both a passing and a failing run shows it is
    # survivable — NOT that the failing runs should adopt the passing run's
    # approach. This compares outcomes, not the traces behind them.
    outcome_split: dict = field(default_factory=dict)
    example_anchors: list[dict] = field(default_factory=list)

    @property
    def outcome_mixed(self) -> bool:
        """True when the affected runs both passed and did not — the case
        where a passing sibling could be mistaken for the correct approach."""
        passing = self.outcome_split.get("pass", 0)
        return passing > 0 and (self.distinct_runs - passing) > 0

    @property
    def all_fallback_basis(self) -> bool:
        """True when EVERY episode in the group is an opaque fallback
        signature — this group's key is not a meaningful diagnostic at all."""
        return self.count > 0 and self.fallback_basis_count == self.count

    @property
    def usage_availability(self) -> str:
        """"unavailable" (no episode in the group ever measured usage),
        "partial" (either a mix of measured/unavailable episodes, OR any
        episode whose OWN window was only partially instrumented — follow-up,
        review of commit 5782f1b: the prior version only looked at
        usage_unavailable_count, so a group made entirely of individually-
        "partial" episodes read as "measured" and total_tokens's own
        incompleteness qualifier was lost), or "measured" (every episode has
        FULLY recorded usage — total_tokens can be trusted as a real count,
        including a genuine zero)."""
        if self.count and self.usage_unavailable_count == self.count:
            return "unavailable"
        if self.usage_unavailable_count > 0 or self.usage_partial_count > 0:
            return "partial"
        return "measured"

    @property
    def distinct_runs(self) -> int:
        return len(set(self.run_ids))

    @property
    def repeat_rate(self) -> Optional[float]:
        """Share of this group's AFFECTED RUNS where the failure recurred
        MORE THAN ONCE within that same run (AGR-06) — bounded [0, 1] by
        construction (a count of runs divided by a count that includes
        them). ``None`` (render as "unavailable") when the group has no
        affected runs at all — an empty denominator, not a rate of zero.

        This is deliberately NOT ``count / distinct_runs`` (episodes per
        run): that is an occurrence count that can exceed 1 and reads as a
        percentage only by coincidence. Two episodes in one affected run
        gives 1/1 = 100%; that same run alongside three singleton-episode
        runs gives 1/4 = 25%, not the old formula's 4 episodes / 4 runs = 1.0
        misread as "100% repeat".
        """
        if not self.run_ids:
            return None
        per_run = Counter(self.run_ids)
        affected = len(per_run)
        if not affected:
            return None
        repeated = sum(1 for n in per_run.values() if n > 1)
        return repeated / affected

    @property
    def unrecovered_share(self) -> float:
        return (self.unrecovered_count / self.count) if self.count else 0.0

    @property
    def confirmed_share(self) -> float:
        return (self.confirmed_count / self.count) if self.count else 0.0

    @property
    def plausible_share(self) -> float:
        return (self.plausible_count / self.count) if self.count else 0.0

    @property
    def is_recurring(self) -> bool:
        """True when the behaviour was seen more than once — across several
        tasks, or repeatedly within the corpus (including several times inside
        one run). One episode in one task is a one-off, not a pattern."""
        return self.task_count > 1 or self.count > 1

    @property
    def evidence_strength(self) -> str:
        """How well the group's error signature itself is supported:
        ``observed`` (every anchor's signature was read from the trace),
        ``mixed`` (some anchors fell back to the last-line heuristic), or
        ``weak`` (every anchor is the opaque fallback — the key is a best
        effort, not an established diagnostic)."""
        if self.all_fallback_basis:
            return "weak"
        if self.fallback_basis_count:
            return "mixed"
        return "observed"

    @property
    def evidence_strength_rank(self) -> int:
        return {"observed": 2, "mixed": 1, "weak": 0}[self.evidence_strength]

    def rank_key(self) -> tuple:
        """The ordering that leads with the most investigable behaviours.

        Lexicographic, and every component is DISPLAYED — deliberately not a
        single blended score, which would let weak evidence borrow precision
        from a strong recurrence count. In order:

          1. recurring before one-off (a one-off never outranks a pattern),
          2. reach: distinct tasks, then distinct runs, then episodes,
          3. evidence strength of the error signature,
          4. measured tokens (0 where usage was never instrumented).

        Missing resource coverage therefore never *raises* a group: it sorts
        last on the impact key and stays visibly "unavailable".
        """
        return (
            1 if self.is_recurring else 0,
            self.task_count,
            self.distinct_runs,
            self.count,
            self.evidence_strength_rank,
            self.total_tokens,
        )

    def to_dict(self) -> dict:
        rate = self.repeat_rate
        return {
            "key": list(self.key),
            "group_by": self.group_by,
            "count": self.count,
            "runs": self.distinct_runs,
            "tasks": self.task_count,
            # P3: the two other ranking inputs, exposed so the list order is
            # explainable from the row itself, not a hidden score.
            "recurring": self.is_recurring,
            "evidence_strength": self.evidence_strength,
            # None (JSON null) when unavailable — the caller renders that as
            # "unavailable", never a misleading 0%.
            "repeat_rate": round(rate, 3) if rate is not None else None,
            "unrecovered_count": self.unrecovered_count,
            "unrecovered_share": round(self.unrecovered_share, 3),
            "confirmed_count": self.confirmed_count,
            "confirmed_share": round(self.confirmed_share, 3),
            "plausible_count": self.plausible_count,
            "plausible_share": round(self.plausible_share, 3),
            "avg_turns_to_resolve": (
                round(self.avg_turns_to_resolve, 2) if self.avg_turns_to_resolve is not None else None
            ),
            # AGR-06: usage is associated with these episodes' windows, not
            # avoidable waste or a projected saving — this is a measured
            # count of tokens the underlying events recorded, nothing more.
            "total_tokens": self.total_tokens,
            "overlapping_usage_events": self.overlapping_usage_events,
            "attempt_tokens_total": self.attempt_tokens_total,
            "usage_unavailable_count": self.usage_unavailable_count,
            "usage_partial_count": self.usage_partial_count,
            "usage_availability": self.usage_availability,
            "usage_note": (
                "total_tokens is the union of underlying usage records across this "
                "group's episodes' windows — for an episode that never resolved, its "
                "window runs to the end of the run, so total_tokens can include tokens "
                "spent on later, unrelated work, not just this failure. "
                "attempt_tokens_total is the sum of each episode's initiating_attempt_"
                "tokens — the cost of the failed attempt itself (the failing call and "
                "its lead-in) — and IS attributable to this group; treat it, not "
                "total_tokens, as the trustworthy 'cost of this bug' figure. "
                "overlapping_usage_events > 0 means some of total_tokens's underlying "
                "events were covered by more than one episode and are counted once, "
                "not per episode."
                + (f" {self.usage_unavailable_count} of {self.count} episode(s) never had "
                   "usage instrumented at all — both totals undercount this group's real "
                   "cost." if self.usage_unavailable_count else "")
                + (f" {self.usage_partial_count} of {self.count} episode(s) had only part of "
                   "their window instrumented — both totals may undercount even where "
                   "nonzero." if self.usage_partial_count else "")
            ),
            "total_wall_ms": self.total_wall_ms,
            "fallback_basis_count": self.fallback_basis_count,
            "all_fallback_basis": self.all_fallback_basis,
            # P3-B: the outcome split of this group's affected runs, in the
            # shared bucket vocabulary, plus whether it is genuinely mixed.
            "outcome_split": {b: self.outcome_split.get(b, 0) for b in OUTCOME_BUCKETS},
            "outcome_mixed": self.outcome_mixed,
            "example_anchors": self.example_anchors,
        }


def fleet_episodes(store: Store, group_by: Optional[list[str]] = None) -> list[EpisodeGroup]:
    """Group every run's persisted recovery episodes by ``group_by``
    (default: tool + error_signature together), ordered so the most
    investigable behaviours lead: recurring before one-off, then by reach
    (distinct tasks, runs, episodes), evidence strength, and measured tokens.
    See :meth:`EpisodeGroup.rank_key`.
    """
    dims = _resolve_dims(group_by)
    runs = read.list_runs(store)
    task_of = {r["run_id"]: r.get("task_id") for r in runs}
    # P3-B: one shared classifier for every run's task outcome, so the
    # Patterns outcome split cannot disagree with the Runs chips.
    bucket_of = {
        r["run_id"]: outcome_bucket((r.get("outcome") or {}).get("status"))
        for r in runs
    }
    buckets: dict[tuple, list[dict]] = {}
    for r in runs:
        run_id = r["run_id"]
        capture_id = r.get("capture_id")
        if not capture_id or not store.has_derived(run_id, capture_id, "recoveries.json"):
            continue
        episodes = store.read_derived(run_id, capture_id, "recoveries.json") or []
        for ep in episodes:
            key = tuple(ep.get(d) for d in dims)
            buckets.setdefault(key, []).append({**ep, "run_id": run_id, "source_capture_id": capture_id})

    groups: list[EpisodeGroup] = []
    for key, eps in buckets.items():
        turns = [e["turns_to_resolve"] for e in eps if e.get("turns_to_resolve") is not None]
        anchors = [
            {
                "run_id": e["run_id"],
                "source_capture_id": e["source_capture_id"],
                "episode_id": e.get("episode_id"),
                "failure_event_id": e.get("failure_event_id"),
                "classification": e.get("classification"),
                # AGR-07: which tier actually selected error_signature for
                # THIS episode — preserved through to the UI's drill-in.
                "error_signature_basis": e.get("error_signature_basis"),
                # P2 (§B1): each representative episode carries its own
                # evidence limits, so the drill-in never shows a confident
                # classification without what it does not establish.
                "limits": episode_limits(e),
            }
            for e in eps[:_MAX_ANCHORS]
        ]
        groups.append(EpisodeGroup(
            key=key,
            group_by=dims,
            count=len(eps),
            run_ids=[e["run_id"] for e in eps],
            task_count=len({task_of.get(e["run_id"]) for e in eps if task_of.get(e["run_id"])}),
            # Distinct affected runs only — an episode is one behaviour, not
            # one run, so two episodes in the same run still count that run once.
            outcome_split=dict(Counter(
                bucket_of.get(run_id, "unverified") for run_id in {e["run_id"] for e in eps}
            )),
            unrecovered_count=sum(1 for e in eps if e.get("classification") == "unrecovered_failure"),
            confirmed_count=sum(
                1 for e in eps
                if e.get("classification") in ("good_recovery", "retry_succeeded_without_strategy_change")),
            plausible_count=sum(1 for e in eps if e.get("classification") == "plausibly_resolved"),
            avg_turns_to_resolve=(sum(turns) / len(turns)) if turns else None,
            # AGR-06: the union of usage_records across this group's
            # episodes, not a plain sum of episode_window_tokens — two
            # episodes whose windows share events (e.g. an unresolved
            # episode's window running into the next failure's) would
            # otherwise double-count those events' tokens.
            **dict(zip(("total_tokens", "overlapping_usage_events"), _usage_union(eps))),
            attempt_tokens_total=sum(e.get("initiating_attempt_tokens") or 0 for e in eps),
            total_wall_ms=sum(e.get("wall_ms") or 0 for e in eps),
            usage_unavailable_count=sum(1 for e in eps if e.get("usage_completeness") == "unavailable"),
            usage_partial_count=sum(1 for e in eps if e.get("usage_completeness") == "partial"),
            fallback_basis_count=sum(
                1 for e in eps if e.get("error_signature_basis") == "fallback_last_nonempty"),
            example_anchors=anchors,
        ))
    groups.sort(key=lambda g: g.rank_key(), reverse=True)
    return groups


def _usage_union(eps: list[dict]) -> tuple[int, int]:
    """``(total_tokens, overlapping_event_count)`` — the union of every
    episode's ``usage_records``, keyed by ``(run_id, event_id)`` since event
    ids repeat across different runs' captures. ``overlapping_event_count``
    is how many distinct events were covered by more than one episode —
    when it is nonzero, ``total_tokens`` is NOT the same as summing each
    episode's own ``episode_window_tokens`` (AGR-06: group usage is
    non-additive with respect to its episode list whenever windows share
    events, by construction — this is a real fact about overlapping
    windows, not something to hide by summing anyway).
    """
    tokens_by_key: dict[tuple, int] = {}
    occurrences: dict[tuple, int] = {}
    for e in eps:
        run_id = e.get("run_id")
        for event_id, tok in (e.get("usage_records") or {}).items():
            key = (run_id, event_id)
            tokens_by_key[key] = tok
            occurrences[key] = occurrences.get(key, 0) + 1
    overlapping = sum(1 for c in occurrences.values() if c > 1)
    return sum(tokens_by_key.values()), overlapping


@dataclass
class FleetUsageSummary:
    """The fleet-wide usage headline — the union across EVERY episode in the
    store, regardless of grouping (AGR-06: summing each group's own
    total_tokens would double-count any event two DIFFERENT groups' episodes
    both cover, e.g. two failures in the same run with different tools/
    signatures whose windows overlap).
    """

    total_tokens: int
    episode_count: int
    affected_runs: int
    overlapping_usage_events: int
    # AGR-05/06 (review 82cc113): how many of EVERY episode in the store has
    # usage_completeness == "unavailable" — no cost was ever recorded for it.
    # Those episodes contribute nothing to total_tokens, so a fleet with many
    # unavailable episodes otherwise reads as "0 token(s)" with no way to
    # tell that apart from a fleet that genuinely cost nothing.
    usage_unavailable_episode_count: int = 0
    # Follow-up (review of commit 5782f1b): how many of EVERY episode has its
    # OWN usage_completeness == "partial" — only part of that episode's
    # window was instrumented. The fleet-wide headline previously carried no
    # equivalent to the per-group usage_availability's "partial" qualifier
    # at all; a store made entirely of partially-instrumented episodes
    # reported a plain total_tokens number with nothing distinguishing it
    # from a fully-measured one.
    usage_partial_episode_count: int = 0
    usage_note: str = (
        "total_tokens is the union of underlying usage records across every recovery "
        "episode in the store; it is associated with episode windows, not avoidable "
        "waste or a projected saving. overlapping_usage_events > 0 means some events "
        "were covered by more than one episode (in the same or different groups) and "
        "are counted once here, not once per episode."
    )

    @property
    def usage_availability(self) -> str:
        """Fleet-wide counterpart to EpisodeGroup.usage_availability: the
        same "measured" / "partial" / "unavailable" vocabulary, so the
        headline can be labelled consistently with the table beneath it."""
        if self.episode_count and self.usage_unavailable_episode_count == self.episode_count:
            return "unavailable"
        if self.usage_unavailable_episode_count > 0 or self.usage_partial_episode_count > 0:
            return "partial"
        return "measured"

    def to_dict(self) -> dict:
        note = self.usage_note
        if self.usage_unavailable_episode_count:
            note += (f" {self.usage_unavailable_episode_count} of {self.episode_count} "
                     "episode(s) never had usage instrumented at all — total_tokens "
                     "undercounts the fleet's real cost.")
        if self.usage_partial_episode_count:
            note += (f" {self.usage_partial_episode_count} of {self.episode_count} "
                     "episode(s) had only part of their window instrumented — "
                     "total_tokens may undercount even where it is nonzero.")
        return {
            "total_tokens": self.total_tokens,
            "episode_count": self.episode_count,
            "affected_runs": self.affected_runs,
            "overlapping_usage_events": self.overlapping_usage_events,
            "usage_unavailable_episode_count": self.usage_unavailable_episode_count,
            "usage_partial_episode_count": self.usage_partial_episode_count,
            "usage_availability": self.usage_availability,
            "usage_note": note,
        }


def fleet_usage_summary(store: Store) -> FleetUsageSummary:
    """The whole-fleet usage headline, independent of any --group-by choice.

    Reads every run's persisted episodes directly (not via :func:`fleet_
    episodes`, which only ever sees one grouping's buckets) so the union is
    computed across the COMPLETE episode list once, not reassembled from
    per-group unions that could themselves double-count a shared event.
    """
    runs = read.list_runs(store)
    all_eps: list[dict] = []
    for r in runs:
        run_id = r["run_id"]
        capture_id = r.get("capture_id")
        if not capture_id or not store.has_derived(run_id, capture_id, "recoveries.json"):
            continue
        episodes = store.read_derived(run_id, capture_id, "recoveries.json") or []
        for ep in episodes:
            all_eps.append({**ep, "run_id": run_id})
    total_tokens, overlapping = _usage_union(all_eps)
    return FleetUsageSummary(
        total_tokens=total_tokens,
        episode_count=len(all_eps),
        affected_runs=len({e["run_id"] for e in all_eps}),
        overlapping_usage_events=overlapping,
        usage_unavailable_episode_count=sum(
            1 for e in all_eps if e.get("usage_completeness") == "unavailable"),
        usage_partial_episode_count=sum(
            1 for e in all_eps if e.get("usage_completeness") == "partial"),
    )


# Efficiency dimension -> the detector whose finding anchors its evidence.
_DIMENSION_DETECTOR = {
    "context_bloat": "context_token_bloat",
    "latency": "excess_latency",
    "redundant_work": "repeated_action_no_new_info",
}


def _anchor_events(record: dict, detector: str) -> list[str]:
    """Every event that anchors this detector's finding (all violations).

    Returning only the first anchor made a run's other violating events
    unreachable from the card. An aggregate finding lists all of them in
    ``anchor_event_ids``, so preserve the whole set.
    """
    for finding in record.get("findings", []) or []:
        if finding.get("detector") == detector:
            return [e for e in (finding.get("anchor_event_ids") or []) if e]
    return []


def fleet_execution_quality(store: Store) -> dict:
    """Execution issue incidence by outcome, using evaluated runs only.

    Every outcome-bearing run is represented. A run with no persisted
    ``execution_quality.json`` (ingested before the detectors existed) is
    derived from its own persisted events rather than dropped; a run with no
    usable telemetry at all stays in the run set but is counted as
    *unevaluated* with the missing capability named, so the card reports
    "N of M runs not evaluated" instead of an unexplained "No results".

    Each dimension also carries the runs behind its numbers — the affected
    runs (with the event that anchors the finding) and the unevaluated runs
    (with their missing capabilities) — so a reader can see the incidents
    separately instead of only a percentage. Distinct task counts ride beside
    the run counts (per bucket and, in ``by_dimension``, across the whole row)
    so repeated runs of one task cannot read as a pattern spanning many.
    """
    dimensions = ("context_bloat", "latency", "redundant_work")
    # Execution quality is independent of outcome, so a run whose result is
    # undetermined or unverified must still be counted — never dropped. Its
    # bucket comes from queue.outcome_bucket, the SAME classifier behind the
    # Runs chips and the pattern outcome splits, so a run can never land in one
    # bucket here and a different one there. (The old single "other" bucket
    # both hid the difference between "a verifier ran, no clean verdict" and
    # "no verifier at all", and could disagree with the inbox.)
    groups: dict[str, list[tuple[str, str | None, dict]]] = {
        bucket: [] for bucket in OUTCOME_BUCKETS
    }
    for run in read.list_runs(store):
        label = outcome_bucket((run.get("outcome") or {}).get("status"))
        run_id, capture_id = run["run_id"], run.get("capture_id")
        if not capture_id:
            continue
        groups[label].append((
            run_id, run.get("task_id"),
            execution_quality_record(store, run_id, capture_id),
        ))
    # Distinct tasks, tracked per dimension ACROSS outcome buckets: a task with
    # both a passing and a failing run must not be counted twice. Runs measure
    # how often something was observed; tasks measure how far it spread (§12.1).
    task_sets: dict[str, dict[str, set]] = {
        dimension: {"eligible": set(), "affected": set(), "unevaluated": set()}
        for dimension in dimensions
    }
    result = {"by_outcome": {}, "by_dimension": {}}
    for label, records in groups.items():
        metrics = {}
        for dimension in dimensions:
            detector = _DIMENSION_DETECTOR[dimension]
            evaluated, affected, healthy, unevaluated = [], [], [], []
            bucket_tasks = {"eligible": set(), "affected": set(), "unevaluated": set()}
            for run_id, task_id, record in records:
                value = (record.get("efficiency") or {}).get(dimension) or {}
                if not value.get("evaluated"):
                    # Missing telemetry belongs to this capture, not to the
                    # outcome bucket. Keeping the reasons beside the run stops
                    # two differently-incomplete runs from inheriting each
                    # other's missing capabilities in the drill-down.
                    unevaluated.append({
                        "run_id": run_id,
                        "reasons": sorted(value.get("unmet_capabilities") or []),
                    })
                    if task_id:
                        bucket_tasks["unevaluated"].add(task_id)
                        task_sets[dimension]["unevaluated"].add(task_id)
                    continue
                evaluated.append(value)
                if task_id:
                    bucket_tasks["eligible"].add(task_id)
                    task_sets[dimension]["eligible"].add(task_id)
                if value.get("violations", 0) > 0:
                    event_ids = _anchor_events(record, detector)
                    affected.append({
                        "run_id": run_id,
                        "event_id": event_ids[0] if event_ids else None,
                        "event_ids": event_ids,
                        "violations": value.get("violations", 0),
                    })
                    if task_id:
                        bucket_tasks["affected"].add(task_id)
                        task_sets[dimension]["affected"].add(task_id)
                else:
                    healthy.append(run_id)
            metrics[dimension] = {
                "eligible_runs": len(evaluated),
                "eligible_tasks": len(bucket_tasks["eligible"]),
                "affected_runs": len(affected),
                "affected_tasks": len(bucket_tasks["affected"]),
                "affected_percent": (
                    round(100 * len(affected) / len(evaluated), 2) if evaluated else None
                ),
                "unevaluated_runs": len(unevaluated),
                "unevaluated_tasks": len(bucket_tasks["unevaluated"]),
                "affected": affected,
                # Every run is addressable from the card — an evaluated run with
                # no violation and an unevaluated run were previously listed as
                # plain text, so a dimension with zero findings had no run you
                # could open to inspect at all.
                "healthy_run_ids": healthy,
                "unevaluated": unevaluated,
            }
        result["by_outcome"][label] = {"runs": len(records), "dimensions": metrics}
    for dimension, sets in task_sets.items():
        # A task counted in two buckets appears once here; the coverage column
        # reads this, not the sum of per-bucket task counts.
        result["by_dimension"][dimension] = {
            "eligible_tasks": len(sets["eligible"]),
            "affected_tasks": len(sets["affected"]),
            "unevaluated_tasks": len(sets["unevaluated"]),
            "tasks": len(sets["eligible"] | sets["unevaluated"]),
        }
    return result
