# review-dashboard

Static web dashboard showing GitHub PR review statistics for a configurable list of reviewers.

## Quick Start

```bash
# Ensure gh CLI is authenticated
gh auth status

# Fetch review data (uses config.json)
python3 fetch_reviews.py

# Serve locally
cd docs && python3 -m http.server 8080
# Open http://localhost:8080
```

## Configuration

Edit `config.json` to set the repository and tracked reviewers:

```json
{
  "repository": "vllm-project/vllm",
  "reviewers": ["user1", "user2"],
  "since": "2025-01-01"
}
```

## CLI Options

```
python3 fetch_reviews.py                               # use config.json defaults
python3 fetch_reviews.py --reviewers user1,user2       # override reviewers
python3 fetch_reviews.py --repo owner/repo             # override repository
python3 fetch_reviews.py --since 2025-06-01            # override start date
python3 fetch_reviews.py --inline                      # embed data in HTML for file:// use
```

## Automated Updates

The GitHub Actions workflow runs daily at 06:07 UTC and commits updated data. You can also trigger it manually from the Actions tab with an optional reviewer list override.

## Hosting

Enable GitHub Pages on the `docs/` folder (Settings > Pages > Source > main branch, /docs folder).
