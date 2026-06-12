#!/usr/bin/env python3
"""Fetch GitHub PR review statistics and write JSON output."""

import argparse
import json
import math
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

GRAPHQL_BATCH_SIZE = 30
SEARCH_SLEEP_SECS = 2


def load_config(path):
    with open(path) as f:
        return json.load(f)


def parse_args():
    p = argparse.ArgumentParser(description="Fetch GitHub PR review stats")
    p.add_argument("--config", default="config.json", help="Path to config file")
    p.add_argument("--reviewers", help="Comma-separated list of GitHub usernames (overrides config)")
    p.add_argument("--repo", help="Repository in owner/repo format (overrides config)")
    p.add_argument("--since", help="Earliest date to look back, YYYY-MM-DD (overrides config)")
    p.add_argument("--output", default="docs/data/reviews.json", help="Output JSON path")
    p.add_argument("--inline", action="store_true", help="Embed JSON in docs/index.html for file:// use")
    return p.parse_args()


def gh_api(endpoint, paginate=False, jq=None):
    cmd = ["gh", "api"]
    if paginate:
        cmd.append("--paginate")
    cmd.append(endpoint)
    if jq:
        cmd.extend(["--jq", jq])
    for attempt in range(3):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            return result.stdout
        stderr = result.stderr.lower()
        if "rate limit" in stderr or "403" in stderr or "secondary" in stderr:
            wait = 30 * (attempt + 1)
            print(f"  Rate limited, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        if "502" in stderr or "503" in stderr:
            time.sleep(5)
            continue
        raise RuntimeError(f"gh api failed: {result.stderr.strip()}")
    raise RuntimeError(f"gh api failed after retries: {endpoint}")


def gh_graphql(query):
    for attempt in range(3):
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if "errors" in data:
                print(f"  GraphQL errors: {data['errors']}", file=sys.stderr)
            return data.get("data", {})
        stderr = result.stderr.lower()
        if "rate limit" in stderr or "403" in stderr or "secondary" in stderr:
            wait = 30 * (attempt + 1)
            print(f"  Rate limited, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        if "502" in stderr or "503" in stderr:
            time.sleep(5)
            continue
        raise RuntimeError(f"gh graphql failed: {result.stderr.strip()}")
    raise RuntimeError("gh graphql failed after retries")


def date_shards(since, shard_months=6):
    start = datetime.strptime(since, "%Y-%m-%d").date()
    today = datetime.now(timezone.utc).date()
    shards = []
    while start < today:
        end = start + timedelta(days=shard_months * 30)
        if end > today:
            end = today
        shards.append((start.isoformat(), end.isoformat()))
        start = end + timedelta(days=1)
    return shards


def fetch_reviewed_prs(repo, user, since):
    shards = date_shards(since)
    all_prs = set()
    for i, (start, end) in enumerate(shards):
        endpoint = (
            f"search/issues?q=type:pr+repo:{repo}+reviewed-by:{user}"
            f"+updated:{start}..{end}&per_page=100&sort=updated&order=desc"
        )
        try:
            raw = gh_api(endpoint, paginate=True, jq=".items[].number")
            for line in raw.strip().split("\n"):
                line = line.strip()
                if line:
                    all_prs.add(int(line))
        except Exception as e:
            print(f"  Warning: search shard {start}..{end} failed for {user}: {e}", file=sys.stderr)
        if i < len(shards) - 1:
            time.sleep(SEARCH_SLEEP_SECS)
    return sorted(all_prs)


def fetch_review_events(repo, user, pr_numbers):
    owner, name = repo.split("/")
    events = []
    total_batches = math.ceil(len(pr_numbers) / GRAPHQL_BATCH_SIZE)

    for batch_idx in range(total_batches):
        batch = pr_numbers[batch_idx * GRAPHQL_BATCH_SIZE:(batch_idx + 1) * GRAPHQL_BATCH_SIZE]
        fields = []
        for i, num in enumerate(batch):
            fields.append(
                f'pr{i}: pullRequest(number: {num}) {{'
                f' number'
                f' reviews(first: 50, author: "{user}") {{'
                f' nodes {{ submittedAt state }}'
                f' }}'
                f' }}'
            )
        query = f'{{ repository(owner: "{owner}", name: "{name}") {{ {" ".join(fields)} }} }}'

        try:
            data = gh_graphql(query)
        except Exception as e:
            print(f"  Warning: GraphQL batch {batch_idx + 1}/{total_batches} failed for {user}: {e}", file=sys.stderr)
            continue

        repo_data = data.get("repository", {})
        for i in range(len(batch)):
            pr = repo_data.get(f"pr{i}")
            if not pr:
                continue
            for review in (pr.get("reviews") or {}).get("nodes") or []:
                if review.get("submittedAt"):
                    events.append({
                        "pr": pr["number"],
                        "submittedAt": review["submittedAt"],
                        "state": review["state"],
                    })

        if (batch_idx + 1) % 5 == 0:
            print(f"    GraphQL {batch_idx + 1}/{total_batches} batches done", file=sys.stderr)

    return events


def compute_stats(events):
    if not events:
        return {
            "total_reviews": 0, "unique_prs": 0,
            "first_review": None, "last_review": None,
            "type_breakdown": {}, "monthly": [], "weekly": [],
            "day_of_week": [0] * 7, "busiest_days": [], "busiest_days_by_prs": [],
            "review_depth": {"avg_per_pr": 0, "single_review": 0, "two_three_reviews": 0, "four_plus_reviews": 0},
            "daily_timeline": [],
            "recent_stats": {"total_reviews": 0, "unique_prs": 0, "type_breakdown": {}},
        }

    for e in events:
        e["date"] = datetime.fromisoformat(e["submittedAt"].replace("Z", "+00:00")).date()

    dates = [e["date"] for e in events]
    first_date = min(dates)
    last_date = max(dates)

    monthly_events = defaultdict(int)
    monthly_prs = defaultdict(set)
    weekly_events = defaultdict(int)
    weekly_prs = defaultdict(set)
    daily_counts = Counter()
    daily_prs = defaultdict(set)
    day_of_week = [0] * 7

    for e in events:
        d = e["date"]
        month_key = d.strftime("%Y-%m")
        monthly_events[month_key] += 1
        monthly_prs[month_key].add(e["pr"])

        iso = d.isocalendar()
        week_key = f"{iso[0]}-W{iso[1]:02d}"
        weekly_events[week_key] += 1
        weekly_prs[week_key].add(e["pr"])

        daily_counts[d] += 1
        daily_prs[d].add(e["pr"])
        day_of_week[d.weekday()] += 1

    monthly = [
        {"month": m, "events": monthly_events[m], "unique_prs": len(monthly_prs[m])}
        for m in sorted(monthly_events)
    ]

    all_weeks = sorted(weekly_events)
    last_12 = all_weeks[-12:]
    weekly = [
        {"week": w, "events": weekly_events[w], "unique_prs": len(weekly_prs[w])}
        for w in last_12
    ]

    busiest = [
        {"date": d.isoformat(), "count": c}
        for d, c in daily_counts.most_common(10)
    ]

    busiest_by_prs = sorted(
        [{"date": d.isoformat(), "unique_prs": len(prs), "events": daily_counts[d]}
         for d, prs in daily_prs.items()],
        key=lambda x: x["unique_prs"],
        reverse=True,
    )[:10]

    pr_counts = Counter(e["pr"] for e in events)
    counts_list = list(pr_counts.values())
    avg_per_pr = sum(counts_list) / len(counts_list) if counts_list else 0

    daily_timeline = [
        {"date": d.isoformat(), "count": c}
        for d, c in sorted(daily_counts.items())
    ]

    state_counts = Counter(e["state"] for e in events)

    cutoff_90d = (datetime.now(timezone.utc).date() - timedelta(days=90))
    recent_events = [e for e in events if e["date"] >= cutoff_90d]
    recent_state_counts = Counter(e["state"] for e in recent_events)
    recent_stats = {
        "total_reviews": len(recent_events),
        "unique_prs": len(set(e["pr"] for e in recent_events)),
        "type_breakdown": dict(recent_state_counts),
    }

    return {
        "total_reviews": len(events),
        "unique_prs": len(set(e["pr"] for e in events)),
        "first_review": first_date.isoformat(),
        "last_review": last_date.isoformat(),
        "type_breakdown": dict(state_counts),
        "monthly": monthly,
        "weekly": weekly,
        "day_of_week": day_of_week,
        "busiest_days": busiest,
        "busiest_days_by_prs": busiest_by_prs,
        "review_depth": {
            "avg_per_pr": round(avg_per_pr, 2),
            "single_review": sum(1 for c in counts_list if c == 1),
            "two_three_reviews": sum(1 for c in counts_list if 2 <= c <= 3),
            "four_plus_reviews": sum(1 for c in counts_list if c >= 4),
        },
        "daily_timeline": daily_timeline,
        "recent_stats": recent_stats,
    }


def main():
    args = parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        config = {}

    repo = args.repo or config.get("repository", "vllm-project/vllm")
    reviewers = (
        args.reviewers.split(",") if args.reviewers
        else config.get("reviewers", [])
    )
    since = args.since or config.get("since", (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%d"))

    if not reviewers:
        print("Error: no reviewers specified (use --reviewers or config.json)", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching review stats for {len(reviewers)} reviewers on {repo} since {since}", file=sys.stderr)

    all_stats = {}
    for i, user in enumerate(reviewers):
        print(f"\n[{i + 1}/{len(reviewers)}] {user}...", file=sys.stderr)

        print(f"  Discovering reviewed PRs...", file=sys.stderr)
        pr_numbers = fetch_reviewed_prs(repo, user, since)
        print(f"  Found {len(pr_numbers)} PRs", file=sys.stderr)

        if not pr_numbers:
            all_stats[user] = compute_stats([])
            continue

        print(f"  Fetching review details...", file=sys.stderr)
        events = fetch_review_events(repo, user, pr_numbers)
        print(f"  Got {len(events)} review events", file=sys.stderr)

        all_stats[user] = compute_stats(events)

    leaderboard = sorted(
        [
            {
                "user": user,
                "total_reviews": stats["total_reviews"],
                "unique_prs": stats["unique_prs"],
                "approved": stats["type_breakdown"].get("APPROVED", 0),
                "commented": stats["type_breakdown"].get("COMMENTED", 0),
                "changes_requested": stats["type_breakdown"].get("CHANGES_REQUESTED", 0),
            }
            for user, stats in all_stats.items()
        ],
        key=lambda x: x["total_reviews"],
        reverse=True,
    )

    recent_leaderboard = sorted(
        [
            {
                "user": user,
                "total_reviews": stats["recent_stats"]["total_reviews"],
                "unique_prs": stats["recent_stats"]["unique_prs"],
                "approved": stats["recent_stats"]["type_breakdown"].get("APPROVED", 0),
                "commented": stats["recent_stats"]["type_breakdown"].get("COMMENTED", 0),
                "changes_requested": stats["recent_stats"]["type_breakdown"].get("CHANGES_REQUESTED", 0),
            }
            for user, stats in all_stats.items()
        ],
        key=lambda x: x["total_reviews"],
        reverse=True,
    )

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repository": repo,
        "since": since,
        "reviewers": all_stats,
        "leaderboard": leaderboard,
        "recent_leaderboard": recent_leaderboard,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote {args.output}", file=sys.stderr)

    if args.inline:
        try:
            with open("docs/index.html") as f:
                html = f.read()
            marker = "// __INLINE_DATA__"
            if marker in html:
                inline_data = f"const INLINE_DATA = {json.dumps(output)};"
                html = html.replace(marker, inline_data)
                with open("docs/index.html", "w") as f:
                    f.write(html)
                print("Inlined data into docs/index.html", file=sys.stderr)
        except FileNotFoundError:
            print("Warning: docs/index.html not found, skipping --inline", file=sys.stderr)


if __name__ == "__main__":
    main()
