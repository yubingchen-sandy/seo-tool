"""Write the daily shortlist into a Feishu/Lark Base (多维表格) review queue.

Env:
  LARK_APP_ID / LARK_APP_SECRET   custom app with bitable:app scope, added as
                                  an editor (可编辑) collaborator of the Base
  LARK_BASE_APP_TOKEN             the Base's app_token (from its URL: /base/<app_token>)
  LARK_BASE_TABLE_ID              table id (from the URL: ?table=<tbl...>)

Fields are created on first run if missing (idempotent). Rows are keyed by
(Date, Topic) so a manual re-run on the same day does not duplicate.
"""
from __future__ import annotations

import json
import logging
import os

import requests

log = logging.getLogger("radar.lark")
API = "https://open.feishu.cn/open-apis"

# name -> (field type, extra property). Types: 1 text, 2 number, 3 single select, 4 multi select, 15 url
FIELDS: dict[str, tuple[int, dict | None]] = {
    "Date": (1, None),
    "Topic": (1, None),
    "Decision": (3, {"options": [{"name": "new_page"}, {"name": "update_existing"}, {"name": "watch"}]}),
    "Intent_Type": (3, {"options": [{"name": n} for n in (
        "Tool_Comparison", "How_To_Tutorial", "Use_Case_Industry", "Feature_Trend_Tie_in",
        "Community_Showcase", "Pricing_Value", "FAQ_Troubleshoot")]}),
    "Page_Type": (1, None),
    "Target_Keyword": (1, None),
    "Bridge": (1, None),
    "Target_URL": (1, None),
    "Internal_Links": (1, None),
    "Signal_Title": (1, None),
    "Sources": (1, None),
    "Source_Count": (2, {"formatter": "0"}),
    "Score": (2, {"formatter": "0.0"}),
    "Confidence": (2, {"formatter": "0.00"}),
    "Risk_Flags": (1, None),
    "Evidence_URL": (15, None),
    "Status": (3, {"options": [{"name": "Pending"}, {"name": "Approved"}, {"name": "Rejected"}, {"name": "Produced"}]}),
    "Review_Notes": (1, None),
}


class LarkBase:
    def __init__(self) -> None:
        self.app_id = os.environ.get("LARK_APP_ID", "")
        self.app_secret = os.environ.get("LARK_APP_SECRET", "")
        self.app_token = os.environ.get("LARK_BASE_APP_TOKEN", "")
        self.table_id = os.environ.get("LARK_BASE_TABLE_ID", "")
        self._tok: str | None = None

    @property
    def configured(self) -> bool:
        return all([self.app_id, self.app_secret, self.app_token, self.table_id])

    # ------------------------------------------------------------------ http
    def token(self) -> str:
        if self._tok:
            return self._tok
        r = requests.post(f"{API}/auth/v3/tenant_access_token/internal",
                          json={"app_id": self.app_id, "app_secret": self.app_secret}, timeout=20).json()
        if r.get("code") != 0:
            raise RuntimeError(f"lark token error: {r}")
        self._tok = r["tenant_access_token"]
        return self._tok

    def call(self, method: str, path: str, **kw) -> dict:
        r = requests.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {self.token()}"},
                             timeout=30, **kw)
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"lark api {path} error: {json.dumps(data, ensure_ascii=False)[:400]}")
        return data.get("data", {})

    # ---------------------------------------------------------------- schema
    def ensure_fields(self) -> None:
        base = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields"
        existing = {f["field_name"] for f in self.call("GET", base, params={"page_size": 100}).get("items", [])}
        for name, (ftype, prop) in FIELDS.items():
            if name in existing:
                continue
            body = {"field_name": name, "type": ftype}
            if prop:
                body["property"] = prop
            self.call("POST", base, json=body)
            log.info("created field %s", name)

    # ----------------------------------------------------------------- rows
    def existing_topics(self, date: str) -> set[str]:
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/search"
        body = {"filter": {"conjunction": "and", "conditions": [
            {"field_name": "Date", "operator": "is", "value": [date]}]}, "page_size": 200}
        items = self.call("POST", path, json=body).get("items", [])
        return {self._text(r["fields"].get("Topic")) for r in items}

    def recent_topics(self, days: int) -> list[str]:
        """Topics queued in the last N days — fed back to the classifier for dedupe."""
        import datetime as dt
        since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/search"
        body = {"filter": {"conjunction": "and", "conditions": [
            {"field_name": "Date", "operator": "isGreater", "value": [since]}]}, "page_size": 500}
        try:
            items = self.call("POST", path, json=body).get("items", [])
        except Exception as e:  # noqa: BLE001
            log.warning("recent_topics failed: %s", e)
            return []
        return [self._text(r["fields"].get("Topic")) for r in items if r["fields"].get("Topic")]

    @staticmethod
    def _text(v) -> str:
        if isinstance(v, list):
            return "".join(seg.get("text", "") for seg in v if isinstance(seg, dict))
        return str(v or "")

    def append(self, date: str, shortlist: list[dict]) -> int:
        self.ensure_fields()
        have = self.existing_topics(date)
        records = []
        for v in shortlist:
            c = v["candidate"]
            topic = v.get("topic_en") or c["title"]
            if topic in have:
                continue
            fields = {
                "Date": date, "Topic": topic, "Decision": v.get("decision", "watch"),
                "Intent_Type": v.get("intent_type"), "Page_Type": v.get("page_type", ""),
                "Target_Keyword": v.get("target_keyword", ""), "Bridge": v.get("bridge", ""),
                "Target_URL": v.get("target_url", ""), "Internal_Links": "\n".join(v.get("internal_links", []) or []),
                "Signal_Title": c["title"], "Sources": ", ".join(c["sources"][:6]),
                "Source_Count": len(c["sources"]), "Score": float(c["score"]),
                "Confidence": float(v.get("confidence", 0) or 0),
                "Risk_Flags": ", ".join(v.get("risk_flags", []) or []),
                "Status": "Pending", "Review_Notes": "",
            }
            if c.get("urls"):
                fields["Evidence_URL"] = {"link": c["urls"][0], "text": "source"}
            records.append({"fields": fields})
        if not records:
            return 0
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/batch_create"
        for i in range(0, len(records), 100):
            self.call("POST", path, json={"records": records[i:i + 100]})
        log.info("appended %d rows to Lark Base", len(records))
        return len(records)
