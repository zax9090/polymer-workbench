"""Paste-to-run sheets over HTTP; parsing and execution live in sheets.py."""
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from .research_api import ResearchRoute
from .sheets import MAX_TEXT, TEMPLATES, preview

router = APIRouter(prefix="/api/research/sheets", route_class=ResearchRoute)


class SheetText(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=MAX_TEXT)


@router.get("/templates")
def templates():
    return {"format": "Polymer Lab 실행표 v1", "templates": TEMPLATES}


@router.post("/preview")
def preview_sheet(payload: SheetText, request: Request):
    return preview(payload.text, request.app.state.manager)


@router.post("", status_code=201)
def create_sheet(payload: SheetText, request: Request):
    from .server import client_label
    return request.app.state.sheets.create(payload.text, client_label(request) or "local")


@router.get("")
def list_sheets(request: Request):
    return {"records": request.app.state.sheets.list()}


@router.get("/{identifier}")
def get_sheet(identifier: str, request: Request):
    return request.app.state.sheets.get(identifier)


@router.get("/{identifier}/text")
def sheet_text(identifier: str, request: Request):
    return PlainTextResponse(request.app.state.sheets.text(identifier))


@router.get("/{identifier}/csv")
def sheet_csv(identifier: str, request: Request):
    path = request.app.state.sheets.csv_path(identifier)
    return FileResponse(path, media_type="text/csv; charset=utf-8", filename=f"PolymerLab-sheet-{identifier}.csv")


@router.post("/{identifier}/cancel")
def cancel_sheet(identifier: str, request: Request):
    return request.app.state.sheets.cancel(identifier)


@router.post("/{identifier}/resume")
def resume_sheet(identifier: str, request: Request):
    return request.app.state.sheets.resume(identifier)
