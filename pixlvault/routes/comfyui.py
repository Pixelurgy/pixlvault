import json
import mimetypes
import os
from copy import deepcopy

import requests
from fastapi import APIRouter, Body, HTTPException, Request
from sqlmodel import select

from pixlvault.db_models import Picture
from pixlvault.picture_utils import PictureUtils
from pixlvault.pixl_logging import get_logger

logger = get_logger(__name__)

PLACEHOLDER_IMAGE = "{{image_path}}"
PLACEHOLDER_CAPTION = "{{caption}}"
DEFAULT_COMFYUI_URL = "http://127.0.0.1:8188/"


def _workflow_dir() -> str:
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return os.path.join(base_dir, "comfyui-workflows")


def _normalize_workflow_name(name: str) -> str:
    safe = os.path.basename(name or "").strip()
    if not safe:
        return ""
    if not safe.lower().endswith(".json"):
        safe = f"{safe}.json"
    return safe


def _load_workflow_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _save_workflow_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True)


def _find_placeholder_usage(payload: dict) -> tuple[bool, list[str]]:
    dump = json.dumps(payload, ensure_ascii=False)
    missing = []
    for placeholder in (PLACEHOLDER_IMAGE, PLACEHOLDER_CAPTION):
        if placeholder not in dump:
            missing.append(placeholder)
    return len(missing) == 0, missing


def _replace_placeholders(value, replacements: dict[str, str]):
    if isinstance(value, str):
        updated = value
        for key, replacement in replacements.items():
            if key in updated:
                updated = updated.replace(key, replacement)
        return updated
    if isinstance(value, list):
        return [_replace_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_placeholders(val, replacements) for key, val in value.items()}
    return value


def _upload_image_to_comfyui(base_url: str, file_path: str) -> str:
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = "application/octet-stream"
    with open(file_path, "rb") as handle:
        files = {
            "image": (os.path.basename(file_path), handle, mime_type),
        }
        data = {
            "type": "input",
            "overwrite": "true",
        }
        try:
            response = requests.post(
                f"{base_url}/upload/image",
                files=files,
                data=data,
                timeout=30,
            )
        except requests.RequestException as exc:
            logger.warning("ComfyUI upload request failed: %s", exc)
            raise HTTPException(
                status_code=502,
                detail="ComfyUI upload request failed",
            ) from exc
    if response.status_code >= 300:
        detail = (response.text or "").strip()
        logger.warning(
            "ComfyUI upload failed: status=%s detail=%s",
            response.status_code,
            detail,
        )
        raise HTTPException(
            status_code=502,
            detail=f"ComfyUI upload failed: {response.status_code} {detail}",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        detail = (response.text or "").strip()
        logger.warning("ComfyUI upload invalid JSON: %s", detail)
        raise HTTPException(
            status_code=502,
            detail="ComfyUI upload returned invalid JSON",
        ) from exc
    name = payload.get("name") or payload.get("filename")
    if not name:
        raise HTTPException(status_code=502, detail="ComfyUI upload response missing name")
    subfolder = payload.get("subfolder") or ""
    if subfolder:
        return f"{subfolder}/{name}"
    return name


def _submit_comfyui_prompt(base_url: str, workflow: dict) -> dict:
    try:
        response = requests.post(
            f"{base_url}/prompt",
            json={"prompt": workflow},
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.warning("ComfyUI prompt request failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="ComfyUI prompt request failed",
        ) from exc
    if response.status_code >= 300:
        detail = (response.text or "").strip()
        logger.warning(
            "ComfyUI prompt failed: status=%s detail=%s",
            response.status_code,
            detail,
        )
        raise HTTPException(
            status_code=502,
            detail=f"ComfyUI prompt failed: {response.status_code} {detail}",
        )
    try:
        return response.json()
    except ValueError as exc:
        detail = (response.text or "").strip()
        logger.warning("ComfyUI prompt invalid JSON: %s", detail)
        raise HTTPException(
            status_code=502,
            detail="ComfyUI prompt returned invalid JSON",
        ) from exc


def create_router(server) -> APIRouter:
    router = APIRouter()

    @router.get("/comfyui/workflows")
    async def list_comfyui_workflows():
        workflow_dir = _workflow_dir()
        if not os.path.isdir(workflow_dir):
            return {"workflows": [], "workflow_dir": workflow_dir}

        workflows = []
        for entry in sorted(os.listdir(workflow_dir)):
            if not entry.lower().endswith(".json"):
                continue
            path = os.path.join(workflow_dir, entry)
            try:
                payload = _load_workflow_json(path)
                valid, missing = _find_placeholder_usage(payload)
            except Exception as exc:
                logger.warning("Failed to read workflow %s: %s", entry, exc)
                valid = False
                missing = [PLACEHOLDER_IMAGE, PLACEHOLDER_CAPTION]
            workflows.append(
                {
                    "name": entry,
                    "display_name": os.path.splitext(entry)[0],
                    "valid": valid,
                    "missing_placeholders": missing,
                }
            )
        return {"workflows": workflows, "workflow_dir": workflow_dir}

    @router.delete("/comfyui/workflows/{workflow_name}")
    async def delete_comfyui_workflow(workflow_name: str):
        normalized = _normalize_workflow_name(workflow_name)
        if not normalized:
            raise HTTPException(status_code=400, detail="workflow_name is required")
        workflow_dir = _workflow_dir()
        path = os.path.join(workflow_dir, normalized)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="Workflow not found")
        try:
            os.remove(path)
        except OSError as exc:
            logger.warning("Failed to delete workflow %s: %s", normalized, exc)
            raise HTTPException(status_code=500, detail="Failed to delete workflow")
        return {"status": "success", "name": normalized}

    @router.post("/comfyui/run_i2i")
    async def run_comfyui_i2i(request: Request, payload: dict = Body(...)):
        workflow_name = _normalize_workflow_name(payload.get("workflow_name"))
        if not workflow_name:
            raise HTTPException(status_code=400, detail="workflow_name is required")

        raw_ids = payload.get("picture_ids")
        if raw_ids is None:
            raw_ids = [payload.get("picture_id")]
        if not isinstance(raw_ids, list) or not raw_ids:
            raise HTTPException(status_code=400, detail="picture_ids must be a list")
        try:
            picture_ids = [int(pid) for pid in raw_ids if pid is not None]
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="picture_ids must be integers")
        if not picture_ids:
            raise HTTPException(status_code=400, detail="picture_ids must be integers")

        caption = payload.get("caption") or ""
        if not isinstance(caption, str):
            caption = str(caption)

        workflow_dir = _workflow_dir()
        workflow_path = os.path.join(workflow_dir, workflow_name)
        if not os.path.isfile(workflow_path):
            raise HTTPException(status_code=404, detail="Workflow not found")

        workflow_payload = _load_workflow_json(workflow_path)
        valid, missing = _find_placeholder_usage(workflow_payload)
        if not valid:
            raise HTTPException(
                status_code=400,
                detail=f"Workflow missing placeholders: {', '.join(missing)}",
            )

        user = server.auth.get_user_for_request(request)
        comfyui_url = getattr(user, "comfyui_url", None) if user else None
        comfyui_url = (comfyui_url or DEFAULT_COMFYUI_URL).rstrip("/")

        def fetch_pictures(session, ids: list[int]):
            return session.exec(select(Picture).where(Picture.id.in_(ids))).all()

        pics = server.vault.db.run_task(fetch_pictures, picture_ids)
        pic_map = {pic.id: pic for pic in pics}

        prompts = []
        for pic_id in picture_ids:
            pic = pic_map.get(pic_id)
            if not pic or not getattr(pic, "file_path", None):
                raise HTTPException(status_code=404, detail="Picture not found")
            resolved_path = PictureUtils.resolve_picture_path(
                server.vault.image_root, pic.file_path
            )
            if not resolved_path or not os.path.isfile(resolved_path):
                raise HTTPException(status_code=404, detail="Picture file missing")

            uploaded_name = _upload_image_to_comfyui(comfyui_url, resolved_path)
            replacements = {
                PLACEHOLDER_IMAGE: uploaded_name,
                PLACEHOLDER_CAPTION: caption,
            }
            workflow_instance = _replace_placeholders(
                deepcopy(workflow_payload), replacements
            )
            response_payload = _submit_comfyui_prompt(comfyui_url, workflow_instance)
            prompt_id = response_payload.get("prompt_id") or response_payload.get("id")
            prompts.append(
                {
                    "picture_id": pic_id,
                    "prompt_id": prompt_id,
                    "workflow": workflow_name,
                }
            )

        return {"status": "success", "prompts": prompts}

    @router.post("/comfyui/workflows/import")
    async def import_comfyui_workflow(payload: dict = Body(...)):
        name = _normalize_workflow_name(payload.get("name"))
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        workflow = payload.get("workflow")
        if not isinstance(workflow, dict):
            raise HTTPException(status_code=400, detail="workflow must be a JSON object")
        overwrite = bool(payload.get("overwrite"))

        workflow_dir = _workflow_dir()
        os.makedirs(workflow_dir, exist_ok=True)
        path = os.path.join(workflow_dir, name)
        if os.path.exists(path) and not overwrite:
            raise HTTPException(status_code=409, detail="Workflow already exists")

        _save_workflow_json(path, workflow)
        return {
            "status": "success",
            "name": name,
            "workflow_dir": workflow_dir,
        }

    return router
