from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Request, Query
from sqlalchemy.orm import load_only, selectinload
from sqlmodel import Session, select

from pixlvault.db_models import Picture, PictureStack
from pixlvault.pixl_logging import get_logger
from pixlvault.utils import safe_model_dict

logger = get_logger(__name__)


def create_router(server) -> APIRouter:
    router = APIRouter()

    def _ensure_secure_when_required(request: Request):
        server.auth.ensure_secure_when_required(request)

    def _normalize_picture_ids(raw_ids) -> list[int]:
        if not isinstance(raw_ids, list):
            raise HTTPException(status_code=400, detail="picture_ids must be a list")
        ids = []
        for raw_id in raw_ids:
            try:
                ids.append(int(raw_id))
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail="picture_ids must be integers",
                )
        if not ids:
            raise HTTPException(status_code=400, detail="picture_ids must not be empty")
        return ids

    def _fetch_stack_pictures(session: Session, stack_id: int):
        return session.exec(
            select(Picture)
            .where(Picture.stack_id == stack_id)
            .order_by(Picture.id)
        ).all()

    @router.get("/stacks/{stack_id}")
    async def get_stack(stack_id: int, request: Request):
        _ensure_secure_when_required(request)
        server.auth.require_user_id(request)

        def fetch_stack(session: Session, stack_id: int):
            stack = session.get(PictureStack, stack_id)
            if not stack:
                return None, []
            pictures = _fetch_stack_pictures(session, stack_id)
            return stack, pictures

        stack, pictures = server.vault.db.run_task(fetch_stack, stack_id)
        if not stack:
            raise HTTPException(status_code=404, detail="Stack not found")

        payload = safe_model_dict(stack)
        payload["picture_ids"] = [pic.id for pic in pictures]
        return payload

    @router.get("/stacks/{stack_id}/pictures")
    async def get_stack_pictures(
        stack_id: int,
        request: Request,
        fields: str = Query("grid"),
    ):
        _ensure_secure_when_required(request)
        server.auth.require_user_id(request)

        def fetch_stack_pictures(
            session: Session,
            stack_id_value: int,
            fields_value: str,
        ):
            stack = session.get(PictureStack, stack_id_value)
            if not stack:
                return None, None

            select_fields = (
                Picture.grid_fields()
                if fields_value == "grid"
                else Picture.metadata_fields()
            )
            query = (
                select(Picture)
                .where(Picture.stack_id == stack_id_value)
                .order_by(Picture.id)
            )

            if select_fields:
                select_fields = list(set(select_fields) | {"id"})
                scalar_attrs = [
                    getattr(Picture, field)
                    for field in Picture.scalar_fields().intersection(select_fields)
                ]
                if scalar_attrs:
                    query = query.options(load_only(*scalar_attrs))
                rel_attrs = [
                    getattr(Picture, field)
                    for field in Picture.relationship_fields().intersection(
                        select_fields
                    )
                ]
                for rel_attr in rel_attrs:
                    query = query.options(selectinload(rel_attr))

            pictures = session.exec(query).all()
            return select_fields, pictures

        select_fields, pictures = server.vault.db.run_task(
            fetch_stack_pictures,
            stack_id,
            fields,
        )
        if select_fields is None:
            raise HTTPException(status_code=404, detail="Stack not found")
        return [
            {field: safe_model_dict(pic).get(field) for field in select_fields}
            for pic in pictures
        ]

    @router.get("/pictures/{picture_id}/stack")
    async def get_stack_for_picture(picture_id: int, request: Request):
        _ensure_secure_when_required(request)
        server.auth.require_user_id(request)

        def fetch_stack_for_picture(session: Session, picture_id: int):
            pic = session.get(Picture, picture_id)
            if not pic or not pic.stack_id:
                return None, None, []
            stack = session.get(PictureStack, pic.stack_id)
            pictures = _fetch_stack_pictures(session, pic.stack_id)
            return pic.stack_id, stack, pictures

        stack_id, stack, pictures = server.vault.db.run_task(
            fetch_stack_for_picture, picture_id
        )
        if not stack_id or not stack:
            return {"stack_id": None, "picture_ids": []}

        payload = safe_model_dict(stack)
        payload["picture_ids"] = [pic.id for pic in pictures]
        return payload

    @router.post("/stacks")
    async def create_stack(payload: dict = Body(...), request: Request = None):
        _ensure_secure_when_required(request)
        server.auth.require_user_id(request)

        picture_ids = _normalize_picture_ids(payload.get("picture_ids") or [])
        name = payload.get("name")
        if name is not None and not isinstance(name, str):
            name = str(name)

        def create_or_assign_stack(session: Session, picture_ids: list[int], name: Optional[str]):
            pictures = session.exec(
                select(Picture).where(Picture.id.in_(picture_ids))
            ).all()
            if len(pictures) != len(picture_ids):
                missing = sorted(set(picture_ids) - {pic.id for pic in pictures})
                raise HTTPException(
                    status_code=404,
                    detail=f"Pictures not found: {missing}",
                )

            existing_stack_ids = {pic.stack_id for pic in pictures if pic.stack_id}
            if len(existing_stack_ids) > 1:
                raise HTTPException(
                    status_code=409,
                    detail="Pictures already belong to multiple stacks",
                )

            if existing_stack_ids:
                stack_id = existing_stack_ids.pop()
                stack = session.get(PictureStack, stack_id)
                if stack is None:
                    raise HTTPException(status_code=404, detail="Stack not found")
            else:
                stack = PictureStack(name=name)
                session.add(stack)
                session.commit()
                session.refresh(stack)

            for pic in pictures:
                pic.stack_id = stack.id
                session.add(pic)

            stack.updated_at = datetime.utcnow()
            session.add(stack)
            session.commit()
            return stack

        stack = server.vault.db.run_task(
            create_or_assign_stack, picture_ids, name
        )
        payload = safe_model_dict(stack)
        payload["picture_ids"] = picture_ids
        return payload

    @router.post("/stacks/{stack_id}/members")
    async def add_stack_members(
        stack_id: int, payload: dict = Body(...), request: Request = None
    ):
        _ensure_secure_when_required(request)
        server.auth.require_user_id(request)

        picture_ids = _normalize_picture_ids(payload.get("picture_ids") or [])

        def add_members(session: Session, stack_id: int, picture_ids: list[int]):
            stack = session.get(PictureStack, stack_id)
            if stack is None:
                raise HTTPException(status_code=404, detail="Stack not found")

            pictures = session.exec(
                select(Picture).where(Picture.id.in_(picture_ids))
            ).all()
            if len(pictures) != len(picture_ids):
                missing = sorted(set(picture_ids) - {pic.id for pic in pictures})
                raise HTTPException(
                    status_code=404,
                    detail=f"Pictures not found: {missing}",
                )

            conflicts = [pic.id for pic in pictures if pic.stack_id not in (None, stack_id)]
            if conflicts:
                raise HTTPException(
                    status_code=409,
                    detail=f"Pictures already in another stack: {sorted(conflicts)}",
                )

            for pic in pictures:
                pic.stack_id = stack_id
                session.add(pic)

            stack.updated_at = datetime.utcnow()
            session.add(stack)
            session.commit()
            return stack

        stack = server.vault.db.run_task(add_members, stack_id, picture_ids)
        payload = safe_model_dict(stack)
        payload["picture_ids"] = picture_ids
        return payload

    @router.delete("/stacks/{stack_id}/members")
    async def remove_stack_members(
        stack_id: int, payload: dict = Body(...), request: Request = None
    ):
        _ensure_secure_when_required(request)
        server.auth.require_user_id(request)

        picture_ids = _normalize_picture_ids(payload.get("picture_ids") or [])

        def remove_members(session: Session, stack_id: int, picture_ids: list[int]):
            stack = session.get(PictureStack, stack_id)
            if stack is None:
                raise HTTPException(status_code=404, detail="Stack not found")

            pictures = session.exec(
                select(Picture).where(Picture.id.in_(picture_ids))
            ).all()
            for pic in pictures:
                if pic.stack_id == stack_id:
                    pic.stack_id = None
                    session.add(pic)

            remaining = session.exec(
                select(Picture.id).where(Picture.stack_id == stack_id)
            ).first()

            if remaining is None:
                session.delete(stack)
            else:
                stack.updated_at = datetime.utcnow()
                session.add(stack)

            session.commit()
            return stack

        stack = server.vault.db.run_task(remove_members, stack_id, picture_ids)
        if stack is None:
            return {"status": "success", "stack_id": None, "picture_ids": picture_ids}

        payload = safe_model_dict(stack)
        payload["picture_ids"] = picture_ids
        return payload

    return router
