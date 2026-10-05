# Progress formulas

This page describes the current `Service.stats` rules. A result is based on scheduled opportunities, not on every calendar day. Future dates do not enter the calculation.

## Closed opportunities

For a daily, weekday, or interval schedule, each scheduled and unpaused date is one opportunity. A past date is closed even when it has no record. Today remains open until the user records `full`, `minimum`, `fail`, or `skip`; `progress`, `none`, and no record leave today's opportunity open.

For a weekly quota, each week touched by the requested range is one opportunity. Eligible scheduled, unpaused dates determine its target:

```text
weekly_target = min(configured_quota, eligible_days_in_week)
```

The week closes when it has reached the target or the week has ended before today. Reaching the target early closes it as complete. This assesses the quota once per week rather than counting every uncompleted day as a failure.

## Completion rate

```text
completion_rate = (full_opportunities + minimum_opportunities) / closed_opportunities
```

If there are no closed opportunities, the rate is `0`. Full and minimum counts are reported separately. A `fail`, `skip`, or missing record counts as an uncompleted closed opportunity. Paused dates are excluded from opportunity counts. A friend verification request changes the verification status; it does not turn a self-reported completion into a skip.

For weekly quotas, a week is `full` when full records alone meet the weekly target. Otherwise, it is `minimum` when full plus minimum records meet the target. Otherwise, a closed week is missing. This intentionally gives a reached minimum week credit as one completed opportunity.

## Quantity and history

Quantity is shown by unit, including the open current date when it has a record.
Binary ticks are not pages or minutes. A technical aggregate `quantity` is kept for
compatibility; interpreting a sum of different units is not useful. Each record
keeps a snapshot of the goal version used at that time. Edits take effect tomorrow;
if the current or new schedule is weekly, the version takes effect next Monday.
Past records keep their snapshots.

The current and best streaks are calculated across closed opportunities for each habit. The displayed aggregate is the **maximum streak from any single habit**; streaks from different habits are not added together. A completed full or minimum opportunity extends that habit's streak. A missed or skipped closed opportunity resets it. Pauses add no opportunity and therefore do not break the streak.

The comparison rate uses the immediately preceding date range with the same number of calendar days, using the same closed-opportunity rules. The level is `1 + floor(total_xp / 100)`. Rewards use a unique opportunity key; undoing a record does not remove an earned reward, so re-editing the same opportunity cannot farm another reward.

Archive excludes unrecorded opportunities from its effective personal date onward.
A pause excludes upcoming opportunities and preserves already recorded completions.
Neither operation repairs earlier missed dates. An open weekly quota is not counted
as a failure before the week ends. A custom range touching a quota week includes
that full week, so comparisons of partial weeks should be interpreted accordingly.

Independence is eligible after eight successive full/minimum opportunities, or four
completed quota weeks. Rest and pause dates contribute no opportunities. Only the
current schedule family qualifies: daily successes cannot stand in for quota weeks.
Only the
user's confirmation reduces reminder times to every other configured time; a single
time becomes none. Opt-in repeats are disabled in independence mode. The change uses
the same future-version rules, and the user can set a new reminder plan later.

Photo/text/timer submissions and a friend's pending/approved/rejected decision are
reported separately from self-reported completion. They never silently turn a saved
full/minimum record into a miss. A rejected check remains visible as rejected.

## Interpretation

These figures describe recorded behavior under the saved schedule. They are not a clinical measure or proof of activity. A missing record means no completion was recorded; it does not establish why the activity did not happen.
