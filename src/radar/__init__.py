"""Trend Radar — stage 1 of the hot-topic → SEO page plan.

gather.py    fetch signals, cluster, pre-score, sitemap overlap
classify.py  Claude maps candidates onto the closed Intent_Type set (or DROP)
lark_base.py write the shortlist into a Feishu Base review queue
run.py       orchestrates a daily run (called from .github/workflows/daily.yml)
"""
