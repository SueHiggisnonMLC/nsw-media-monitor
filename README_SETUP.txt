NSW Media Monitor - GitHub setup files

Upload these files to a PRIVATE GitHub repository together with:
- nsw_media_bot.py
- seen_releases.json

Files supplied here:
- requirements.txt
- .github/workflows/monitor.yml

Then add a repository Actions secret named SLACK_WEBHOOK containing your existing Slack webhook URL.

The workflow can be run manually from the Actions tab and is scheduled every 10 minutes:
03, 13, 23, 33, 43 and 53 minutes past each hour.
