def test_upload_iteration_to_existing_picture():
    """Test uploading additional iterations to an existing picture."""
    import uuid
    with tempfile.TemporaryDirectory() as temp_dir:
        vault_path = os.path.join(temp_dir, "vault.db")
        image_root = os.path.join(temp_dir, "images")
        os.makedirs(image_root, exist_ok=True)
        server = Server(vault_db_path=vault_path, image_root=image_root)
        client = TestClient(server.app)

        # Create a new picture with a master iteration
        img_bytes = random_images[0]
        files = {"image": ("master.png", img_bytes, "image/png")}
        data = {
            "character_id": "testchar",
            "description": "original master",
            "tags": "[]",
        }
        r = client.post("/pictures", files=files, data=data)
        assert r.status_code == 200
        resp = r.json()
        assert resp["results"][0]["status"] == "success"
        picture_id = resp["results"][0]["picture_id"]

        # Upload a new iteration to the same picture
        img_bytes2 = random_images[1]
        files2 = {"file": ("iteration2.png", img_bytes2, "image/png")}
        data2 = {"picture_id": picture_id}
        r2 = client.post("/iterations/", files=files2, data=data2)
        assert r2.status_code == 200
        resp2 = r2.json()
        assert resp2["status"] == "success"
        iteration_id = resp2["iteration_id"]

        # Fetch the new iteration and check association
        r3 = client.get(f"/iterations/{iteration_id}")
        assert r3.status_code == 200
        it = r3.json()
        assert it["picture_id"] == picture_id
        assert it["id"] == iteration_id

        # Upload a third iteration with transform metadata
        img_bytes3 = random_images[2]
        files3 = {"file": ("iteration3.png", img_bytes3, "image/png")}
        data3 = {"picture_id": picture_id, "transform_metadata": "{\"filter\":\"blur\"}"}
        r4 = client.post("/iterations/", files=files3, data=data3)
        assert r4.status_code == 200
        resp4 = r4.json()
        assert resp4["status"] == "success"
        iteration_id3 = resp4["iteration_id"]
        r5 = client.get(f"/iterations/{iteration_id3}")
        assert r5.status_code == 200
        it3 = r5.json()
        assert it3["picture_id"] == picture_id
        assert it3["transform_metadata"] == "{\"filter\":\"blur\"}"
import time
import os
import tempfile
import numpy as np
from PIL import Image
import pytest
from fastapi.testclient import TestClient
from pixelurgy_vault.server import Server, Picture
import random
from io import BytesIO


TEST_SIZE = 50
random_images = []
total_bytes = 0
for i in range(TEST_SIZE):
    arr = np.random.randint(0, 256, (1024, 1024, 3), dtype=np.uint8)
    img = Image.fromarray(arr)
    buf = BytesIO()
    img.save(buf, format="PNG")
    img_bytes = buf.getvalue()
    random_images.append(img_bytes)
    total_bytes += len(img_bytes)


def test_post_logo_altered_pixel_upload():
    with tempfile.TemporaryDirectory() as temp_dir:
        vault_path = os.path.join(temp_dir, "vault.db")
        image_root = os.path.join(temp_dir, "images")
        os.makedirs(image_root, exist_ok=True)
        server = Server(vault_db_path=vault_path, image_root=image_root)
        client = TestClient(server.app)
        logo_path = os.path.join(os.path.dirname(__file__), "../Logo.png")
        img = Image.open(logo_path).convert("RGBA")
        arr = np.array(img)
        arr[0, 0] = [255, 0, 0, 255]  # Red pixel
        altered_img = Image.fromarray(arr)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            altered_img.save(tmp.name)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as f:
            files = {"image": ("altered_logo.png", f, "image/png")}
            data = {
                "character_id": "test",
                "description": "altered pixel",
                "tags": "[]",
            }
            r = client.post("/pictures", files=files, data=data)
        os.remove(tmp_path)
        assert r.status_code == 200
        resp = r.json()
        assert "results" in resp
        assert resp["results"][0]["status"] == "success"
        assert resp["results"][0]["picture_id"]
        assert resp["results"][0]["iteration_id"]


def test_benchmark_add_images_by_path():
    with tempfile.TemporaryDirectory() as temp_dir:
        vault_path = os.path.join(temp_dir, "vault.db")
        image_root = os.path.join(temp_dir, "images")
        os.makedirs(image_root, exist_ok=True)
        server = Server(vault_db_path=vault_path, image_root=image_root)
        client = TestClient(server.app)
        image_paths = []
        total_bytes = 0
        for i, img in enumerate(random_images):
            img_path = os.path.join(temp_dir, f"image_{i:04d}.png")
            with open(img_path, "wb") as f:
                f.write(img)
            image_paths.append(img_path)
            total_bytes += os.path.getsize(img_path)
        start = time.time()
        ids = []
        for i, img_path in enumerate(image_paths):
            data = {
                "file_path": img_path,
                "character_id": "bench",
                "description": f"benchmark image {i}",
                "tags": "[]",
            }
            r = client.post("/pictures", data=data)
            assert r.status_code == 200
            resp = r.json()
            assert "results" in resp
            assert resp["results"][0]["status"] == "success"
            ids.append(resp["results"][0]["picture_id"])
        end = time.time()
        print(
            f"Path Benchmark: Added {TEST_SIZE} images in {end - start:.2f} seconds or {total_bytes / (end - start) / 1024 / 1024:.2f} MB/s"
        )

        # Read back and check a few images
        random_indices = random.sample(range(TEST_SIZE), 3)
        for check_idx in random_indices:
            title = f"benchmark image {check_idx}"
            # Query metadata to get id
            resp = client.get(f"/pictures?description={title}")
            assert resp.status_code == 200
            results = resp.json()
            assert len(results) > 0
            pic_id = results[0]["ids"][0]
            # Fetch image by id
            img_resp = client.get(f"/pictures/{pic_id}")
            assert img_resp.status_code == 200
            # Compare first 1024 bytes for speed
            with open(image_paths[check_idx], "rb") as f:
                image = f.read()
            assert img_resp.content[:1024] == image[:1024]


def test_post_logo_altered_pixel_path():
    with tempfile.TemporaryDirectory() as temp_dir:
        vault_path = os.path.join(temp_dir, "vault.db")
        image_root = os.path.join(temp_dir, "images")
        os.makedirs(image_root, exist_ok=True)
        server = Server(vault_db_path=vault_path, image_root=image_root)
        client = TestClient(server.app)
        logo_path = os.path.join(os.path.dirname(__file__), "../Logo.png")
        img = Image.open(logo_path).convert("RGBA")
        arr = np.array(img)
        arr[0, 1] = [0, 255, 0, 255]  # Green pixel
        altered_img = Image.fromarray(arr)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            altered_img.save(tmp.name)
            tmp_path = tmp.name
        data = {
            "file_path": tmp_path,
            "character_id": "test",
            "description": "altered pixel path",
            "tags": "[]",
        }
        r = client.post("/pictures", data=data)
        os.remove(tmp_path)
        assert r.status_code == 200
        resp = r.json()
        assert "results" in resp
        assert resp["results"][0]["status"] == "success"
        assert resp["results"][0]["picture_id"]


def test_read_root():
    with tempfile.TemporaryDirectory() as temp_dir:
        vault_path = os.path.join(temp_dir, "vault.db")
        image_root = os.path.join(temp_dir, "images")
        server = Server(vault_db_path=vault_path, image_root=image_root)
        client = TestClient(server.app)
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {
            "message": "Pixelurgy Vault REST API",
            "version": "0.1.0",
        }


def test_benchmark_add_images_by_binary_upload():
    TEST_SIZE = 50
    with tempfile.TemporaryDirectory() as temp_dir:
        vault_path = os.path.join(temp_dir, "vault.db")
        image_root = os.path.join(temp_dir, "images")
        os.makedirs(image_root, exist_ok=True)
        server = Server(vault_db_path=vault_path, image_root=image_root)
        client = TestClient(server.app)

        start = time.time()
        ids = []
        for i, img_bytes in enumerate(random_images):
            files = {"image": (f"image_{i:04d}.png", img_bytes, "image/png")}
            data = {
                "character_id": "bench",
                "description": f"benchmark image {i}",
                "tags": "[]",
            }
            r = client.post("/pictures", files=files, data=data)
            assert r.status_code == 200
            resp = r.json()
            assert "results" in resp
            assert resp["results"][0]["status"] == "success"
            ids.append(resp["results"][0]["picture_id"])
        end = time.time()
        print(
            f"Upload Benchmark: Added {TEST_SIZE} images in {end - start:.2f} seconds or {total_bytes / (end - start) / 1024 / 1024:.2f} MB/s"
        )

        # Read back and check a few images
        random_indices = random.sample(range(TEST_SIZE), 3)
        for check_idx in random_indices:
            pic_id = ids[check_idx]
            img_resp = client.get(f"/pictures/{pic_id}")
            assert img_resp.status_code == 200
            assert img_resp.content[:1024] == random_images[check_idx][:1024]


def test_benchmark_add_images_by_path():
    with tempfile.TemporaryDirectory() as temp_dir:
        vault_path = os.path.join(temp_dir, "vault.db")
        image_root = os.path.join(temp_dir, "images")
        os.makedirs(image_root, exist_ok=True)
        server = Server(vault_db_path=vault_path, image_root=image_root)
        client = TestClient(server.app)
        image_paths = []
        total_bytes = 0
        for i, img in enumerate(random_images):
            img_path = os.path.join(temp_dir, f"image_{i:04d}.png")
            with open(img_path, "wb") as f:
                f.write(img)
            image_paths.append(img_path)
            total_bytes += os.path.getsize(img_path)
        start = time.time()

        ids = []
        for i, img_path in enumerate(image_paths):
            data = {
                "file_path": img_path,
                "character_id": "bench",
                "description": f"benchmark image {i}",
                "tags": "[]",
            }
            r = client.post("/pictures", data=data)
            assert r.status_code == 200
            resp = r.json()
            assert "results" in resp
            assert resp["results"][0]["status"] == "success"
            ids.append(resp["results"][0]["picture_id"])
        end = time.time()
        print(
            f"Single Image Path Benchmark: Added {TEST_SIZE} images in {end - start:.2f} seconds or {total_bytes / (end - start) / 1024 / 1024:.2f} MB/s"
        )

        # Read back and check a few images
        random_indices = random.sample(range(TEST_SIZE), 3)
        for check_idx in random_indices:
            pic_id = ids[check_idx]
            img_resp = client.get(f"/pictures/{pic_id}")
            assert img_resp.status_code == 200
            with open(image_paths[check_idx], "rb") as f:
                image = f.read()
            assert img_resp.content[:1024] == image[:1024]


def test_benchmark_add_images_by_directory():
    with tempfile.TemporaryDirectory() as temp_dir:
        vault_path = os.path.join(temp_dir, "vault.db")
        image_root = os.path.join(temp_dir, "images")
        os.makedirs(image_root, exist_ok=True)
        server = Server(vault_db_path=vault_path, image_root=image_root)
        client = TestClient(server.app)
        image_path = os.path.join(temp_dir, "image_dir")
        os.makedirs(image_path, exist_ok=True)
        total_bytes = 0
        for i, img in enumerate(random_images):
            img_path = os.path.join(image_path, f"image_{i:04d}.png")
            with open(img_path, "wb") as f:
                f.write(img)
            total_bytes += os.path.getsize(img_path)
        start = time.time()
        data = {
            "file_path": image_path,
            "character_id": "bench",
            "description": f"benchmark images from directory",
            "tags": "[]",
        }
        r = client.post("/pictures", data=data)
        assert r.status_code == 200
        resp = r.json()
        assert "results" in resp
        file_to_picid = {}
        for result in resp["results"]:
            assert result["status"] == "success"
            assert result["picture_id"]
            # Extract file name from result["file"] if present, else assign sequentially
            file_name = os.path.basename(result.get("file", ""))
            file_to_picid[file_name] = result["picture_id"]
        end = time.time()
        print(
            f"Path Benchmark: Added {TEST_SIZE} images in {end - start:.2f} seconds or {total_bytes / (end - start) / 1024 / 1024:.2f} MB/s"
        )

        # Read back and check a few images
        random_indices = random.sample(range(TEST_SIZE), 3)
        for check_idx in random_indices:
            file_name = f"image_{check_idx:04d}.png"
            pic_id = file_to_picid[file_name]
            img_resp = client.get(f"/pictures/{pic_id}")
            assert img_resp.status_code == 200
            with open(os.path.join(image_path, file_name), "rb") as f:
                image = f.read()
            assert img_resp.content[:1024] == image[:1024]
