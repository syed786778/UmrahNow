name: Update news

on:
  schedule:
    - cron: "0 3 * * *"      # every day at 03:00 UTC (06:00 Makkah time)
  workflow_dispatch:          # also lets you run it manually from the Actions tab

permissions:
  contents: write             # needed so the job can commit the refreshed page

jobs:
  refresh:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      # If your page isn't called index.html, change the filename below.
      - name: Refresh the news cards
        run: python scripts/update_news.py index.html

      - name: Commit if anything changed
        run: |
          if git diff --quiet; then
            echo "No changes to commit."
          else
            git config user.name  "news-bot"
            git config user.email "news-bot@users.noreply.github.com"
            git add -A
            git commit -m "Refresh news"
            git push
          fi
