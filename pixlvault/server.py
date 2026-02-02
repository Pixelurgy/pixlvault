import gc
import pickle
import numpy as np
import uvicorn
import os
import json
import re
import asyncio
import threading

from collections import defaultdict
from sqlalchemy import exists, desc, func
from sqlmodel import Session, select

from contextlib import asynccontextmanager
from fastapi import (
    FastAPI,
    Request,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from pillow_heif import register_heif_opener

from pixlvault.db_models import (
    Character,
    Face,
    FaceCharacterLikeness,
    Picture,
    PictureSet,
    PictureSetMember,
    Quality,
    Tag,
    User,
    DEFAULT_SMART_SCORE_PENALIZED_TAGS,
    DEFAULT_SMART_SCORE_PENALIZED_TAG_WEIGHT,
)

from pixlvault.database import DBPriority
from pixlvault.event_types import EventType
from pixlvault.utils import (
    safe_model_dict,
    normalize_smart_score_penalized_tags,
)
from pixlvault.auth import AuthService, LoginRequest
from pixlvault.picture_utils import PictureUtils
from pixlvault.pixl_logging import get_logger, uvicorn_log_config
from pixlvault.vault import Vault
from pixlvault.watch_folder_worker import WatchFolderWorker
from pixlvault.routes.config import create_router as create_config_router
from pixlvault.routes.characters import create_router as create_characters_router
from pixlvault.routes.picture_sets import create_router as create_picture_sets_router
from pixlvault.routes.tags import create_router as create_tags_router
from pixlvault.routes.pictures import create_router as create_pictures_router


# Logging will be set up after config is loaded
logger = get_logger(__name__)


class Server:
    """
    Main server class for the PixlVault FastAPI application.

    Attributes:
        server_config_path(str): Server-side-only configuration file.
    """

    def __init__(
        self,
        server_config_path,
    ):
        """
        Initialize the Server instance.

        Args:
            server_config_path (str): Path to the server-only config file.
        """

        # Ensure garbage collection before starting server to free up memory
        # This is mainly to ensure repeated runs within the testing framework do not accumulate memory usage
        gc.collect()

        self._server_config_path = server_config_path

        self._server_config = self._init_server_config(server_config_path)
        with open(server_config_path, "w") as f:
            json.dump(self._server_config, f, indent=2)

        # SSL config
        if self._server_config.get("require_ssl", False):
            self._ensure_ssl_certificates()

        logger.info(
            "Creating Vault instance with image root: "
            + str(self._server_config["image_root"])
        )

        register_heif_opener()

        self.vault = Vault(
            image_root=self._server_config["image_root"],
            description=User().description,
        )

        WatchFolderWorker.configure(self._server_config_path)

        self._ws_clients = []
        self._ws_clients_lock = threading.Lock()
        self._ws_loop = None
        self.vault.add_event_listener(self._handle_vault_event)

        self.auth = AuthService(
            self.vault.db,
            self._server_config,
            self._server_config_path,
            logger,
        )
        self._user = self.auth.ensure_user()
        if self._user and self._user.description is not None:
            self.vault.set_description(self._user.description)

        self.api = FastAPI(lifespan=self.lifespan)
        # Enable CORS for any origin (credentials require explicit origin echo)
        self.allow_origins = []
        self.allow_origin_regex = r".*"
        self.api.add_middleware(
            CORSMiddleware,
            allow_origins=self.allow_origins,
            allow_origin_regex=self.allow_origin_regex,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self._add_cors_exception_handler()
        self._setup_routes()

        # Temporary storage for export tasks
        self.export_tasks = {}
        self.TEMP_EXPORT_DIR = "tmp/exports"
        os.makedirs(self.TEMP_EXPORT_DIR, exist_ok=True)

        # Temporary storage for import tasks
        self.import_tasks = {}
        self._shutdown_on_lifespan = False

    def __enter__(self):
        # Allow use as a context manager for robust cleanup
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self, "vault"):
            logger.warning("Closing the vault and cleaning up resources")
            self.vault.close()
        gc.collect()

    def _handle_vault_event(self, event_type: EventType, data=None):
        if not self._ws_loop:
            return
        try:
            logger.info("Got the following event from vault: %s", event_type)
            asyncio.run_coroutine_threadsafe(
                self._broadcast_ws_event(event_type, data), self._ws_loop
            )
        except Exception as exc:
            logger.debug("Failed to dispatch websocket event: %s", exc)

    def _should_send_ws_update(self, event_type: EventType, filters: dict) -> bool:
        return event_type in (
            EventType.CHANGED_PICTURES,
            EventType.CHANGED_TAGS,
            EventType.CLEARED_TAGS,
        )

    async def _broadcast_ws_event(self, event_type: EventType, data=None):
        with self._ws_clients_lock:
            clients = list(self._ws_clients)
        if not clients:
            return
        if event_type in (EventType.CHANGED_TAGS, EventType.CLEARED_TAGS):
            picture_ids = data if isinstance(data, (list, tuple, set)) else []
            payload = {
                "type": "tags_changed",
                "event": event_type.name,
                "picture_ids": list(picture_ids),
            }
        else:
            payload = {
                "type": "pictures_changed",
                "event": event_type.name,
            }
        stale = []
        for client in clients:
            ws = client.get("ws")
            filters = client.get("filters") or {}
            if not ws:
                stale.append(client)
                continue
            if not self._should_send_ws_update(event_type, filters):
                continue
            try:
                logger.info("Sending websocket event: %s", payload)
                await ws.send_json(payload)
            except Exception:
                stale.append(client)
        if stale:
            with self._ws_clients_lock:
                for client in stale:
                    if client in self._ws_clients:
                        self._ws_clients.remove(client)

    def run(self):
        self._shutdown_on_lifespan = True
        uvicorn_kwargs = dict(
            host="0.0.0.0",
            port=self._server_config.get("port", 8000),
            log_config=uvicorn_log_config,
        )
        if self._server_config.get("require_ssl", False):
            uvicorn_kwargs["ssl_keyfile"] = self._server_config.get("ssl_keyfile")
            uvicorn_kwargs["ssl_certfile"] = self._server_config.get("ssl_certfile")
            print(
                f"[SSL] Running with SSL: keyfile={self._server_config.get('ssl_keyfile')}, certfile={self._server_config.get('ssl_certfile')}"
            )
        uvicorn.run(self.api, **uvicorn_kwargs)

    @asynccontextmanager
    async def lifespan(self, app):
        # Startup logic (if needed)
        self._ws_loop = asyncio.get_running_loop()
        yield
        # Shutdown logic
        self._ws_loop = None
        if self._shutdown_on_lifespan and hasattr(self, "vault"):
            self.vault.close()

    @staticmethod
    def _init_server_config(server_config_path):
        config_dir = os.path.dirname(server_config_path)
        os.makedirs(config_dir, exist_ok=True)

        default_log_path = os.path.join(config_dir, "server.log")
        default_ssl_cert_path = os.path.join(config_dir, "ssl", "cert.pem")
        default_ssl_key_path = os.path.join(config_dir, "ssl", "key.pem")
        default_image_root = os.path.join(config_dir, "images")

        server_config = {}
        if not os.path.exists(server_config_path):
            server_config = {
                "host": "localhost",
                "port": 8000,
                "log_level": "info",
                "log_file": default_log_path,
                "require_ssl": False,
                "ssl_keyfile": default_ssl_key_path,
                "ssl_certfile": default_ssl_cert_path,
                "cookie_samesite": "Lax",
                "cookie_secure": False,
                "image_root": default_image_root,
                "default_device": "cpu",
                "USERNAME": None,
                "watch_folders": [],
            }
            with open(server_config_path, "w") as f:
                json.dump(server_config, f, indent=2)
        else:
            with open(server_config_path, "r") as f:
                server_config = json.load(f)

                # Ensure server config options exist
                if "host" not in server_config:
                    server_config["host"] = "localhost"
                if "port" not in server_config:
                    server_config["port"] = 8000
                if "log_level" not in server_config:
                    server_config["log_level"] = "info"
                if "log_file" not in server_config:
                    server_config["log_file"] = default_log_path
                if "require_ssl" not in server_config:
                    server_config["require_ssl"] = False
                if "ssl_keyfile" not in server_config:
                    server_config["ssl_keyfile"] = default_ssl_key_path
                if "ssl_certfile" not in server_config:
                    server_config["ssl_certfile"] = default_ssl_cert_path
                if "cookie_samesite" not in server_config:
                    server_config["cookie_samesite"] = "Lax"
                if "cookie_secure" not in server_config:
                    server_config["cookie_secure"] = False
                if "image_root" not in server_config:
                    server_config["image_root"] = default_image_root
                if "default_device" not in server_config:
                    server_config["default_device"] = "cpu"
                if "USERNAME" not in server_config:
                    server_config["USERNAME"] = None
                if "watch_folders" not in server_config:
                    server_config["watch_folders"] = []

        return server_config

    def _get_smart_score_penalized_tags_from_request(self, request: Request):
        user_id = self.auth.get_user_id(request)
        if user_id is None:
            return DEFAULT_SMART_SCORE_PENALIZED_TAGS
        user = self.vault.db.run_task(
            lambda session: session.get(User, user_id),
            priority=DBPriority.IMMEDIATE,
        )
        return normalize_smart_score_penalized_tags(
            user.smart_score_penalized_tags if user else None,
            DEFAULT_SMART_SCORE_PENALIZED_TAGS,
            default_weight=DEFAULT_SMART_SCORE_PENALIZED_TAG_WEIGHT,
        )

    def _ensure_ssl_certificates(self):
        import subprocess

        keyfile = self._server_config.get("ssl_keyfile")
        certfile = self._server_config.get("ssl_certfile")
        # If either file is missing, generate self-signed cert
        if not (os.path.exists(keyfile) and os.path.exists(certfile)):
            os.makedirs(os.path.dirname(keyfile), exist_ok=True)
            os.makedirs(os.path.dirname(certfile), exist_ok=True)
            print(f"[SSL] Generating self-signed certificate: {certfile}, {keyfile}")
            try:
                subprocess.run(
                    [
                        "openssl",
                        "req",
                        "-x509",
                        "-nodes",
                        "-days",
                        "365",
                        "-newkey",
                        "rsa:2048",
                        "-keyout",
                        keyfile,
                        "-out",
                        certfile,
                        "-subj",
                        "/CN=localhost",
                    ],
                    check=True,
                )
            except Exception as e:
                print(f"[SSL] Failed to generate self-signed certificate: {e}")
                raise

    def _add_cors_exception_handler(self):
        @self.api.exception_handler(HTTPException)
        async def cors_exception_handler(request, exc):
            origin = request.headers.get("origin")
            headers = {
                "Access-Control-Allow-Credentials": "true",
            }
            if origin and (
                origin in self.allow_origins
                or (
                    self.allow_origin_regex
                    and re.match(self.allow_origin_regex, origin)
                )
            ):
                headers["Access-Control-Allow-Origin"] = origin
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=headers,
            )

        @self.api.exception_handler(Exception)
        async def generic_exception_handler(request, exc):
            logger.error(f"Unhandled exception: {exc}")
            origin = request.headers.get("origin")
            headers = {
                "Access-Control-Allow-Credentials": "true",
            }
            if origin and (
                origin in self.allow_origins
                or (
                    self.allow_origin_regex
                    and re.match(self.allow_origin_regex, origin)
                )
            ):
                headers["Access-Control-Allow-Origin"] = origin
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal Server Error"},
                headers=headers,
            )

        @self.api.exception_handler(RequestValidationError)
        async def validation_exception_handler(request, exc):
            origin = request.headers.get("origin")
            headers = {
                "Access-Control-Allow-Credentials": "true",
            }
            if origin and (
                origin in self.allow_origins
                or (
                    self.allow_origin_regex
                    and re.match(self.allow_origin_regex, origin)
                )
            ):
                headers["Access-Control-Allow-Origin"] = origin

            detail = exc.errors()
            for err in detail:
                if err.get("type") == "string_too_short" and "password" in (
                    err.get("loc") or []
                ):
                    return JSONResponse(
                        status_code=422,
                        content={
                            "detail": "Password must be at least 8 characters long."
                        },
                        headers=headers,
                    )

            return JSONResponse(
                status_code=422,
                content={"detail": detail},
                headers=headers,
            )

    def _get_version(self):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        pyproject_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "pyproject.toml"
        )
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("version", "unknown")

    def _find_pictures_by_character_likeness(
        self, character_id, reference_character_id, offset, limit, descending
    ):
        reference_character_id = int(reference_character_id)

        # List pictures by likeness to character
        # 1. Fetch reference faces from the reference pictures set for character
        # 2. Fetch all faces
        # 3. Order them by average likeness to reference faces
        # 4. Return pictures containing those faces in the same order as the faces
        def get_character_reference_faces(session, reference_character_id):
            # Need to get pictures in the reference set for this character
            character = Character.find(session, id=reference_character_id)
            reference_set = session.get(
                PictureSet, character[0].reference_picture_set_id
            )
            if not reference_set:
                return []
            members = session.exec(
                select(PictureSetMember).where(
                    PictureSetMember.set_id == reference_set.id
                )
            ).all()
            picture_ids = [m.picture_id for m in members]
            if not picture_ids:
                logger.debug(
                    f"No pictures in reference set id={reference_set.id} for character id={character_id}"
                )
                return []
            faces = Face.find(session, picture_id=picture_ids)
            return faces

        # 1. Get reference faces (use set of face IDs for uniqueness)
        reference_faces = self.vault.db.run_task(
            get_character_reference_faces,
            reference_character_id,
            priority=DBPriority.IMMEDIATE,
        )

        if not reference_faces:
            logger.warning(f"No reference faces found for character id={character_id}")
            return []

        # 2. Get all faces
        def get_all_faces(session, character_id):
            query = select(Face)
            if character_id == "ALL" or character_id is None:
                pass
            elif character_id == "UNASSIGNED":
                query = query.where(Face.character_id.is_(None))
            else:
                query = query.where(Face.character_id == int(character_id))
            faces = session.exec(query).all()
            return faces

        candidate_faces = self.vault.db.run_task(get_all_faces, character_id)
        if not candidate_faces:
            logger.warning("No unassigned faces found")
            return []

        # Fetch likeness scores directly from FaceCharacterLikeness
        def fetch_character_likeness(session, reference_character_id):
            rows = session.exec(
                select(
                    FaceCharacterLikeness.face_id,
                    FaceCharacterLikeness.likeness,
                ).where(FaceCharacterLikeness.character_id == reference_character_id)
            ).all()
            return {row.face_id: row.likeness for row in rows}

        character_likeness_map = self.vault.db.run_task(
            fetch_character_likeness, reference_character_id
        )

        # Debug logging for character likeness map
        logger.debug(f"Character likeness map: {character_likeness_map}")

        # 3. Get unique picture IDs in that order
        # For each picture, use the maximum character_likeness among all its unassigned faces
        picture_likeness_map = {}
        for face in candidate_faces:
            pic_id = face.picture_id
            likeness = character_likeness_map.get(face.id, 0.0)
            if pic_id not in picture_likeness_map:
                picture_likeness_map[pic_id] = likeness
            else:
                picture_likeness_map[pic_id] = max(
                    picture_likeness_map[pic_id], likeness
                )

        # Debug logging for picture likeness map
        logger.debug(f"Picture likeness map: {picture_likeness_map}")

        # Fetch Picture objects
        candidate_pics = self.vault.db.run_task(
            Picture.find,
            id=list(picture_likeness_map.keys()),
            select_fields=Picture.metadata_fields() | {"characters", "picture_sets"},
        )

        # Assign character_likeness to pictures
        dicts = []
        for pic in candidate_pics:
            if character_id == "UNASSIGNED":
                character_ids = [c.id for c in pic.characters]
                if reference_character_id in character_ids or character_ids:
                    # Skip pictures that already have any characters assigned
                    continue
                if getattr(pic, "picture_sets", None):
                    if pic.picture_sets:
                        # Skip pictures that are already in a picture set
                        continue
            pic_dict = safe_model_dict(pic)
            pic_id = pic_dict["id"]
            pic_dict["character_likeness"] = picture_likeness_map.get(pic_id, 0.0)
            dicts.append(pic_dict)

        # Sort by character_likeness honoring descending flag
        dicts.sort(key=lambda x: x["character_likeness"], reverse=descending)

        # Apply offset and limit
        selected_pics = dicts[offset : offset + limit]
        return selected_pics

    def _fetch_smart_score_data(
        self, character_id, format, candidate_ids=None, penalized_tags=None
    ):
        """Fetch anchors, character references, and candidates for smart score calculation."""

        def fetch_data(session: Session):
            # Anchors
            good = session.exec(
                select(Picture.image_embedding, Picture.score)
                .where(Picture.score >= 4)
                .where(Picture.image_embedding.is_not(None))
                .order_by(desc(Picture.score), desc(Picture.created_at))
                .limit(200)
            ).all()

            bad = session.exec(
                select(Picture.image_embedding, Picture.score)
                .where(Picture.score <= 1)
                .where(Picture.score > 0)
                .where(Picture.image_embedding.is_not(None))
                .order_by(Picture.score, desc(Picture.created_at))
                .limit(200)
            ).all()

            # Candidates
            query = select(Picture, Quality).outerjoin(
                Quality, Quality.picture_id == Picture.id
            )

            if candidate_ids is not None:
                if not candidate_ids:
                    return good, bad, [], {}
                query = query.where(Picture.id.in_(candidate_ids))

            # Apply Filter Logic Matches list_pictures
            if character_id == "UNASSIGNED":
                query = query.where(
                    ~exists(
                        select(Face.id).where(
                            Face.picture_id == Picture.id,
                            Face.character_id.is_not(None),
                        )
                    ),
                    ~exists(
                        select(PictureSetMember.picture_id).where(
                            PictureSetMember.picture_id == Picture.id
                        )
                    ),
                )
            elif character_id and character_id != "ALL":
                try:
                    cid = int(character_id)
                    character_picture_ids = session.exec(
                        select(Face.picture_id).where(Face.character_id == cid)
                    ).all()
                    if not character_picture_ids:
                        return good, bad, [], {}
                    query = query.where(Picture.id.in_(character_picture_ids))
                except ValueError:
                    pass

            if format:
                query = query.where(Picture.format.in_(format))

            query = query.where(Picture.image_embedding.is_not(None))

            candidate_rows = session.exec(query).all()

            penalized_tag_weights = {
                str(tag).strip().lower(): int(weight)
                for tag, weight in (penalized_tags or {}).items()
                if str(tag).strip()
            }

            candidates = []
            candidate_id_list = []
            for pic, quality in candidate_rows:
                aest = pic.aesthetic_score
                quality_score = None
                if quality is not None:
                    try:
                        quality_score = quality.calculate_quality_score()
                    except Exception as e:
                        logger.warning(
                            "Failed to compute heuristic quality score for picture %s: %s",
                            pic.id,
                            e,
                        )
                if aest is None:
                    aest = quality_score
                candidates.append(
                    {
                        "id": pic.id,
                        "image_embedding": pic.image_embedding,
                        "aesthetic_score": aest,
                        "width": pic.width,
                        "height": pic.height,
                        "noise_level": quality.noise_level if quality else None,
                        "edge_density": quality.edge_density if quality else None,
                    }
                )
                candidate_id_list.append(pic.id)

            penalized_tag_map = defaultdict(int)
            if penalized_tag_weights and candidate_id_list:
                tag_rows = session.exec(
                    select(Tag.picture_id, Tag.tag).where(
                        Tag.picture_id.in_(candidate_id_list),
                    )
                ).all()
                for pic_id, tag in tag_rows:
                    if not tag:
                        continue
                    key = tag.strip().lower()
                    weight = penalized_tag_weights.get(key)
                    if weight is not None:
                        penalized_tag_map[pic_id] += weight

                if penalized_tag_map:
                    for candidate in candidates:
                        candidate["penalized_tag_count"] = penalized_tag_map.get(
                            candidate["id"], 0
                        )

            # Pre-fetch Max Face-Character Likeness Map for Candidates
            pic_likeness_map = {}
            char_id = None
            if character_id is not None:
                try:
                    char_id = int(character_id)
                except (TypeError, ValueError):
                    char_id = None
            if candidate_id_list and char_id is not None:
                try:
                    stmt = (
                        select(
                            Face.picture_id, func.max(FaceCharacterLikeness.likeness)
                        )
                        .join(
                            FaceCharacterLikeness,
                            Face.id == FaceCharacterLikeness.face_id,
                        )
                        .where(Face.picture_id.in_(candidate_id_list))
                        .where(Face.character_id == char_id)
                        .where(FaceCharacterLikeness.character_id == char_id)
                        .group_by(Face.picture_id)
                    )
                    rows = session.exec(stmt).all()
                    pic_likeness_map = {r[0]: r[1] for r in rows}
                except Exception as e:
                    logger.warning(f"Failed to fetch likeness map: {e}")

            return good, bad, candidates, pic_likeness_map

        return self.vault.db.run_task(fetch_data, priority=DBPriority.IMMEDIATE)

    def _fetch_smart_score_unscored_ids(
        self, character_id, format, candidate_ids=None, descending=True
    ):
        def fetch_ids(session: Session):
            query = select(Picture.id)

            if candidate_ids is not None:
                if not candidate_ids:
                    return []
                query = query.where(Picture.id.in_(candidate_ids))

            if character_id == "UNASSIGNED":
                query = query.where(
                    ~exists(
                        select(Face.id).where(
                            Face.picture_id == Picture.id,
                            Face.character_id.is_not(None),
                        )
                    ),
                    ~exists(
                        select(PictureSetMember.picture_id).where(
                            PictureSetMember.picture_id == Picture.id
                        )
                    ),
                )
            elif character_id and character_id != "ALL":
                try:
                    cid = int(character_id)
                    character_picture_ids = session.exec(
                        select(Face.picture_id).where(Face.character_id == cid)
                    ).all()
                    if not character_picture_ids:
                        return []
                    query = query.where(Picture.id.in_(character_picture_ids))
                except ValueError:
                    pass

            if format:
                query = query.where(Picture.format.in_(format))

            query = query.where(Picture.image_embedding.is_(None))

            if descending:
                query = query.order_by(desc(Picture.created_at), desc(Picture.id))
            else:
                query = query.order_by(Picture.created_at, Picture.id)

            return [row for row in session.exec(query).all()]

        return self.vault.db.run_task(fetch_ids, priority=DBPriority.IMMEDIATE)

    def _prepare_smart_score_inputs(
        self, good_anchors, bad_anchors, candidates, pic_likeness_map
    ):
        """Unpickle embeddings and prepare lists of dictionaries for calculation."""

        def get_attr(item, key):
            if isinstance(item, dict):
                return item.get(key)
            return getattr(item, key, None)

        def get_vec(blob):
            try:
                obj = pickle.loads(blob)
                if isinstance(obj, np.ndarray):
                    return obj
                return np.array(obj)
            except Exception:
                return None

        def process_list(items):
            result = []
            for p in items:
                v = get_vec(p.image_embedding)
                if v is not None:
                    result.append({"embedding": v, "score": getattr(p, "score", 0)})
            return result

        good_list = process_list(good_anchors)
        bad_list = process_list(bad_anchors)

        cand_list = []
        cand_ids = []

        for p in candidates:
            pid = get_attr(p, "id")
            v = get_vec(get_attr(p, "image_embedding"))
            if v is not None:
                cand_ids.append(pid)
                cand_list.append(
                    {
                        "id": pid,
                        "embedding": v,
                        "aesthetic_score": get_attr(p, "aesthetic_score"),
                        "character_likeness": pic_likeness_map.get(pid),
                        "penalized_tag_count": get_attr(p, "penalized_tag_count") or 0,
                        "width": get_attr(p, "width"),
                        "height": get_attr(p, "height"),
                        "noise_level": get_attr(p, "noise_level"),
                        "edge_density": get_attr(p, "edge_density"),
                    }
                )

        return good_list, bad_list, cand_list, cand_ids

    def _find_pictures_by_smart_score(
        self,
        character_id,
        format,
        offset,
        limit,
        descending,
        candidate_ids=None,
        penalized_tags=None,
    ):
        # 1. Fetch data
        good_anchors, bad_anchors, candidates, pic_likeness_map = (
            self._fetch_smart_score_data(
                character_id,
                format,
                candidate_ids=candidate_ids,
                penalized_tags=penalized_tags,
            )
        )

        unscored_ids = self._fetch_smart_score_unscored_ids(
            character_id,
            format,
            candidate_ids=candidate_ids,
            descending=descending,
        )

        score_map = {}
        scored_ids = []

        if candidates:
            # 2. Prepare inputs (unpickling)
            good_list, bad_list, cand_list, cand_ids = self._prepare_smart_score_inputs(
                good_anchors, bad_anchors, candidates, pic_likeness_map
            )

            if cand_list:
                # 3. Calculate Scores (delegated to PictureUtils)
                scores = PictureUtils.calculate_smart_score_batch_numpy(
                    cand_list, good_list, bad_list
                )

                # 4. Sort and build scored id list
                if descending:
                    sorted_indices = np.argsort(-scores)
                else:
                    sorted_indices = np.argsort(scores)

                scored_ids = [cand_ids[i] for i in sorted_indices]
                score_map = {cand_ids[i]: float(scores[i]) for i in range(len(scores))}

        combined_ids = scored_ids + unscored_ids
        if not combined_ids:
            return []

        final_ids = combined_ids[offset : offset + limit]

        if len(final_ids) == 0:
            return []

        # 5. Fetch Final Objects
        def fetch_final_pics(session, ids):
            return session.exec(select(Picture).where(Picture.id.in_(ids))).all()

        res_pics = self.vault.db.run_task(
            fetch_final_pics, final_ids, priority=DBPriority.IMMEDIATE
        )
        pmap = {p.id: p for p in res_pics}
        metadata_fields = Picture.metadata_fields()

        results = []
        for pid in final_ids:
            if pid in pmap:
                p = pmap[pid]
                d = {field: getattr(p, field) for field in metadata_fields}
                d["smartScore"] = score_map.get(pid)
                results.append(d)

        return results

    def _setup_routes(self):
        ###############################
        # Static file endpoints      ##
        ###############################
        @self.api.get("/")
        async def read_root():
            version = self._get_version()
            return {"message": "PixlVault REST API", "version": version}

        @self.api.get("/favicon.ico")
        def favicon():
            favicon_path = os.path.join(
                os.path.dirname(__file__), "..", "frontend", "public", "favicon.ico"
            )
            return FileResponse(favicon_path)

        @self.api.websocket("/ws/updates")
        async def websocket_updates(websocket: WebSocket):
            await websocket.accept()
            client = {"ws": websocket, "filters": {}}
            with self._ws_clients_lock:
                self._ws_clients.append(client)
            try:
                while True:
                    message = await websocket.receive_text()
                    if not message:
                        continue
                    try:
                        payload = json.loads(message)
                    except Exception:
                        continue
                    if payload.get("type") == "set_filters":
                        filters = {
                            "selected_character": payload.get("selected_character"),
                            "selected_set": payload.get("selected_set"),
                            "search_query": payload.get("search_query"),
                        }
                        client["filters"] = filters
            except WebSocketDisconnect:
                pass
            finally:
                with self._ws_clients_lock:
                    if client in self._ws_clients:
                        self._ws_clients.remove(client)

        self.api.include_router(create_config_router(self))
        self.api.include_router(create_characters_router(self))
        self.api.include_router(create_picture_sets_router(self))
        self.api.include_router(create_tags_router(self))
        self.api.include_router(create_pictures_router(self))

        ###############################
        # Config endpoints            #
        ###############################
        def _ensure_secure_when_required(request: Request):
            self.auth.ensure_secure_when_required(request)

        @self.api.middleware("http")
        async def auth_middleware(request: Request, call_next):
            return await self.auth.auth_middleware(
                request,
                call_next,
                self.allow_origins,
                self.allow_origin_regex,
            )

        @self.api.get("/check-session")
        async def check_session(request: Request):
            return self.auth.check_session(request)

        @self.api.post("/login")
        def login(request: LoginRequest):
            response = self.auth.login(request)
            self._user = self.auth.user
            return response

        @self.api.get("/login")
        def check_registration():
            return self.auth.check_registration()

        @self.api.post("/logout")
        def logout(response: Response, request: Request):
            return self.auth.logout(response, request)

        @self.api.get("/protected")
        async def protected():
            return {"message": "You are authenticated!"}
