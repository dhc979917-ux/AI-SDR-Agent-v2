"""Read-only acceptance check for a running local Demo; never calls a model."""
from pathlib import Path
import json
import sys
import os
import getpass
import argparse
import re

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sdr.models import Lead


def main():
    parser=argparse.ArgumentParser(description='登录本机Demo并核验已审核合成记录；不调用外部模型')
    parser.add_argument('--credentials-file',type=Path,help='仅用于本机首次管理员凭据文件；密码不会打印')
    args=parser.parse_args()
    if args.credentials_file:
        saved=args.credentials_file.read_text()
        username=re.search(r'账号：([^\n]+)',saved).group(1)
        password=re.search(r'密码：([^\n]+)',saved).group(1)
    else:
        username=os.getenv('SDR_CHECK_USERNAME') or input('登录账号：').strip()
        password=os.getenv('SDR_CHECK_PASSWORD') or getpass.getpass('密码：')
    with httpx.Client(base_url="http://127.0.0.1:8765", timeout=10, trust_env=False) as client:
        health = client.get("/healthz"); health.raise_for_status()
        login=client.post('/api/auth/login',json={'username':username,'password':password})
        login.raise_for_status()
        client.headers['X-CSRF-Token']=login.json()['csrf']
        rows_response=client.get('/api/analyses');rows_response.raise_for_status()
        rows = rows_response.json()
        row = next((r for r in rows if r["data_mode"] == "synthetic" and r["review_status"] == "approved"),None)
        if row is None:
            parser.error('请先生成并批准一条合成演示记录，再使用有权访问它的账号验证')
        response = client.get("/api/exports/" + row["id"])
        response.raise_for_status()
        export = response.json()
        fields = {k: v for k, v in export.items() if k != "_meta"}
        Lead.model_validate(fields)
        assert len(fields) == 15 and export["_meta"]["demo"]
        assert export["_meta"]["review"]["status"] == "approved"
        qualified = client.get("/api/exports/" + row["id"], params={"kind": "qualified"})
        assert qualified.status_code == 409
        csv = client.get("/api/export.csv"); csv.raise_for_status()
        assert "review_status" in csv.text
        output = ROOT / "docs/examples"
        output.mkdir(parents=True, exist_ok=True)
        (output / "verified-reviewed.json").write_text(json.dumps(export, ensure_ascii=False, indent=2))
        (ROOT / "eval/artifacts/local-api-check.json").write_text(json.dumps({
            "health": health.json(), "fields": len(fields), "candidate_export": response.status_code,
            "synthetic_qualified_export_blocked": qualified.status_code,
            "csv_export": csv.status_code, "analysis_id": row["id"], "revision": row["revision"],
            "note": "本机已完成审核的合成记录；只读取API，未调用模型。"
        }, ensure_ascii=False, indent=2))
        client.post('/api/auth/logout').raise_for_status()
        print("PASS: health, 15-field JSON, review metadata, synthetic export block, CSV")


if __name__ == "__main__":
    main()
