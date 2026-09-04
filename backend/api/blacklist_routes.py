"""Blacklist API routes — CRUD, batch ops, CSV/XLSX import/export, template.

Prefix: /api/blacklist
All write/read endpoints require API access (same as the hunt router).
"""

from __future__ import annotations

import csv
import io
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api.security import require_api_access
from config.settings import get_settings
from tools.blacklist_store import BlacklistStore, is_valid_domain

router = APIRouter(prefix="/api/v1/blacklist", tags=["blacklist"])


# ── Schemas ─────────────────────────────────────────────────────────────────
class BlacklistEntry(BaseModel):
    id: int
    customer_name: str
    domain: str
    note: str = ""
    tags: str = ""
    source: str = ""
    created_at: str = ""
    updated_at: str = ""


class BlacklistCreate(BaseModel):
    customer_name: str = ""
    domain: str
    note: str = ""
    tags: str = ""
    source: str = "manual"


class BlacklistUpdate(BaseModel):
    customer_name: str | None = None
    domain: str | None = None
    note: str | None = None
    tags: str | None = None


class BlacklistBatch(BaseModel):
    action: Literal["delete", "tag"]
    ids: list[int] = Field(default_factory=list)
    tag: str = ""


class UpsertResult(BaseModel):
    status: str
    id: int
    domain: str
    customer_name: str


class ImportResult(BaseModel):
    imported: int
    updated: int
    skipped: int
    errors: list[dict] = Field(default_factory=list)


# ── helpers ─────────────────────────────────────────────────────────────────
def _store() -> BlacklistStore:
    store = BlacklistStore(get_settings().blacklist_db_path)
    store.init_db()
    return store


def _entry_to_dict(row: dict) -> dict:
    return row


# ── CRUD ────────────────────────────────────────────────────────────────────
@router.get("", dependencies=[Depends(require_api_access)])
async def list_blacklist(
    q: str = Query(default=""),
    tag: str = Query(default=""),
    source: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
):
    """List blacklist entries with optional filters and pagination."""
    store = _store()
    try:
        return store.list(q=q, tag=tag, source=source, page=page, page_size=page_size)
    finally:
        store.close()


@router.get("/check", dependencies=[Depends(require_api_access)])
async def check_entry(domain: str = Query(default=""), name: str = Query(default="")):
    """Validate + probe uniqueness for a candidate domain / company name."""
    store = _store()
    try:
        return store.check(domain=domain, customer_name=name)
    finally:
        store.close()


@router.post("", status_code=201, dependencies=[Depends(require_api_access)])
async def create_entry(payload: BlacklistCreate) -> UpsertResult:
    store = _store()
    try:
        try:
            result = store.upsert(
                payload.customer_name,
                payload.domain,
                note=payload.note,
                tags=payload.tags,
                source=payload.source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        entry = result["entry"] or {}
        return UpsertResult(
            status=result["status"],
            id=result["id"],
            domain=entry.get("domain", ""),
            customer_name=entry.get("customer_name", ""),
        )
    finally:
        store.close()


@router.put("/{entry_id}", dependencies=[Depends(require_api_access)])
async def update_entry(entry_id: int, payload: BlacklistUpdate):
    store = _store()
    try:
        try:
            entry = store.update(
                entry_id,
                customer_name=payload.customer_name,
                domain=payload.domain,
                note=payload.note,
                tags=payload.tags,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if entry is None:
            raise HTTPException(status_code=404, detail="Blacklist entry not found")
        return entry
    finally:
        store.close()


@router.delete("/{entry_id}", status_code=204, dependencies=[Depends(require_api_access)])
async def delete_entry(entry_id: int):
    store = _store()
    try:
        if not store.delete(entry_id):
            raise HTTPException(status_code=404, detail="Blacklist entry not found")
    finally:
        store.close()


# ── Batch ───────────────────────────────────────────────────────────────────
@router.post("/batch", dependencies=[Depends(require_api_access)])
async def batch_operation(payload: BlacklistBatch):
    store = _store()
    try:
        if not payload.ids:
            return {"affected": 0}
        if payload.action == "delete":
            return {"affected": store.delete_many(payload.ids)}
        return {"affected": store.add_tags(payload.ids, payload.tag)}
    finally:
        store.close()


# ── Import / Export / Template ──────────────────────────────────────────────
_TEMPLATE_HEADERS = ["customer_name", "domain", "note", "tags"]


def _parse_import_file(content: bytes, filename: str) -> list[dict[str, str]]:
    """Parse CSV or XLSX bytes into a list of header→value rows."""
    name = (filename or "").lower()
    rows: list[dict[str, str]] = []
    if name.endswith(".xlsx") or name.endswith(".xls"):
        import pandas as pd

        df = pd.read_excel(io.BytesIO(content))
        for _, r in df.iterrows():
            rows.append({str(k): ("" if pd.isna(v) else str(v).strip()) for k, v in r.items()})
    else:  # treat as CSV
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        for r in reader:
            rows.append({(k or "").strip(): (v or "").strip() for k, v in r.items()})
    return rows


def _row_field(row: dict[str, str], *names: str) -> str:
    """Return the first non-empty value for any of the given (case-insensitive) header names."""
    lowered = {k.lower(): v for k, v in row.items()}
    for n in names:
        if lowered.get(n.lower(), "").strip():
            return lowered[n.lower()].strip()
    return ""


@router.post("/import", response_model=ImportResult, dependencies=[Depends(require_api_access)])
async def import_blacklist(file: UploadFile = File(...)):
    content = await file.read()
    rows = _parse_import_file(content, file.filename or "")
    store = _store()
    imported = updated = skipped = 0
    errors: list[dict] = []
    try:
        for idx, row in enumerate(rows, start=1):
            customer_name = _row_field(row, "customer_name", "name", "company", "公司名称")
            domain = _row_field(row, "domain", "website", "url", "官网域名")
            note = _row_field(row, "note", "备注")
            tags = _row_field(row, "tags", "标签")
            if not domain:
                skipped += 1
                errors.append({"row": idx, "error": "missing domain", "customer_name": customer_name})
                continue
            if not is_valid_domain(domain):
                skipped += 1
                errors.append({"row": idx, "error": f"invalid domain {domain!r}", "customer_name": customer_name})
                continue
            try:
                result = store.upsert(customer_name, domain, note=note, tags=tags, source="import")
                if result["status"] == "created":
                    imported += 1
                else:
                    updated += 1
            except ValueError as exc:
                skipped += 1
                errors.append({"row": idx, "error": str(exc), "customer_name": customer_name})
        return ImportResult(imported=imported, updated=updated, skipped=skipped, errors=errors)
    finally:
        store.close()


@router.get("/export", dependencies=[Depends(require_api_access)])
async def export_blacklist():
    store = _store()
    try:
        data = store.list(page=1, page_size=100000)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_TEMPLATE_HEADERS)
        for item in data["items"]:
            writer.writerow([item["customer_name"], item["domain"], item["note"], item["tags"]])
        # UTF-8 BOM so Excel opens Chinese content correctly.
        csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
        return Response(
            content=csv_bytes,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="blacklist.csv"'},
        )
    finally:
        store.close()


@router.get("/template", dependencies=[Depends(require_api_access)])
async def download_template(fmt: str = Query(default="csv", alias="format")):
    """Download an import template (csv or xlsx)."""
    if fmt == "xlsx":
        import pandas as pd

        df = pd.DataFrame(columns=_TEMPLATE_HEADERS)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="blacklist")
        return Response(
            content=buf.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="blacklist_template.xlsx"'},
        )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_TEMPLATE_HEADERS)
    writer.writerow(["Acme GmbH", "acme.com", "已合作客户", "competitor"])
    writer.writerow(["Beta Ltd", "www.beta.co.uk", "", "partner"])
    csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="blacklist_template.csv"'},
    )
