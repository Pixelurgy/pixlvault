import argparse
import requests
import os
import json
from platformdirs import user_config_dir

APP_NAME = "pixelurgy-vault"
CONFIG_PATH = os.path.join(user_config_dir(APP_NAME), "config.json")

def get_default_port():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
        return config.get("port", 8000)
    return 8000

def main():
    default_port = get_default_port()
    parser = argparse.ArgumentParser(description="Pixelurgy Vault CLI Tool")
    parser.add_argument("--api-url", default=None, help="Base URL of the Pixelurgy Vault API.")
    parser.add_argument("import", dest="import_path", help="Path to image file or directory to import.")
    parser.add_argument("--character", required=True, help="Character name for the imported images.")
    parser.add_argument("--description", default="", help="Description for the character (if created).")
    args = parser.parse_args()

    api_url = args.api_url
    if not api_url:
        api_url = f"http://localhost:{default_port}"

    # Check if character exists
    r = requests.get(f"{api_url}/characters", params={"name": args.character})
    r.raise_for_status()
    chars = r.json()
    if chars:
        char_id = chars[0]["id"]
        print(f"Character '{args.character}' exists (id={char_id})")
    else:
        # Create character
        print(f"Creating character '{args.character}'...")
        resp = requests.post(f"{api_url}/characters", json={"id": args.character, "name": args.character, "description": args.description})
        resp.raise_for_status()
        char_id = resp.json()["character"]["id"]
        print(f"Character created (id={char_id})")

    # Import images
    import_path = args.import_path
    if os.path.isdir(import_path):
        for fname in os.listdir(import_path):
            fpath = os.path.join(import_path, fname)
            if not os.path.isfile(fpath):
                continue
            with open(fpath, "rb") as f:
                files = {"image": (fname, f, "image/png")}
                data = {"character_id": char_id, "description": f"Imported {fname}", "tags": "[]"}
                r = requests.post(f"{api_url}/pictures", files=files, data=data)
                print(f"Imported {fname}: {r.status_code} {r.text}")
    else:
        fname = os.path.basename(import_path)
        with open(import_path, "rb") as f:
            files = {"image": (fname, f, "image/png")}
            data = {"character_id": char_id, "description": f"Imported {fname}", "tags": "[]"}
            r = requests.post(f"{api_url}/pictures", files=files, data=data)
            print(f"Imported {fname}: {r.status_code} {r.text}")

if __name__ == "__main__":
    main()
